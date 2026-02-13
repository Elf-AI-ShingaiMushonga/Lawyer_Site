from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re

import sqlalchemy as sa

from ..extensions import db
from ..types import ConflictReport


_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9&.\-]{1,}")
_MAX_OCR_SCAN_ROWS = 450
_MAX_SEMANTIC_HITS = 8
_EMBEDDING_DIM = 96
_RELATIONSHIP_MARKERS = {
    "subsidiary",
    "affiliate",
    "holding company",
    "trading as",
    "t/a",
    "formerly",
    "division",
    "group company",
}
_ENTITY_STOPWORDS = {
    "and",
    "the",
    "of",
    "for",
    "to",
    "in",
    "on",
    "by",
    "at",
    "pty",
    "ltd",
    "limited",
    "inc",
    "llc",
    "co",
    "company",
    "group",
    "holdings",
    "holding",
}


def _safe_json_loads(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _tokenize(value: str) -> list[str]:
    if not value:
        return []
    return [token for token in _TOKEN_RE.findall(value.lower()) if token]


def _canonical_entity_name(value: str) -> str:
    tokens = [token for token in _tokenize(value) if token not in _ENTITY_STOPWORDS]
    if not tokens:
        return " ".join(_tokenize(value))
    return " ".join(tokens)


def _entity_variants(value: str) -> list[str]:
    raw = " ".join(_tokenize(value))
    canonical = _canonical_entity_name(value)
    variants = [raw, canonical]
    if canonical:
        c_tokens = canonical.split()
        if len(c_tokens) >= 2:
            variants.append(f"{c_tokens[0]} {c_tokens[-1]}")
        if len(c_tokens) >= 3:
            variants.append(" ".join(c_tokens[:2]))
            variants.append(" ".join(c_tokens[-2:]))
    deduped: list[str] = []
    seen: set[str] = set()
    for variant in variants:
        clean = " ".join(variant.strip().split()).lower()
        if len(clean) < 3 or clean in seen:
            continue
        seen.add(clean)
        deduped.append(clean)
    return deduped


def _hashed_embedding(tokens: set[str], dimensions: int = _EMBEDDING_DIM) -> list[float]:
    vector = [0.0] * dimensions
    if not tokens:
        return vector
    norm = 0.0
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        bucket = digest[0] % dimensions
        sign = -1.0 if digest[1] % 2 else 1.0
        weight = 1.0 + min(1.5, len(token) / 10.0)
        vector[bucket] += sign * weight
        norm += weight * weight
    if norm <= 0:
        return vector
    inv = 1.0 / math.sqrt(norm)
    return [value * inv for value in vector]


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    score = sum(a * b for a, b in zip(left, right))
    return max(-1.0, min(1.0, score))


def _lexical_overlap(query_tokens: set[str], doc_tokens: set[str]) -> float:
    if not query_tokens or not doc_tokens:
        return 0.0
    overlap = len(query_tokens & doc_tokens)
    return float(overlap) / float(max(1, len(query_tokens)))


def _extract_excerpt(text: str, terms: list[str], max_len: int = 260) -> str:
    compact = " ".join((text or "").split())
    if not compact:
        return ""

    lower_text = compact.lower()
    anchor = -1
    for term in terms:
        idx = lower_text.find(term)
        if idx >= 0 and (anchor < 0 or idx < anchor):
            anchor = idx
    if anchor < 0:
        return compact[:max_len]

    start = max(0, anchor - 90)
    end = min(len(compact), anchor + max_len - 90)
    return compact[start:end]


class ConflictEngine:
    """Conflict checking against known entities, contacts, and matters."""

    @staticmethod
    def _extract_intake_entities(payload: dict) -> list[str]:
        candidates: list[str] = []
        for key in ["client_name", "lead_name", "opposing_party", "opposing_counsel"]:
            value = str(payload.get(key) or "").strip()
            if value:
                candidates.append(value)

        entities = payload.get("entities")
        if isinstance(entities, list):
            for item in entities:
                value = str(item or "").strip()
                if value:
                    candidates.append(value)

        related_entities = payload.get("related_entities")
        if isinstance(related_entities, list):
            for item in related_entities:
                value = str(item or "").strip()
                if value:
                    candidates.append(value)

        deduped: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            normalized = " ".join(candidate.split())
            marker = normalized.lower()
            if not marker or marker in seen:
                continue
            seen.add(marker)
            deduped.append(normalized)
        return deduped

    @staticmethod
    def _direct_matches(entity_names: list[str]) -> list[str]:
        from ..models import Contact, Entity, Matter

        matches: set[str] = set()
        for name in entity_names:
            search_terms = [name]
            canonical = _canonical_entity_name(name)
            if canonical and canonical.lower() != name.lower():
                search_terms.append(canonical)
            for term in search_terms:
                like = f"%{term}%"
                for row in Contact.query.filter(Contact.name.ilike(like)).limit(10).all():
                    matches.add(f"contact:{row.name}")
                for row in Matter.query.filter(Matter.client_name.ilike(like)).limit(10).all():
                    matches.add(f"matter-client:{row.client_name}")
                for row in Entity.query.filter(Entity.name.ilike(like)).limit(10).all():
                    matches.add(f"entity:{row.name}")
        return sorted(matches)

    @staticmethod
    def _semantic_hits(entity_names: list[str], *, intake_matter_id: int | None) -> list[dict]:
        from ..models import DocumentOCRText, DocumentRecord, DocumentVersion

        if not entity_names:
            return []

        query = (
            db.session.query(DocumentOCRText, DocumentVersion, DocumentRecord)
            .join(DocumentVersion, DocumentVersion.id == DocumentOCRText.document_version_id)
            .join(DocumentRecord, DocumentRecord.id == DocumentVersion.document_id)
            .order_by(DocumentOCRText.extracted_at.desc())
        )
        if intake_matter_id:
            query = query.filter(DocumentRecord.matter_id != intake_matter_id)

        search_terms: list[str] = []
        for name in entity_names:
            for term in _entity_variants(name):
                if len(term) >= 4:
                    search_terms.append(term)
        search_terms = sorted(set(search_terms))[:10]

        rows = []
        if search_terms:
            predicates = [sa.func.lower(DocumentOCRText.extracted_text).like(f"%{term}%") for term in search_terms]
            rows = query.filter(sa.or_(*predicates)).limit(_MAX_OCR_SCAN_ROWS).all()
        if not rows:
            rows = query.limit(min(220, _MAX_OCR_SCAN_ROWS)).all()

        query_profiles = []
        for name in entity_names:
            query_terms = _entity_variants(name)
            query_tokens = set(_tokenize(_canonical_entity_name(name)))
            if not query_tokens:
                continue
            query_profiles.append(
                {
                    "name": name,
                    "terms": query_terms,
                    "tokens": query_tokens,
                    "vector": _hashed_embedding(query_tokens),
                }
            )

        ranked: list[dict] = []
        for ocr, version, document in rows:
            text = (ocr.extracted_text or "").strip()
            if not text:
                continue
            lower_text = text.lower()
            doc_tokens = set(_tokenize(lower_text))
            if not doc_tokens:
                continue
            doc_vector = _hashed_embedding(doc_tokens)
            has_relation_marker = any(marker in lower_text for marker in _RELATIONSHIP_MARKERS)

            for profile in query_profiles:
                query_terms = profile["terms"]
                query_tokens = profile["tokens"]
                entity_name = str(profile["name"])

                lexical = _lexical_overlap(query_tokens, doc_tokens)
                vector = max(0.0, _cosine_similarity(profile["vector"], doc_vector))
                relation_boost = 0.08 if has_relation_marker and lexical > 0 else 0.0
                combined = min(1.0, (0.54 * vector) + (0.36 * lexical) + relation_boost)
                if combined < 0.40 and lexical < 0.25:
                    continue

                matched_phrase = None
                for term in query_terms:
                    if term in lower_text:
                        matched_phrase = term
                        break
                if matched_phrase is None and lexical > 0:
                    shared = sorted(query_tokens & doc_tokens)
                    if shared:
                        matched_phrase = " ".join(shared[:4])

                reason_parts = []
                if lexical >= 0.25:
                    reason_parts.append("token_overlap")
                if vector >= 0.45:
                    reason_parts.append("semantic_vector")
                if relation_boost > 0:
                    reason_parts.append("relationship_marker")
                if not reason_parts:
                    reason_parts.append("semantic_vector")

                ranked.append(
                    {
                        "document_ocr_text_id": int(ocr.id),
                        "document_version_id": int(version.id),
                        "matter_id": int(document.matter_id) if document.matter_id is not None else None,
                        "document_title": (document.title or "").strip() or f"Document {document.id}",
                        "candidate_entity": entity_name,
                        "matched_phrase": matched_phrase,
                        "match_reason": "+".join(reason_parts),
                        "similarity_score": round(float(combined), 4),
                        "lexical_score": round(float(lexical), 4),
                        "vector_score": round(float(vector), 4),
                        "excerpt": _extract_excerpt(text, query_terms),
                    }
                )

        deduped: dict[tuple[int, str], dict] = {}
        for row in ranked:
            key = (int(row["document_ocr_text_id"]), str(row["candidate_entity"]).lower())
            prior = deduped.get(key)
            if prior is None or float(row["similarity_score"]) > float(prior["similarity_score"]):
                deduped[key] = row

        ordered = sorted(
            deduped.values(),
            key=lambda item: (float(item["similarity_score"]), float(item["lexical_score"]), float(item["vector_score"])),
            reverse=True,
        )[:_MAX_SEMANTIC_HITS]
        for idx, item in enumerate(ordered, start=1):
            item["semantic_rank"] = idx
        return ordered

    @staticmethod
    def _persist_semantic_hits(conflict_check_id: int, semantic_hits: list[dict]) -> None:
        from ..models import ConflictSemanticHit

        ConflictSemanticHit.query.filter_by(conflict_check_id=conflict_check_id).delete(synchronize_session=False)
        for item in semantic_hits:
            db.session.add(
                ConflictSemanticHit(
                    conflict_check_id=conflict_check_id,
                    document_ocr_text_id=int(item["document_ocr_text_id"]),
                    document_version_id=int(item["document_version_id"]) if item.get("document_version_id") else None,
                    matter_id=int(item["matter_id"]) if item.get("matter_id") else None,
                    candidate_entity=str(item.get("candidate_entity") or "")[:255],
                    matched_phrase=(str(item.get("matched_phrase") or "")[:255] or None),
                    match_reason=(str(item.get("match_reason") or "")[:255] or None),
                    similarity_score=float(item.get("similarity_score") or 0.0),
                    lexical_score=float(item.get("lexical_score") or 0.0),
                    vector_score=float(item.get("vector_score") or 0.0),
                    excerpt=(str(item.get("excerpt") or "")[:2000] or None),
                    semantic_rank=int(item.get("semantic_rank") or 1),
                    created_at=dt.datetime.utcnow(),
                )
            )

    @staticmethod
    def _build_result_payload(
        direct_matches: list[str],
        semantic_hits: list[dict],
        *,
        semantic_status: str,
        prior_payload: dict | None = None,
    ) -> dict:
        result = dict(prior_payload or {})
        result["matches"] = sorted(direct_matches)
        result["semantic_hits"] = semantic_hits
        result["semantic_hit_count"] = len(semantic_hits)
        result["semantic_status"] = semantic_status
        result["generated_at"] = dt.datetime.utcnow().isoformat()
        return result

    @staticmethod
    def enqueue_semantic_scan(
        intake_id: int,
        *,
        requested_by: int | None = None,
        conflict_check_id: int | None = None,
    ) -> int | None:
        from ..jobs.queue import enqueue_job
        from ..models import ConflictCheck, IntakeForm, JobQueue

        intake = db.session.get(IntakeForm, intake_id)
        if intake is None:
            return None

        check = db.session.get(ConflictCheck, conflict_check_id) if conflict_check_id else None
        if check is not None and check.intake_form_id != intake_id:
            check = None
        if check is None:
            check = ConflictCheck(
                intake_form_id=intake_id,
                status="pending",
                result_json=json.dumps(
                    {
                        "matches": [],
                        "semantic_hits": [],
                        "semantic_hit_count": 0,
                        "semantic_status": "queued",
                        "queued_at": dt.datetime.utcnow().isoformat(),
                    },
                    sort_keys=True,
                ),
                override_required=False,
            )
            db.session.add(check)
            db.session.flush()
        else:
            prior_payload = _safe_json_loads(check.result_json)
            prior_payload.setdefault("matches", [])
            prior_payload.setdefault("semantic_hits", [])
            prior_payload["semantic_status"] = "queued"
            prior_payload["queued_at"] = dt.datetime.utcnow().isoformat()
            prior_payload["requested_by"] = int(requested_by or 0)
            check.result_json = json.dumps(prior_payload, sort_keys=True)
            if check.status not in {"clear", "potential_conflict", "overridden"}:
                check.status = "pending"

        existing_job = (
            JobQueue.query.filter(
                JobQueue.job_type == "conflict_semantic_scan",
                JobQueue.status.in_(["queued", "running", "failed"]),
                sa.or_(
                    JobQueue.payload_json.like(f'%"conflict_check_id": {check.id},%'),
                    JobQueue.payload_json.like(f'%"conflict_check_id": {check.id}}}%'),
                ),
            )
            .order_by(JobQueue.created_at.desc())
            .first()
        )
        if existing_job is None:
            enqueue_job(
                "conflict_semantic_scan",
                {
                    "intake_id": intake_id,
                    "conflict_check_id": check.id,
                    "requested_by": int(requested_by or 0),
                },
            )
        db.session.commit()
        return check.id

    @staticmethod
    def run_check(
        intake_id: int,
        *,
        include_semantic: bool = False,
        conflict_check_id: int | None = None,
    ) -> ConflictReport:
        from ..models import ConflictCheck, IntakeForm

        intake = db.session.get(IntakeForm, intake_id)
        if intake is None:
            return ConflictReport(conflict_check_id=None, status="error", matched_entities=[], notes="intake not found")

        payload = _safe_json_loads(intake.data_json)
        entity_names = ConflictEngine._extract_intake_entities(payload)
        if not entity_names:
            fallback = str(payload.get("client_name") or "").strip()
            if fallback:
                entity_names = [fallback]

        direct_matches = ConflictEngine._direct_matches(entity_names)
        semantic_hits: list[dict] = []

        check = db.session.get(ConflictCheck, conflict_check_id) if conflict_check_id else None
        if check is not None and check.intake_form_id != intake.id:
            check = None

        prior_payload = _safe_json_loads(check.result_json) if check is not None else {}
        if include_semantic:
            semantic_hits = ConflictEngine._semantic_hits(entity_names, intake_matter_id=intake.matter_id)
            semantic_status = "completed"
        else:
            semantic_hits = prior_payload.get("semantic_hits", []) if isinstance(prior_payload.get("semantic_hits"), list) else []
            semantic_status = str(prior_payload.get("semantic_status") or "not_requested")

        has_hits = bool(direct_matches or semantic_hits)
        computed_status = "potential_conflict" if has_hits else "clear"
        override_required = bool(has_hits)

        if check is None:
            check = ConflictCheck(
                intake_form_id=intake.id,
                status=computed_status,
                result_json=None,
                override_required=override_required,
            )
            db.session.add(check)
            db.session.flush()
        else:
            if check.status != "overridden":
                check.status = computed_status
                check.override_required = override_required

        result_payload = ConflictEngine._build_result_payload(
            direct_matches,
            semantic_hits,
            semantic_status=semantic_status,
            prior_payload=prior_payload,
        )
        check.result_json = json.dumps(result_payload, sort_keys=True)

        if include_semantic:
            ConflictEngine._persist_semantic_hits(check.id, semantic_hits)

        db.session.commit()

        semantic_labels = [
            f"semantic:{hit.get('candidate_entity')}:{float(hit.get('similarity_score') or 0.0):.2f}"
            for hit in semantic_hits
        ]
        matched_entities = sorted(direct_matches) + semantic_labels
        if check.status == "overridden":
            notes = "Conflict overridden by reviewer"
        elif has_hits:
            notes = "Manual sign-off required"
        else:
            notes = "No direct or semantic matches detected"
        return ConflictReport(
            conflict_check_id=check.id,
            status=check.status,
            matched_entities=matched_entities,
            notes=notes,
        )

    @staticmethod
    def run_semantic_scan(intake_id: int, *, conflict_check_id: int | None = None) -> ConflictReport:
        return ConflictEngine.run_check(intake_id, include_semantic=True, conflict_check_id=conflict_check_id)
