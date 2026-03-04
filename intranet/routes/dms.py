from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re

from flask import Response, abort, current_app, flash, redirect, request, send_from_directory, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from werkzeug.utils import secure_filename

from ..extensions import db
from ..helpers import allowed_doc, audit, can_access_matter, is_admin, sha256_file
from ..models import (
    BatesRange,
    ConflictSemanticHit,
    DocumentFile,
    DocumentLock,
    DocumentOCRText,
    DocumentRecord,
    DocumentTemplate,
    DocumentVersion,
    EmailCapture,
    Matter,
    PortalLinkToken,
    ProductionItem,
    ProductionSet,
    SavedSearch,
    TrustLedgerEntry,
)
from ..policies import enforce_permission, visible_matter_ids
from ..policies.residency import enforce_data_residency
from ..roles import canonical_role, role_is_admin
from ..services.dms_option_lists import DEFAULT_DMS_OPTION_LISTS, load_dms_option_lists
from ..services.notification_engine import NotificationEngine
from ..services.semantic_search import SemanticSearchService
from ..services.storage_paths import build_matter_storage_name, resolve_upload_path
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


def _can_delete_document(role: str | None) -> bool:
    return canonical_role(role) == "senior_attorney"


def _match_option(raw: str | None, options: list[str]) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    lookup = {
        str(option).strip().casefold(): str(option).strip()
        for option in options
        if str(option).strip()
    }
    return lookup.get(value.casefold(), "")


def _coerce_option_value(
    raw: str | None,
    options: list[str],
    *,
    field_label: str,
    default_value: str | None = None,
    allow_blank: bool = False,
) -> str | None:
    candidate = str(raw or "").strip()
    if not candidate and default_value:
        candidate = str(default_value).strip()
    if not candidate:
        if allow_blank:
            return None
        raise ValueError(f"{field_label} is required.")
    matched = _match_option(candidate, options)
    if not matched:
        raise ValueError(f"Invalid {field_label.lower()}. Select a value from the configured list.")
    return matched


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


def _template_token_requirements(template_body: str | None, builtin_context_keys: set[str]) -> dict[str, list[str]]:
    raw_tokens = {
        (match.group(1) or "").strip().lower()
        for match in TEMPLATE_TOKEN_PATTERN.finditer(template_body or "")
        if (match.group(1) or "").strip()
    }
    all_tokens = sorted(raw_tokens)
    built_in_tokens = [token for token in all_tokens if token in builtin_context_keys]
    custom_tokens = [token for token in all_tokens if token not in builtin_context_keys]
    return {
        "all_tokens": all_tokens,
        "built_in_tokens": built_in_tokens,
        "custom_tokens": custom_tokens,
    }


def _safe_load_dms_option_lists() -> dict[str, list[str]]:
    try:
        payload = load_dms_option_lists()
    except Exception:  # pragma: no cover - defensive fallback for runtime config/schema drift
        db.session.rollback()
        current_app.logger.exception("Failed to load DMS option lists; falling back to defaults.")
        payload = {}
    normalized: dict[str, list[str]] = {}
    for key, defaults in DEFAULT_DMS_OPTION_LISTS.items():
        values = payload.get(key) if isinstance(payload, dict) else None
        options = [str(item).strip() for item in (values or []) if str(item).strip()]
        normalized[key] = options if options else list(defaults)
    return normalized


def register_dms_routes(app):
    @app.get("/dms")
    @login_required
    def dms_home():
        enforce_permission("dms", "read")
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
        action = (request.form.get("action") or "upload_document").strip().lower() if request.method == "POST" else ""
        is_upload_action = request.method == "POST" and action == "upload_document"
        # Uploading a document is intentionally available to any authenticated user,
        # regardless of matter access or role-level DMS grants.
        if not is_upload_action:
            enforce_permission("dms", "read")
        has_matter_access = can_access_matter(matter_id)
        if not has_matter_access and not is_upload_action:
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)
        dms_option_lists = _safe_load_dms_option_lists()
        document_type_options = list(dms_option_lists.get("document_types") or [])
        confidentiality_options = list(dms_option_lists.get("confidentialities") or [])
        privilege_label_options = list(dms_option_lists.get("privilege_labels") or [])
        retention_category_options = list(dms_option_lists.get("retention_categories") or [])
        default_document_type = document_type_options[0] if document_type_options else "General"
        default_confidentiality = confidentiality_options[0] if confidentiality_options else "Internal"

        if request.method == "POST":
            action = action or "upload_document"
            if action == "generate_from_template":
                enforce_permission("dms", "write")
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
                try:
                    generated_document_type = _coerce_option_value(
                        (request.form.get("generated_document_type") or template.template_type),
                        document_type_options,
                        field_label="Document type",
                        default_value=default_document_type,
                    )
                    generated_confidentiality = _coerce_option_value(
                        request.form.get("generated_confidentiality"),
                        confidentiality_options,
                        field_label="Confidentiality",
                        default_value=default_confidentiality,
                    )
                    generated_privilege_label = _coerce_option_value(
                        request.form.get("generated_privilege_label"),
                        privilege_label_options,
                        field_label="Privilege label",
                        allow_blank=True,
                    )
                    generated_retention_category = _coerce_option_value(
                        request.form.get("generated_retention_category"),
                        retention_category_options,
                        field_label="Retention category",
                        allow_blank=True,
                    )
                except ValueError as exc:
                    flash(str(exc), "warning")
                    return redirect(url_for("matter_dms", matter_id=matter_id))

                enforce_data_residency("primary_storage")
                os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)
                base_name = secure_filename(generated_title) or f"generated_{template.id}"
                safe_name = f"{base_name[:80]}.txt"
                stored = build_matter_storage_name("dms", matter_id, safe_name)
                try:
                    stored, path = resolve_upload_path(app.config["UPLOAD_DIR"], stored, create_parent=True)
                except ValueError:
                    flash("Storage path validation failed for generated document.", "warning")
                    return redirect(url_for("matter_dms", matter_id=matter_id))
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
                        document_type=generated_document_type,
                        confidentiality=generated_confidentiality,
                        privilege_label=generated_privilege_label,
                        retention_category=generated_retention_category,
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
                try:
                    SemanticSearchService.enqueue_document_version_index(version.id, requested_by=current_user.id)
                except Exception:  # pragma: no cover - non-blocking indexing fallback
                    current_app.logger.exception(
                        "Failed to queue semantic index for generated document version_id=%s", version.id
                    )
                if missing_tokens:
                    flash(
                        "Document generated, but some merge fields were blank: " + ", ".join(missing_tokens[:6]),
                        "warning",
                    )
                else:
                    flash("Document generated from template.", "info")
                return redirect(url_for("matter_dms", matter_id=matter_id))
            if action != "upload_document":
                flash("Unsupported DMS action.", "warning")
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
            try:
                document_type = _coerce_option_value(
                    request.form.get("document_type"),
                    document_type_options,
                    field_label="Document type",
                    default_value=default_document_type,
                )
                confidentiality = _coerce_option_value(
                    request.form.get("confidentiality"),
                    confidentiality_options,
                    field_label="Confidentiality",
                    default_value=default_confidentiality,
                )
                privilege_label = _coerce_option_value(
                    request.form.get("privilege_label"),
                    privilege_label_options,
                    field_label="Privilege label",
                    allow_blank=True,
                )
                retention_category = _coerce_option_value(
                    request.form.get("retention_category"),
                    retention_category_options,
                    field_label="Retention category",
                    allow_blank=True,
                )
            except ValueError as exc:
                flash(str(exc), "warning")
                return redirect(url_for("matter_dms", matter_id=matter_id))

            safe_name = secure_filename(f.filename)
            if not safe_name:
                flash("Invalid filename.", "warning")
                return redirect(url_for("matter_dms", matter_id=matter_id))
            stored = build_matter_storage_name("dms", matter_id, safe_name)
            try:
                stored, path = resolve_upload_path(app.config["UPLOAD_DIR"], stored, create_parent=True)
            except ValueError:
                flash("Storage path validation failed for upload.", "warning")
                return redirect(url_for("matter_dms", matter_id=matter_id))
            f.save(path)
            sha = sha256_file(path)
            try:
                container = DocumentRecord(
                    matter_id=matter_id,
                    title=title,
                    document_type=document_type,
                    confidentiality=confidentiality,
                    privilege_label=privilege_label,
                    retention_category=retention_category,
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
            try:
                SemanticSearchService.enqueue_document_version_index(version.id, requested_by=current_user.id)
            except Exception:  # pragma: no cover - non-blocking indexing fallback
                current_app.logger.exception("Failed to queue semantic index for document version_id=%s", version.id)
            flash("Document created in DMS.", "info")
            return redirect(url_for("matter_dms", matter_id=matter_id))

        q = (request.args.get("q") or "").strip().lower()
        filter_type = _match_option(request.args.get("document_type"), document_type_options) or (
            request.args.get("document_type") or ""
        ).strip()
        filter_confidentiality = _match_option(request.args.get("confidentiality"), confidentiality_options) or (
            request.args.get("confidentiality") or ""
        ).strip()
        page_number = request.args.get("page", default=1, type=int) or 1
        if page_number < 1:
            page_number = 1

        docs_query = DocumentRecord.query.filter(DocumentRecord.matter_id == matter_id)
        if filter_type:
            docs_query = docs_query.filter(
                func.lower(func.coalesce(DocumentRecord.document_type, "")) == filter_type.lower()
            )
        if filter_confidentiality:
            docs_query = docs_query.filter(
                func.lower(func.coalesce(DocumentRecord.confidentiality, "")) == filter_confidentiality.lower()
            )
        if q:
            like = f"%{q}%"
            scope_doc_ids_query = docs_query.with_entities(DocumentRecord.id)
            latest_version_no_subquery = (
                db.session.query(
                    DocumentVersion.document_id.label("document_id"),
                    func.max(DocumentVersion.version_no).label("max_version_no"),
                )
                .filter(DocumentVersion.document_id.in_(scope_doc_ids_query))
                .group_by(DocumentVersion.document_id)
                .subquery()
            )
            latest_version_rows = (
                db.session.query(DocumentVersion.document_id, DocumentVersion.id, DocumentVersion.notes)
                .join(
                    latest_version_no_subquery,
                    (DocumentVersion.document_id == latest_version_no_subquery.c.document_id)
                    & (DocumentVersion.version_no == latest_version_no_subquery.c.max_version_no),
                )
                .subquery()
            )
            ocr_match_doc_ids = (
                db.session.query(latest_version_rows.c.document_id)
                .join(DocumentOCRText, DocumentOCRText.document_version_id == latest_version_rows.c.id)
                .filter(DocumentOCRText.extracted_text.ilike(like))
            )
            notes_match_doc_ids = db.session.query(latest_version_rows.c.document_id).filter(
                latest_version_rows.c.notes.ilike(like)
            )
            docs_query = docs_query.filter(
                or_(
                    DocumentRecord.title.ilike(like),
                    DocumentRecord.document_type.ilike(like),
                    DocumentRecord.confidentiality.ilike(like),
                    DocumentRecord.privilege_label.ilike(like),
                    DocumentRecord.id.in_(notes_match_doc_ids),
                    DocumentRecord.id.in_(ocr_match_doc_ids),
                )
            )

        pagination = docs_query.order_by(DocumentRecord.created_at.desc()).paginate(
            page=page_number,
            per_page=50,
            error_out=False,
        )
        docs = pagination.items

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
        if q and docs:
            version_ids = [v.id for v in latest_versions.values() if v is not None]
            ocr_rows = (
                DocumentOCRText.query.filter(DocumentOCRText.document_version_id.in_(version_ids)).all()
                if version_ids
                else []
            )
            ocr_by_version = {row.document_version_id: (row.extracted_text or "").lower() for row in ocr_rows}
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
                    search_scores[doc.id] = score

        try:
            doc_templates = DocumentTemplate.query.order_by(DocumentTemplate.created_at.desc()).limit(300).all()
        except Exception:  # pragma: no cover - defensive fallback for schema drift
            current_app.logger.exception("Failed to load document templates for matter_dms(matter_id=%s).", matter_id)
            doc_templates = []
        builtin_context_keys = set(_template_context(m).keys())
        template_requirements_map = {
            str(template.id): _template_token_requirements(template.body, builtin_context_keys)
            for template in doc_templates
        }
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
            pagination=pagination,
            dms_document_types=document_type_options,
            dms_confidentialities=confidentiality_options,
            dms_privilege_labels=privilege_label_options,
            dms_retention_categories=retention_category_options,
            default_document_type=default_document_type,
            default_confidentiality=default_confidentiality,
            template_requirements_map=template_requirements_map,
        )

    @app.route("/documents/<int:document_id>/versions", methods=["GET", "POST"])
    @login_required
    def document_versions(document_id: int):
        # Uploading a new version is intentionally available to any authenticated
        # user, regardless of matter access or role-level DMS grants.
        is_upload_version = request.method == "POST"
        if not is_upload_version:
            enforce_permission("dms", "read")
        doc = db.session.get(DocumentRecord, document_id)
        if not doc:
            abort(404)
        has_matter_access = can_access_matter(doc.matter_id)
        if not has_matter_access and not is_upload_version:
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
            stored = build_matter_storage_name("dms", doc.matter_id, safe_name)
            try:
                stored, path = resolve_upload_path(app.config["UPLOAD_DIR"], stored, create_parent=True)
            except ValueError:
                flash("Storage path validation failed for upload.", "warning")
                return redirect(url_for("document_versions", document_id=document_id))
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
            try:
                SemanticSearchService.enqueue_document_version_index(ver.id, requested_by=current_user.id)
            except Exception:  # pragma: no cover - non-blocking indexing fallback
                current_app.logger.exception("Failed to queue semantic index for document version_id=%s", ver.id)
            audit("dms_version_add", "DocumentVersion", ver.id, {"document_id": doc.id, "version_no": next_no})
            flash("Version uploaded.", "info")
            return redirect(url_for("document_versions", document_id=document_id))

        page_number = request.args.get("page", default=1, type=int) or 1
        if page_number < 1:
            page_number = 1
        pagination = (
            DocumentVersion.query.filter_by(document_id=document_id)
            .order_by(DocumentVersion.version_no.desc())
            .paginate(page=page_number, per_page=30, error_out=False)
        )
        versions = pagination.items
        locks = DocumentLock.query.filter_by(document_id=document_id).order_by(DocumentLock.locked_at.desc()).limit(10).all()
        audit("dms_version_access", "DocumentRecord", doc.id)
        return page(
            "Document Versions",
            "dms/versions.html",
            doc=doc,
            versions=versions,
            locks=locks,
            pagination=pagination,
            can_delete_document=_can_delete_document(getattr(current_user, "role", None)),
        )

    @app.post("/documents/<int:document_id>/delete")
    @login_required
    def document_delete(document_id: int):
        enforce_permission("dms", "read")
        doc = db.session.get(DocumentRecord, document_id)
        if not doc:
            abort(404)
        if not can_access_matter(doc.matter_id):
            abort(403)
        if not _can_delete_document(getattr(current_user, "role", None)):
            abort(403)

        matter_id = int(doc.matter_id)
        versions = DocumentVersion.query.filter_by(document_id=document_id).all()
        version_ids = [int(row.id) for row in versions]
        ocr_rows = DocumentOCRText.query.filter(DocumentOCRText.document_version_id.in_(version_ids)).all() if version_ids else []
        ocr_ids = [int(row.id) for row in ocr_rows]
        document_file_ids = sorted({int(row.document_file_id) for row in versions if row.document_file_id})
        stored_filenames = sorted({str(row.stored_filename) for row in versions if row.stored_filename})

        try:
            if version_ids:
                (
                    TrustLedgerEntry.query.filter(TrustLedgerEntry.supporting_document_id.in_(version_ids)).update(
                        {TrustLedgerEntry.supporting_document_id: None},
                        synchronize_session=False,
                    )
                )
                PortalLinkToken.query.filter(PortalLinkToken.document_version_id.in_(version_ids)).delete(
                    synchronize_session=False
                )
                ProductionItem.query.filter(ProductionItem.document_version_id.in_(version_ids)).delete(
                    synchronize_session=False
                )
                hit_filters = [ConflictSemanticHit.document_version_id.in_(version_ids)]
                if ocr_ids:
                    hit_filters.append(ConflictSemanticHit.document_ocr_text_id.in_(ocr_ids))
                ConflictSemanticHit.query.filter(or_(*hit_filters)).delete(synchronize_session=False)
                DocumentOCRText.query.filter(DocumentOCRText.document_version_id.in_(version_ids)).delete(
                    synchronize_session=False
                )
                DocumentVersion.query.filter(DocumentVersion.id.in_(version_ids)).delete(synchronize_session=False)
            DocumentLock.query.filter(DocumentLock.document_id == document_id).delete(synchronize_session=False)
            if document_file_ids:
                DocumentFile.query.filter(DocumentFile.id.in_(document_file_ids)).delete(synchronize_session=False)
            db.session.delete(doc)
            db.session.commit()
        except Exception:
            db.session.rollback()
            flash("Document delete failed. Please retry.", "warning")
            return redirect(url_for("document_versions", document_id=document_id))

        upload_dir = str(app.config.get("UPLOAD_DIR") or "").strip()
        if upload_dir:
            for stored_name in stored_filenames:
                try:
                    _, file_path = resolve_upload_path(upload_dir, stored_name)
                except ValueError:
                    continue
                _safe_remove_file(file_path)

        audit(
            "dms_document_delete",
            "DocumentRecord",
            document_id,
            {"matter_id": matter_id, "version_count": len(version_ids)},
        )
        flash("Document deleted.", "info")
        return redirect(url_for("matter_dms", matter_id=matter_id))

    @app.post("/documents/<int:document_id>/lock")
    @login_required
    def document_lock(document_id: int):
        enforce_permission("dms", "manage")
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
        enforce_permission("dms", "manage")
        doc = db.session.get(DocumentRecord, document_id)
        if not doc:
            abort(404)
        if not can_access_matter(doc.matter_id):
            abort(403)

        active = DocumentLock.query.filter_by(document_id=document_id, released_at=None).first()
        if active and (active.locked_by == current_user.id or role_is_admin(getattr(current_user, "role", None))):
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
        enforce_permission("dms", "manage")
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
        enforce_permission("dms", "read")
        if request.method == "POST":
            enforce_permission("dms", "export")
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
        enforce_permission("dms", "export")
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
        enforce_permission("dms", "read")
        if request.method == "POST":
            enforce_permission("dms", "export")
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
        enforce_permission("dms", "read")
        if request.method == "POST":
            enforce_permission("dms", "write")
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
        enforce_permission("dms", "read")
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        if request.method == "POST":
            enforce_permission("dms", "write")
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
                storage_name = build_matter_storage_name("email_capture", matter_id, safe_name)
                try:
                    stored_filename, attachment_path = resolve_upload_path(
                        app.config["UPLOAD_DIR"],
                        storage_name,
                        create_parent=True,
                    )
                except ValueError:
                    flash("Storage path validation failed for attachment.", "warning")
                    return redirect(url_for("matter_email_capture", matter_id=matter_id))
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
        enforce_permission("dms", "read")
        row = db.session.get(EmailCapture, capture_id)
        if row is None:
            abort(404)
        if not can_access_matter(int(row.matter_id)):
            abort(403)
        enforce_data_residency("exports")
        if not row.stored_filename:
            abort(404)
        try:
            stored_filename, path = resolve_upload_path(app.config["UPLOAD_DIR"], row.stored_filename)
        except ValueError:
            abort(404)
        if not os.path.isfile(path):
            abort(404)
        audit("email_capture_attachment_access", "EmailCapture", row.id, {"matter_id": row.matter_id})
        return send_from_directory(
            app.config["UPLOAD_DIR"],
            stored_filename,
            as_attachment=True,
            download_name=os.path.basename(stored_filename),
        )
