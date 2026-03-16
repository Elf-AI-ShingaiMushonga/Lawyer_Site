from __future__ import annotations

import datetime as dt
from ..timeutils import utc_now
import hashlib
import json
import os
import re
import uuid
from collections import defaultdict

from flask import abort, current_app, flash, jsonify, redirect, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.utils import secure_filename

from ..config import BUDGET_STATUSES, RISK_LEVELS
from ..extensions import db
from ..helpers import (
    allowed_audio,
    audit,
    can_access_matter,
    enforce_case_team_role,
    filter_accessible_document_files,
    filter_accessible_matter_notes,
    has_active_legal_hold,
    matter_activity,
    normalize_query,
    sha256_file,
)
from ..models import (
    ContractTemplate,
    Deadline,
    DocumentFile,
    DocumentOCRText,
    DocumentRecord,
    DocumentTemplate,
    DocumentVersion,
    Entity,
    Matter,
    MatterClosingChecklistItem,
    MatterMember,
    MatterNote,
    MatterNoteACL,
    MatterParty,
    MatterStageHistory,
    MatterTimelineEvent,
    MatterTemplate,
    MatterWorkspaceDocument,
    MatterWorkspaceDocumentComment,
    MatterWorkspaceDocumentPresence,
    Task,
    TaskAssignee,
    User,
)
from ..services.archetypes import (
    build_document_context,
    collect_required_field_values,
    humanize_required_field_label,
    load_required_fields,
    render_template_text,
    validate_required_field_values,
)
from ..services.archetype_playbook import (
    build_archetype_compliance_snapshot,
    ensure_matter_closing_checklist_items,
)
from ..services.contracts import (
    auto_contract_templates_for_archetype,
    cleanup_generated_files,
    collect_contract_field_values,
    contract_required_fields_union,
    persist_generated_document_template_document,
    persist_generated_contract_document,
    render_contract_template_for_matter,
    validate_contract_field_values,
)
from ..services.intake_ai import suggest_matter_intake
from ..services.dms_option_lists import DEFAULT_DMS_OPTION_LISTS, load_dms_option_lists
from ..services.matter_magic import attach_matter_magic_links, build_matter_launch_pack, build_matter_magic_snapshot
from ..services.matter_option_lists import legal_category_options, practice_area_options
from ..services.storage_paths import build_matter_storage_name, harden_private_file, resolve_upload_path
from ..services.workflow_automation import auto_pause_running_timers_for_matter
from ..services.notification_engine import NotificationEngine
from ..policies import enforce_permission, has_permission
from ..policies.residency import residency_allowed
from ..roles import role_is_admin
from ..templates import page

CUSTOM_ARCHETYPE_SENTINEL = "custom"
WORKSPACE_DOCUMENT_STATUSES = {"draft", "review", "final"}
WORKSPACE_PRESENCE_STATES = {"viewing", "editing", "reviewing"}
WORKSPACE_STALE_MINUTES = 15
WORKSPACE_TEMPLATE_TOKEN_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}")


def _display_missing_field_labels(labels: list[str]) -> list[str]:
    normalized: list[str] = []
    for label in labels:
        text = humanize_required_field_label(label)
        if text.lower().startswith("contract_field_"):
            text = text[len("contract_field_") :]
        if "_" in text and text.lower() == text:
            text = " ".join(part for part in text.split("_") if part).strip().title()
        if text:
            normalized.append(text)
    return normalized


def _safe_remove_file(path: str) -> None:
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


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
        raise ValueError(f"Invalid {field_label.lower()}. Select a configured value.")
    return matched


def _safe_load_dms_option_lists() -> dict[str, list[str]]:
    try:
        payload = load_dms_option_lists()
    except Exception:  # pragma: no cover - defensive fallback for schema drift
        db.session.rollback()
        current_app.logger.exception("Failed to load DMS option lists for collaborative drafting.")
        payload = {}
    normalized: dict[str, list[str]] = {}
    for key, defaults in DEFAULT_DMS_OPTION_LISTS.items():
        values = payload.get(key) if isinstance(payload, dict) else None
        options = [str(item).strip() for item in (values or []) if str(item).strip()]
        normalized[key] = options if options else list(defaults)
    return normalized


def _normalize_workspace_status(raw: str | None) -> str:
    value = str(raw or "").strip().lower() or "draft"
    return value if value in WORKSPACE_DOCUMENT_STATUSES else "draft"


def _normalize_workspace_presence_state(raw: str | None) -> str:
    value = str(raw or "").strip().lower() or "viewing"
    return value if value in WORKSPACE_PRESENCE_STATES else "viewing"


def _workspace_template_context(matter: Matter) -> dict[str, str]:
    now = utc_now()
    return {
        "matter_id": str(matter.id),
        "matter_no": matter.matter_no or "",
        "matter_title": matter.title or "",
        "title": matter.title or "",
        "client_name": matter.client_name or "",
        "status": matter.status or "",
        "stage": matter.stage or "",
        "jurisdiction": matter.jurisdiction or "",
        "practice_area": matter.practice_area or "",
        "case_type": matter.case_type or "",
        "today": now.date().isoformat(),
        "now": now.replace(microsecond=0).isoformat(),
        "generated_by_name": current_user.full_name or "",
        "generated_by_email": current_user.email or "",
    }


def _render_workspace_template(body: str, context: dict[str, str]) -> tuple[str, list[str]]:
    missing: list[str] = []

    def replace_token(match: re.Match[str]) -> str:
        key = (match.group(1) or "").strip().lower()
        if not key:
            return ""
        if key in context:
            return str(context[key])
        missing.append(key)
        return ""

    rendered = WORKSPACE_TEMPLATE_TOKEN_PATTERN.sub(replace_token, body or "")
    return rendered, sorted(set(missing))


def _default_workspace_body(matter: Matter) -> str:
    return (
        f"{matter.matter_no} - {matter.title}\n"
        f"Client: {matter.client_name}\n\n"
        "Objective\n"
        "- \n\n"
        "Facts\n"
        "- \n\n"
        "Strategy\n"
        "- \n\n"
        "Open Questions\n"
        "- \n\n"
        "Next Steps\n"
        "- \n"
    )


def _upsert_workspace_presence(
    *,
    workspace_document_id: int,
    user_id: int,
    state: str,
    cursor_label: str | None = None,
) -> MatterWorkspaceDocumentPresence:
    row = MatterWorkspaceDocumentPresence.query.filter_by(
        workspace_document_id=workspace_document_id,
        user_id=user_id,
    ).first()
    if row is None:
        row = MatterWorkspaceDocumentPresence(
            workspace_document_id=workspace_document_id,
            user_id=user_id,
        )
        db.session.add(row)
    row.state = _normalize_workspace_presence_state(state)
    row.cursor_label = (cursor_label or "").strip()[:120] or None
    row.last_seen_at = utc_now()
    return row


def _active_workspace_presence_snapshot(
    workspace_document_id: int,
) -> list[dict[str, object]]:
    cutoff = utc_now() - dt.timedelta(minutes=WORKSPACE_STALE_MINUTES)
    rows = (
        MatterWorkspaceDocumentPresence.query.filter(
            MatterWorkspaceDocumentPresence.workspace_document_id == workspace_document_id,
            MatterWorkspaceDocumentPresence.last_seen_at >= cutoff,
        )
        .order_by(MatterWorkspaceDocumentPresence.last_seen_at.desc())
        .all()
    )
    if not rows:
        return []
    user_ids = sorted({int(row.user_id) for row in rows})
    user_lookup = {
        int(user.id): user
        for user in User.query.filter(User.id.in_(user_ids)).all()
    }
    snapshot: list[dict[str, object]] = []
    for row in rows:
        user = user_lookup.get(int(row.user_id))
        snapshot.append(
            {
                "user_id": int(row.user_id),
                "display_name": user.full_name if user is not None else f"User {row.user_id}",
                "state": row.state or "viewing",
                "cursor_label": row.cursor_label or "",
                "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else "",
                "is_current_user": int(row.user_id) == int(getattr(current_user, "id", 0) or 0),
            }
        )
    return snapshot


def _latest_document_version(document_id: int) -> DocumentVersion | None:
    return (
        DocumentVersion.query.filter_by(document_id=document_id)
        .order_by(DocumentVersion.version_no.desc(), DocumentVersion.uploaded_at.desc())
        .first()
    )


def _chain_hash(prev_hash: str | None, file_sha256: str) -> str:
    seed = f"{prev_hash or 'GENESIS'}:{file_sha256}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _publish_workspace_document_snapshot(
    *,
    matter: Matter,
    workspace_document: MatterWorkspaceDocument,
) -> tuple[DocumentRecord, DocumentVersion, str]:
    allowed_primary_storage, residency_message = residency_allowed("primary_storage")
    if not allowed_primary_storage:
        raise ValueError(residency_message or "Data residency policy blocked this publish target.")

    os.makedirs(current_app.config["UPLOAD_DIR"], exist_ok=True)
    base_name = secure_filename(workspace_document.title) or f"workspace_document_{workspace_document.id}"
    safe_name = f"{base_name[:80]}.txt"
    stored = build_matter_storage_name("workspace", matter.id, safe_name)
    stored, path = resolve_upload_path(current_app.config["UPLOAD_DIR"], stored, create_parent=True)

    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(workspace_document.body or "")
        harden_private_file(path)
        sha = sha256_file(path)

        container = (
            db.session.get(DocumentRecord, workspace_document.published_document_id)
            if workspace_document.published_document_id
            else None
        )
        if container is None or int(container.matter_id) != int(matter.id):
            container = DocumentRecord(
                matter_id=matter.id,
                title=workspace_document.title,
                document_type=workspace_document.document_type,
                confidentiality=workspace_document.confidentiality,
                privilege_label=workspace_document.privilege_label,
                retention_category=workspace_document.retention_category,
                legal_hold=bool(workspace_document.legal_hold),
                created_by=current_user.id,
            )
            db.session.add(container)
            db.session.flush()
        else:
            container.title = workspace_document.title
            container.document_type = workspace_document.document_type
            container.confidentiality = workspace_document.confidentiality
            container.privilege_label = workspace_document.privilege_label
            container.retention_category = workspace_document.retention_category
            container.legal_hold = bool(workspace_document.legal_hold)

        db.session.query(DocumentRecord).filter_by(id=container.id).with_for_update().first()
        last = _latest_document_version(container.id)
        next_no = (last.version_no if last else 0) + 1
        prev_hash = last.hash_chain_current if last else None

        legacy_file = DocumentFile(
            matter_id=matter.id,
            original_filename=safe_name,
            stored_filename=stored,
            sha256=sha,
            content_type="text/plain",
            category=container.document_type,
            doc_version=str(next_no),
            lifecycle_stage=("For Review" if workspace_document.status == "review" else workspace_document.status.title()),
            owner_name=current_user.full_name,
            is_privileged=bool(container.privilege_label),
            uploaded_by=current_user.id,
        )
        db.session.add(legacy_file)
        db.session.flush()

        version = DocumentVersion(
            document_id=container.id,
            document_file_id=legacy_file.id,
            version_no=next_no,
            original_filename=safe_name,
            stored_filename=stored,
            sha256=sha,
            hash_chain_prev=prev_hash,
            hash_chain_current=_chain_hash(prev_hash, sha),
            state="reviewed" if workspace_document.status == "review" else workspace_document.status,
            notes=f"Published from collaborative workspace document '{workspace_document.title}'.",
            uploaded_by=current_user.id,
        )
        db.session.add(version)
        db.session.flush()
        db.session.add(
            DocumentOCRText(
                document_version_id=version.id,
                extracted_text=(workspace_document.body or "").replace("\x00", "") or "No collaborative text captured.",
            )
        )
        workspace_document.published_document_id = container.id
        workspace_document.published_version_id = version.id
        workspace_document.last_published_at = utc_now()
        return container, version, path
    except Exception:
        _safe_remove_file(path)
        raise


def register_matters_plus_routes(app):
    @app.post("/matters/intake/ai/parse")
    @login_required
    def matters_intake_ai_parse():
        payload = request.get_json(silent=True) or {}
        prompt = " ".join(str(payload.get("prompt") or "").split()).strip()
        if len(prompt) < 20:
            return jsonify({"ok": False, "error": "Provide at least 20 characters describing the matter intake."}), 400

        templates = MatterTemplate.query.order_by(MatterTemplate.name.asc()).all()
        suggestion = suggest_matter_intake(prompt=prompt, templates=templates)
        template_id = suggestion.get("template_id")
        try:
            template_id = int(template_id) if template_id is not None else None
        except (TypeError, ValueError):
            template_id = None
        audit(
            "matter_intake_ai_parse",
            "MatterTemplate",
            template_id,
            {"source": suggestion.get("source"), "legal_category": suggestion.get("legal_category")},
        )
        return jsonify({"ok": True, "suggestion": suggestion})

    @app.route("/matters/intake", methods=["GET", "POST"])
    @login_required
    def matters_intake():
        if request.method == "POST":
            matter_no = normalize_query(request.form.get("matter_no", "")).upper()
            title = normalize_query(request.form.get("title", ""))
            client_name = normalize_query(request.form.get("client_name", ""))
            if not matter_no or not title or not client_name:
                flash("Matter number, title, and client are required.", "warning")
                return redirect(url_for("matters_intake"))
            if Matter.query.filter_by(matter_no=matter_no).first():
                flash("Matter number already exists.", "warning")
                return redirect(url_for("matters_intake"))

            legal_category = normalize_query(request.form.get("legal_category", ""))
            template_raw_value = str(request.form.get("template_id", "") or "").strip()
            is_custom_archetype = template_raw_value.lower() == CUSTOM_ARCHETYPE_SENTINEL
            template_id = request.form.get("template_id", type=int) if not is_custom_archetype else None
            template = db.session.get(MatterTemplate, template_id) if template_id else None
            if template is None and not is_custom_archetype:
                flash("Select a matter archetype or choose Custom (No Archetype).", "warning")
                return redirect(url_for("matters_intake"))
            if not legal_category:
                legal_category = normalize_query(template.legal_category or "") if template else ""
            if template is not None and template.legal_category and legal_category != normalize_query(template.legal_category):
                flash("Selected archetype does not belong to the selected legal category.", "warning")
                return redirect(url_for("matters_intake"))

            required_field_defs = load_required_fields(template.required_fields_json if template else None)
            matter_specific_values = collect_required_field_values(request.form, required_field_defs)
            missing_required_fields = validate_required_field_values(required_field_defs, matter_specific_values)
            if missing_required_fields:
                preview = ", ".join(_display_missing_field_labels(missing_required_fields)[:5])
                archetype_name = template.name if template is not None else "selected archetype"
                flash(f"Provide required archetype fields for '{archetype_name}': {preview}.", "warning")
                return redirect(url_for("matters_intake"))

            auto_contract_templates = auto_contract_templates_for_archetype(template.id if template else None)
            auto_document_templates = (
                DocumentTemplate.query.filter(DocumentTemplate.archetype_id == int(template.id))
                .order_by(DocumentTemplate.name.asc())
                .all()
                if template is not None
                else []
            )
            contract_required_defs = contract_required_fields_union(auto_contract_templates)
            contract_field_values = collect_contract_field_values(request.form, contract_required_defs)
            for key, value in matter_specific_values.items():
                normalized_key = str(key or "").strip()
                if normalized_key and normalized_key not in contract_field_values and str(value or "").strip():
                    contract_field_values[normalized_key] = str(value).strip()
            missing_contract_fields = validate_contract_field_values(contract_required_defs, contract_field_values)
            if missing_contract_fields:
                preview = ", ".join(_display_missing_field_labels(missing_contract_fields)[:5])
                flash(f"Provide required contract fields: {preview}.", "warning")
                return redirect(url_for("matters_intake"))

            now = utc_now()
            stage_value = (request.form.get("stage") or (template.default_stage if template else None) or "Intake").strip() or "Intake"
            practice_area_value = (
                request.form.get("practice_area")
                or (template.practice_area if template else None)
                or ""
            ).strip() or None
            risk_level_value = (
                normalize_query(request.form.get("risk_level") or (template.default_risk_level if template else "Medium"))
                or "Medium"
            )
            budget_status_value = (
                normalize_query(request.form.get("budget_status") or "On Track")
                or "On Track"
            )
            if risk_level_value not in RISK_LEVELS:
                flash("Invalid risk level.", "warning")
                return redirect(url_for("matters_intake"))
            if budget_status_value not in BUDGET_STATUSES:
                flash("Invalid budget status.", "warning")
                return redirect(url_for("matters_intake"))
            m = Matter(
                matter_no=matter_no,
                title=title,
                client_name=client_name,
                status="Open",
                description=(request.form.get("description") or "").strip() or None,
                objective=(request.form.get("objective") or "").strip() or None,
                risk_level=risk_level_value,
                budget_status=budget_status_value,
                jurisdiction=(request.form.get("jurisdiction") or "ZA").strip() or "ZA",
                stage=stage_value,
                practice_area=practice_area_value,
                case_type=(request.form.get("case_type") or "General").strip() or "General",
                created_by=current_user.id,
                last_updated_at=now,
                legal_category=legal_category or None,
                archetype_id=template.id if template else None,
                archetype_data_json=(json.dumps(matter_specific_values, ensure_ascii=True) if matter_specific_values else None),
            )
            db.session.add(m)
            db.session.flush()
            db.session.add(MatterMember(matter_id=m.id, user_id=current_user.id, role_in_matter="Responsible"))
            checklist_seeded_count = ensure_matter_closing_checklist_items(m.id, template)
            generated_contract_file_paths: list[str] = []
            generated_contract_template_ids: list[int] = []
            generated_contract_missing_tokens: list[tuple[str, list[str]]] = []
            generated_document_template_ids: list[int] = []
            generated_document_missing_tokens: list[tuple[str, list[str]]] = []
            for contract_template in auto_contract_templates:
                rendered_contract, missing_tokens = render_contract_template_for_matter(
                    template=contract_template,
                    matter=m,
                    archetype=template,
                    archetype_values=matter_specific_values,
                    contract_values=contract_field_values,
                )
                if not rendered_contract.strip():
                    db.session.rollback()
                    cleanup_generated_files(generated_contract_file_paths)
                    flash(
                        f"Contract template '{contract_template.name}' produced an empty document. Intake was not created.",
                        "warning",
                    )
                    return redirect(url_for("matters_intake"))
                try:
                    _, _, file_path = persist_generated_contract_document(
                        matter=m,
                        template=contract_template,
                        rendered_body=rendered_contract,
                        actor_user_id=current_user.id,
                        actor_full_name=current_user.full_name,
                    )
                except Exception:
                    db.session.rollback()
                    cleanup_generated_files(generated_contract_file_paths)
                    flash(
                        f"Failed to generate contract '{contract_template.name}'. Intake was not created.",
                        "warning",
                    )
                    return redirect(url_for("matters_intake"))
                generated_contract_template_ids.append(int(contract_template.id))
                generated_contract_file_paths.append(file_path)
                if missing_tokens:
                    generated_contract_missing_tokens.append((contract_template.name, missing_tokens))
            for document_template in auto_document_templates:
                context = build_document_context(
                    m,
                    archetype=template,
                    required_values=matter_specific_values,
                )
                context["document_template_name"] = document_template.name or ""
                rendered_document, missing_tokens = render_template_text(document_template.body, context)
                if not rendered_document.strip():
                    db.session.rollback()
                    cleanup_generated_files(generated_contract_file_paths)
                    flash(
                        f"Document template '{document_template.name}' produced an empty document. Intake was not created.",
                        "warning",
                    )
                    return redirect(url_for("matters_intake"))
                try:
                    _, _, file_path = persist_generated_document_template_document(
                        matter=m,
                        template=document_template,
                        rendered_body=rendered_document,
                        actor_user_id=current_user.id,
                        actor_full_name=current_user.full_name,
                    )
                except Exception:
                    db.session.rollback()
                    cleanup_generated_files(generated_contract_file_paths)
                    flash(
                        f"Failed to generate document '{document_template.name}'. Intake was not created.",
                        "warning",
                    )
                    return redirect(url_for("matters_intake"))
                generated_document_template_ids.append(int(document_template.id))
                generated_contract_file_paths.append(file_path)
                if missing_tokens:
                    generated_document_missing_tokens.append((document_template.name, missing_tokens))

            db.session.add(
                MatterStageHistory(
                    matter_id=m.id,
                    from_stage=None,
                    to_stage=m.stage or "Intake",
                    reason="Matter intake",
                    changed_by=current_user.id,
                )
            )
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
                cleanup_generated_files(generated_contract_file_paths)
                flash("Matter intake could not be created due to a storage error. Please retry.", "warning")
                return redirect(url_for("matters_intake"))
            audit(
                "matter_intake",
                "Matter",
                m.id,
                {
                    "matter_no": m.matter_no,
                    "contract_template_ids": generated_contract_template_ids,
                    "document_template_ids": generated_document_template_ids,
                    "playbook_checklist_seeded": checklist_seeded_count,
                },
            )
            matter_activity(m.id, "Matter intake created", f"Stage {m.stage}")
            if generated_contract_template_ids or generated_document_template_ids:
                flash(
                    (
                        "Matter intake created. "
                        f"{len(generated_contract_template_ids)} contract draft(s) and "
                        f"{len(generated_document_template_ids)} document draft(s) were attached."
                    ),
                    "info",
                )
            else:
                flash("Matter intake created.", "info")
            if generated_contract_missing_tokens:
                warnings = [
                    f"{template_name}: {', '.join(tokens[:4])}"
                    for template_name, tokens in generated_contract_missing_tokens[:3]
                ]
                flash("Some contract merge fields were blank: " + "; ".join(warnings), "warning")
            if generated_document_missing_tokens:
                warnings = [
                    f"{template_name}: {', '.join(tokens[:4])}"
                    for template_name, tokens in generated_document_missing_tokens[:3]
                ]
                flash("Some document merge fields were blank: " + "; ".join(warnings), "warning")
            if checklist_seeded_count > 0:
                flash(f"Archetype playbook checklist seeded with {checklist_seeded_count} item(s).", "info")
            return redirect(url_for("matter_workspace", matter_id=m.id))

        templates = MatterTemplate.query.order_by(MatterTemplate.name.asc()).all()
        template_ids = [int(row.id) for row in templates]
        linked_contract_templates = (
            ContractTemplate.query.filter(
                ContractTemplate.archetype_id.in_(template_ids),
                ContractTemplate.is_active.is_(True),
                ContractTemplate.auto_create_on_matter_open.is_(True),
            )
            .order_by(ContractTemplate.name.asc())
            .all()
            if template_ids
            else []
        )
        contract_templates_by_archetype: dict[int, list[ContractTemplate]] = {}
        for contract_template in linked_contract_templates:
            if not contract_template.archetype_id:
                continue
            key = int(contract_template.archetype_id)
            contract_templates_by_archetype.setdefault(key, []).append(contract_template)
        template_payload = {
            row.id: {
                "id": row.id,
                "name": row.name,
                "legal_category": row.legal_category or "",
                "required_fields": load_required_fields(row.required_fields_json),
                "contract_templates": [
                    {
                        "id": int(contract_template.id),
                        "name": contract_template.name or "",
                        "required_fields": load_required_fields(contract_template.required_fields_json),
                    }
                    for contract_template in contract_templates_by_archetype.get(int(row.id), [])
                ],
            }
            for row in templates
        }
        return page(
            "Matter Intake",
            "matters_plus/intake.html",
            templates=templates,
            template_payload=template_payload,
            legal_categories=legal_category_options(),
            practice_areas=practice_area_options(),
            risk_levels=list(RISK_LEVELS),
            budget_statuses=list(BUDGET_STATUSES),
        )

    @app.get("/matters/<int:matter_id>/workspace")
    @login_required
    def matter_workspace(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        can_view_dms = has_permission("dms", "read")
        accessible_note_count = len(
            filter_accessible_matter_notes(
                MatterNote.query.filter_by(matter_id=matter_id).all()
            )
        )
        stats = {
            "open_tasks": Task.query.filter(Task.matter_id == matter_id, Task.status != "Done").count(),
            "deadlines": Deadline.query.filter_by(matter_id=matter_id).count(),
            "documents": DocumentRecord.query.filter_by(matter_id=matter_id).count() if can_view_dms else 0,
            "notes": accessible_note_count,
            "workspace_documents": MatterWorkspaceDocument.query.filter_by(matter_id=matter_id).count() if can_view_dms else 0,
        }

        stage_history = (
            MatterStageHistory.query.filter_by(matter_id=matter_id)
            .order_by(MatterStageHistory.changed_at.desc())
            .limit(20)
            .all()
        )
        checklist = MatterClosingChecklistItem.query.filter_by(matter_id=matter_id).order_by(MatterClosingChecklistItem.id.asc()).all()
        archetype = db.session.get(MatterTemplate, m.archetype_id) if m.archetype_id else None
        archetype_compliance = build_archetype_compliance_snapshot(m, archetype)
        open_tasks = (
            Task.query.filter(Task.matter_id == matter_id, Task.status != "Done")
            .order_by(Task.due_date.asc(), Task.id.asc())
            .limit(40)
            .all()
        )
        recent_docs = []
        if can_view_dms:
            recent_docs = filter_accessible_document_files(
                DocumentFile.query.filter_by(matter_id=matter_id)
                .order_by(DocumentFile.uploaded_at.desc(), DocumentFile.id.desc())
                .limit(12)
                .all()
            )
        upcoming_timeline = (
            MatterTimelineEvent.query.filter(
                MatterTimelineEvent.matter_id == matter_id,
                MatterTimelineEvent.event_date >= dt.date.today(),
            )
            .order_by(MatterTimelineEvent.event_date.asc(), MatterTimelineEvent.id.asc())
            .limit(12)
            .all()
        )
        matter_magic = build_matter_magic_snapshot(
            m,
            today=dt.date.today(),
            tasks=open_tasks,
            docs=recent_docs,
            timeline=upcoming_timeline,
            team_size=MatterMember.query.filter_by(matter_id=matter_id).count(),
            notes_count=stats["notes"],
            checklist_remaining=sum(1 for item in checklist if not item.is_done),
            archetype_compliance=archetype_compliance,
        ) or {}
        matter_magic["actions"] = attach_matter_magic_links(list(matter_magic.get("actions", [])), m.id)
        matter_launch_pack = build_matter_launch_pack(m, snapshot=matter_magic, today=dt.date.today()) or {}
        recent_workspace_documents = []
        if can_view_dms:
            recent_workspace_documents = (
                MatterWorkspaceDocument.query.filter_by(matter_id=matter_id)
                .order_by(MatterWorkspaceDocument.updated_at.desc(), MatterWorkspaceDocument.id.desc())
                .limit(6)
                .all()
            )

        return page(
            f"Matter Workspace {m.matter_no}",
            "matters_plus/workspace.html",
            m=m,
            stats=stats,
            stage_history=stage_history,
            checklist=checklist,
            archetype=archetype,
            archetype_compliance=archetype_compliance,
            matter_magic=matter_magic,
            matter_launch_pack=matter_launch_pack,
            recent_workspace_documents=recent_workspace_documents,
        )

    @app.route("/matters/<int:matter_id>/documents/workbench", methods=["GET", "POST"])
    @login_required
    def matter_document_workbench(matter_id: int):
        enforce_permission("dms", "read")
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        option_lists = _safe_load_dms_option_lists()
        document_type_options = list(option_lists.get("document_types") or [])
        confidentiality_options = list(option_lists.get("confidentialities") or [])
        privilege_label_options = list(option_lists.get("privilege_labels") or [])
        retention_category_options = list(option_lists.get("retention_categories") or [])
        default_document_type = document_type_options[0] if document_type_options else "General"
        default_confidentiality = confidentiality_options[0] if confidentiality_options else "Internal"
        json_payload = request.get_json(silent=True) if request.is_json else {}
        wants_json = request.is_json or "application/json" in str(request.headers.get("Accept") or "").lower()

        def _value(name: str, default=None, *, as_int: bool = False):
            raw = (
                json_payload.get(name, default)
                if isinstance(json_payload, dict) and name in json_payload
                else request.form.get(name, default)
            )
            if as_int:
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    return None
            return raw

        def _redirect(document_id: int | None = None, anchor: str | None = None):
            target = url_for("matter_document_workbench", matter_id=m.id, document_id=document_id) if document_id else url_for(
                "matter_document_workbench",
                matter_id=m.id,
            )
            if anchor:
                target = f"{target}#{anchor.lstrip('#')}"
            return redirect(target)

        if request.method == "POST":
            enforce_permission("dms", "write")
            action = str(_value("action", "save_document") or "").strip().lower()
            if action == "create_document":
                title = normalize_query(_value("title", ""))
                if not title:
                    if wants_json:
                        return jsonify({"ok": False, "error": "Document title is required."}), 400
                    flash("Document title is required.", "warning")
                    return _redirect()
                template_id = _value("template_id", as_int=True)
                template = db.session.get(DocumentTemplate, template_id) if template_id else None
                try:
                    document_type = _coerce_option_value(
                        _value("document_type"),
                        document_type_options,
                        field_label="Document type",
                        default_value=default_document_type,
                    )
                    confidentiality = _coerce_option_value(
                        _value("confidentiality"),
                        confidentiality_options,
                        field_label="Confidentiality",
                        default_value=default_confidentiality,
                    )
                    privilege_label = _coerce_option_value(
                        _value("privilege_label"),
                        privilege_label_options,
                        field_label="Privilege label",
                        allow_blank=True,
                    )
                    retention_category = _coerce_option_value(
                        _value("retention_category"),
                        retention_category_options,
                        field_label="Retention category",
                        allow_blank=True,
                    )
                except ValueError as exc:
                    if wants_json:
                        return jsonify({"ok": False, "error": str(exc)}), 400
                    flash(str(exc), "warning")
                    return _redirect()

                context = _workspace_template_context(m)
                body = _default_workspace_body(m)
                missing_tokens: list[str] = []
                if template is not None:
                    body, missing_tokens = _render_workspace_template(template.body, context)
                    if not body.strip():
                        body = _default_workspace_body(m)
                row = MatterWorkspaceDocument(
                    matter_id=m.id,
                    title=title,
                    body=body,
                    status=_normalize_workspace_status(_value("status", "draft")),
                    template_id=template.id if template is not None else None,
                    document_type=document_type,
                    confidentiality=confidentiality,
                    privilege_label=privilege_label,
                    retention_category=retention_category,
                    legal_hold=str(_value("legal_hold", "")).lower() in {"1", "true", "yes", "on"},
                    created_by=current_user.id,
                    last_edited_by=current_user.id,
                    updated_at=utc_now(),
                )
                db.session.add(row)
                db.session.commit()
                audit(
                    "matter_workspace_document_create",
                    "MatterWorkspaceDocument",
                    row.id,
                    {"matter_id": m.id, "template_id": row.template_id, "document_type": row.document_type},
                )
                matter_activity(m.id, "Collaborative draft created", row.title)
                if wants_json:
                    return jsonify(
                        {
                            "ok": True,
                            "document_id": row.id,
                            "redirect": url_for("matter_document_workbench", matter_id=m.id, document_id=row.id),
                            "missing_tokens": missing_tokens,
                        }
                    )
                if missing_tokens:
                    flash("Draft created, but some merge fields were blank: " + ", ".join(missing_tokens[:6]), "warning")
                else:
                    flash("Collaborative draft created.", "info")
                return _redirect(row.id)

            workspace_document_id = _value("document_id", as_int=True)
            workspace_document = db.session.get(MatterWorkspaceDocument, workspace_document_id) if workspace_document_id else None
            if workspace_document is None or int(workspace_document.matter_id) != int(m.id):
                if wants_json:
                    return jsonify({"ok": False, "error": "Document not found."}), 404
                abort(404)

            if action == "save_document":
                title = normalize_query(_value("title", workspace_document.title))
                body = str(_value("body", workspace_document.body) or "")
                autosave = str(_value("autosave", "")).lower() in {"1", "true", "yes", "on"}
                if not title:
                    if wants_json:
                        return jsonify({"ok": False, "error": "Document title is required."}), 400
                    flash("Document title is required.", "warning")
                    return _redirect(workspace_document.id)
                try:
                    workspace_document.document_type = _coerce_option_value(
                        _value("document_type", workspace_document.document_type),
                        document_type_options,
                        field_label="Document type",
                        default_value=default_document_type,
                    )
                    workspace_document.confidentiality = _coerce_option_value(
                        _value("confidentiality", workspace_document.confidentiality),
                        confidentiality_options,
                        field_label="Confidentiality",
                        default_value=default_confidentiality,
                    )
                    workspace_document.privilege_label = _coerce_option_value(
                        _value("privilege_label", workspace_document.privilege_label),
                        privilege_label_options,
                        field_label="Privilege label",
                        allow_blank=True,
                    )
                    workspace_document.retention_category = _coerce_option_value(
                        _value("retention_category", workspace_document.retention_category),
                        retention_category_options,
                        field_label="Retention category",
                        allow_blank=True,
                    )
                except ValueError as exc:
                    if wants_json:
                        return jsonify({"ok": False, "error": str(exc)}), 400
                    flash(str(exc), "warning")
                    return _redirect(workspace_document.id)

                workspace_document.title = title
                workspace_document.body = body
                workspace_document.status = _normalize_workspace_status(_value("status", workspace_document.status))
                workspace_document.legal_hold = str(_value("legal_hold", "")).lower() in {"1", "true", "yes", "on"}
                workspace_document.last_edited_by = current_user.id
                workspace_document.updated_at = utc_now()
                _upsert_workspace_presence(
                    workspace_document_id=workspace_document.id,
                    user_id=current_user.id,
                    state="editing",
                    cursor_label=str(_value("cursor_label", "") or ""),
                )
                db.session.commit()
                if not autosave:
                    audit(
                        "matter_workspace_document_save",
                        "MatterWorkspaceDocument",
                        workspace_document.id,
                        {"matter_id": m.id, "status": workspace_document.status},
                    )
                    matter_activity(m.id, "Collaborative draft updated", workspace_document.title)
                if wants_json:
                    return jsonify(
                        {
                            "ok": True,
                            "updated_at": workspace_document.updated_at.isoformat(),
                            "presence": _active_workspace_presence_snapshot(workspace_document.id),
                        }
                    )
                flash("Collaborative draft saved.", "info")
                return _redirect(workspace_document.id)

            if action == "add_comment":
                body = str(_value("comment_body", "") or "").strip()
                if not body:
                    flash("Comment text is required.", "warning")
                    return _redirect(workspace_document.id, "workspace-comments")
                db.session.add(
                    MatterWorkspaceDocumentComment(
                        workspace_document_id=workspace_document.id,
                        anchor_label=str(_value("anchor_label", "") or "").strip()[:120] or None,
                        body=body,
                        created_by=current_user.id,
                    )
                )
                _upsert_workspace_presence(
                    workspace_document_id=workspace_document.id,
                    user_id=current_user.id,
                    state="reviewing",
                )
                db.session.commit()
                audit(
                    "matter_workspace_document_comment_create",
                    "MatterWorkspaceDocument",
                    workspace_document.id,
                    {"matter_id": m.id},
                )
                matter_activity(m.id, "Collaborative draft comment added", workspace_document.title)
                flash("Comment added.", "info")
                return _redirect(workspace_document.id, "workspace-comments")

            if action == "resolve_comment":
                comment_id = _value("comment_id", as_int=True)
                comment = db.session.get(MatterWorkspaceDocumentComment, comment_id) if comment_id else None
                if comment is None or int(comment.workspace_document_id) != int(workspace_document.id):
                    abort(404)
                should_resolve = str(_value("resolved", "1")).lower() not in {"0", "false", "no", "off"}
                comment.is_resolved = should_resolve
                comment.resolved_at = utc_now() if should_resolve else None
                comment.resolved_by = current_user.id if should_resolve else None
                db.session.commit()
                flash("Comment resolved." if should_resolve else "Comment reopened.", "info")
                return _redirect(workspace_document.id, "workspace-comments")

            if action == "publish_document":
                if not (workspace_document.body or "").strip():
                    flash("Add draft content before publishing to DMS.", "warning")
                    return _redirect(workspace_document.id)
                published_path = ""
                try:
                    container, version, published_path = _publish_workspace_document_snapshot(
                        matter=m,
                        workspace_document=workspace_document,
                    )
                    db.session.commit()
                except ValueError as exc:
                    db.session.rollback()
                    flash(str(exc), "warning")
                    return _redirect(workspace_document.id)
                except Exception:
                    db.session.rollback()
                    _safe_remove_file(published_path)
                    current_app.logger.exception(
                        "Failed to publish collaborative draft workspace_document_id=%s matter_id=%s",
                        workspace_document.id,
                        m.id,
                    )
                    flash("DMS publish failed. Please retry.", "warning")
                    return _redirect(workspace_document.id)
                NotificationEngine.enqueue("document_generated", current_user.id, f"document_version:{version.id}")
                audit(
                    "matter_workspace_document_publish",
                    "MatterWorkspaceDocument",
                    workspace_document.id,
                    {"matter_id": m.id, "document_record_id": container.id, "document_version_id": version.id},
                )
                matter_activity(m.id, "Collaborative draft published", workspace_document.title)
                flash("Snapshot published to DMS.", "info")
                return _redirect(workspace_document.id)

            if wants_json:
                return jsonify({"ok": False, "error": "Unsupported workbench action."}), 400
            flash("Unsupported workbench action.", "warning")
            return _redirect(workspace_document.id)

        requested_document_id = request.args.get("document_id", type=int)
        workspace_documents = (
            MatterWorkspaceDocument.query.filter_by(matter_id=m.id)
            .order_by(MatterWorkspaceDocument.updated_at.desc(), MatterWorkspaceDocument.id.desc())
            .all()
        )
        selected_document = None
        if requested_document_id:
            selected_document = next(
                (row for row in workspace_documents if int(row.id) == int(requested_document_id)),
                None,
            )
        if selected_document is None and workspace_documents:
            selected_document = workspace_documents[0]

        comments: list[MatterWorkspaceDocumentComment] = []
        presence_snapshot: list[dict[str, object]] = []
        published_record = None
        if selected_document is not None:
            comments = (
                MatterWorkspaceDocumentComment.query.filter_by(workspace_document_id=selected_document.id)
                .order_by(
                    MatterWorkspaceDocumentComment.is_resolved.asc(),
                    MatterWorkspaceDocumentComment.created_at.desc(),
                )
                .all()
            )
            presence_snapshot = _active_workspace_presence_snapshot(selected_document.id)
            published_record = (
                db.session.get(DocumentRecord, selected_document.published_document_id)
                if selected_document.published_document_id
                else None
            )

        page_stats = {
            "drafts": len(workspace_documents),
            "active_collaborators": len(presence_snapshot),
            "open_comments": sum(1 for row in comments if not row.is_resolved),
            "published": sum(1 for row in workspace_documents if row.published_document_id),
        }

        return page(
            f"Document Workbench {m.matter_no}",
            "matters_plus/document_workbench.html",
            m=m,
            workspace_documents=workspace_documents,
            selected_document=selected_document,
            comments=comments,
            presence_snapshot=presence_snapshot,
            page_stats=page_stats,
            published_record=published_record,
            dms_document_types=document_type_options,
            dms_confidentialities=confidentiality_options,
            dms_privilege_labels=privilege_label_options,
            dms_retention_categories=retention_category_options,
            default_document_type=default_document_type,
            default_confidentiality=default_confidentiality,
            doc_templates=DocumentTemplate.query.order_by(DocumentTemplate.name.asc()).limit(300).all(),
        )

    @app.post("/matters/<int:matter_id>/documents/workbench/presence")
    @login_required
    def matter_document_workbench_presence(matter_id: int):
        enforce_permission("dms", "read")
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)
        payload = request.get_json(silent=True) or {}
        workspace_document_id = request.form.get("document_id", type=int)
        if not workspace_document_id:
            try:
                workspace_document_id = int(payload.get("document_id"))
            except (TypeError, ValueError):
                workspace_document_id = None
        workspace_document = db.session.get(MatterWorkspaceDocument, workspace_document_id) if workspace_document_id else None
        if workspace_document is None or int(workspace_document.matter_id) != int(m.id):
            return jsonify({"ok": False, "error": "Document not found."}), 404
        _upsert_workspace_presence(
            workspace_document_id=workspace_document.id,
            user_id=current_user.id,
            state=str(payload.get("state") or request.form.get("state") or "viewing"),
            cursor_label=str(payload.get("cursor_label") or request.form.get("cursor_label") or ""),
        )
        db.session.commit()
        return jsonify({"ok": True, "presence": _active_workspace_presence_snapshot(workspace_document.id)})

    @app.post("/matters/<int:matter_id>/archetype/sync-checklist")
    @login_required
    def matter_archetype_sync_checklist(matter_id: int):
        enforce_permission("matter_team", "manage")
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)
        archetype = db.session.get(MatterTemplate, m.archetype_id) if m.archetype_id else None
        if archetype is None:
            flash("No archetype is linked to this matter.", "warning")
            return redirect(url_for("matter_workspace", matter_id=m.id))

        seeded = ensure_matter_closing_checklist_items(m.id, archetype)
        db.session.commit()
        audit(
            "matter_archetype_sync_checklist",
            "Matter",
            m.id,
            {"archetype_id": m.archetype_id, "seeded_count": seeded},
        )
        matter_activity(m.id, "Archetype checklist sync", f"{seeded} checklist item(s) added")
        if seeded > 0:
            flash(f"Archetype checklist synced: {seeded} item(s) added.", "info")
        else:
            flash("Archetype checklist already in sync.", "info")
        return redirect(url_for("matter_workspace", matter_id=m.id))

    @app.route("/matters/<int:matter_id>/parties", methods=["GET", "POST"])
    @login_required
    def matter_parties(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        if request.method == "POST":
            enforce_case_team_role()
            entity_name = normalize_query(request.form.get("entity_name", ""))
            party_role = normalize_query(request.form.get("party_role", "Client")) or "Client"
            if not entity_name:
                flash("Entity name is required.", "warning")
                return redirect(url_for("matter_parties", matter_id=matter_id))

            entity = Entity.query.filter_by(name=entity_name).first()
            if entity is None:
                entity = Entity(
                    name=entity_name,
                    entity_type=(request.form.get("entity_type") or "organization").strip() or "organization",
                    email=(request.form.get("email") or "").strip() or None,
                    phone=(request.form.get("phone") or "").strip() or None,
                )
                db.session.add(entity)
                db.session.flush()

            db.session.add(
                MatterParty(
                    matter_id=matter_id,
                    entity_id=entity.id,
                    party_role=party_role,
                    is_primary=(request.form.get("is_primary") or "").lower() in {"1", "true", "yes", "on"},
                )
            )
            db.session.commit()
            audit("matter_party_add", "Matter", matter_id, {"entity_id": entity.id, "role": party_role})
            matter_activity(matter_id, "Party linked", f"{entity.name} ({party_role})")
            flash("Party linked to matter.", "info")
            return redirect(url_for("matter_parties", matter_id=matter_id))

        parties = (
            db.session.query(MatterParty, Entity)
            .join(Entity, Entity.id == MatterParty.entity_id)
            .filter(MatterParty.matter_id == matter_id)
            .order_by(MatterParty.id.desc())
            .all()
        )
        return page("Matter Parties", "matters_plus/parties.html", m=m, parties=parties)

    @app.route("/matters/<int:matter_id>/notes", methods=["GET", "POST"])
    @login_required
    def matter_notes(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        if request.method == "POST":
            enforce_case_team_role()
            body = (request.form.get("body") or "").strip()
            voice_file = request.files.get("voice_note")
            has_voice_note = bool(voice_file and (voice_file.filename or "").strip())
            if not body and not has_voice_note:
                flash("Add note text or upload a voice note.", "warning")
                return redirect(url_for("matter_notes", matter_id=matter_id))
            if has_voice_note and not allowed_audio(voice_file.filename or ""):
                flash("Voice note must be one of: .m4a, .mp3, .wav, .ogg, .webm.", "warning")
                return redirect(url_for("matter_notes", matter_id=matter_id))
            note = MatterNote(
                matter_id=matter_id,
                body=body or "Voice note captured.",
                tags=(request.form.get("tags") or "").strip() or None,
                privilege_label=(request.form.get("privilege_label") or "").strip() or None,
                created_by=current_user.id,
            )
            db.session.add(note)
            db.session.flush()

            acl_emails = [x.strip().lower() for x in (request.form.get("acl_emails") or "").split(",") if x.strip()]
            for email in acl_emails:
                user = User.query.filter_by(email=email).first()
                if user:
                    db.session.add(MatterNoteACL(note_id=note.id, user_id=user.id, can_read=True, can_edit=False))

            if has_voice_note and voice_file:
                safe_name = secure_filename(voice_file.filename or "")
                if not safe_name:
                    flash("Invalid voice note filename.", "warning")
                    db.session.rollback()
                    return redirect(url_for("matter_notes", matter_id=matter_id))
                ext = safe_name.rsplit(".", 1)[-1].lower()
                stored_name = f"matter{matter_id}_note{note.id}_{utc_now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}.{ext}"
                try:
                    stored_name, path = resolve_upload_path(
                        current_app.config["UPLOAD_DIR"],
                        stored_name,
                        create_parent=True,
                    )
                except ValueError:
                    flash("Storage path validation failed for voice note.", "warning")
                    db.session.rollback()
                    return redirect(url_for("matter_notes", matter_id=matter_id))
                voice_file.save(path)
                harden_private_file(path)
                db.session.add(
                    DocumentFile(
                        matter_id=matter_id,
                        original_filename=safe_name,
                        stored_filename=stored_name,
                        sha256=sha256_file(path),
                        content_type=(voice_file.mimetype or "").strip() or None,
                        category="Voice Note",
                        doc_version="v1",
                        lifecycle_stage="Recorded",
                        owner_name=f"note:{note.id}",
                        is_privileged=bool(note.privilege_label),
                        uploaded_by=current_user.id,
                    )
                )

            db.session.commit()
            audit("matter_note_create", "MatterNote", note.id, {"matter_id": matter_id})
            if has_voice_note:
                audit("matter_voice_note_upload", "MatterNote", note.id, {"matter_id": matter_id})
            matter_activity(matter_id, "Matter note added")
            flash("Note added.", "info")
            return redirect(url_for("matter_notes", matter_id=matter_id))

        notes = MatterNote.query.filter_by(matter_id=matter_id).order_by(MatterNote.created_at.desc()).limit(200).all()
        notes = filter_accessible_matter_notes(notes)

        voice_notes_by_note_id: dict[int, list[DocumentFile]] = defaultdict(list)
        note_ids = [note.id for note in notes]
        if note_ids:
            owner_tokens = [f"note:{note_id}" for note_id in note_ids]
            voice_rows = (
                DocumentFile.query.filter(
                    DocumentFile.matter_id == matter_id,
                    DocumentFile.category == "Voice Note",
                    DocumentFile.owner_name.in_(owner_tokens),
                )
                .order_by(DocumentFile.uploaded_at.desc())
                .all()
            )
            for row in voice_rows:
                token = (row.owner_name or "").strip().lower()
                if not token.startswith("note:"):
                    continue
                try:
                    note_id = int(token.split(":", 1)[1])
                except ValueError:
                    continue
                voice_notes_by_note_id[note_id].append(row)

        team_user_ids = {current_user.id, m.created_by}
        member_ids = [int(user_id) for (user_id,) in db.session.query(MatterMember.user_id).filter_by(matter_id=matter_id).all()]
        team_user_ids.update(member_ids)
        team_users = User.query.filter(User.id.in_(team_user_ids)).order_by(User.full_name.asc()).all() if team_user_ids else []

        return page(
            "Matter Notes",
            "matters_plus/notes.html",
            m=m,
            notes=notes,
            voice_notes_by_note_id=voice_notes_by_note_id,
            team_users=team_users,
        )

    @app.post("/matters/<int:matter_id>/stage")
    @login_required
    def matter_stage_update(matter_id: int):
        enforce_permission("matter_team", "manage")
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        next_stage = normalize_query(request.form.get("stage", ""))
        reason = (request.form.get("reason") or "").strip() or None
        if not next_stage:
            flash("Stage is required.", "warning")
            return redirect(url_for("matter_workspace", matter_id=matter_id))

        prev = m.stage
        m.stage = next_stage
        m.last_updated_at = utc_now()
        db.session.add(
            MatterStageHistory(
                matter_id=m.id,
                from_stage=prev,
                to_stage=next_stage,
                reason=reason,
                changed_by=current_user.id,
            )
        )
        db.session.commit()
        NotificationEngine.enqueue("matter_stage_changed", current_user.id, f"matter:{m.id}:stage:{next_stage}")
        audit("matter_stage_update", "Matter", matter_id, {"from": prev, "to": next_stage})
        matter_activity(m.id, "Matter stage updated", f"{prev or 'None'} -> {next_stage}")
        flash("Matter stage updated.", "info")
        return redirect(url_for("matter_workspace", matter_id=matter_id))

    @app.post("/matters/<int:matter_id>/close")
    @login_required
    def matter_close(matter_id: int):
        enforce_permission("matter_team", "manage")
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        archetype = db.session.get(MatterTemplate, m.archetype_id) if m.archetype_id else None
        ensure_matter_closing_checklist_items(m.id, archetype)

        for raw in request.form.getlist("checklist_done"):
            try:
                item_id = int(str(raw).strip())
            except (TypeError, ValueError):
                continue
            item = db.session.get(MatterClosingChecklistItem, item_id)
            if item and item.matter_id == m.id:
                item.is_done = True
                item.done_at = utc_now()
                item.done_by = current_user.id

        incomplete = MatterClosingChecklistItem.query.filter_by(matter_id=m.id, is_done=False).count()
        compliance = build_archetype_compliance_snapshot(m, archetype)
        missing_labels = list(compliance.get("required_missing_labels") or [])
        if incomplete > 0 or missing_labels:
            if incomplete > 0:
                flash(f"{incomplete} checklist item(s) remain before close.", "warning")
            if missing_labels:
                flash(
                    "Required archetype fields still missing: " + ", ".join(missing_labels[:6]) + ".",
                    "warning",
                )
            db.session.commit()
            return redirect(url_for("matter_workspace", matter_id=m.id))

        m.status = "Closed"
        m.closed_at = utc_now()
        auto_pause_summary = auto_pause_running_timers_for_matter(
            m.id,
            actor_user_id=current_user.id,
            pause_reason="matter_closed",
        )
        open_task_count = Task.query.filter(Task.matter_id == m.id, Task.status != "Done").count()
        closure_followup_task_id = None
        if open_task_count > 0:
            followup_title = f"Post-close task review for {m.matter_no}"
            existing_followup = (
                Task.query.filter_by(matter_id=m.id, title=followup_title)
                .order_by(Task.id.desc())
                .first()
            )
            if existing_followup is not None:
                closure_followup_task_id = int(existing_followup.id)
            else:
                assignee_id = current_user.id
                lead_member = (
                    MatterMember.query.filter_by(matter_id=m.id)
                    .order_by(MatterMember.id.asc())
                    .first()
                )
                if lead_member is not None:
                    assignee_id = int(lead_member.user_id)
                followup_task = Task(
                    matter_id=m.id,
                    title=followup_title,
                    description=(
                        f"Review {open_task_count} open task(s) after matter closure and document final outcomes."
                    ),
                    status="Todo",
                    due_date=dt.date.today() + dt.timedelta(days=1),
                    assigned_to=assignee_id,
                    created_by=current_user.id,
                    priority="High",
                )
                db.session.add(followup_task)
                db.session.flush()
                db.session.add(
                    TaskAssignee(
                        task_id=followup_task.id,
                        user_id=assignee_id,
                        assigned_by=current_user.id,
                    )
                )
                closure_followup_task_id = int(followup_task.id)
        if has_active_legal_hold(m.id):
            m.archival_status = "legal_hold_blocked"
            m.archival_due_at = None
            db.session.commit()
            audit("matter_close_legal_hold_blocked", "Matter", m.id)
            matter_activity(m.id, "Matter closed with legal hold", "Archival blocked by active legal hold")
            if auto_pause_summary.get("paused", 0) > 0:
                flash(
                    (
                        f"Auto-paused {auto_pause_summary.get('paused', 0)} running timer(s) and captured "
                        f"{auto_pause_summary.get('captured_entries', 0)} draft entry(ies)."
                    ),
                    "info",
                )
            if closure_followup_task_id is not None:
                flash(f"Created follow-up task #{closure_followup_task_id} to resolve open work.", "warning")
            flash("Matter closed. Archival blocked because an active legal hold exists.", "warning")
            return redirect(url_for("matter_workspace", matter_id=m.id))

        m.archival_status = "archive_pending"
        m.archival_due_at = utc_now() + dt.timedelta(days=30)
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            if has_active_legal_hold(m.id):
                m = db.session.get(Matter, m.id)
                if m is None:
                    abort(404)
                m.archival_status = "legal_hold_blocked"
                m.archival_due_at = None
                db.session.commit()
                audit("matter_close_legal_hold_blocked", "Matter", m.id)
                matter_activity(m.id, "Matter closed with legal hold", "Archival blocked by active legal hold")
                flash("Matter closed. Archival blocked because an active legal hold exists.", "warning")
                return redirect(url_for("matter_workspace", matter_id=m.id))
            raise
        audit("matter_close", "Matter", m.id)
        matter_activity(m.id, "Matter closed")
        if auto_pause_summary.get("paused", 0) > 0:
            flash(
                (
                    f"Auto-paused {auto_pause_summary.get('paused', 0)} running timer(s) and captured "
                    f"{auto_pause_summary.get('captured_entries', 0)} draft entry(ies)."
                ),
                "info",
            )
        if closure_followup_task_id is not None:
            flash(f"Created follow-up task #{closure_followup_task_id} to resolve open work.", "warning")
        flash("Matter closed and archival workflow started.", "info")
        return redirect(url_for("matter_workspace", matter_id=m.id))
