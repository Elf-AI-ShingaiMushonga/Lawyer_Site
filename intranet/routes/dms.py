from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import uuid

from flask import Response, abort, flash, redirect, request, send_from_directory, url_for
from flask_login import current_user, login_required
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from werkzeug.utils import secure_filename

from ..extensions import db
from ..helpers import allowed_doc, audit, can_access_matter, is_admin, sha256_file
from ..models import (
    BatesRange,
    DocumentFile,
    DocumentLock,
    DocumentOCRText,
    DocumentRecord,
    DocumentTemplate,
    DocumentVersion,
    EmailCapture,
    Matter,
    ProductionItem,
    ProductionSet,
    SavedSearch,
)
from ..policies import visible_matter_ids
from ..policies.residency import enforce_data_residency
from ..services.notification_engine import NotificationEngine
from ..templates import page

DOCUMENT_STATES = {"draft", "reviewed", "final", "filed"}
TEMPLATE_TOKEN_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}")


def _latest_version(document_id: int) -> DocumentVersion | None:
    return (
        DocumentVersion.query.filter_by(document_id=document_id)
        .order_by(DocumentVersion.version_no.desc(), DocumentVersion.uploaded_at.desc())
        .first()
    )


def _chain_hash(prev_hash: str | None, file_sha256: str) -> str:
    seed = f"{prev_hash or 'GENESIS'}:{file_sha256}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _extract_ocr_text(path: str, content_type: str | None) -> str:
    ctype = (content_type or "").lower()
    text_like = ctype.startswith("text/") or ctype in {"application/json", "application/xml"}
    with open(path, "rb") as f:
        chunk = f.read(1024 * 1024)
    if text_like:
        extracted = chunk.decode("utf-8", errors="ignore")
    else:
        extracted = chunk.decode("utf-8", errors="ignore")
        if not extracted.strip():
            extracted = "OCR pending for binary document. Text extraction unavailable in this environment."
    return (extracted.strip() or "No OCR text extracted.")[:12000]


def _safe_query_json(raw: str) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_remove_file(path: str) -> None:
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def _parse_generation_fields(raw: str | None) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in (raw or "").splitlines():
        candidate = line.strip()
        if not candidate or "=" not in candidate:
            continue
        key_raw, value_raw = candidate.split("=", 1)
        key = key_raw.strip().lower().replace(" ", "_")
        if not key:
            continue
        fields[key] = value_raw.strip()
    return fields


def _template_context(matter: Matter) -> dict[str, str]:
    now = dt.datetime.utcnow()
    return {
        "matter_id": str(matter.id),
        "matter_no": matter.matter_no or "",
        "matter_title": matter.title or "",
        "title": matter.title or "",
        "client_name": matter.client_name or "",
        "status": matter.status or "",
        "stage": matter.stage or "",
        "jurisdiction": matter.jurisdiction or "",
        "court_name": matter.court_name or "",
        "judge_name": matter.judge_name or "",
        "practice_area": matter.practice_area or "",
        "case_type": matter.case_type or "",
        "today": now.date().isoformat(),
        "now": now.replace(microsecond=0).isoformat(),
        "generated_by_name": current_user.full_name or "",
        "generated_by_email": current_user.email or "",
    }


def _render_template_body(body: str, context: dict[str, str]) -> tuple[str, list[str]]:
    missing: list[str] = []

    def replace_token(match: re.Match[str]) -> str:
        key = (match.group(1) or "").strip().lower()
        if not key:
            return ""
        if key in context:
            return str(context[key])
        missing.append(key)
        return ""

    rendered = TEMPLATE_TOKEN_PATTERN.sub(replace_token, body or "")
    missing_sorted = sorted(set(missing))
    return rendered, missing_sorted


def register_dms_routes(app):
    @app.get("/dms")
    @login_required
    def dms_home():
        matter_query = Matter.query
        if not is_admin():
            scoped_ids = visible_matter_ids()
            if not scoped_ids:
                flash("You do not currently have matter access for DMS.", "warning")
                return redirect(url_for("matters"))
            matter_query = matter_query.filter(Matter.id.in_(scoped_ids))
        matter = matter_query.order_by(Matter.last_updated_at.desc(), Matter.opened_at.desc()).first()
        if matter is None:
            flash("Create a matter first to use DMS.", "warning")
            return redirect(url_for("matters"))
        return redirect(url_for("matter_dms", matter_id=matter.id))

    @app.route("/matters/<int:matter_id>/dms", methods=["GET", "POST"])
    @login_required
    def matter_dms(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        if request.method == "POST":
            action = (request.form.get("action") or "upload_document").strip().lower()
            if action == "generate_from_template":
                template_id = request.form.get("template_id", type=int)
                template = db.session.get(DocumentTemplate, template_id) if template_id else None
                if template is None:
                    flash("Document template not found.", "warning")
                    return redirect(url_for("matter_dms", matter_id=matter_id))

                generated_title = (request.form.get("generated_title") or "").strip() or f"{template.name} - {m.matter_no}"
                context = _template_context(m)
                context.update(_parse_generation_fields(request.form.get("custom_fields")))
                rendered_body, missing_tokens = _render_template_body(template.body, context)
                if not rendered_body.strip():
                    flash("Generated document body is empty. Add template text or merge fields.", "warning")
                    return redirect(url_for("matter_dms", matter_id=matter_id))

                enforce_data_residency("primary_storage")
                os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)
                base_name = secure_filename(generated_title) or f"generated_{template.id}"
                safe_name = f"{base_name[:80]}.txt"
                stored = f"dms_{matter_id}_{uuid.uuid4().hex}_{safe_name}"
                path = os.path.join(app.config["UPLOAD_DIR"], stored)
                try:
                    with open(path, "w", encoding="utf-8") as handle:
                        handle.write(rendered_body)
                except OSError:
                    flash("Failed to persist generated document.", "warning")
                    return redirect(url_for("matter_dms", matter_id=matter_id))

                sha = sha256_file(path)
                try:
                    container = DocumentRecord(
                        matter_id=matter_id,
                        title=generated_title,
                        document_type=(request.form.get("generated_document_type") or template.template_type or "General").strip()
                        or "General",
                        confidentiality=(request.form.get("generated_confidentiality") or "Internal").strip() or "Internal",
                        privilege_label=(request.form.get("generated_privilege_label") or "").strip() or None,
                        retention_category=(request.form.get("generated_retention_category") or "").strip() or None,
                        legal_hold=(request.form.get("generated_legal_hold") or "").lower() in {"1", "true", "yes", "on"},
                        created_by=current_user.id,
                    )
                    db.session.add(container)
                    db.session.flush()

                    legacy_file = DocumentFile(
                        matter_id=matter_id,
                        original_filename=safe_name,
                        stored_filename=stored,
                        sha256=sha,
                        content_type="text/plain",
                        category=container.document_type,
                        doc_version="1",
                        lifecycle_stage="Draft",
                        owner_name=current_user.full_name,
                        is_privileged=bool(container.privilege_label),
                        uploaded_by=current_user.id,
                    )
                    db.session.add(legacy_file)
                    db.session.flush()

                    notes = (request.form.get("generated_version_notes") or "").strip()
                    if not notes:
                        notes = f"Generated from template '{template.name}'."
                    version = DocumentVersion(
                        document_id=container.id,
                        document_file_id=legacy_file.id,
                        version_no=1,
                        original_filename=safe_name,
                        stored_filename=stored,
                        sha256=sha,
                        hash_chain_prev=None,
                        hash_chain_current=_chain_hash(None, sha),
                        state="draft",
                        notes=notes,
                        uploaded_by=current_user.id,
                    )
                    db.session.add(version)
                    db.session.flush()
                    db.session.add(
                        DocumentOCRText(
                            document_version_id=version.id,
                            extracted_text=(rendered_body.strip() or "No generated text."),
                        )
                    )
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                    _safe_remove_file(path)
                    flash("Failed to save generated document. Please retry.", "warning")
                    return redirect(url_for("matter_dms", matter_id=matter_id))

                audit(
                    "dms_template_generate",
                    "DocumentRecord",
                    container.id,
                    {"matter_id": matter_id, "template_id": template.id, "missing_tokens": missing_tokens},
                )
                NotificationEngine.enqueue("document_generated", current_user.id, f"document_version:{version.id}")
                if missing_tokens:
                    flash(
                        "Document generated, but some merge fields were blank: " + ", ".join(missing_tokens[:6]),
                        "warning",
                    )
                else:
                    flash("Document generated from template.", "info")
                return redirect(url_for("matter_dms", matter_id=matter_id))

            title = (request.form.get("title") or "").strip()
            if not title:
                flash("Document title required.", "warning")
                return redirect(url_for("matter_dms", matter_id=matter_id))

            f = request.files.get("file")
            if not f or not f.filename:
                flash("Document file required.", "warning")
                return redirect(url_for("matter_dms", matter_id=matter_id))
            if not allowed_doc(f.filename):
                flash("Unsupported file type.", "warning")
                return redirect(url_for("matter_dms", matter_id=matter_id))
            enforce_data_residency("primary_storage")

            safe_name = secure_filename(f.filename)
            if not safe_name:
                flash("Invalid filename.", "warning")
                return redirect(url_for("matter_dms", matter_id=matter_id))
            stored = f"dms_{matter_id}_{uuid.uuid4().hex}_{safe_name}"
            path = os.path.join(app.config["UPLOAD_DIR"], stored)
            f.save(path)
            sha = sha256_file(path)
            try:
                container = DocumentRecord(
                    matter_id=matter_id,
                    title=title,
                    document_type=(request.form.get("document_type") or "General").strip() or "General",
                    confidentiality=(request.form.get("confidentiality") or "Internal").strip() or "Internal",
                    privilege_label=(request.form.get("privilege_label") or "").strip() or None,
                    retention_category=(request.form.get("retention_category") or "").strip() or None,
                    legal_hold=(request.form.get("legal_hold") or "").lower() in {"1", "true", "yes", "on"},
                    created_by=current_user.id,
                )
                db.session.add(container)
                db.session.flush()

                legacy_file = DocumentFile(
                    matter_id=matter_id,
                    original_filename=safe_name,
                    stored_filename=stored,
                    sha256=sha,
                    content_type=f.mimetype,
                    category=container.document_type,
                    doc_version="1",
                    lifecycle_stage="Draft",
                    owner_name=current_user.full_name,
                    is_privileged=bool(container.privilege_label),
                    uploaded_by=current_user.id,
                )
                db.session.add(legacy_file)
                db.session.flush()

                version = DocumentVersion(
                    document_id=container.id,
                    document_file_id=legacy_file.id,
                    version_no=1,
                    original_filename=safe_name,
                    stored_filename=stored,
                    sha256=sha,
                    hash_chain_prev=None,
                    hash_chain_current=_chain_hash(None, sha),
                    state="draft",
                    notes=(request.form.get("version_notes") or "").strip() or None,
                    uploaded_by=current_user.id,
                )
                db.session.add(version)
                db.session.flush()
                db.session.add(
                    DocumentOCRText(
                        document_version_id=version.id,
                        extracted_text=_extract_ocr_text(path, f.mimetype),
                    )
                )
                db.session.commit()
            except Exception:
                db.session.rollback()
                _safe_remove_file(path)
                flash("Document upload failed. Please retry.", "warning")
                return redirect(url_for("matter_dms", matter_id=matter_id))

            audit("dms_document_create", "DocumentRecord", container.id, {"matter_id": matter_id})
            flash("Document created in DMS.", "info")
            return redirect(url_for("matter_dms", matter_id=matter_id))

        q = (request.args.get("q") or "").strip().lower()
        filter_type = (request.args.get("document_type") or "").strip().lower()
        filter_confidentiality = (request.args.get("confidentiality") or "").strip().lower()

        docs_query = DocumentRecord.query.filter(DocumentRecord.matter_id == matter_id)
        if filter_type:
            docs_query = docs_query.filter(func.lower(func.coalesce(DocumentRecord.document_type, "")) == filter_type)
        if filter_confidentiality:
            docs_query = docs_query.filter(
                func.lower(func.coalesce(DocumentRecord.confidentiality, "")) == filter_confidentiality
            )
        docs = docs_query.order_by(DocumentRecord.created_at.desc()).all()

        latest_versions: dict[int, DocumentVersion] = {}
        if docs:
            doc_ids = [d.id for d in docs]
            latest_version_no_subquery = (
                db.session.query(
                    DocumentVersion.document_id.label("document_id"),
                    func.max(DocumentVersion.version_no).label("max_version_no"),
                )
                .filter(DocumentVersion.document_id.in_(doc_ids))
                .group_by(DocumentVersion.document_id)
                .subquery()
            )
            latest_rows = (
                db.session.query(DocumentVersion)
                .join(
                    latest_version_no_subquery,
                    (DocumentVersion.document_id == latest_version_no_subquery.c.document_id)
                    & (DocumentVersion.version_no == latest_version_no_subquery.c.max_version_no),
                )
                .all()
            )
            latest_versions = {row.document_id: row for row in latest_rows}
        search_scores: dict[int, int] = {}
        if q:
            version_ids = [v.id for v in latest_versions.values() if v is not None]
            ocr_rows = (
                DocumentOCRText.query.filter(DocumentOCRText.document_version_id.in_(version_ids)).all()
                if version_ids
                else []
            )
            ocr_by_version = {row.document_version_id: (row.extracted_text or "").lower() for row in ocr_rows}
            ranked: list[tuple[int, DocumentRecord]] = []
            for doc in docs:
                score = 0
                if q in (doc.title or "").lower():
                    score += 6
                if q in (doc.document_type or "").lower():
                    score += 3
                if q in (doc.confidentiality or "").lower():
                    score += 2
                if q in (doc.privilege_label or "").lower():
                    score += 2
                latest = latest_versions.get(doc.id)
                if latest is not None:
                    if q in (latest.notes or "").lower():
                        score += 1
                    if q in ocr_by_version.get(latest.id, ""):
                        score += 1
                if score > 0:
                    ranked.append((score, doc))
                    search_scores[doc.id] = score
            ranked.sort(key=lambda item: (item[0], item[1].created_at), reverse=True)
            docs = [item[1] for item in ranked]

        doc_templates = DocumentTemplate.query.order_by(DocumentTemplate.created_at.desc()).limit(300).all()
        audit("dms_repository_access", "Matter", matter_id)
        return page(
            "Matter DMS",
            "dms/matter_dms.html",
            m=m,
            docs=docs,
            latest_versions=latest_versions,
            q=q,
            filter_type=filter_type,
            filter_confidentiality=filter_confidentiality,
            search_scores=search_scores,
            doc_templates=doc_templates,
        )

    @app.route("/documents/<int:document_id>/versions", methods=["GET", "POST"])
    @login_required
    def document_versions(document_id: int):
        doc = db.session.get(DocumentRecord, document_id)
        if not doc:
            abort(404)
        if not can_access_matter(doc.matter_id):
            abort(403)

        if request.method == "POST":
            lock = (
                DocumentLock.query.filter_by(document_id=document_id, released_at=None)
                .order_by(DocumentLock.locked_at.desc())
                .first()
            )
            if lock and lock.locked_by != current_user.id:
                flash("Document is locked by another user.", "warning")
                return redirect(url_for("document_versions", document_id=document_id))

            state = (request.form.get("state") or "draft").strip().lower() or "draft"
            if state not in DOCUMENT_STATES:
                flash("Invalid version state.", "warning")
                return redirect(url_for("document_versions", document_id=document_id))

            f = request.files.get("file")
            if not f or not f.filename:
                flash("Version file required.", "warning")
                return redirect(url_for("document_versions", document_id=document_id))
            if not allowed_doc(f.filename):
                flash("Unsupported file type.", "warning")
                return redirect(url_for("document_versions", document_id=document_id))
            enforce_data_residency("primary_storage")

            safe_name = secure_filename(f.filename)
            if not safe_name:
                flash("Invalid filename.", "warning")
                return redirect(url_for("document_versions", document_id=document_id))
            stored = f"dms_{doc.matter_id}_{uuid.uuid4().hex}_{safe_name}"
            path = os.path.join(app.config["UPLOAD_DIR"], stored)
            f.save(path)
            sha = sha256_file(path)

            # Serialize version number assignment for this document on databases that support row locks.
            db.session.query(DocumentRecord).filter_by(id=doc.id).with_for_update().first()
            last = _latest_version(document_id)
            next_no = (last.version_no if last else 0) + 1
            prev_hash = last.hash_chain_current if last else None
            chain_hash = _chain_hash(prev_hash, sha)
            try:
                legacy_file = DocumentFile(
                    matter_id=doc.matter_id,
                    original_filename=safe_name,
                    stored_filename=stored,
                    sha256=sha,
                    content_type=f.mimetype,
                    category=doc.document_type,
                    doc_version=str(next_no),
                    lifecycle_stage=state.capitalize(),
                    owner_name=current_user.full_name,
                    is_privileged=bool(doc.privilege_label),
                    uploaded_by=current_user.id,
                )
                db.session.add(legacy_file)
                db.session.flush()

                ver = DocumentVersion(
                    document_id=doc.id,
                    document_file_id=legacy_file.id,
                    version_no=next_no,
                    original_filename=safe_name,
                    stored_filename=stored,
                    sha256=sha,
                    hash_chain_prev=prev_hash,
                    hash_chain_current=chain_hash,
                    state=state,
                    notes=(request.form.get("notes") or "").strip() or None,
                    uploaded_by=current_user.id,
                )
                db.session.add(ver)
                db.session.flush()
                db.session.add(
                    DocumentOCRText(
                        document_version_id=ver.id,
                        extracted_text=_extract_ocr_text(path, f.mimetype),
                    )
                )
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                _safe_remove_file(path)
                flash("Another version was uploaded at the same time. Please retry.", "warning")
                return redirect(url_for("document_versions", document_id=document_id))
            except Exception:
                db.session.rollback()
                _safe_remove_file(path)
                flash("Version upload failed. Please retry.", "warning")
                return redirect(url_for("document_versions", document_id=document_id))
            NotificationEngine.enqueue("document_uploaded", current_user.id, f"document_version:{ver.id}")
            audit("dms_version_add", "DocumentVersion", ver.id, {"document_id": doc.id, "version_no": next_no})
            flash("Version uploaded.", "info")
            return redirect(url_for("document_versions", document_id=document_id))

        versions = DocumentVersion.query.filter_by(document_id=document_id).order_by(DocumentVersion.version_no.desc()).all()
        locks = DocumentLock.query.filter_by(document_id=document_id).order_by(DocumentLock.locked_at.desc()).limit(10).all()
        audit("dms_version_access", "DocumentRecord", doc.id)
        return page("Document Versions", "dms/versions.html", doc=doc, versions=versions, locks=locks)

    @app.post("/documents/<int:document_id>/lock")
    @login_required
    def document_lock(document_id: int):
        doc = db.session.get(DocumentRecord, document_id)
        if not doc:
            abort(404)
        if not can_access_matter(doc.matter_id):
            abort(403)

        active = DocumentLock.query.filter_by(document_id=document_id, released_at=None).first()
        if active and active.locked_by != current_user.id:
            flash("Document already locked by another user.", "warning")
            return redirect(url_for("document_versions", document_id=document_id))

        if not active:
            db.session.add(
                DocumentLock(
                    document_id=document_id,
                    locked_by=current_user.id,
                    lock_reason=(request.form.get("reason") or "").strip() or None,
                    expires_at=dt.datetime.utcnow() + dt.timedelta(hours=8),
                )
            )
            db.session.commit()
            audit("dms_lock", "DocumentRecord", document_id)
        flash("Document locked.", "info")
        return redirect(url_for("document_versions", document_id=document_id))

    @app.post("/documents/<int:document_id>/unlock")
    @login_required
    def document_unlock(document_id: int):
        doc = db.session.get(DocumentRecord, document_id)
        if not doc:
            abort(404)
        if not can_access_matter(doc.matter_id):
            abort(403)

        active = DocumentLock.query.filter_by(document_id=document_id, released_at=None).first()
        if active and (active.locked_by == current_user.id or current_user.role == "admin"):
            active.released_at = dt.datetime.utcnow()
            db.session.commit()
            audit("dms_unlock", "DocumentRecord", document_id)
            flash("Document unlocked.", "info")
        else:
            flash("No releasable lock found.", "warning")
        return redirect(url_for("document_versions", document_id=document_id))

    @app.post("/documents/<int:document_id>/state")
    @login_required
    def document_state(document_id: int):
        doc = db.session.get(DocumentRecord, document_id)
        if not doc:
            abort(404)
        if not can_access_matter(doc.matter_id):
            abort(403)

        state = (request.form.get("state") or "draft").strip().lower()
        if state not in DOCUMENT_STATES:
            flash("Invalid state.", "warning")
            return redirect(url_for("document_versions", document_id=document_id))

        latest = _latest_version(document_id)
        if not latest:
            flash("No version exists.", "warning")
            return redirect(url_for("document_versions", document_id=document_id))
        if latest.is_immutable and latest.state == "filed" and state != "filed":
            flash("Filed version is immutable. Upload a new version for changes.", "warning")
            return redirect(url_for("document_versions", document_id=document_id))
        latest.state = state
        if state == "filed":
            latest.is_immutable = True
            latest.filed_reference = (request.form.get("filed_reference") or "").strip() or None
        db.session.commit()
        audit("dms_state_change", "DocumentVersion", latest.id, {"state": state})
        flash("Document state updated.", "info")
        return redirect(url_for("document_versions", document_id=document_id))

    @app.route("/productions", methods=["GET", "POST"])
    @login_required
    def productions():
        if request.method == "POST":
            matter_id = request.form.get("matter_id", type=int)
            if not matter_id or not can_access_matter(matter_id):
                abort(403)
            name = (request.form.get("name") or "").strip()
            if not name:
                flash("Production set name required.", "warning")
                return redirect(url_for("productions"))
            row = ProductionSet(
                matter_id=matter_id,
                name=name,
                confidentiality_designation=(request.form.get("confidentiality_designation") or "").strip() or None,
                watermark_text=(request.form.get("watermark_text") or "").strip() or None,
                bates_prefix=(request.form.get("bates_prefix") or "").strip() or None,
                bates_start=request.form.get("bates_start", type=int),
                bates_end=request.form.get("bates_end", type=int),
                created_by=current_user.id,
            )
            db.session.add(row)
            db.session.commit()
            audit("production_set_create", "ProductionSet", row.id)
            flash("Production set created.", "info")
            return redirect(url_for("productions"))

        set_query = ProductionSet.query
        matter_query = Matter.query
        if not is_admin():
            scoped_ids = visible_matter_ids()
            if scoped_ids:
                set_query = set_query.filter(ProductionSet.matter_id.in_(scoped_ids))
                matter_query = matter_query.filter(Matter.id.in_(scoped_ids))
            else:
                set_query = set_query.filter(ProductionSet.id == -1)
                matter_query = matter_query.filter(Matter.id == -1)
        sets = set_query.order_by(ProductionSet.created_at.desc()).limit(200).all()
        matters = matter_query.order_by(Matter.opened_at.desc()).limit(300).all()
        return page("Productions", "dms/productions.html", sets=sets, matters=matters)

    @app.get("/productions/<int:production_id>/export")
    @login_required
    def production_export(production_id: int):
        row = db.session.get(ProductionSet, production_id)
        if not row:
            abort(404)
        if not can_access_matter(row.matter_id):
            abort(403)
        enforce_data_residency("exports")

        items = (
            db.session.query(ProductionItem, DocumentVersion)
            .join(DocumentVersion, DocumentVersion.id == ProductionItem.document_version_id)
            .filter(ProductionItem.production_set_id == production_id)
            .order_by(ProductionItem.id.asc())
            .all()
        )

        payload = {
            "production_set": {
                "id": row.id,
                "name": row.name,
                "matter_id": row.matter_id,
                "confidentiality_designation": row.confidentiality_designation,
                "watermark_text": row.watermark_text,
            },
            "items": [
                {
                    "production_item_id": item.id,
                    "bates_number": item.bates_number,
                    "document_version_id": ver.id,
                    "filename": ver.original_filename,
                    "sha256": ver.sha256,
                    "state": ver.state,
                }
                for item, ver in items
            ],
        }
        audit("production_export", "ProductionSet", row.id, {"item_count": len(items)})
        return Response(json.dumps(payload, indent=2), mimetype="application/json")

    @app.route("/bates/ranges", methods=["GET", "POST"])
    @login_required
    def bates_ranges():
        if request.method == "POST":
            production_set_id = request.form.get("production_set_id", type=int)
            production = db.session.get(ProductionSet, production_set_id) if production_set_id else None
            if not production:
                flash("Production set required.", "warning")
                return redirect(url_for("bates_ranges"))
            if not can_access_matter(production.matter_id):
                abort(403)

            prefix = (request.form.get("prefix") or "").strip().upper()
            start_no = request.form.get("start_no", type=int)
            end_no = request.form.get("end_no", type=int)
            if not prefix or start_no is None or end_no is None or end_no < start_no:
                flash("Valid prefix/start/end required.", "warning")
                return redirect(url_for("bates_ranges"))

            rng = BatesRange(
                production_set_id=production_set_id,
                prefix=prefix,
                start_no=start_no,
                end_no=end_no,
            )
            db.session.add(rng)

            versions = (
                DocumentVersion.query.join(DocumentRecord, DocumentRecord.id == DocumentVersion.document_id)
                .filter(DocumentRecord.matter_id == production.matter_id)
                .order_by(DocumentVersion.id.asc())
                .all()
            )
            for idx, ver in enumerate(versions, start=start_no):
                if idx > end_no:
                    break
                bates_no = f"{prefix}{idx:06d}"
                item = ProductionItem.query.filter_by(production_set_id=production_set_id, document_version_id=ver.id).first()
                if item is None:
                    item = ProductionItem(production_set_id=production_set_id, document_version_id=ver.id)
                    db.session.add(item)
                item.bates_number = bates_no

            db.session.commit()
            audit("bates_range_assign", "BatesRange", rng.id)
            flash("Bates range assigned.", "info")
            return redirect(url_for("bates_ranges"))

        range_query = BatesRange.query.join(ProductionSet, ProductionSet.id == BatesRange.production_set_id)
        set_query = ProductionSet.query
        if not is_admin():
            scoped_ids = visible_matter_ids()
            if scoped_ids:
                range_query = range_query.filter(ProductionSet.matter_id.in_(scoped_ids))
                set_query = set_query.filter(ProductionSet.matter_id.in_(scoped_ids))
            else:
                range_query = range_query.filter(BatesRange.id == -1)
                set_query = set_query.filter(ProductionSet.id == -1)
        ranges = range_query.order_by(BatesRange.created_at.desc()).limit(200).all()
        sets = set_query.order_by(ProductionSet.created_at.desc()).limit(200).all()
        return page("Bates Ranges", "dms/bates_ranges.html", ranges=ranges, sets=sets)

    @app.route("/dms/saved-searches", methods=["GET", "POST"])
    @login_required
    def dms_saved_searches():
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            query_text = (request.form.get("query") or "").strip()
            matter_id = request.form.get("matter_id", type=int)
            if not name or not query_text:
                flash("Search name and query are required.", "warning")
                return redirect(url_for("dms_saved_searches"))
            if matter_id and not can_access_matter(matter_id):
                abort(403)

            row = SavedSearch(
                user_id=current_user.id,
                name=name,
                query_json=json.dumps({"q": query_text}),
                matter_id=matter_id,
            )
            db.session.add(row)
            db.session.commit()
            audit("dms_saved_search_create", "SavedSearch", row.id)
            flash("Saved search created.", "info")
            return redirect(url_for("dms_saved_searches"))

        rows = SavedSearch.query.filter_by(user_id=current_user.id).order_by(SavedSearch.created_at.desc()).limit(200).all()
        if not is_admin():
            scope_ids = visible_matter_ids()
            rows = [r for r in rows if (r.matter_id is None or r.matter_id in scope_ids)]
            matter_query = Matter.query.filter(Matter.id.in_(scope_ids)) if scope_ids else Matter.query.filter(Matter.id == -1)
        else:
            matter_query = Matter.query
        matters = matter_query.order_by(Matter.opened_at.desc()).limit(300).all()
        queries = {row.id: _safe_query_json(row.query_json).get("q", "") for row in rows}
        return page("DMS Saved Searches", "dms/saved_searches.html", searches=rows, queries=queries, matters=matters)

    @app.route("/matters/<int:matter_id>/email-capture", methods=["GET", "POST"])
    @login_required
    def matter_email_capture(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        if request.method == "POST":
            message_id = (request.form.get("message_id") or "").strip()
            if not message_id:
                flash("Message id is required.", "warning")
                return redirect(url_for("matter_email_capture", matter_id=matter_id))

            message_hash = hashlib.sha256(message_id.encode("utf-8")).hexdigest()
            dedup_key = (request.form.get("dedup_key") or "").strip() or message_hash
            duplicate = EmailCapture.query.filter_by(matter_id=matter_id, dedup_key=dedup_key).first()
            if duplicate:
                flash("Duplicate email capture ignored.", "warning")
                return redirect(url_for("matter_email_capture", matter_id=matter_id))

            received_at = None
            received_raw = (request.form.get("received_at") or "").strip()
            if received_raw:
                try:
                    received_at = dt.datetime.fromisoformat(received_raw)
                except ValueError:
                    flash("Invalid received datetime.", "warning")
                    return redirect(url_for("matter_email_capture", matter_id=matter_id))

            stored_filename = None
            attachment_hash = None
            attachment_path = None
            f = request.files.get("attachment")
            if f and f.filename:
                if not allowed_doc(f.filename):
                    flash("Unsupported attachment type.", "warning")
                    return redirect(url_for("matter_email_capture", matter_id=matter_id))
                enforce_data_residency("primary_storage")
                safe_name = secure_filename(f.filename)
                if not safe_name:
                    flash("Invalid attachment filename.", "warning")
                    return redirect(url_for("matter_email_capture", matter_id=matter_id))
                stored_filename = f"email_{matter_id}_{uuid.uuid4().hex}_{safe_name}"
                attachment_path = os.path.join(app.config["UPLOAD_DIR"], stored_filename)
                f.save(attachment_path)
                attachment_hash = sha256_file(attachment_path)

            try:
                row = EmailCapture(
                    matter_id=matter_id,
                    message_id_hash=message_hash,
                    dedup_key=dedup_key,
                    subject=(request.form.get("subject") or "").strip() or None,
                    sender=(request.form.get("sender") or "").strip() or None,
                    received_at=received_at,
                    stored_filename=stored_filename,
                    attachment_hash=attachment_hash,
                    captured_by=current_user.id,
                )
                db.session.add(row)
                db.session.commit()
            except Exception:
                db.session.rollback()
                _safe_remove_file(attachment_path or "")
                flash("Failed to capture email. Please retry.", "warning")
                return redirect(url_for("matter_email_capture", matter_id=matter_id))
            audit("email_capture_create", "EmailCapture", row.id, {"matter_id": matter_id})
            flash("Email captured.", "info")
            return redirect(url_for("matter_email_capture", matter_id=matter_id))

        rows = EmailCapture.query.filter_by(matter_id=matter_id).order_by(EmailCapture.captured_at.desc()).limit(300).all()
        return page("Email Capture", "dms/email_capture.html", m=m, rows=rows)

    @app.get("/email-capture/<int:capture_id>/attachment")
    @login_required
    def email_capture_attachment(capture_id: int):
        row = db.session.get(EmailCapture, capture_id)
        if row is None:
            abort(404)
        if not can_access_matter(int(row.matter_id)):
            abort(403)
        enforce_data_residency("exports")
        if not row.stored_filename:
            abort(404)
        path = os.path.join(app.config["UPLOAD_DIR"], row.stored_filename)
        if not os.path.isfile(path):
            abort(404)
        audit("email_capture_attachment_access", "EmailCapture", row.id, {"matter_id": row.matter_id})
        return send_from_directory(
            app.config["UPLOAD_DIR"],
            row.stored_filename,
            as_attachment=True,
            download_name=os.path.basename(row.stored_filename),
        )
