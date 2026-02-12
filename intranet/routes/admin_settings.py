from __future__ import annotations

import datetime as dt
import json

from flask import abort, flash, redirect, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..helpers import audit
from ..models import (
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
    TaskTemplate,
    TaskTemplateItem,
    TrustAccount,
    TrustThresholdAlert,
)
from ..templates import page


def _admin_required() -> None:
    if getattr(current_user, "role", None) != "admin":
        abort(403)


def register_admin_settings_routes(app):
    @app.route("/admin/settings/firm", methods=["GET", "POST"])
    @login_required
    def admin_settings_firm():
        _admin_required()
        if request.method == "POST":
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
        return page("Firm Settings", "admin_settings/firm.html", settings=rows)

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
        return page("Practice Areas", "admin_settings/practice_areas.html", areas=rows)

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

    @app.route("/admin/templates/matters", methods=["GET", "POST"])
    @login_required
    def admin_templates_matters():
        _admin_required()
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            if not name:
                flash("Template name required.", "warning")
                return redirect(url_for("admin_templates_matters"))
            if MatterTemplate.query.filter_by(name=name).first() is None:
                row = MatterTemplate(
                    name=name,
                    practice_area=(request.form.get("practice_area") or "").strip() or None,
                    default_stage=(request.form.get("default_stage") or "").strip() or None,
                    default_risk_level=(request.form.get("default_risk_level") or "Medium").strip() or "Medium",
                    checklist_json=json.dumps(
                        [line.strip() for line in (request.form.get("checklist") or "").splitlines() if line.strip()]
                    ),
                    created_by=current_user.id,
                )
                db.session.add(row)
                db.session.commit()
                audit("matter_template_create", "MatterTemplate", row.id)
            flash("Matter template saved.", "info")
            return redirect(url_for("admin_templates_matters"))

        rows = MatterTemplate.query.order_by(MatterTemplate.created_at.desc()).all()
        return page("Matter Templates", "admin_settings/templates_matters.html", templates=rows)

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
            if row is None:
                row = TaskTemplate(
                    name=name,
                    matter_type=(request.form.get("matter_type") or "").strip() or None,
                    priority=(request.form.get("priority") or "Medium").strip() or "Medium",
                    sla_hours=request.form.get("sla_hours", type=int),
                    recurrence_rule=(request.form.get("recurrence_rule") or "").strip() or None,
                    created_by=current_user.id,
                )
                db.session.add(row)
                db.session.flush()
            else:
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

    @app.route("/admin/templates/documents", methods=["GET", "POST"])
    @login_required
    def admin_templates_documents():
        _admin_required()
        if request.method == "POST":
            name = (request.form.get("name") or "").strip()
            body = (request.form.get("body") or "").strip()
            if not name or not body:
                flash("Template name and body are required.", "warning")
                return redirect(url_for("admin_templates_documents"))
            row = DocumentTemplate(
                name=name,
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
        return page("Document Templates", "admin_settings/templates_documents.html", templates=rows)

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
                row.released_at = dt.datetime.utcnow()
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
                    reference = matter.closed_at or dt.datetime.utcnow()
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
