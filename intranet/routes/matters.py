from __future__ import annotations

import datetime as dt
from ..timeutils import utc_now
import json
import os
import time
from urllib.parse import urlsplit

import sqlalchemy as sa
from flask import Response, abort, current_app, flash, jsonify, redirect, request, send_from_directory, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.utils import secure_filename

from ..config import ALLOWED_DOC_EXT, BUDGET_STATUSES, MATTER_STATUSES, RISK_LEVELS, is_valid_email
from ..extensions import db
from ..helpers import (
    allowed_doc,
    audit,
    can_access_matter,
    is_admin,
    matter_activity,
    normalize_query,
    sha256_file,
)
from ..models import (
    ContractTemplate,
    DocumentFile,
    DocumentTemplate,
    Matter,
    MatterActivity,
    MatterMember,
    MatterNote,
    MatterPin,
    MatterRecentView,
    MatterTemplate,
    MatterTimelineEvent,
    Task,
    TaskAssignee,
    TaskTemplate,
    TaskTemplateItem,
    User,
)
from ..policies import enforce_data_residency, enforce_permission, visible_matter_ids
from ..roles import role_is_director, role_query_values_for_legal_team
from ..services.archetypes import (
    build_document_context,
    collect_required_field_values,
    humanize_required_field_label,
    load_required_fields,
    parse_matter_archetype_values,
    render_template_text,
    validate_required_field_values,
)
from ..services.archetype_playbook import (
    build_archetype_compliance_snapshot,
    ensure_matter_closing_checklist_items,
)
from ..services.contracts import (
    cleanup_generated_files,
    collect_contract_field_values,
    contract_required_fields_union,
    persist_generated_document_template_document,
    persist_generated_contract_document,
    render_contract_template_for_matter,
    validate_contract_field_values,
)
from ..services.assist_ai import suggest_matter_client_update, suggest_matter_executive_summary
from ..services.director_team import director_team_member_ids, user_in_director_scope
from ..services.matter_magic import attach_matter_magic_links, build_matter_magic_snapshot, build_task_tracker_snapshot
from ..services.matter_option_lists import legal_category_options
from ..services.storage_paths import build_matter_storage_name, harden_private_file, resolve_upload_path
from ..services.workflow_automation import auto_pause_running_timers_for_matter
from ..templates import page

TIMELINE_EVENT_TYPES = {"Milestone", "Filing", "Hearing", "Client Update", "Internal Review", "Delivery"}
DOC_CATEGORIES = {"Pleading", "Evidence", "Contract", "Advisory", "Correspondence", "Court Filing", "General"}
DOC_LIFECYCLE_STAGES = {"Draft", "For Review", "Final", "Executed"}
CUSTOM_ARCHETYPE_SENTINEL = "custom"
TASK_QUEUE_OPTIONS = (
    ("all", "All matters"),
    ("unassigned", "Unassigned work"),
    ("urgent-unassigned", "Urgent unassigned"),
)
TASK_QUEUE_VALUES = {value for value, _ in TASK_QUEUE_OPTIONS}


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


def _safe_next_path(next_path: str | None, fallback: str) -> str:
    if not next_path:
        return fallback
    parsed = urlsplit(next_path)
    if parsed.scheme or parsed.netloc:
        return fallback
    if not parsed.path.startswith("/"):
        return fallback
    return next_path


def _matter_has_unassigned_tasks(*, urgent_only: bool, today: dt.date) -> sa.sql.elements.Exists:
    has_any_assignee = db.session.query(TaskAssignee.id).filter(TaskAssignee.task_id == Task.id).exists()
    filters: list[object] = [
        Task.matter_id == Matter.id,
        Task.status != "Done",
        Task.assigned_to.is_(None),
        ~has_any_assignee,
    ]
    if urgent_only:
        filters.extend(
            [
                Task.due_date.isnot(None),
                Task.due_date <= (today + dt.timedelta(days=3)),
            ]
        )
    return db.session.query(Task.id).filter(*filters).exists()


def _record_recent_matter_view(user_id: int, matter_id: int) -> None:
    now = utc_now()
    row = MatterRecentView.query.filter_by(user_id=user_id, matter_id=matter_id).first()
    if row is None:
        db.session.add(
            MatterRecentView(
                user_id=user_id,
                matter_id=matter_id,
                first_viewed_at=now,
                last_viewed_at=now,
                view_count=1,
            )
        )
        db.session.commit()
        return

    if row.last_viewed_at and (now - row.last_viewed_at).total_seconds() < 60:
        return

    row.last_viewed_at = now
    row.view_count = int(row.view_count or 0) + 1
    db.session.commit()


def _archetype_templates() -> list[MatterTemplate]:
    return (
        MatterTemplate.query.order_by(
            MatterTemplate.legal_category.asc().nullslast(),
            MatterTemplate.name.asc(),
        )
        .limit(500)
        .all()
    )


def _serialize_template_payload(
    templates: list[MatterTemplate],
    contract_templates_by_archetype: dict[int, list[ContractTemplate]] | None = None,
    document_templates_by_archetype: dict[int, list[DocumentTemplate]] | None = None,
) -> dict[int, dict[str, object]]:
    contract_templates_by_archetype = contract_templates_by_archetype or {}
    document_templates_by_archetype = document_templates_by_archetype or {}
    payload: dict[int, dict[str, object]] = {}
    for template in templates:
        linked_contract_templates = contract_templates_by_archetype.get(int(template.id), [])
        linked_document_templates = document_templates_by_archetype.get(int(template.id), [])
        payload[template.id] = {
            "id": int(template.id),
            "name": template.name or "",
            "legal_category": template.legal_category or "",
            "required_fields": load_required_fields(template.required_fields_json),
            "contract_templates": [
                {
                    "id": int(contract_template.id),
                    "name": contract_template.name or "",
                    "required_fields": load_required_fields(contract_template.required_fields_json),
                }
                for contract_template in linked_contract_templates
            ],
            "document_templates": [
                {
                    "id": int(document_template.id),
                    "name": document_template.name or "",
                    "template_type": document_template.template_type or "",
                }
                for document_template in linked_document_templates
            ],
        }
    return payload


def _generate_archetype_document(matter: Matter, template: MatterTemplate | None) -> tuple[str, list[str]]:
    if template is None:
        return "", []
    if not (template.boilerplate_template or "").strip():
        return "", []
    matter_specific_values = parse_matter_archetype_values(matter.archetype_data_json)
    context = build_document_context(matter, archetype=template, required_values=matter_specific_values)
    return render_template_text(template.boilerplate_template, context)


def _build_matter_ai_context(
    *,
    matter: Matter,
    tasks: list[Task],
    timeline: list[MatterTimelineEvent],
    docs: list[DocumentFile],
    notes: list[MatterNote],
    activity_items: list[MatterActivity] | None = None,
) -> dict[str, object]:
    today = dt.date.today()
    open_tasks = [task for task in tasks if (task.status or "").strip().lower() != "done"]
    overdue_tasks = [task for task in open_tasks if task.due_date and task.due_date < today]
    next_due_task_row = (
        sorted(
            [task for task in open_tasks if task.due_date],
            key=lambda task: (task.due_date, task.id),
        )[0]
        if open_tasks
        else None
    )

    recent_timeline = [
        {
            "date": row.event_date.isoformat() if row.event_date else "",
            "type": row.event_type or "",
            "title": (row.title or "")[:180],
        }
        for row in timeline[:8]
    ]
    recent_notes = [
        (row.body or "").strip().replace("\n", " ")[:260]
        for row in notes[:6]
        if (row.body or "").strip()
    ]
    recent_docs = [
        {
            "filename": (row.original_filename or "")[:180],
            "category": row.category or "",
            "uploaded_at": row.uploaded_at.isoformat() if row.uploaded_at else "",
        }
        for row in docs[:8]
    ]
    recent_activity = [
        {
            "action": (row.action or "")[:140],
            "details": (row.details or "")[:220],
            "created_at": row.created_at.isoformat() if row.created_at else "",
        }
        for row in (activity_items or [])[:8]
    ]

    next_due_task = ""
    if next_due_task_row is not None:
        due_text = next_due_task_row.due_date.isoformat() if next_due_task_row.due_date else ""
        next_due_task = f"{next_due_task_row.title or 'Task'} ({due_text})"

    return {
        "matter_id": int(matter.id),
        "matter_no": matter.matter_no or "",
        "title": matter.title or "",
        "client_name": matter.client_name or "",
        "status": matter.status or "",
        "risk_level": matter.risk_level or "Medium",
        "budget_status": matter.budget_status or "On Track",
        "legal_category": matter.legal_category or "",
        "objective": matter.objective or "",
        "last_update_note": matter.last_update_note or "",
        "outcome_summary": matter.outcome_summary or "",
        "open_task_count": len(open_tasks),
        "overdue_task_count": len(overdue_tasks),
        "next_due_task": next_due_task,
        "latest_timeline_title": recent_timeline[0]["title"] if recent_timeline else "",
        "recent_timeline": recent_timeline,
        "recent_notes": recent_notes,
        "recent_documents": recent_docs,
        "recent_activity": recent_activity,
    }


def register_matter_routes(app):
    @app.get("/matters")
    @login_required
    def matters():
        today = dt.date.today()
        q = normalize_query(request.args.get("q", ""))
        sort = normalize_query(request.args.get("sort", "opened_desc")).lower() or "opened_desc"
        task_queue = normalize_query(request.args.get("task_queue", "all")).lower() or "all"
        page_number = request.args.get("page", default=1, type=int) or 1
        if page_number < 1:
            page_number = 1
        if task_queue not in TASK_QUEUE_VALUES:
            task_queue = "all"
        risk_rank = sa.case(
            (Matter.risk_level == "Critical", 4),
            (Matter.risk_level == "High", 3),
            (Matter.risk_level == "Medium", 2),
            (Matter.risk_level == "Low", 1),
            else_=0,
        )
        sort_order = {
            "opened_desc": (Matter.opened_at.desc(), Matter.id.desc()),
            "opened_asc": (Matter.opened_at.asc(), Matter.id.asc()),
            "updated_desc": (Matter.last_updated_at.desc(), Matter.id.desc()),
            "updated_asc": (Matter.last_updated_at.asc(), Matter.id.asc()),
            "matter_no_asc": (Matter.matter_no.asc(), Matter.id.asc()),
            "matter_no_desc": (Matter.matter_no.desc(), Matter.id.desc()),
            "client_asc": (Matter.client_name.asc(), Matter.id.asc()),
            "client_desc": (Matter.client_name.desc(), Matter.id.desc()),
            "title_asc": (Matter.title.asc(), Matter.id.asc()),
            "title_desc": (Matter.title.desc(), Matter.id.desc()),
            "risk_desc": (risk_rank.desc(), Matter.opened_at.desc()),
            "risk_asc": (risk_rank.asc(), Matter.opened_at.desc()),
            "status_asc": (Matter.status.asc(), Matter.opened_at.desc()),
            "status_desc": (Matter.status.desc(), Matter.opened_at.desc()),
        }
        sort_options = (
            ("opened_desc", "Opened date (newest)"),
            ("opened_asc", "Opened date (oldest)"),
            ("updated_desc", "Last updated (newest)"),
            ("updated_asc", "Last updated (oldest)"),
            ("matter_no_asc", "Matter number (A-Z)"),
            ("matter_no_desc", "Matter number (Z-A)"),
            ("client_asc", "Client name (A-Z)"),
            ("client_desc", "Client name (Z-A)"),
            ("title_asc", "Title (A-Z)"),
            ("title_desc", "Title (Z-A)"),
            ("risk_desc", "Risk (Critical -> Low)"),
            ("risk_asc", "Risk (Low -> Critical)"),
            ("status_asc", "Status (A-Z)"),
            ("status_desc", "Status (Z-A)"),
        )
        if sort not in sort_order:
            sort = "opened_desc"
        base = Matter.query
        if not is_admin():
            ids = visible_matter_ids()
            if not ids:
                base = base.filter(Matter.id == -1)
            else:
                base = base.filter(Matter.id.in_(ids))
        if task_queue == "unassigned":
            base = base.filter(_matter_has_unassigned_tasks(urgent_only=False, today=today))
        elif task_queue == "urgent-unassigned":
            base = base.filter(_matter_has_unassigned_tasks(urgent_only=True, today=today))
        if q:
            like = f"%{q}%"
            base = base.filter((Matter.matter_no.ilike(like)) | (Matter.title.ilike(like)) | (Matter.client_name.ilike(like)))
        pagination = base.order_by(*sort_order[sort]).paginate(page=page_number, per_page=50, error_out=False)
        ms = pagination.items
        pinned_matter_ids: set[int] = set()
        matter_ids = [matter.id for matter in ms]
        if matter_ids:
            pinned_matter_ids = {
                int(row[0])
                for row in (
                    db.session.query(MatterPin.matter_id)
                    .filter(
                        MatterPin.user_id == current_user.id,
                        MatterPin.matter_id.in_(matter_ids),
                    )
                    .all()
                )
            }
        status_counts = {"Open": 0, "On Hold": 0, "Closed": 0}
        for status, count in base.with_entities(Matter.status, sa.func.count(Matter.id)).group_by(Matter.status).all():
            if status in status_counts:
                status_counts[status] = int(count)
        return page(
            "Matters",
            "matters/list.html",
            ms=ms,
            q=q,
            sort=sort,
            task_queue=task_queue,
            task_queue_options=TASK_QUEUE_OPTIONS,
            task_queue_label=dict(TASK_QUEUE_OPTIONS).get(task_queue, "All matters"),
            sort_options=sort_options,
            pagination=pagination,
            status_counts=status_counts,
            pinned_matter_ids=pinned_matter_ids,
        )

    @app.route("/matters/new", methods=["GET", "POST"])
    @login_required
    def matter_create():
        enforce_permission("matter", "create")
        try:
            archetype_templates = _archetype_templates()
            template_by_id = {row.id: row for row in archetype_templates}
            archetype_ids = [int(row.id) for row in archetype_templates]
            linked_contract_templates = (
                ContractTemplate.query.filter(
                    ContractTemplate.archetype_id.in_(archetype_ids),
                    ContractTemplate.is_active.is_(True),
                    ContractTemplate.auto_create_on_matter_open.is_(True),
                )
                .order_by(ContractTemplate.name.asc())
                .all()
                if archetype_ids
                else []
            )
            contract_templates_by_archetype: dict[int, list[ContractTemplate]] = {}
            for contract_template in linked_contract_templates:
                if not contract_template.archetype_id:
                    continue
                key = int(contract_template.archetype_id)
                contract_templates_by_archetype.setdefault(key, []).append(contract_template)
            linked_document_templates = (
                DocumentTemplate.query.filter(
                    DocumentTemplate.archetype_id.in_(archetype_ids),
                )
                .order_by(DocumentTemplate.name.asc())
                .all()
                if archetype_ids
                else []
            )
            document_templates_by_archetype: dict[int, list[DocumentTemplate]] = {}
            for document_template in linked_document_templates:
                if not document_template.archetype_id:
                    continue
                key = int(document_template.archetype_id)
                document_templates_by_archetype.setdefault(key, []).append(document_template)
            legal_team_role_values = tuple(sorted(role_query_values_for_legal_team()))
            assignable_lawyers = (
                User.query.filter(
                    User.is_active.is_(True),
                    sa.func.lower(User.role).in_(legal_team_role_values),
                )
                .order_by(User.full_name.asc(), User.email.asc())
                .all()
            )
            if role_is_director(getattr(current_user, "role", None)):
                scoped_ids = director_team_member_ids(int(current_user.id))
                assignable_lawyers = [user for user in assignable_lawyers if int(user.id) in scoped_ids]
            assignable_lawyer_ids = {int(user.id) for user in assignable_lawyers}
        except SQLAlchemyError:
            current_app.logger.exception("Matter create preload failed.")
            flash(
                (
                    "New Matter is unavailable because the database schema appears out of sync. "
                    "Run schema sync/migrations on the server, then retry."
                ),
                "warning",
            )
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            matter_no = normalize_query(request.form.get("matter_no", "")).upper()
            title = normalize_query(request.form.get("title", ""))
            client_name = normalize_query(request.form.get("client_name", ""))
            status = normalize_query(request.form.get("status", "Open")) or "Open"
            description = (request.form.get("description") or "").strip()
            objective = (request.form.get("objective") or "").strip()
            risk_level = normalize_query(request.form.get("risk_level", "Medium")) or "Medium"
            budget_status = normalize_query(request.form.get("budget_status", "On Track")) or "On Track"
            last_update_note = (request.form.get("last_update_note") or "").strip()
            legal_category = normalize_query(request.form.get("legal_category", ""))
            archetype_raw_value = str(request.form.get("archetype_id", "") or "").strip()
            is_custom_archetype = archetype_raw_value.lower() == CUSTOM_ARCHETYPE_SENTINEL
            archetype_id = request.form.get("archetype_id", type=int) if not is_custom_archetype else None
            archetype = (template_by_id.get(archetype_id) or db.session.get(MatterTemplate, archetype_id)) if archetype_id else None
            selected_lawyer_ids: list[int] = []
            for raw_user_id in request.form.getlist("lawyer_user_ids"):
                try:
                    user_id = int(str(raw_user_id).strip())
                except (TypeError, ValueError):
                    continue
                if user_id > 0 and user_id not in selected_lawyer_ids:
                    selected_lawyer_ids.append(user_id)

            if not matter_no or not title or not client_name:
                flash("Matter number, title, and client name are required.", "warning")
                return redirect(url_for("matter_create"))
            if not legal_category:
                flash("Legal category is required.", "warning")
                return redirect(url_for("matter_create"))
            if not is_custom_archetype and not archetype_templates:
                flash("Create at least one matter archetype in Admin before opening a matter.", "warning")
                return redirect(url_for("admin_templates_matters"))
            if not is_custom_archetype and archetype is None:
                flash("Select a valid archetype or choose Custom (No Archetype).", "warning")
                return redirect(url_for("matter_create"))
            if archetype is not None and archetype.legal_category and legal_category != normalize_query(archetype.legal_category):
                flash("Selected archetype does not belong to the selected legal category.", "warning")
                return redirect(url_for("matter_create"))

            if Matter.query.filter_by(matter_no=matter_no).first():
                flash("Matter number already exists.", "warning")
                return redirect(url_for("matter_create"))
            if status not in MATTER_STATUSES:
                flash("Invalid matter status.", "warning")
                return redirect(url_for("matter_create"))
            if risk_level not in RISK_LEVELS:
                flash("Invalid risk level.", "warning")
                return redirect(url_for("matter_create"))
            if budget_status not in BUDGET_STATUSES:
                flash("Invalid budget status.", "warning")
                return redirect(url_for("matter_create"))
            invalid_selected_lawyers = [user_id for user_id in selected_lawyer_ids if user_id not in assignable_lawyer_ids]
            if invalid_selected_lawyers:
                flash("One or more selected lawyers are invalid or inactive.", "warning")
                return redirect(url_for("matter_create"))

            required_field_defs = load_required_fields(archetype.required_fields_json if archetype else None)
            matter_specific_values = collect_required_field_values(request.form, required_field_defs)
            missing_required_fields = validate_required_field_values(required_field_defs, matter_specific_values)
            if missing_required_fields:
                preview = ", ".join(_display_missing_field_labels(missing_required_fields)[:5])
                archetype_name = archetype.name if archetype is not None else "selected archetype"
                flash(f"Provide required archetype fields for '{archetype_name}': {preview}.", "warning")
                return redirect(url_for("matter_create"))

            auto_contract_templates = (
                contract_templates_by_archetype.get(int(archetype.id), [])
                if archetype is not None
                else []
            )
            auto_document_templates = (
                document_templates_by_archetype.get(int(archetype.id), [])
                if archetype is not None
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
                return redirect(url_for("matter_create"))

            now = utc_now()

            m = Matter(
                matter_no=matter_no,
                title=title,
                client_name=client_name,
                status=status,
                description=description,
                objective=objective or None,
                risk_level=risk_level,
                budget_status=budget_status,
                last_update_note=last_update_note or None,
                last_updated_at=now,
                created_by=current_user.id,
                legal_category=legal_category or None,
                archetype_id=archetype.id if archetype else None,
                archetype_data_json=(json.dumps(matter_specific_values, ensure_ascii=True) if matter_specific_values else None),
                stage=(archetype.default_stage if archetype and archetype.default_stage else None),
                practice_area=(archetype.practice_area if archetype and archetype.practice_area else None),
            )
            db.session.add(m)
            db.session.flush()
            db.session.add(MatterMember(matter_id=m.id, user_id=current_user.id, role_in_matter="Responsible"))
            checklist_seeded_count = ensure_matter_closing_checklist_items(m.id, archetype)
            team_member_ids = {current_user.id}
            for lawyer_user_id in selected_lawyer_ids:
                if lawyer_user_id in team_member_ids:
                    continue
                db.session.add(MatterMember(matter_id=m.id, user_id=lawyer_user_id, role_in_matter="Team"))
                team_member_ids.add(lawyer_user_id)
            generated_contract_file_paths: list[str] = []
            generated_contract_template_ids: list[int] = []
            generated_contract_missing_tokens: list[tuple[str, list[str]]] = []
            generated_document_template_ids: list[int] = []
            generated_document_missing_tokens: list[tuple[str, list[str]]] = []
            for contract_template in auto_contract_templates:
                rendered_contract, missing_tokens = render_contract_template_for_matter(
                    template=contract_template,
                    matter=m,
                    archetype=archetype,
                    archetype_values=matter_specific_values,
                    contract_values=contract_field_values,
                )
                if not rendered_contract.strip():
                    db.session.rollback()
                    cleanup_generated_files(generated_contract_file_paths)
                    flash(
                        f"Contract template '{contract_template.name}' produced an empty document. Update the template body and retry.",
                        "warning",
                    )
                    return redirect(url_for("matter_create"))
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
                        f"Failed to generate contract '{contract_template.name}'. Matter was not created.",
                        "warning",
                    )
                    return redirect(url_for("matter_create"))
                generated_contract_template_ids.append(int(contract_template.id))
                generated_contract_file_paths.append(file_path)
                if missing_tokens:
                    generated_contract_missing_tokens.append((contract_template.name, missing_tokens))
            for document_template in auto_document_templates:
                context = build_document_context(
                    m,
                    archetype=archetype,
                    required_values=matter_specific_values,
                )
                context["document_template_name"] = document_template.name or ""
                rendered_document, missing_tokens = render_template_text(document_template.body, context)
                if not rendered_document.strip():
                    db.session.rollback()
                    cleanup_generated_files(generated_contract_file_paths)
                    flash(
                        f"Document template '{document_template.name}' produced an empty document. Update the template and retry.",
                        "warning",
                    )
                    return redirect(url_for("matter_create"))
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
                        f"Failed to generate document '{document_template.name}'. Matter was not created.",
                        "warning",
                    )
                    return redirect(url_for("matter_create"))
                generated_document_template_ids.append(int(document_template.id))
                generated_contract_file_paths.append(file_path)
                if missing_tokens:
                    generated_document_missing_tokens.append((document_template.name, missing_tokens))
            db.session.add(
                MatterTimelineEvent(
                    matter_id=m.id,
                    event_date=now.date(),
                    event_type="Milestone",
                    title="Matter opened",
                    description="Matter intake completed and baseline plan created.",
                    is_milestone=True,
                    created_by=current_user.id,
                )
            )
            db.session.add(
                MatterActivity(
                    matter_id=m.id,
                    actor_user_id=current_user.id,
                    action="Matter opened",
                    details=f"{m.matter_no} - {m.title}",
                )
            )
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
                cleanup_generated_files(generated_contract_file_paths)
                flash("Matter could not be created due to a storage error. Please retry.", "warning")
                return redirect(url_for("matter_create"))

            audit(
                "matter_create",
                "Matter",
                m.id,
                {
                    "matter_no": m.matter_no,
                    "risk_level": m.risk_level,
                    "legal_category": m.legal_category,
                    "archetype_id": m.archetype_id,
                    "assigned_lawyer_ids": sorted(user_id for user_id in team_member_ids if user_id != current_user.id),
                    "contract_template_ids": generated_contract_template_ids,
                    "document_template_ids": generated_document_template_ids,
                    "playbook_checklist_seeded": checklist_seeded_count,
                },
            )
            generated_contract_count = len(generated_contract_template_ids)
            generated_document_count = len(generated_document_template_ids)
            if generated_contract_count or generated_document_count:
                flash(
                    "Matter created. "
                    f"{generated_contract_count} contract draft(s) and {generated_document_count} document draft(s) were attached.",
                    "info",
                )
            else:
                flash("Matter created.", "info")
            if generated_contract_missing_tokens:
                warnings = [
                    f"{template_name}: {', '.join(tokens[:4])}"
                    for template_name, tokens in generated_contract_missing_tokens[:3]
                ]
                flash(
                    "Some contract merge fields were blank: " + "; ".join(warnings),
                    "warning",
                )
            if generated_document_missing_tokens:
                warnings = [
                    f"{template_name}: {', '.join(tokens[:4])}"
                    for template_name, tokens in generated_document_missing_tokens[:3]
                ]
                flash(
                    "Some document merge fields were blank: " + "; ".join(warnings),
                    "warning",
                )
            if checklist_seeded_count > 0:
                flash(f"Archetype playbook checklist seeded with {checklist_seeded_count} item(s).", "info")
            return redirect(url_for("matter_detail", matter_id=m.id))

        archetype_payload = _serialize_template_payload(
            archetype_templates,
            contract_templates_by_archetype=contract_templates_by_archetype,
            document_templates_by_archetype=document_templates_by_archetype,
        )
        return page(
            "New Matter",
            "matters/new.html",
            risk_levels=list(RISK_LEVELS),
            budget_statuses=list(BUDGET_STATUSES),
            legal_categories=legal_category_options(),
            archetype_templates=archetype_templates,
            archetype_payload=archetype_payload,
            assignable_lawyers=assignable_lawyers,
        )

    @app.post("/matters/<int:matter_id>/summary")
    @login_required
    def matter_summary_update(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        objective = (request.form.get("objective") or "").strip()
        risk_level = normalize_query(request.form.get("risk_level", m.risk_level)) or m.risk_level
        budget_status = normalize_query(request.form.get("budget_status", m.budget_status)) or m.budget_status
        status = normalize_query(request.form.get("status", m.status)) or m.status
        last_update_note = (request.form.get("last_update_note") or "").strip()
        outcome_summary = (request.form.get("outcome_summary") or "").strip()

        if risk_level not in RISK_LEVELS:
            flash("Invalid risk level.", "warning")
            return redirect(url_for("matter_detail", matter_id=matter_id))
        if budget_status not in BUDGET_STATUSES:
            flash("Invalid budget status.", "warning")
            return redirect(url_for("matter_detail", matter_id=matter_id))
        if status not in MATTER_STATUSES:
            flash("Invalid status.", "warning")
            return redirect(url_for("matter_detail", matter_id=matter_id))

        previous_status = m.status
        archetype = db.session.get(MatterTemplate, m.archetype_id) if m.archetype_id else None
        if previous_status != "Closed" and status == "Closed":
            ensure_matter_closing_checklist_items(m.id, archetype)
            compliance = build_archetype_compliance_snapshot(m, archetype)
            close_blockers: list[str] = []
            missing_labels = list(compliance.get("required_missing_labels") or [])
            checklist_remaining = int(compliance.get("checklist_remaining") or 0)
            if missing_labels:
                preview = ", ".join(missing_labels[:5])
                close_blockers.append(f"complete required archetype fields ({preview})")
            if checklist_remaining > 0:
                close_blockers.append(f"finish {checklist_remaining} archetype checklist item(s)")
            if close_blockers:
                m.objective = objective or None
                m.risk_level = risk_level
                m.budget_status = budget_status
                m.status = previous_status
                m.last_update_note = last_update_note or None
                m.outcome_summary = outcome_summary or None
                m.last_updated_at = utc_now()
                db.session.commit()
                flash("Summary saved, but matter was not closed: " + "; ".join(close_blockers) + ".", "warning")
                flash("Use Workspace close-out once archetype requirements are complete.", "info")
                return redirect(url_for("matter_workspace", matter_id=m.id))

        m.objective = objective or None
        m.risk_level = risk_level
        m.budget_status = budget_status
        m.status = status
        m.last_update_note = last_update_note or None
        m.outcome_summary = outcome_summary or None
        m.last_updated_at = utc_now()
        auto_pause_summary = {"paused": 0, "captured_entries": 0}
        open_task_count_on_close = 0
        if previous_status != "Closed" and status == "Closed":
            m.closed_at = m.last_updated_at
            auto_pause_summary = auto_pause_running_timers_for_matter(
                m.id,
                actor_user_id=current_user.id,
                pause_reason="matter_closed",
            )
            open_task_count_on_close = Task.query.filter(Task.matter_id == m.id, Task.status != "Done").count()
        if previous_status == "Closed" and status != "Closed":
            m.closed_at = None
        db.session.commit()

        audit(
            "matter_summary_update",
            "Matter",
            m.id,
            {"status": m.status, "risk_level": m.risk_level, "budget_status": m.budget_status},
        )
        matter_activity(
            m.id,
            "Executive summary updated",
            f"Status {m.status}, risk {m.risk_level}, budget {m.budget_status}",
        )
        if status == "Closed" and auto_pause_summary.get("paused", 0) > 0:
            flash(
                (
                    f"Auto-paused {auto_pause_summary.get('paused', 0)} running timer(s) "
                    f"and captured {auto_pause_summary.get('captured_entries', 0)} draft entry(ies)."
                ),
                "info",
            )
        if status == "Closed" and open_task_count_on_close > 0:
            flash(
                f"Matter closed with {open_task_count_on_close} open task(s). Review outstanding tasks before archiving.",
                "warning",
            )
        flash("Matter summary updated.", "info")
        return redirect(url_for("matter_detail", matter_id=m.id))

    @app.post("/matters/<int:matter_id>/ai/summary")
    @login_required
    def matter_ai_summary_draft(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        matter = db.session.get(Matter, matter_id)
        if matter is None:
            abort(404)

        payload_raw = request.get_json(silent=True) or {}
        payload = payload_raw if isinstance(payload_raw, dict) else {}
        tasks = (
            Task.query.filter_by(matter_id=matter_id)
            .order_by(Task.status.asc(), Task.due_date.asc().nullslast(), Task.id.desc())
            .limit(180)
            .all()
        )
        timeline = (
            MatterTimelineEvent.query.filter_by(matter_id=matter_id)
            .order_by(MatterTimelineEvent.event_date.desc(), MatterTimelineEvent.created_at.desc())
            .limit(40)
            .all()
        )
        docs = (
            DocumentFile.query.filter_by(matter_id=matter_id)
            .order_by(DocumentFile.uploaded_at.desc())
            .limit(30)
            .all()
        )
        notes = (
            MatterNote.query.filter_by(matter_id=matter_id)
            .order_by(MatterNote.updated_at.desc(), MatterNote.id.desc())
            .limit(20)
            .all()
        )
        activity_rows = (
            MatterActivity.query.filter_by(matter_id=matter_id)
            .order_by(MatterActivity.created_at.desc())
            .limit(20)
            .all()
        )
        matter_context = _build_matter_ai_context(
            matter=matter,
            tasks=tasks,
            timeline=timeline,
            docs=docs,
            notes=notes,
            activity_items=activity_rows,
        )
        current_values = {
            "objective": (payload.get("objective") or "").strip()[:900],
            "last_update_note": (payload.get("last_update_note") or "").strip()[:900],
            "outcome_summary": (payload.get("outcome_summary") or "").strip()[:900],
            "risk_level": (payload.get("risk_level") or "").strip()[:60],
            "budget_status": (payload.get("budget_status") or "").strip()[:60],
        }

        started = time.perf_counter()
        suggestion = suggest_matter_executive_summary(
            matter_context=matter_context,
            current_values=current_values,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        audit(
            "matter_ai_summary_draft",
            "Matter",
            matter.id,
            {
                "source": suggestion.get("source"),
                "fallback_reason": suggestion.get("fallback_reason"),
                "elapsed_ms": elapsed_ms,
            },
        )
        return jsonify(
            {
                "ok": True,
                "suggestion": suggestion,
                "elapsed_ms": elapsed_ms,
                "fallback_reason": suggestion.get("fallback_reason"),
                "fallback_detail": suggestion.get("fallback_detail"),
            }
        )

    @app.post("/matters/<int:matter_id>/ai/client-update")
    @login_required
    def matter_ai_client_update_draft(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        matter = db.session.get(Matter, matter_id)
        if matter is None:
            abort(404)

        payload_raw = request.get_json(silent=True) or {}
        payload = payload_raw if isinstance(payload_raw, dict) else {}
        tone_hint = " ".join(str(payload.get("tone_hint") or "").split()).strip()[:120]
        tasks = (
            Task.query.filter_by(matter_id=matter_id)
            .order_by(Task.status.asc(), Task.due_date.asc().nullslast(), Task.id.desc())
            .limit(180)
            .all()
        )
        timeline = (
            MatterTimelineEvent.query.filter_by(matter_id=matter_id)
            .order_by(MatterTimelineEvent.event_date.desc(), MatterTimelineEvent.created_at.desc())
            .limit(40)
            .all()
        )
        docs = (
            DocumentFile.query.filter_by(matter_id=matter_id)
            .order_by(DocumentFile.uploaded_at.desc())
            .limit(30)
            .all()
        )
        notes = (
            MatterNote.query.filter_by(matter_id=matter_id)
            .order_by(MatterNote.updated_at.desc(), MatterNote.id.desc())
            .limit(20)
            .all()
        )
        activity_rows = (
            MatterActivity.query.filter_by(matter_id=matter_id)
            .order_by(MatterActivity.created_at.desc())
            .limit(20)
            .all()
        )
        matter_context = _build_matter_ai_context(
            matter=matter,
            tasks=tasks,
            timeline=timeline,
            docs=docs,
            notes=notes,
            activity_items=activity_rows,
        )

        started = time.perf_counter()
        suggestion = suggest_matter_client_update(
            matter_context=matter_context,
            tone_hint=tone_hint,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        audit(
            "matter_ai_client_update_draft",
            "Matter",
            matter.id,
            {
                "source": suggestion.get("source"),
                "fallback_reason": suggestion.get("fallback_reason"),
                "elapsed_ms": elapsed_ms,
            },
        )
        return jsonify(
            {
                "ok": True,
                "suggestion": suggestion,
                "elapsed_ms": elapsed_ms,
                "fallback_reason": suggestion.get("fallback_reason"),
                "fallback_detail": suggestion.get("fallback_detail"),
            }
        )

    @app.post("/matters/<int:matter_id>/timeline")
    @login_required
    def matter_timeline_add(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        title = normalize_query(request.form.get("title", ""))
        event_type = normalize_query(request.form.get("event_type", "Milestone")) or "Milestone"
        event_date_raw = normalize_query(request.form.get("event_date", ""))
        description = (request.form.get("description") or "").strip()
        is_milestone = (request.form.get("is_milestone") or "").strip().lower() in {"1", "true", "yes", "on"}

        if not title:
            flash("Timeline title is required.", "warning")
            return redirect(url_for("matter_detail", matter_id=m.id))
        if event_type not in TIMELINE_EVENT_TYPES:
            flash("Invalid timeline event type.", "warning")
            return redirect(url_for("matter_detail", matter_id=m.id))
        try:
            event_date = dt.date.fromisoformat(event_date_raw) if event_date_raw else dt.date.today()
        except ValueError:
            flash("Timeline date must be YYYY-MM-DD.", "warning")
            return redirect(url_for("matter_detail", matter_id=m.id))

        event = MatterTimelineEvent(
            matter_id=m.id,
            event_date=event_date,
            event_type=event_type,
            title=title,
            description=description or None,
            is_milestone=is_milestone,
            created_by=current_user.id,
        )
        m.last_updated_at = utc_now()
        db.session.add(event)
        db.session.commit()

        audit(
            "matter_timeline_add",
            "MatterTimelineEvent",
            event.id,
            {"matter_id": m.id, "event_type": event.event_type, "event_date": str(event.event_date)},
        )
        matter_activity(m.id, f"Timeline event added: {event.title}", event.event_type)
        flash("Timeline event added.", "info")
        return redirect(url_for("matter_detail", matter_id=m.id))

    @app.post("/matters/<int:matter_id>/archetype-document")
    @login_required
    def matter_archetype_document_download(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)

        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        archetype = db.session.get(MatterTemplate, m.archetype_id) if m.archetype_id else None
        rendered, missing_tokens = _generate_archetype_document(m, archetype)
        if not rendered.strip():
            flash("No archetype boilerplate is configured for this matter yet.", "warning")
            return redirect(url_for("matter_detail", matter_id=m.id))

        if missing_tokens:
            missing_summary = ", ".join(missing_tokens[:10])
            rendered = f"[Missing merge fields: {missing_summary}]\n\n{rendered}"

        archetype_name = archetype.name if archetype else "archetype"
        filename_base = secure_filename(f"{m.matter_no}_{archetype_name}_draft") or f"matter_{m.id}_archetype_draft"
        audit(
            "matter_archetype_document_download",
            "Matter",
            m.id,
            {"archetype_id": m.archetype_id, "missing_tokens": missing_tokens},
        )
        return Response(
            rendered,
            mimetype="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename_base}.txt"'},
        )

    @app.post("/matters/<int:matter_id>/archetype-fields")
    @login_required
    def matter_archetype_fields_update(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)
        archetype = db.session.get(MatterTemplate, m.archetype_id) if m.archetype_id else None
        if archetype is None:
            flash("No archetype is linked to this matter.", "warning")
            return redirect(url_for("matter_detail", matter_id=m.id))
        field_defs = load_required_fields(archetype.required_fields_json)
        if not field_defs:
            flash("This archetype has no required fields to update.", "info")
            return redirect(url_for("matter_detail", matter_id=m.id))

        field_values = collect_required_field_values(request.form, field_defs)
        m.archetype_data_json = json.dumps(field_values, ensure_ascii=True) if field_values else None
        m.last_updated_at = utc_now()
        db.session.commit()

        missing_labels = validate_required_field_values(field_defs, field_values)
        audit(
            "matter_archetype_fields_update",
            "Matter",
            m.id,
            {"archetype_id": m.archetype_id, "missing_required_count": len(missing_labels)},
        )
        matter_activity(m.id, "Archetype fields updated")
        if missing_labels:
            flash("Archetype fields saved. Still missing: " + ", ".join(missing_labels[:5]) + ".", "warning")
        else:
            flash("Archetype fields updated and fully compliant.", "info")
        return redirect(url_for("matter_detail", matter_id=m.id))

    @app.get("/matters/<int:matter_id>")
    @login_required
    def matter_detail(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)

        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)
        _record_recent_matter_view(current_user.id, m.id)
        is_pinned = MatterPin.query.filter_by(user_id=current_user.id, matter_id=m.id).first() is not None

        members = (
            db.session.query(User, MatterMember)
            .join(MatterMember, MatterMember.user_id == User.id)
            .filter(MatterMember.matter_id == matter_id)
            .all()
        )
        tasks = Task.query.filter_by(matter_id=matter_id).order_by(Task.status.asc(), Task.due_date.asc().nullslast()).limit(50).all()
        docs = DocumentFile.query.filter_by(matter_id=matter_id).order_by(DocumentFile.uploaded_at.desc()).limit(30).all()
        timeline = (
            MatterTimelineEvent.query.filter_by(matter_id=matter_id)
            .order_by(MatterTimelineEvent.event_date.desc(), MatterTimelineEvent.created_at.desc())
            .limit(80)
            .all()
        )
        activity_items = (
            db.session.query(MatterActivity, User)
            .outerjoin(User, MatterActivity.actor_user_id == User.id)
            .filter(MatterActivity.matter_id == matter_id)
            .order_by(MatterActivity.created_at.desc())
            .limit(80)
            .all()
        )

        today = dt.date.today()
        overdue_tasks = [t for t in tasks if t.status != "Done" and t.due_date and t.due_date < today]
        due_soon_tasks = [t for t in tasks if t.status != "Done" and t.due_date and today <= t.due_date <= (today + dt.timedelta(days=7))]
        archetype = db.session.get(MatterTemplate, m.archetype_id) if m.archetype_id else None
        archetype_fields = load_required_fields(archetype.required_fields_json if archetype else None)
        archetype_values = parse_matter_archetype_values(m.archetype_data_json)
        archetype_document, archetype_missing_tokens = _generate_archetype_document(m, archetype)
        archetype_compliance = build_archetype_compliance_snapshot(m, archetype)
        notes_count = MatterNote.query.filter_by(matter_id=matter_id).count()
        matter_magic = build_matter_magic_snapshot(
            m,
            today=today,
            tasks=tasks,
            docs=docs,
            timeline=timeline,
            team_size=len(members),
            notes_count=notes_count,
            archetype_compliance=archetype_compliance,
        ) or {}
        matter_magic["actions"] = attach_matter_magic_links(list(matter_magic.get("actions", [])), m.id)

        return page(
            f"Matter {m.matter_no}",
            "matters/detail.html",
            m=m,
            members=members,
            tasks=tasks,
            docs=docs,
            timeline=timeline,
            activity_items=activity_items,
            overdue_tasks=overdue_tasks,
            due_soon_tasks=due_soon_tasks,
            risk_levels=list(RISK_LEVELS),
            budget_statuses=list(BUDGET_STATUSES),
            matter_statuses=sorted(MATTER_STATUSES),
            timeline_event_types=sorted(TIMELINE_EVENT_TYPES),
            today=today.isoformat(),
            is_pinned=is_pinned,
            archetype=archetype,
            archetype_fields=archetype_fields,
            archetype_values=archetype_values,
            archetype_document=archetype_document,
            archetype_missing_tokens=archetype_missing_tokens,
            archetype_compliance=archetype_compliance,
            matter_magic=matter_magic,
        )

    @app.post("/matters/<int:matter_id>/pin")
    @login_required
    def matter_pin_toggle(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        row = MatterPin.query.filter_by(user_id=current_user.id, matter_id=matter_id).first()
        next_path = _safe_next_path(request.form.get("next"), url_for("matter_detail", matter_id=matter_id))
        if row is None:
            row = MatterPin(user_id=current_user.id, matter_id=matter_id)
            db.session.add(row)
            db.session.commit()
            audit("matter_pin_add", "Matter", matter_id)
            flash("Matter pinned to your quick access list.", "info")
        else:
            db.session.delete(row)
            db.session.commit()
            audit("matter_pin_remove", "Matter", matter_id)
            flash("Matter removed from your quick access list.", "info")
        return redirect(next_path)

    @app.post("/matters/recent/clear")
    @login_required
    def matter_recent_clear():
        removed = (
            MatterRecentView.query.filter_by(user_id=current_user.id).delete(synchronize_session=False)
        )
        db.session.commit()
        audit("matter_recent_clear", "MatterRecentView", details={"removed": removed})
        flash("Recent matter history cleared.", "info")
        next_path = _safe_next_path(request.form.get("next"), url_for("dashboard"))
        return redirect(next_path)

    @app.route("/matters/<int:matter_id>/team", methods=["GET", "POST"])
    @login_required
    def matter_team(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        if request.method == "POST":
            enforce_permission("matter_team", "manage")
            email = normalize_query(request.form.get("email", "")).lower()
            role_in_matter = normalize_query(request.form.get("role_in_matter", "")) or "Team"
            if not is_valid_email(email):
                flash("Provide a valid email address.", "warning")
                return redirect(url_for("matter_team", matter_id=matter_id))
            u = User.query.filter_by(email=email).first()
            if not u:
                flash("No such user. Admin must create them first.", "warning")
                return redirect(url_for("matter_team", matter_id=matter_id))
            if role_is_director(getattr(current_user, "role", None)) and not user_in_director_scope(
                int(current_user.id),
                int(u.id),
            ):
                flash("Directors can only add attorneys from their own team.", "warning")
                return redirect(url_for("matter_team", matter_id=matter_id))
            if MatterMember.query.filter_by(matter_id=matter_id, user_id=u.id).first():
                flash("User already in team.", "warning")
                return redirect(url_for("matter_team", matter_id=matter_id))
            db.session.add(MatterMember(matter_id=matter_id, user_id=u.id, role_in_matter=role_in_matter))
            db.session.commit()
            audit("matter_team_add", "Matter", matter_id, {"user_id": u.id, "role_in_matter": role_in_matter})
            matter_activity(matter_id, "Team member added", f"{u.full_name} ({role_in_matter})")
            flash("Added to matter team.", "info")
            return redirect(url_for("matter_team", matter_id=matter_id))

        members = (
            db.session.query(User, MatterMember)
            .join(MatterMember, MatterMember.user_id == User.id)
            .filter(MatterMember.matter_id == matter_id)
            .all()
        )
        users_query = User.query.order_by(User.full_name.asc())
        if role_is_director(getattr(current_user, "role", None)):
            scoped_ids = director_team_member_ids(int(current_user.id)).union({int(current_user.id)})
            if scoped_ids:
                users_query = users_query.filter(User.id.in_(scoped_ids))
            else:
                users_query = users_query.filter(User.id == -1)
        users = users_query.limit(500).all()

        return page("Matter Team", "matters/team.html", m=m, members=members, users=users)

    def _task_form_context() -> tuple[list[User], list[TaskTemplate], dict[int, TaskTemplateItem]]:
        users = User.query.order_by(User.full_name.asc()).limit(500).all()
        task_templates = TaskTemplate.query.order_by(TaskTemplate.name.asc()).limit(250).all()
        template_primary_items: dict[int, TaskTemplateItem] = {}
        template_ids = [row.id for row in task_templates]
        if template_ids:
            template_items = (
                TaskTemplateItem.query.filter(TaskTemplateItem.task_template_id.in_(template_ids))
                .order_by(TaskTemplateItem.task_template_id.asc(), TaskTemplateItem.position.asc())
                .all()
            )
            for item in template_items:
                if item.task_template_id not in template_primary_items:
                    template_primary_items[item.task_template_id] = item
        return users, task_templates, template_primary_items

    def _create_task_from_request(m: Matter):
        matter_id = int(m.id)

        def _redirect_to_create():
            return redirect(url_for("matter_task_create", matter_id=matter_id))

        template_id = request.form.get("template_id", type=int)
        template = db.session.get(TaskTemplate, template_id) if template_id else None
        if template_id and template is None:
            flash("Selected task template was not found.", "warning")
            return _redirect_to_create()

        template_items: list[TaskTemplateItem] = []
        if template is not None:
            template_items = (
                TaskTemplateItem.query.filter_by(task_template_id=template.id)
                .order_by(TaskTemplateItem.position.asc())
                .all()
            )
        template_primary_item = template_items[0] if template_items else None

        title = normalize_query(request.form.get("title", ""))
        description = (request.form.get("description") or "").strip()
        if not title and template_primary_item is not None:
            title = normalize_query(template_primary_item.title or "")
        if not description and template_primary_item is not None and template_primary_item.description:
            description = template_primary_item.description.strip()
        if not description and len(template_items) > 1:
            checklist_lines = "\n".join(f"- {item.title}" for item in template_items[1:] if item.title)
            if checklist_lines:
                description = f"Template checklist:\n{checklist_lines}"

        due = normalize_query(request.form.get("due_date", ""))
        assigned_to_email = normalize_query(request.form.get("assigned_to", "")).lower()
        save_as_template = (request.form.get("save_as_template") or "").strip().lower() in {"1", "true", "yes", "on"}
        template_name = normalize_query(request.form.get("template_name", ""))

        if not title:
            flash("Task title is required.", "warning")
            return _redirect_to_create()
        if save_as_template and not template_name:
            flash("Template name is required when saving this task as a template.", "warning")
            return _redirect_to_create()

        due_date = None
        if due:
            try:
                due_date = dt.date.fromisoformat(due)
            except ValueError:
                flash("Invalid due date. Use YYYY-MM-DD.", "warning")
                return _redirect_to_create()
        elif template is not None and template.sla_hours:
            due_days = max(1, (int(template.sla_hours) + 23) // 24)
            due_date = dt.date.today() + dt.timedelta(days=due_days)

        assignee_ids: list[int] = []
        seen_assignees: set[int] = set()
        for raw_user_id in request.form.getlist("assignee_user_ids"):
            raw_user_id = (raw_user_id or "").strip()
            if not raw_user_id:
                continue
            try:
                user_id = int(raw_user_id)
            except (TypeError, ValueError):
                flash("One or more assignee selections are invalid.", "warning")
                return _redirect_to_create()
            if user_id in seen_assignees:
                continue
            if db.session.get(User, user_id) is None:
                flash("One or more selected assignees could not be found.", "warning")
                return _redirect_to_create()
            assignee_ids.append(user_id)
            seen_assignees.add(user_id)

        if assigned_to_email:
            u = User.query.filter_by(email=assigned_to_email).first()
            if not u:
                flash("Assigned-to user not found.", "warning")
                return _redirect_to_create()
            if u.id not in seen_assignees:
                assignee_ids.append(u.id)
                seen_assignees.add(u.id)

        assigned_to = assignee_ids[0] if assignee_ids else None
        requires_two_person_review = (request.form.get("requires_two_person_review") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        task_priority = (template.priority if template is not None else "Medium") or "Medium"
        task_sla_hours = template.sla_hours if template is not None else None
        task_recurrence_rule = template.recurrence_rule if template is not None else None

        t = Task(
            matter_id=matter_id,
            title=title,
            description=description,
            due_date=due_date,
            assigned_to=assigned_to,
            created_by=current_user.id,
            requires_two_person_review=requires_two_person_review,
            priority=task_priority,
            sla_hours=task_sla_hours,
            recurrence_rule=task_recurrence_rule,
        )
        db.session.add(t)
        db.session.flush()
        for user_id in assignee_ids:
            db.session.add(TaskAssignee(task_id=t.id, user_id=user_id, assigned_by=current_user.id))

        template_saved_name: str | None = None
        template_saved_id: int | None = None
        if save_as_template:
            template_row = TaskTemplate.query.filter_by(name=template_name).first()
            if template_row is None:
                template_row = TaskTemplate(
                    name=template_name,
                    matter_type=(m.case_type or m.practice_area or "").strip() or None,
                    priority=task_priority,
                    sla_hours=task_sla_hours,
                    recurrence_rule=task_recurrence_rule,
                    created_by=current_user.id,
                )
                db.session.add(template_row)
                db.session.flush()
            else:
                if template_row.matter_type is None:
                    template_row.matter_type = (m.case_type or m.practice_area or "").strip() or None
                template_row.priority = task_priority
                template_row.sla_hours = task_sla_hours
                template_row.recurrence_rule = task_recurrence_rule
                TaskTemplateItem.query.filter_by(task_template_id=template_row.id).delete(synchronize_session=False)
            db.session.add(
                TaskTemplateItem(
                    task_template_id=template_row.id,
                    title=title,
                    description=description or None,
                    position=1,
                )
            )
            template_saved_name = template_row.name
            template_saved_id = template_row.id

        db.session.commit()
        audit(
            "task_create",
            "Task",
            t.id,
            {"matter_id": matter_id, "assignee_count": len(assignee_ids), "template_id": template.id if template else None},
        )
        if template_saved_name:
            audit("task_template_save_from_task", "TaskTemplate", template_saved_id, {"name": template_saved_name})
        matter_activity(matter_id, f"Task created: {t.title}", f"Due {t.due_date}" if t.due_date else "No due date")
        if template_saved_name:
            flash(f"Task created and template '{template_saved_name}' saved.", "info")
        else:
            flash("Task created.", "info")
        return redirect(url_for("matter_tasks", matter_id=matter_id))

    @app.get("/matters/<int:matter_id>/tasks")
    @login_required
    def matter_tasks(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        tasks = Task.query.filter_by(matter_id=matter_id).order_by(Task.status.asc(), Task.due_date.asc().nullslast()).limit(200).all()
        task_assignees_map: dict[int, list[User]] = {task.id: [] for task in tasks}
        task_ids = [task.id for task in tasks]
        users_map: dict[int, User] = {}
        if task_ids:
            assignment_rows = (
                db.session.query(TaskAssignee, User)
                .join(User, User.id == TaskAssignee.user_id)
                .filter(TaskAssignee.task_id.in_(task_ids))
                .order_by(TaskAssignee.task_id.asc(), User.full_name.asc())
                .all()
            )
            for assignment, user in assignment_rows:
                task_assignees_map.setdefault(assignment.task_id, []).append(user)
                users_map[user.id] = user
        fallback_ids = {int(task.assigned_to) for task in tasks if task.assigned_to and task.assigned_to not in users_map}
        if fallback_ids:
            for row in User.query.filter(User.id.in_(fallback_ids)).all():
                users_map[row.id] = row
        for task in tasks:
            if task_assignees_map.get(task.id) or task.assigned_to is None:
                continue
            fallback_user = users_map.get(task.assigned_to)
            if fallback_user is not None:
                task_assignees_map[task.id] = [fallback_user]
        task_tracker = build_task_tracker_snapshot(
            m,
            tasks,
            task_assignees_map=task_assignees_map,
            today=dt.date.today(),
        ) or {}

        return page(
            "Matter Tasks",
            "matters/tasks.html",
            m=m,
            tasks=tasks,
            task_assignees_map=task_assignees_map,
            task_tracker=task_tracker,
            today=dt.date.today(),
        )

    @app.route("/matters/<int:matter_id>/tasks/new", methods=["GET", "POST"])
    @login_required
    def matter_task_create(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        if request.method == "POST":
            return _create_task_from_request(m)

        users, task_templates, template_primary_items = _task_form_context()
        prefill_title = (request.args.get("prefill_title") or "").strip()
        prefill_description = (request.args.get("prefill_description") or "").strip()
        prefill_due_date_raw = (request.args.get("prefill_due_date") or "").strip()
        try:
            prefill_due_date = dt.date.fromisoformat(prefill_due_date_raw).isoformat() if prefill_due_date_raw else ""
        except ValueError:
            prefill_due_date = ""
        return page(
            "Add Task",
            "matters/task_new.html",
            m=m,
            users=users,
            task_templates=task_templates,
            template_primary_items=template_primary_items,
            prefill_title=prefill_title,
            prefill_description=prefill_description,
            prefill_due_date=prefill_due_date,
        )

    @app.post("/tasks/<int:task_id>/status")
    @login_required
    def task_update(task_id: int):
        t = db.session.get(Task, task_id)
        if not t:
            abort(404)
        if not can_access_matter(t.matter_id):
            abort(403)
        previous_status = t.status
        status = normalize_query(request.form.get("status", "Todo")) or "Todo"
        if status not in {"Todo", "Doing", "Done"}:
            abort(400)
        t.status = status
        db.session.commit()
        audit("task_status", "Task", t.id, {"status": status, "matter_id": t.matter_id})
        matter_activity(t.matter_id, f"Task status changed: {t.title}", f"Now {status}")
        suggest_time_on_done = (request.form.get("suggest_time_on_done") or "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if status == "Done" and previous_status != "Done" and suggest_time_on_done:
            end_at = utc_now().replace(second=0, microsecond=0)
            start_at = end_at - dt.timedelta(minutes=30)
            flash("Task marked done. Confirm the suggested time entry.", "info")
            return redirect(
                url_for(
                    "time_entries",
                    matter_id=t.matter_id,
                    task_id=t.id,
                    start_at=start_at.isoformat(timespec="minutes"),
                    end_at=end_at.isoformat(timespec="minutes"),
                    narrative=f"Completed task: {t.title}",
                    is_billable=1,
                )
            )
        return redirect(url_for("matter_tasks", matter_id=t.matter_id))

    @app.route("/matters/<int:matter_id>/documents", methods=["GET", "POST"])
    @login_required
    def matter_documents(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        if request.method == "POST":
            if "file" not in request.files:
                flash("No file uploaded.", "warning")
                return redirect(url_for("matter_documents", matter_id=matter_id))
            f = request.files["file"]
            if not f or not f.filename:
                flash("No file selected.", "warning")
                return redirect(url_for("matter_documents", matter_id=matter_id))
            if not allowed_doc(f.filename):
                flash("File type not allowed.", "warning")
                return redirect(url_for("matter_documents", matter_id=matter_id))
            enforce_data_residency("primary_storage")

            safe = secure_filename(f.filename)
            if not safe:
                flash("Invalid filename.", "warning")
                return redirect(url_for("matter_documents", matter_id=matter_id))
            storage_name = build_matter_storage_name("matter_docs", matter_id, safe)
            try:
                stored, dest = resolve_upload_path(app.config["UPLOAD_DIR"], storage_name, create_parent=True)
            except ValueError:
                flash("Storage path validation failed.", "warning")
                return redirect(url_for("matter_documents", matter_id=matter_id))
            f.save(dest)
            harden_private_file(dest)

            category = normalize_query(request.form.get("category", "General")) or "General"
            doc_version = normalize_query(request.form.get("doc_version", ""))
            lifecycle_stage = normalize_query(request.form.get("lifecycle_stage", "Draft")) or "Draft"
            owner_name = normalize_query(request.form.get("owner_name", ""))
            is_privileged = (request.form.get("is_privileged") or "").strip().lower() in {"1", "true", "yes", "on"}

            if category not in DOC_CATEGORIES:
                if os.path.isfile(dest):
                    os.remove(dest)
                flash("Invalid document category.", "warning")
                return redirect(url_for("matter_documents", matter_id=matter_id))
            if lifecycle_stage not in DOC_LIFECYCLE_STAGES:
                if os.path.isfile(dest):
                    os.remove(dest)
                flash("Invalid document stage.", "warning")
                return redirect(url_for("matter_documents", matter_id=matter_id))

            doc = DocumentFile(
                matter_id=matter_id,
                original_filename=safe,
                stored_filename=stored,
                sha256=sha256_file(dest),
                content_type=f.mimetype,
                category=category,
                doc_version=doc_version or None,
                lifecycle_stage=lifecycle_stage,
                owner_name=owner_name or None,
                is_privileged=is_privileged,
                uploaded_by=current_user.id,
            )
            db.session.add(doc)
            db.session.commit()
            audit(
                "document_upload",
                "DocumentFile",
                doc.id,
                {
                    "matter_id": matter_id,
                    "filename": safe,
                    "category": doc.category,
                    "lifecycle_stage": doc.lifecycle_stage,
                    "is_privileged": doc.is_privileged,
                },
            )
            matter_activity(
                matter_id,
                f"Document uploaded: {doc.original_filename}",
                f"{doc.category} / {doc.lifecycle_stage}",
            )
            flash("Uploaded.", "info")
            return redirect(url_for("matter_documents", matter_id=matter_id))

        docs = DocumentFile.query.filter_by(matter_id=matter_id).order_by(DocumentFile.uploaded_at.desc()).limit(200).all()

        return page(
            "Documents",
            "matters/documents.html",
            m=m,
            docs=docs,
            allowed=sorted(ALLOWED_DOC_EXT),
            doc_categories=sorted(DOC_CATEGORIES),
            doc_stages=sorted(DOC_LIFECYCLE_STAGES),
        )

    @app.get("/documents/<int:doc_id>/download")
    @login_required
    def doc_download(doc_id: int):
        enforce_permission("dms", "read")
        d = db.session.get(DocumentFile, doc_id)
        if not d:
            abort(404)
        if not can_access_matter(d.matter_id):
            abort(403)
        enforce_data_residency("exports")
        try:
            stored_filename, file_path = resolve_upload_path(app.config["UPLOAD_DIR"], d.stored_filename)
        except ValueError:
            abort(404)
        if not os.path.isfile(file_path):
            abort(404)
        inline = (request.args.get("inline") or "").strip().lower() in {"1", "true", "yes", "on"}
        if inline:
            audit("document_preview", "DocumentFile", d.id, {"matter_id": d.matter_id})
            matter_activity(d.matter_id, f"Document previewed: {d.original_filename}")
            return send_from_directory(
                app.config["UPLOAD_DIR"],
                stored_filename,
                as_attachment=False,
                download_name=d.original_filename,
                mimetype=d.content_type or None,
            )
        audit("document_download", "DocumentFile", d.id, {"matter_id": d.matter_id})
        matter_activity(d.matter_id, f"Document downloaded: {d.original_filename}")
        return send_from_directory(
            app.config["UPLOAD_DIR"],
            stored_filename,
            as_attachment=True,
            download_name=d.original_filename,
        )
