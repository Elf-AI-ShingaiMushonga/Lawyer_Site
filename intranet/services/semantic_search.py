from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

import sqlalchemy as sa
from flask import current_app

from ..extensions import db
from .ai_provider import _cosine_similarity, embed_texts, embedding_from_json, embedding_to_json


_DOC_SOURCE_TYPE = "document_version"
_MAX_INDEXABLE_CHARS = 24_000
_CHUNK_SIZE = 1_100
_CHUNK_OVERLAP = 140


def _safe_text(value: str | None) -> str:
    return " ".join(str(value or "").split())


def _split_chunks(text: str) -> list[str]:
    source = (text or "").strip()
    if not source:
        return []
    source = source[:_MAX_INDEXABLE_CHARS]
    chunks: list[str] = []
    start = 0
    while start < len(source):
        end = min(len(source), start + _CHUNK_SIZE)
        window = source[start:end].strip()
        if window:
            chunks.append(window)
        if end >= len(source):
            break
        start = max(0, end - _CHUNK_OVERLAP)
    return chunks


class SemanticSearchService:
    @staticmethod
    def enqueue_document_version_index(document_version_id: int, *, requested_by: int | None = None) -> int | None:
        from ..jobs.queue import enqueue_job
        from ..models import DocumentVersion, JobQueue

        version = db.session.get(DocumentVersion, int(document_version_id or 0))
        if version is None:
            return None

        requested_by_id = int(requested_by or 0)
        existing = (
            JobQueue.query.filter(
                JobQueue.job_type == "semantic_index_document_version",
                JobQueue.status.in_(["queued", "running", "failed"]),
                sa.or_(
                    JobQueue.payload_json.like(f'%"document_version_id": {version.id},%'),
                    JobQueue.payload_json.like(f'%"document_version_id": {version.id}}}%'),
                ),
            )
            .order_by(JobQueue.created_at.desc())
            .first()
        )
        if existing is not None:
            return int(existing.id)

        job_id = enqueue_job(
            "semantic_index_document_version",
            {"document_version_id": int(version.id), "requested_by": requested_by_id},
        )
        db.session.commit()
        return int(job_id)

    @staticmethod
    def _materialize_document_text(document_version_id: int) -> dict[str, Any] | None:
        from ..models import DocumentOCRText, DocumentRecord, DocumentVersion

        row = (
            db.session.query(DocumentVersion, DocumentRecord)
            .join(DocumentRecord, DocumentRecord.id == DocumentVersion.document_id)
            .filter(DocumentVersion.id == int(document_version_id))
            .first()
        )
        if row is None:
            return None
        version, record = row
        ocr = (
            DocumentOCRText.query.filter(DocumentOCRText.document_version_id == version.id)
            .order_by(DocumentOCRText.extracted_at.desc())
            .first()
        )

        pieces = [
            _safe_text(record.title),
            _safe_text(record.document_type),
            _safe_text(version.original_filename),
            _safe_text(version.notes),
            _safe_text(ocr.extracted_text if ocr else ""),
        ]
        merged = "\n".join(piece for piece in pieces if piece).strip()
        if not merged:
            return {
                "matter_id": int(record.matter_id),
                "title": _safe_text(record.title) or f"Document {record.id}",
                "content": "",
                "version_id": int(version.id),
                "document_id": int(record.id),
            }
        return {
            "matter_id": int(record.matter_id),
            "title": _safe_text(record.title) or f"Document {record.id}",
            "content": merged,
            "version_id": int(version.id),
            "document_id": int(record.id),
        }

    @staticmethod
    def index_document_version(document_version_id: int, *, requested_by: int | None = None) -> dict[str, Any]:
        from ..models import SemanticIndexEntry

        materialized = SemanticSearchService._materialize_document_text(document_version_id)
        if materialized is None:
            return {"indexed_chunks": 0, "reason": "document_version_missing"}

        source_id = int(materialized["version_id"])
        text = str(materialized.get("content") or "").strip()
        matter_id = int(materialized.get("matter_id") or 0) or None
        title = str(materialized.get("title") or "")
        SemanticIndexEntry.query.filter_by(source_type=_DOC_SOURCE_TYPE, source_id=source_id).delete(
            synchronize_session=False
        )

        if not text:
            db.session.commit()
            return {"indexed_chunks": 0, "reason": "empty_content"}

        chunks = _split_chunks(text)
        vectors, provider_meta = embed_texts(chunks, operation_type="semantic_index_document")
        now = dt.datetime.utcnow()
        for idx, chunk in enumerate(chunks):
            vector = vectors[idx] if idx < len(vectors) else []
            content_sha256 = hashlib.sha256(chunk.encode("utf-8")).hexdigest()
            db.session.add(
                SemanticIndexEntry(
                    source_type=_DOC_SOURCE_TYPE,
                    source_id=source_id,
                    matter_id=matter_id,
                    chunk_index=idx,
                    title=title[:255] if title else None,
                    content_text=chunk,
                    content_sha256=content_sha256,
                    embedding_json=embedding_to_json(vector),
                    embedding_model=str(provider_meta.get("model") or "")[:120] or None,
                    embedding_dim=len(vector),
                    provider=str(provider_meta.get("provider") or "")[:40] or None,
                    redaction_meta_json=json.dumps(
                        {
                            "requested_by": int(requested_by or 0),
                            "redaction_counts": provider_meta.get("redaction_counts") or {},
                        },
                        sort_keys=True,
                    ),
                    created_at=now,
                    updated_at=now,
                )
            )
        db.session.commit()
        return {
            "indexed_chunks": len(chunks),
            "fallback_used": bool(provider_meta.get("fallback_used")),
            "model": provider_meta.get("model"),
            "provider": provider_meta.get("provider"),
        }

    @staticmethod
    def search(
        query: str,
        *,
        matter_scope_ids: set[int] | None = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        from ..models import DocumentRecord, DocumentVersion, SemanticIndexEntry

        q = (query or "").strip()
        if len(q) < 3:
            return []

        query_vectors, _meta = embed_texts([q], operation_type="semantic_query")
        if not query_vectors:
            return []
        q_vec = query_vectors[0]
        if not q_vec:
            return []

        candidate_limit = max(50, int(current_app.config.get("AI_SEMANTIC_CANDIDATE_LIMIT", 600) or 600))

        base = SemanticIndexEntry.query.filter(SemanticIndexEntry.source_type == _DOC_SOURCE_TYPE)
        if matter_scope_ids is not None:
            if not matter_scope_ids:
                return []
            base = base.filter(SemanticIndexEntry.matter_id.in_(sorted(matter_scope_ids)))
        rows = (
            base.order_by(SemanticIndexEntry.updated_at.desc(), SemanticIndexEntry.id.desc())
            .limit(candidate_limit)
            .all()
        )
        if not rows:
            return []

        scored: list[tuple[float, SemanticIndexEntry]] = []
        for row in rows:
            emb = embedding_from_json(row.embedding_json)
            if not emb:
                continue
            score = _cosine_similarity(q_vec, emb)
            if score < 0.18:
                continue
            scored.append((float(score), row))
        if not scored:
            return []

        scored.sort(key=lambda item: item[0], reverse=True)
        top = scored[: max(1, int(limit))]
        version_ids = sorted({int(item[1].source_id) for item in top if int(item[1].source_id) > 0})
        versions = (
            db.session.query(DocumentVersion, DocumentRecord)
            .join(DocumentRecord, DocumentRecord.id == DocumentVersion.document_id)
            .filter(DocumentVersion.id.in_(version_ids))
            .all()
            if version_ids
            else []
        )
        version_map = {
            int(version.id): {"version": version, "record": record}
            for version, record in versions
        }

        response: list[dict[str, Any]] = []
        for score, row in top:
            linked = version_map.get(int(row.source_id))
            if linked:
                version = linked["version"]
                record = linked["record"]
                title = _safe_text(record.title) or _safe_text(version.original_filename) or f"Document {version.id}"
                link = f"/documents/{int(record.id)}/versions"
                matter_id = int(record.matter_id)
            else:
                title = _safe_text(row.title) or f"Document version {row.source_id}"
                link = None
                matter_id = int(row.matter_id or 0) or None

            response.append(
                {
                    "score": round(float(score), 4),
                    "source_type": row.source_type,
                    "source_id": int(row.source_id),
                    "matter_id": matter_id,
                    "title": title,
                    "excerpt": _safe_text(row.content_text)[:260],
                    "url": link,
                }
            )
        return response
