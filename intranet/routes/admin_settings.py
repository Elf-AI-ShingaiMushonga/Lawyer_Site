from __future__ import annotations

import datetime as dt
from ..timeutils import utc_now
import json
import time

from flask import abort, flash, jsonify, redirect, request, url_for
from flask_login import current_user, login_required

from ..config import RISK_LEVELS
from ..extensions import db
from ..helpers import audit
from ..models import (
    ContractTemplate,
    DeadlineRule,
    DocumentTemplate,
    FirmSetting,
    LegalHold,
    Matter,
    MatterTemplate,
    Office,
    PracticeArea,
    RateCard,
    RetentionPolicy,
    ScheduledJob,
    TaskTemplate,
    TaskTemplateItem,
    TrustAccount,
    TrustThresholdAlert,
)
from ..services.archetypes import load_required_fields, parse_required_fields_definition
from ..services.archetype_ai import suggest_matter_archetype
from ..services.dms_option_lists import load_dms_option_lists, save_dms_option_lists
from ..services.matter_option_lists import legal_category_options, practice_area_options
from ..services.sa_practice import (
    DEFAULT_SA_PRACTICE_AREAS,
    SOUTH_AFRICA_PLAYBOOKS,
    seed_south_africa_playbooks,
    seed_south_africa_practice_areas,
)
from ..services.template_ai import suggest_contract_template, suggest_document_template
from ..services.priority_inbox import (
    load_priority_inbox_config,
    save_priority_inbox_config,
)
from ..templates import page
from ..roles import role_is_admin


def _admin_required() -> None:
    if not role_is_admin(getattr(current_user, "role", None)):
        abort(403)


def _required_fields_to_text(raw_json: str | None) -> str:
    lines: list[str] = []
    for field in load_required_fields(raw_json):
        key = str(field.get("key") or "").strip()
        if not key:
            continue
        label = str(field.get("label") or "").strip()
        help_text = str(field.get("help") or "").strip()
        if help_text:
            lines.append(f"{key}|{label}|{help_text}")
        elif label:
            lines.append(f"{key}|{label}")
        else:
            lines.append(key)
    return "\n".join(lines)


def _sync_priority_digest_schedule(config: dict) -> None:
    now = utc_now()
    interval = int(config.get("digest_interval_minutes") or 60)
    enabled = bool(config.get("digest_enabled"))
    row = ScheduledJob.query.filter_by(job_type="priority_inbox_digest").first()
    if row is None:
        row = ScheduledJob(
            job_type="priority_inbox_digest",
            default_payload={},
            interval_minutes=interval,
            next_run_at=now + dt.timedelta(minutes=interval),
            is_active=enabled,
        )
        db.session.add(row)
    else:
        row.interval_minutes = interval
        row.is_active = enabled
        if row.next_run_at is None or row.next_run_at <= now:
            row.next_run_at = now + dt.timedelta(minutes=interval)
    db.session.commit()


def register_admin_settings_routes(app):
    @app.route("/admin/settings/firm", methods=["GET", "POST"])
    @login_required
    def admin_settings_firm():
        _admin_required()
        if request.method == "POST":
            action = (request.form.get("action") or "generic").strip()
            if action == "priority_inbox":
                portal_response_sla_hours = request.form.get("portal_response_sla_hours", type=int)
                followup_horizon_hours = request.form.get("followup_horizon_hours", type=int)
                billing_capture_sla_hours = request.form.get("billing_capture_sla_hours", type=int)
                digest_interval_minutes = request.form.get("digest_interval_minutes", type=int)
                digest_enabled = (request.form.get("digest_enabled") or "").strip().lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }

                if portal_response_sla_hours is None or not 1 <= portal_response_sla_hours <= 168:
                    flash("Client response SLA must be between 1 and 168 hours.", "warning")
                    return redirect(url_for("admin_settings_firm"))
                if followup_horizon_hours is None or not 1 <= followup_horizon_hours <= 168:
                    flash("Follow-up horizon must be between 1 and 168 hours.", "warning")
                    return redirect(url_for("admin_settings_firm"))
                if billing_capture_sla_hours is None or not 1 <= billing_capture_sla_hours <= 336:
                    flash("Billing capture SLA must be between 1 and 336 hours.", "warning")
                    return redirect(url_for("admin_settings_firm"))
                if digest_interval_minutes is None or not 15 <= digest_interval_minutes <= 1440:
                    flash("Digest interval must be between 15 and 1440 minutes.", "warning")
                    return redirect(url_for("admin_settings_firm"))

                payload = save_priority_inbox_config(
                    {
                        "portal_response_sla_hours": portal_response_sla_hours,
                        "followup_horizon_hours": followup_horizon_hours,
                        "billing_capture_sla_hours": billing_capture_sla_hours,
                        "digest_enabled": digest_enabled,
                        "digest_interval_minutes": digest_interval_minutes,
                    },
                    updated_by=current_user.id,
                )
                _sync_priority_digest_schedule(payload)
                audit(
                    "priority_inbox_config_update",
                    "FirmSetting",
                    None,
                    {
                        "digest_enabled": payload["digest_enabled"],
                        "digest_interval_minutes": payload["digest_interval_minutes"],
                    },
                )
                flash("Priority Inbox settings updated.", "info")
                return redirect(url_for("admin_settings_firm"))

            if action == "dms_option_lists":
                payload = save_dms_option_lists(
                    {
                        "document_types": request.form.get("document_types"),
                        "confidentialities": request.form.get("confidentialities"),
                        "privilege_labels": request.form.get("privilege_labels"),
                        "retention_categories": request.form.get("retention_categories"),
                    },
                    updated_by=current_user.id,
                )
                audit(
                    "dms_option_lists_update",
                    "FirmSetting",
                    None,
                    {
                        "document_types": len(payload.get("document_types", [])),
                        "confidentialities": len(payload.get("confidentialities", [])),
                        "privilege_labels": len(payload.get("privilege_labels", [])),
                        "retention_categories": len(payload.get("retention_categories", [])),
                    },
                )
                flash("DMS metadata option lists updated.", "info")
                return redirect(url_for("admin_settings_firm"))

            setting_key = (request.form.get("setting_key") or "").strip()
            setting_value = (request.form.get("setting_value") or "").strip()
            if not setting_key:
                flash("Setting key is required.", "warning")
                return redirect(url_for("admin_settings_firm"))
            row = FirmSetting.query.filter_by(setting_key=setting_key).first()
            if row is None:
                row = FirmSetting(setting_key=setting_key, setting_value_json="{}", updated_by=current_user.id)
                db.session.add(row)
            row.setting_value_json = json.dumps({"value": setting_value})
            row.updated_by = current_user.id
            db.session.commit()
            audit("firm_setting_update", "FirmSetting", row.id, {"setting_key": setting_key})
            flash("Setting updated.", "info")
            return redirect(url_for("admin_settings_firm"))

        rows = FirmSetting.query.order_by(FirmSetting.setting_key.asc()).all()
        priority_inbox_config = load_priority_inbox_config()
        dms_option_lists = load_dms_option_lists()
        return page(
            "Firm Settings",
            "admin_settings/firm.html",
            settings=rows,
            priority_inbox_config=priority_inbox_config,
            dms_option_lists=dms_option_lists,
        )

    @app.route("/admin/settings/offices", methods=["GET", "POST"])
    @login_required
    def admin_settings_offices():
        _admin_required()
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            jurisdiction = (request.form.get("jurisdiction") or "").strip()
            if not name:
                flash("Office name required.", "warning")
                return redirect(url_for("admin_settings_offices"))
            if Office.query.filter_by(name=name).first() is None:
                db.session.add(Office(name=name, jurisdiction=jurisdiction or None, is_active=True))
                db.session.commit()
                audit("office_create", "Office", None, {"name": name})
            flash("Office saved.", "info")
            return redirect(url_for("admin_settings_offices"))

        rows = Office.query.order_by(Office.name.asc()).all()
        return page("Offices", "admin_settings/offices.html", offices=rows)

    @app.route("/admin/settings/practice-areas", methods=["GET", "POST"])
    @login_required
    def admin_settings_practice_areas():
        _admin_required()
        if request.method == "POST":
            action = (request.form.get("action") or "create").strip().lower()
            if action == "seed_south_africa":
                created = seed_south_africa_practice_areas(db.session)
                db.session.commit()
                audit("practice_area_seed_south_africa", "PracticeArea", None, {"created": created})
                flash(f"South Africa defaults applied. Added {created} practice area(s).", "info")
                return redirect(url_for("admin_settings_practice_areas"))

            name = (request.form.get("name") or "").strip()
            if not name:
                flash("Practice area required.", "warning")
                return redirect(url_for("admin_settings_practice_areas"))
            if PracticeArea.query.filter_by(name=name).first() is None:
                db.session.add(PracticeArea(name=name, is_active=True))
                db.session.commit()
                audit("practice_area_create", "PracticeArea", None, {"name": name})
            flash("Practice area saved.", "info")
            return redirect(url_for("admin_settings_practice_areas"))

        rows = PracticeArea.query.order_by(PracticeArea.name.asc()).all()
        return page(
            "Practice Areas",
            "admin_settings/practice_areas.html",
            areas=rows,
            south_africa_defaults=DEFAULT_SA_PRACTICE_AREAS,
        )

    @app.route("/admin/settings/rates", methods=["GET", "POST"])
    @login_required
    def admin_settings_rates():
        _admin_required()
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            client_name = (request.form.get("client_name") or "").strip()
            rate_per_hour = request.form.get("rate_per_hour", type=float)
            if rate_per_hour is None:
                flash("Rate per hour is required.", "warning")
                return redirect(url_for("admin_settings_rates"))
            db.session.add(
                RateCard(
                    name=name or None,
                    client_name=client_name or None,
                    rate_per_hour=rate_per_hour,
                    currency=(request.form.get("currency") or "ZAR").strip().upper(),
                    is_active=True,
                )
            )
            db.session.commit()
            audit("rate_card_create", "RateCard", None, {"client_name": client_name, "rate": rate_per_hour})
            flash("Rate card saved.", "info")
            return redirect(url_for("admin_settings_rates"))

        rates = RateCard.query.order_by(RateCard.id.desc()).limit(200).all()
        return page("Rates", "admin_settings/rates.html", rates=rates)

    @app.get("/admin/automation")
    @login_required
    def admin_automation():
        _admin_required()
        stats = {
            "matter_archetypes": MatterTemplate.query.count(),
            "contract_templates": ContractTemplate.query.count(),
            "task_templates": TaskTemplate.query.count(),
            "document_templates": DocumentTemplate.query.count(),
            "linked_contract_templates": ContractTemplate.query.filter(ContractTemplate.archetype_id.isnot(None)).count(),
            "linked_document_templates": DocumentTemplate.query.filter(DocumentTemplate.archetype_id.isnot(None)).count(),
            "auto_contract_templates": ContractTemplate.query.filter_by(
                is_active=True,
                auto_create_on_matter_open=True,
            ).count(),
        }
        return page("Automation Studio", "admin_settings/automation.html", stats=stats)

    @app.route("/admin/templates/matters", methods=["GET", "POST"])
    @login_required
    def admin_templates_matters():
        _admin_required()
        if request.method == "POST":
            action = (request.form.get("action") or "save").strip().lower()
            template_id = request.form.get("template_id", type=int)
            row = db.session.get(MatterTemplate, template_id) if template_id else None

            if action == "seed_south_africa":
                counts = seed_south_africa_playbooks(db.session, created_by=current_user.id)
                db.session.commit()
                audit("matter_template_seed_south_africa", "MatterTemplate", None, counts)
                flash(
                    "South Africa playbooks added. "
                    f"Archetypes: {counts['matter_archetypes']}, "
                    f"documents: {counts['document_templates']}, "
                    f"contracts: {counts['contract_templates']}, "
                    f"task templates: {counts['task_templates']}.",
                    "info",
                )
                return redirect(url_for("admin_templates_matters"))

            if action == "delete":
                if row is None:
                    flash("Matter archetype not found.", "warning")
                    return redirect(url_for("admin_templates_matters"))
                linked_count = Matter.query.filter_by(archetype_id=row.id).count()
                if linked_count > 0:
                    flash(
                        f"Cannot delete archetype '{row.name}' because {linked_count} matter(s) still use it.",
                        "warning",
                    )
                    return redirect(url_for("admin_templates_matters"))
                deleted_id = int(row.id)
                deleted_name = row.name
                db.session.delete(row)
                db.session.commit()
                audit("matter_template_delete", "MatterTemplate", deleted_id, {"name": deleted_name})
                flash("Matter archetype deleted.", "info")
                return redirect(url_for("admin_templates_matters"))

            name = (request.form.get("name") or "").strip()
            legal_category = (request.form.get("legal_category") or "").strip()
            legal_category_new = (request.form.get("legal_category_new") or "").strip()
            if legal_category_new:
                legal_category = legal_category_new
            boilerplate_template = (request.form.get("boilerplate_template") or "").strip()
            required_fields, field_errors = parse_required_fields_definition(request.form.get("required_fields"))
            if not name:
                flash("Template name required.", "warning")
                return redirect(url_for("admin_templates_matters"))

            if not legal_category:
                flash("Legal category is required.", "warning")
                return redirect(url_for("admin_templates_matters"))
            if not boilerplate_template:
                flash("Boilerplate template is required.", "warning")
                return redirect(url_for("admin_templates_matters"))
            if field_errors:
                flash(field_errors[0], "warning")
                return redirect(url_for("admin_templates_matters"))

            if template_id and row is None:
                flash("Matter archetype not found.", "warning")
                return redirect(url_for("admin_templates_matters"))

            duplicate_name = (
                MatterTemplate.query.filter(MatterTemplate.name == name, MatterTemplate.id != template_id).first()
                if template_id
                else MatterTemplate.query.filter_by(name=name).first()
            )
            if duplicate_name is not None and row is not duplicate_name:
                flash("Another archetype already uses that name.", "warning")
                return redirect(url_for("admin_templates_matters"))

            if row is None:
                row = duplicate_name
            is_new = row is None
            if row is None:
                row = MatterTemplate(name=name, created_by=current_user.id)
                db.session.add(row)
            else:
                row.name = name

            row.legal_category = legal_category
            row.practice_area = (request.form.get("practice_area") or "").strip() or None
            row.default_stage = (request.form.get("default_stage") or "").strip() or None
            default_risk_level = (request.form.get("default_risk_level") or "Medium").strip() or "Medium"
            if default_risk_level not in RISK_LEVELS:
                flash("Default risk level must be one of: " + ", ".join(RISK_LEVELS) + ".", "warning")
                return redirect(url_for("admin_templates_matters"))
            row.default_risk_level = default_risk_level
            row.checklist_json = json.dumps(
                [line.strip() for line in (request.form.get("checklist") or "").splitlines() if line.strip()]
            )
            row.required_fields_json = json.dumps(required_fields, ensure_ascii=True)
            row.boilerplate_template = boilerplate_template
            db.session.commit()
            audit("matter_template_create" if is_new else "matter_template_update", "MatterTemplate", row.id)
            flash("Matter archetype saved.", "info")
            return redirect(url_for("admin_templates_matters"))

        edit_id = request.args.get("edit_id", type=int)
        edit_row = db.session.get(MatterTemplate, edit_id) if edit_id else None
        if edit_id and edit_row is None:
            flash("Matter archetype not found.", "warning")
            return redirect(url_for("admin_templates_matters"))

        rows = MatterTemplate.query.order_by(MatterTemplate.created_at.desc()).all()
        template_fields_map = {row.id: load_required_fields(row.required_fields_json) for row in rows}
        linked_document_rows = (
            db.session.query(DocumentTemplate.archetype_id, DocumentTemplate.name)
            .filter(DocumentTemplate.archetype_id.isnot(None))
            .order_by(DocumentTemplate.name.asc())
            .all()
        )
        template_document_map: dict[int, list[str]] = {}
        for archetype_id, template_name in linked_document_rows:
            if not archetype_id or not template_name:
                continue
            template_document_map.setdefault(int(archetype_id), []).append(str(template_name))
        categories = legal_category_options(extra_values=[edit_row.legal_category if edit_row else None])
        practice_areas = practice_area_options(extra_values=[edit_row.practice_area if edit_row else None])
        usage_rows = (
            db.session.query(Matter.archetype_id, db.func.count(Matter.id))
            .filter(Matter.archetype_id.isnot(None))
            .group_by(Matter.archetype_id)
            .all()
        )
        template_usage_map = {int(archetype_id): int(count) for archetype_id, count in usage_rows if archetype_id}
        form_data = {
            "name": edit_row.name if edit_row else "",
            "legal_category": edit_row.legal_category if edit_row else "",
            "practice_area": edit_row.practice_area if edit_row else "",
            "default_stage": edit_row.default_stage if edit_row else "",
            "default_risk_level": (edit_row.default_risk_level if edit_row else "Medium") or "Medium",
            "checklist": (
                "\n".join(
                    str(item).strip()
                    for item in (
                        json.loads(edit_row.checklist_json)
                        if edit_row and edit_row.checklist_json
                        else []
                    )
                    if str(item).strip()
                )
                if edit_row
                else ""
            ),
            "required_fields": _required_fields_to_text(edit_row.required_fields_json if edit_row else None),
            "boilerplate_template": edit_row.boilerplate_template if edit_row else "",
        }
        return page(
            "Matter Templates",
            "admin_settings/templates_matters.html",
            templates=rows,
            template_fields_map=template_fields_map,
            categories=categories,
            practice_areas=practice_areas,
            risk_levels=list(RISK_LEVELS),
            edit_template_id=(edit_row.id if edit_row else None),
            form_data=form_data,
            template_usage_map=template_usage_map,
            template_document_map=template_document_map,
            south_africa_playbooks=[pack["archetype"] for pack in SOUTH_AFRICA_PLAYBOOKS],
        )

    @app.post("/admin/templates/matters/ai/suggest")
    @login_required
    def admin_templates_matters_ai_suggest():
        _admin_required()
        payload = request.get_json(silent=True) if request.is_json else request.form
        prompt = " ".join(str(payload.get("prompt") or "").split()).strip() if payload else ""
        if len(prompt) < 20:
            return jsonify({"ok": False, "error": "Provide at least 20 characters describing the archetype."}), 400

        legal_category_hint = " ".join(str(payload.get("legal_category_hint") or "").split()).strip() if payload else ""
        name_hint = " ".join(str(payload.get("name_hint") or "").split()).strip() if payload else ""
        started = time.perf_counter()
        suggestion = suggest_matter_archetype(
            prompt=prompt,
            legal_category_hint=legal_category_hint,
            name_hint=name_hint,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        audit(
            "matter_template_ai_suggest",
            "MatterTemplate",
            None,
            {
                "source": suggestion.get("source"),
                "fallback_reason": suggestion.get("fallback_reason"),
                "required_fields": len(suggestion.get("required_fields") or []),
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

    @app.route("/admin/templates/tasks", methods=["GET", "POST"])
    @login_required
    def admin_templates_tasks():
        _admin_required()
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            if not name:
                flash("Template name required.", "warning")
                return redirect(url_for("admin_templates_tasks"))

            row = TaskTemplate.query.filter_by(name=name).first()
            matter_type = (request.form.get("matter_type") or "").strip() or None
            priority = (request.form.get("priority") or "Medium").strip() or "Medium"
            sla_hours = request.form.get("sla_hours", type=int)
            recurrence_rule = (request.form.get("recurrence_rule") or "").strip() or None
            if row is None:
                row = TaskTemplate(
                    name=name,
                    matter_type=matter_type,
                    priority=priority,
                    sla_hours=sla_hours,
                    recurrence_rule=recurrence_rule,
                    created_by=current_user.id,
                )
                db.session.add(row)
                db.session.flush()
            else:
                row.matter_type = matter_type
                row.priority = priority
                row.sla_hours = sla_hours
                row.recurrence_rule = recurrence_rule

            TaskTemplateItem.query.filter_by(task_template_id=row.id).delete(synchronize_session=False)

            for i, line in enumerate((request.form.get("items") or "").splitlines(), start=1):
                item = line.strip()
                if item:
                    db.session.add(TaskTemplateItem(task_template_id=row.id, title=item, position=i))

            db.session.commit()
            audit("task_template_upsert", "TaskTemplate", row.id)
            flash("Task template saved.", "info")
            return redirect(url_for("admin_templates_tasks"))

        rows = TaskTemplate.query.order_by(TaskTemplate.created_at.desc()).all()
        items = TaskTemplateItem.query.order_by(TaskTemplateItem.task_template_id.asc(), TaskTemplateItem.position.asc()).all()
        return page("Task Templates", "admin_settings/templates_tasks.html", templates=rows, items=items)

    @app.post("/admin/templates/documents/ai/suggest")
    @login_required
    def admin_templates_documents_ai_suggest():
        _admin_required()
        payload = request.get_json(silent=True) if request.is_json else request.form
        prompt = " ".join(str(payload.get("prompt") or "").split()).strip() if payload else ""
        if len(prompt) < 20:
            return jsonify({"ok": False, "error": "Provide at least 20 characters describing the document template."}), 400

        name_hint = " ".join(str(payload.get("name_hint") or "").split()).strip() if payload else ""
        template_type_hint = " ".join(str(payload.get("template_type_hint") or "").split()).strip() if payload else ""
        started = time.perf_counter()
        suggestion = suggest_document_template(
            prompt=prompt,
            name_hint=name_hint,
            template_type_hint=template_type_hint,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        audit(
            "document_template_ai_suggest",
            "DocumentTemplate",
            None,
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

    @app.route("/admin/templates/documents", methods=["GET", "POST"])
    @login_required
    def admin_templates_documents():
        _admin_required()
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            body = (request.form.get("body") or "").strip()
            archetype_id = request.form.get("archetype_id", type=int)
            archetype = db.session.get(MatterTemplate, archetype_id) if archetype_id else None
            if not name or not body:
                flash("Template name and body are required.", "warning")
                return redirect(url_for("admin_templates_documents"))
            if archetype_id and archetype is None:
                flash("Selected archetype was not found.", "warning")
                return redirect(url_for("admin_templates_documents"))
            row = DocumentTemplate(
                name=name,
                archetype_id=archetype.id if archetype else None,
                template_type=(request.form.get("template_type") or "general").strip(),
                body=body,
                requires_signature=(request.form.get("requires_signature") or "").lower() in {"1", "true", "on", "yes"},
                created_by=current_user.id,
            )
            db.session.add(row)
            db.session.commit()
            audit("document_template_create", "DocumentTemplate", row.id)
            flash("Document template saved.", "info")
            return redirect(url_for("admin_templates_documents"))

        rows = DocumentTemplate.query.order_by(DocumentTemplate.created_at.desc()).all()
        archetypes = MatterTemplate.query.order_by(MatterTemplate.name.asc()).all()
        archetype_name_map = {int(row.id): row.name for row in archetypes}
        return page(
            "Document Templates",
            "admin_settings/templates_documents.html",
            templates=rows,
            archetypes=archetypes,
            archetype_name_map=archetype_name_map,
        )

    @app.post("/admin/templates/contracts/ai/suggest")
    @login_required
    def admin_templates_contracts_ai_suggest():
        _admin_required()
        payload = request.get_json(silent=True) if request.is_json else request.form
        prompt = " ".join(str(payload.get("prompt") or "").split()).strip() if payload else ""
        if len(prompt) < 20:
            return jsonify({"ok": False, "error": "Provide at least 20 characters describing the contract template."}), 400

        legal_category_hint = " ".join(str(payload.get("legal_category_hint") or "").split()).strip() if payload else ""
        name_hint = " ".join(str(payload.get("name_hint") or "").split()).strip() if payload else ""
        contract_type_hint = " ".join(str(payload.get("contract_type_hint") or "").split()).strip() if payload else ""
        started = time.perf_counter()
        suggestion = suggest_contract_template(
            prompt=prompt,
            legal_category_hint=legal_category_hint,
            name_hint=name_hint,
            contract_type_hint=contract_type_hint,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        audit(
            "contract_template_ai_suggest",
            "ContractTemplate",
            None,
            {
                "source": suggestion.get("source"),
                "fallback_reason": suggestion.get("fallback_reason"),
                "required_fields": len(suggestion.get("required_fields") or []),
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

    @app.route("/admin/templates/contracts", methods=["GET", "POST"])
    @login_required
    def admin_templates_contracts():
        _admin_required()
        if request.method == "POST":
            action = (request.form.get("action") or "save").strip().lower()
            template_id = request.form.get("template_id", type=int)
            row = db.session.get(ContractTemplate, template_id) if template_id else None

            if action == "delete":
                if row is None:
                    flash("Contract template not found.", "warning")
                    return redirect(url_for("admin_templates_contracts"))
                deleted_id = int(row.id)
                deleted_name = row.name
                db.session.delete(row)
                db.session.commit()
                audit("contract_template_delete", "ContractTemplate", deleted_id, {"name": deleted_name})
                flash("Contract template deleted.", "info")
                return redirect(url_for("admin_templates_contracts"))

            name = (request.form.get("name") or "").strip()
            body = (request.form.get("body") or "").strip()
            archetype_id = request.form.get("archetype_id", type=int)
            archetype = db.session.get(MatterTemplate, archetype_id) if archetype_id else None
            required_fields, field_errors = parse_required_fields_definition(request.form.get("required_fields"))

            if not name:
                flash("Contract template name is required.", "warning")
                return redirect(url_for("admin_templates_contracts"))
            if not body:
                flash("Contract template body is required.", "warning")
                return redirect(url_for("admin_templates_contracts"))
            if archetype_id and archetype is None:
                flash("Selected archetype was not found.", "warning")
                return redirect(url_for("admin_templates_contracts"))
            if field_errors:
                flash(field_errors[0], "warning")
                return redirect(url_for("admin_templates_contracts"))
            if template_id and row is None:
                flash("Contract template not found.", "warning")
                return redirect(url_for("admin_templates_contracts"))

            duplicate_name = (
                ContractTemplate.query.filter(ContractTemplate.name == name, ContractTemplate.id != template_id).first()
                if template_id
                else ContractTemplate.query.filter_by(name=name).first()
            )
            if duplicate_name is not None and row is not duplicate_name:
                flash("Another contract template already uses that name.", "warning")
                return redirect(url_for("admin_templates_contracts"))

            if row is None:
                row = ContractTemplate(name=name, created_by=current_user.id)
                db.session.add(row)
                is_new = True
            else:
                row.name = name
                is_new = False

            selected_legal_category = (request.form.get("legal_category") or "").strip()
            if not selected_legal_category and archetype and archetype.legal_category:
                selected_legal_category = str(archetype.legal_category).strip()
            row.legal_category = selected_legal_category or None
            row.archetype_id = archetype.id if archetype else None
            row.contract_type = (request.form.get("contract_type") or "Contract").strip() or "Contract"
            row.required_fields_json = json.dumps(required_fields, ensure_ascii=True)
            row.body = body
            row.requires_signature = (request.form.get("requires_signature") or "").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            row.auto_create_on_matter_open = (request.form.get("auto_create_on_matter_open") or "").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            row.is_active = (request.form.get("is_active") or "").strip().lower() in {"1", "true", "yes", "on"}
            db.session.commit()
            audit("contract_template_create" if is_new else "contract_template_update", "ContractTemplate", row.id)
            flash("Contract template saved.", "info")
            return redirect(url_for("admin_templates_contracts"))

        edit_id = request.args.get("edit_id", type=int)
        edit_row = db.session.get(ContractTemplate, edit_id) if edit_id else None
        if edit_id and edit_row is None:
            flash("Contract template not found.", "warning")
            return redirect(url_for("admin_templates_contracts"))

        rows = ContractTemplate.query.order_by(ContractTemplate.created_at.desc()).all()
        archetypes = MatterTemplate.query.order_by(MatterTemplate.name.asc()).all()
        categories = legal_category_options(extra_values=[edit_row.legal_category if edit_row else None])
        archetype_name_map = {int(row.id): row.name for row in archetypes}
        contract_fields_map = {row.id: load_required_fields(row.required_fields_json) for row in rows}
        form_data = {
            "name": edit_row.name if edit_row else "",
            "legal_category": edit_row.legal_category if edit_row else "",
            "archetype_id": edit_row.archetype_id if edit_row else "",
            "contract_type": (edit_row.contract_type if edit_row else "Contract") or "Contract",
            "required_fields": _required_fields_to_text(edit_row.required_fields_json if edit_row else None),
            "body": edit_row.body if edit_row else "",
            "requires_signature": bool(edit_row.requires_signature) if edit_row else True,
            "auto_create_on_matter_open": bool(edit_row.auto_create_on_matter_open) if edit_row else True,
            "is_active": bool(edit_row.is_active) if edit_row else True,
        }
        return page(
            "Contract Templates",
            "admin_settings/templates_contracts.html",
            templates=rows,
            archetypes=archetypes,
            categories=categories,
            archetype_name_map=archetype_name_map,
            contract_fields_map=contract_fields_map,
            edit_template_id=(edit_row.id if edit_row else None),
            form_data=form_data,
        )

    @app.route("/admin/rules/deadlines", methods=["GET", "POST"])
    @login_required
    def admin_rules_deadlines():
        _admin_required()
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            trigger_type = (request.form.get("trigger_type") or "").strip()
            if not name or not trigger_type:
                flash("Name and trigger type are required.", "warning")
                return redirect(url_for("admin_rules_deadlines"))

            row = DeadlineRule(
                name=name,
                trigger_type=trigger_type,
                offset_days=request.form.get("offset_days", type=int) or 0,
                jurisdiction=(request.form.get("jurisdiction") or "").strip() or None,
                business_day_adjust=(request.form.get("business_day_adjust") or "").lower() in {"1", "true", "on", "yes"},
                is_active=True,
                created_by=current_user.id,
            )
            db.session.add(row)
            db.session.commit()
            audit("deadline_rule_create", "DeadlineRule", row.id)
            flash("Deadline rule saved.", "info")
            return redirect(url_for("admin_rules_deadlines"))

        rows = DeadlineRule.query.order_by(DeadlineRule.created_at.desc()).all()
        return page("Deadline Rules", "admin_settings/rules_deadlines.html", rules=rows)

    @app.route("/admin/rules/retention", methods=["GET", "POST"])
    @login_required
    def admin_rules_retention():
        _admin_required()
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            retain_days = request.form.get("retain_days", type=int)
            if not name or retain_days is None:
                flash("Name and retention days are required.", "warning")
                return redirect(url_for("admin_rules_retention"))
            row = RetentionPolicy(
                name=name,
                matter_type=(request.form.get("matter_type") or "").strip() or None,
                jurisdiction=(request.form.get("jurisdiction") or "").strip() or None,
                retain_days=retain_days,
                archive_after_days=request.form.get("archive_after_days", type=int),
                is_active=True,
            )
            db.session.add(row)
            db.session.commit()
            audit("retention_policy_create", "RetentionPolicy", row.id)
            flash("Retention policy saved.", "info")
            return redirect(url_for("admin_rules_retention"))

        rows = RetentionPolicy.query.order_by(RetentionPolicy.id.desc()).all()
        return page("Retention Rules", "admin_settings/rules_retention.html", policies=rows)

    @app.route("/admin/rules/legal-holds", methods=["GET", "POST"])
    @login_required
    def admin_rules_legal_holds():
        _admin_required()
        if request.method == "POST":
            action = (request.form.get("action") or "create").strip()
            if action == "create":
                matter_id = request.form.get("matter_id", type=int)
                reason = (request.form.get("reason") or "").strip()
                if not matter_id or not reason:
                    flash("Matter and reason are required.", "warning")
                    return redirect(url_for("admin_rules_legal_holds"))
                matter = db.session.get(Matter, matter_id)
                if matter is None:
                    flash("Matter not found.", "warning")
                    return redirect(url_for("admin_rules_legal_holds"))
                existing = LegalHold.query.filter(LegalHold.matter_id == matter_id, LegalHold.is_active.is_(True)).first()
                if existing:
                    flash("An active legal hold already exists for this matter.", "warning")
                    return redirect(url_for("admin_rules_legal_holds"))
                row = LegalHold(matter_id=matter_id, reason=reason, is_active=True, created_by=current_user.id)
                db.session.add(row)
                if matter.status == "Closed":
                    matter.archival_status = "legal_hold_blocked"
                    matter.archival_due_at = None
                db.session.commit()
                audit("legal_hold_create", "LegalHold", row.id, {"matter_id": matter_id})
                flash("Legal hold created.", "info")
            elif action == "release":
                hold_id = request.form.get("hold_id", type=int)
                row = db.session.get(LegalHold, hold_id) if hold_id else None
                if row is None:
                    flash("Legal hold not found.", "warning")
                    return redirect(url_for("admin_rules_legal_holds"))
                if not row.is_active:
                    flash("Legal hold is already released.", "warning")
                    return redirect(url_for("admin_rules_legal_holds"))
                note = (request.form.get("release_note") or "").strip()
                row.is_active = False
                row.released_at = utc_now()
                if note:
                    row.reason = f"{row.reason}\n\nRelease note: {note}"
                matter = db.session.get(Matter, row.matter_id)
                if matter is not None and matter.status == "Closed" and matter.archival_status == "legal_hold_blocked":
                    policy = (
                        RetentionPolicy.query.filter(
                            RetentionPolicy.is_active.is_(True),
                            (RetentionPolicy.jurisdiction.is_(None))
                            | (RetentionPolicy.jurisdiction == matter.jurisdiction),
                        )
                        .order_by(RetentionPolicy.id.desc())
                        .first()
                    )
                    archive_days = int(policy.archive_after_days or 30) if policy else 30
                    reference = matter.closed_at or utc_now()
                    matter.archival_status = "archive_pending"
                    matter.archival_due_at = reference + dt.timedelta(days=max(1, archive_days))
                db.session.commit()
                audit("legal_hold_release", "LegalHold", row.id)
                flash("Legal hold released.", "info")
            else:
                flash("Unsupported legal hold action.", "warning")
            return redirect(url_for("admin_rules_legal_holds"))

        holds = LegalHold.query.order_by(LegalHold.created_at.desc()).limit(300).all()
        matters = Matter.query.order_by(Matter.opened_at.desc()).limit(300).all()
        return page("Legal Holds", "admin_settings/rules_legal_holds.html", holds=holds, matters=matters)

    @app.route("/admin/rules/trust", methods=["GET", "POST"])
    @login_required
    def admin_rules_trust():
        _admin_required()
        if request.method == "POST":
            action = (request.form.get("action") or "account").strip()
            if action == "account":
                name = (request.form.get("name") or "").strip()
                if not name:
                    flash("Trust account name required.", "warning")
                    return redirect(url_for("admin_rules_trust"))
                row = TrustAccount(
                    name=name,
                    bank_name=(request.form.get("bank_name") or "").strip() or None,
                    account_no_last4=(request.form.get("account_no_last4") or "").strip() or None,
                    jurisdiction=(request.form.get("jurisdiction") or "ZA").strip() or "ZA",
                    currency=(request.form.get("currency") or "ZAR").strip().upper(),
                    is_active=True,
                )
                db.session.add(row)
                db.session.commit()
                audit("trust_account_create", "TrustAccount", row.id)
            flash("Trust rules updated.", "info")
            return redirect(url_for("admin_rules_trust"))

        accounts = TrustAccount.query.order_by(TrustAccount.id.desc()).all()
        alerts = TrustThresholdAlert.query.order_by(TrustThresholdAlert.created_at.desc()).limit(50).all()
        return page("Trust Rules", "admin_settings/rules_trust.html", accounts=accounts, alerts=alerts)
