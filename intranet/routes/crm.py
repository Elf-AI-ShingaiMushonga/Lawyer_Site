from __future__ import annotations

import datetime as dt
import json

from flask import Response, abort, flash, redirect, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..helpers import audit, normalize_query
from ..reports import export_conflict_report_csv
from ..models import CRMFollowUp, CRMLead, ConflictCheck, EngagementLetter, IntakeForm, Matter
from ..services.conflict_engine import ConflictEngine
from ..templates import page


LEAD_STAGES = ["new", "contacted", "qualified", "proposal", "retained", "closed_lost"]


def register_crm_routes(app):
    @app.route("/crm/leads", methods=["GET", "POST"])
    @login_required
    def crm_leads():
        if request.method == "POST":
            full_name = normalize_query(request.form.get("full_name", ""))
            if not full_name:
                flash("Lead name is required.", "warning")
                return redirect(url_for("crm_leads"))
            row = CRMLead(
                full_name=full_name,
                organization=normalize_query(request.form.get("organization", "")) or None,
                email=normalize_query(request.form.get("email", "")).lower() or None,
                phone=normalize_query(request.form.get("phone", "")) or None,
                source=normalize_query(request.form.get("source", "")) or None,
                stage="new",
                notes=(request.form.get("notes") or "").strip() or None,
                created_by=current_user.id,
                assigned_to=request.form.get("assigned_to", type=int),
            )
            db.session.add(row)
            db.session.commit()
            audit("crm_lead_create", "CRMLead", row.id)
            flash("Lead created.", "info")
            return redirect(url_for("crm_leads"))

        q = normalize_query(request.args.get("q", ""))
        base = CRMLead.query
        if q:
            like = f"%{q}%"
            base = base.filter(
                (CRMLead.full_name.ilike(like))
                | (CRMLead.organization.ilike(like))
                | (CRMLead.email.ilike(like))
                | (CRMLead.source.ilike(like))
            )
        leads = base.order_by(CRMLead.created_at.desc()).limit(300).all()
        return page("CRM Leads", "crm/leads.html", leads=leads, stages=LEAD_STAGES, q=q)

    @app.route("/crm/leads/<int:lead_id>", methods=["GET", "POST"])
    @login_required
    def crm_lead_detail(lead_id: int):
        lead = db.session.get(CRMLead, lead_id)
        if not lead:
            abort(404)

        if request.method == "POST":
            action = (request.form.get("action") or "update").strip()
            if action == "update":
                stage = (request.form.get("stage") or lead.stage).strip()
                if stage not in LEAD_STAGES:
                    flash("Invalid stage.", "warning")
                    return redirect(url_for("crm_lead_detail", lead_id=lead_id))
                lead.stage = stage
                lead.notes = (request.form.get("notes") or "").strip() or lead.notes
                lead.updated_at = dt.datetime.utcnow()
                db.session.commit()
                audit("crm_lead_update", "CRMLead", lead.id, {"stage": stage})
                flash("Lead updated.", "info")

            elif action == "follow_up":
                due_raw = (request.form.get("due_at") or "").strip()
                note = (request.form.get("note") or "").strip()
                if not due_raw or not note:
                    flash("Follow-up due date and note required.", "warning")
                    return redirect(url_for("crm_lead_detail", lead_id=lead_id))
                try:
                    due_at = dt.datetime.fromisoformat(due_raw)
                except ValueError:
                    flash("Invalid follow-up datetime.", "warning")
                    return redirect(url_for("crm_lead_detail", lead_id=lead_id))
                row = CRMFollowUp(lead_id=lead.id, due_at=due_at, note=note, status="open", created_by=current_user.id)
                db.session.add(row)
                db.session.commit()
                audit("crm_followup_create", "CRMFollowUp", row.id)
                flash("Follow-up added.", "info")

            elif action == "intake":
                matter_id = request.form.get("matter_id", type=int)
                payload = {
                    "client_name": lead.organization or lead.full_name,
                    "lead_name": lead.full_name,
                    "entities": [lead.organization or lead.full_name],
                    "notes": lead.notes,
                }
                intake = IntakeForm(
                    lead_id=lead.id,
                    matter_id=matter_id,
                    data_json=json.dumps(payload),
                    created_by=current_user.id,
                )
                db.session.add(intake)
                db.session.commit()
                audit("intake_create", "IntakeForm", intake.id)
                flash("Intake form created.", "info")

            elif action == "engagement":
                matter_id = request.form.get("matter_id", type=int)
                if not matter_id:
                    flash("Matter id is required for engagement letter.", "warning")
                    return redirect(url_for("crm_lead_detail", lead_id=lead_id))
                letter = EngagementLetter(
                    matter_id=matter_id,
                    template_name=(request.form.get("template_name") or "default").strip(),
                    content=(request.form.get("content") or "").strip() or "Engagement terms",
                    status="pending_signature",
                    created_by=current_user.id,
                )
                db.session.add(letter)
                db.session.commit()
                audit("engagement_create", "EngagementLetter", letter.id)
                flash("Engagement letter created.", "info")

            return redirect(url_for("crm_lead_detail", lead_id=lead_id))

        followups = CRMFollowUp.query.filter_by(lead_id=lead.id).order_by(CRMFollowUp.due_at.asc()).all()
        intakes = IntakeForm.query.filter_by(lead_id=lead.id).order_by(IntakeForm.created_at.desc()).all()
        conflicts = (
            ConflictCheck.query.filter(ConflictCheck.intake_form_id.in_([i.id for i in intakes]))
            .order_by(ConflictCheck.created_at.desc())
            .all()
            if intakes
            else []
        )
        matters = Matter.query.order_by(Matter.opened_at.desc()).limit(200).all()
        letters = (
            EngagementLetter.query.filter(EngagementLetter.matter_id.in_([m.id for m in matters]))
            .order_by(EngagementLetter.created_at.desc())
            .limit(100)
            .all()
        )
        return page(
            "Lead Detail",
            "crm/lead_detail.html",
            lead=lead,
            followups=followups,
            intakes=intakes,
            conflicts=conflicts,
            matters=matters,
            letters=letters,
            stages=LEAD_STAGES,
        )

    @app.post("/crm/conflicts/check")
    @login_required
    def crm_conflicts_check():
        intake_id = request.form.get("intake_id", type=int)
        if not intake_id:
            flash("Intake id required.", "warning")
            return redirect(url_for("crm_leads"))

        report = ConflictEngine.run_check(intake_id)
        if report.conflict_check_id is not None:
            audit(
                "conflict_check_run",
                "ConflictCheck",
                report.conflict_check_id,
                {"status": report.status, "matches": report.matched_entities},
            )
        flash(f"Conflict check status: {report.status}", "info")
        intake = db.session.get(IntakeForm, intake_id)
        if intake and intake.lead_id:
            return redirect(url_for("crm_lead_detail", lead_id=intake.lead_id))
        return redirect(url_for("crm_leads"))

    @app.post("/crm/conflicts/<int:conflict_id>/override")
    @login_required
    def crm_conflict_override(conflict_id: int):
        if current_user.role not in {"admin", "lawyer"}:
            abort(403)
        row = db.session.get(ConflictCheck, conflict_id)
        if not row:
            abort(404)

        reason = (request.form.get("reason") or "").strip()
        if not reason:
            flash("Override reason required.", "warning")
            return redirect(url_for("crm_leads"))

        row.overridden_by = current_user.id
        row.override_reason = reason
        row.status = "overridden"
        db.session.commit()
        audit("conflict_override", "ConflictCheck", row.id, {"reason": reason})
        flash("Conflict override recorded.", "info")

        intake = db.session.get(IntakeForm, row.intake_form_id)
        if intake and intake.lead_id:
            return redirect(url_for("crm_lead_detail", lead_id=intake.lead_id))
        return redirect(url_for("crm_leads"))

    @app.get("/crm/conflicts/<int:conflict_id>/export")
    @login_required
    def crm_conflict_export(conflict_id: int):
        row = db.session.get(ConflictCheck, conflict_id)
        if not row:
            abort(404)

        payload = export_conflict_report_csv(conflict_id)
        audit("conflict_export", "ConflictCheck", conflict_id)
        return Response(
            payload,
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="conflict_{conflict_id}.csv"'},
        )

    @app.post("/crm/engagements/<int:engagement_id>/sign")
    @login_required
    def crm_engagement_sign(engagement_id: int):
        row = db.session.get(EngagementLetter, engagement_id)
        if not row:
            abort(404)

        signer_name = (request.form.get("signer_name") or "").strip()
        if not signer_name:
            flash("Signer name required.", "warning")
            return redirect(url_for("crm_leads"))

        row.status = "signed"
        row.signed_by = signer_name
        row.signed_at = dt.datetime.utcnow()
        row.signed_ip = request.remote_addr
        db.session.commit()
        audit("engagement_signed", "EngagementLetter", row.id, {"signed_by": signer_name})
        flash("Engagement signed.", "info")
        return redirect(url_for("crm_leads"))
