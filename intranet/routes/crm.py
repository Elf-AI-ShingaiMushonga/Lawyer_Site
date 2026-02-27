from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict
from urllib.parse import urlsplit

import sqlalchemy as sa
from flask import Response, abort, current_app, flash, redirect, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..helpers import audit, can_access_matter, get_active_matter_id, is_admin, normalize_query, set_active_matter_context
from ..reports import export_conflict_report_csv
from ..models import CRMFollowUp, CRMLead, ConflictCheck, ConflictSemanticHit, EngagementLetter, IntakeForm, LeadQuote, Matter
from ..policies import enforce_permission, visible_matter_ids
from ..services.workflow_automation import create_engagement_signed_tasks
from ..services.conflict_engine import ConflictEngine
from ..templates import page


LEAD_STAGES = ["new", "contacted", "qualified", "proposal", "retained", "closed_lost"]
QUOTE_STATUSES = ["draft", "sent", "accepted", "rejected", "expired"]
QUOTE_FEE_MODELS = ["fixed", "hourly", "capped"]


def _safe_next_path(next_path: str | None, fallback: str) -> str:
    if not next_path:
        return fallback
    parsed = urlsplit(next_path)
    if parsed.scheme or parsed.netloc:
        return fallback
    if not parsed.path.startswith("/"):
        return fallback
    return next_path


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _quote_financials(row: LeadQuote) -> dict[str, float]:
    base = round(max(0.0, _safe_float(row.estimated_amount)), 2)
    disbursements = round(max(0.0, _safe_float(row.disbursement_estimate)), 2)
    tax_rate = round(max(0.0, _safe_float(row.tax_rate, 15.0)), 2)
    subtotal = round(base + disbursements, 2)
    tax_amount = round(subtotal * (tax_rate / 100.0), 2)
    grand_total = round(subtotal + tax_amount, 2)
    return {
        "base": base,
        "disbursements": disbursements,
        "tax_rate": tax_rate,
        "tax_amount": tax_amount,
        "subtotal": subtotal,
        "grand_total": grand_total,
    }


def register_crm_routes(app):
    @app.route("/crm/leads", methods=["GET", "POST"])
    @login_required
    def crm_leads():
        enforce_permission("crm", "read")
        if request.method == "POST":
            enforce_permission("crm", "write")
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
        page_number = request.args.get("page", default=1, type=int) or 1
        if page_number < 1:
            page_number = 1
        base = CRMLead.query
        if q:
            like = f"%{q}%"
            base = base.filter(
                (CRMLead.full_name.ilike(like))
                | (CRMLead.organization.ilike(like))
                | (CRMLead.email.ilike(like))
                | (CRMLead.source.ilike(like))
            )
        pagination = base.order_by(CRMLead.created_at.desc()).paginate(page=page_number, per_page=50, error_out=False)
        leads = pagination.items
        stage_counts = {stage: 0 for stage in LEAD_STAGES}
        for stage, count in base.with_entities(CRMLead.stage, sa.func.count(CRMLead.id)).group_by(CRMLead.stage).all():
            if stage in stage_counts:
                stage_counts[stage] = int(count)
        return page(
            "CRM Leads",
            "crm/leads.html",
            leads=leads,
            stages=LEAD_STAGES,
            q=q,
            pagination=pagination,
            stage_counts=stage_counts,
            total_leads=pagination.total,
        )

    @app.route("/crm/leads/<int:lead_id>", methods=["GET", "POST"])
    @login_required
    def crm_lead_detail(lead_id: int):
        enforce_permission("crm", "read")
        lead = db.session.get(CRMLead, lead_id)
        if not lead:
            abort(404)

        def _selected_matter_id(*, fallback_to_active: bool = False) -> int | None:
            selected = request.form.get("matter_id", type=int)
            if selected:
                return selected
            selected = request.form.get("matter_id_select", type=int)
            if selected:
                return selected
            if fallback_to_active:
                return get_active_matter_id()
            return None

        if request.method == "POST":
            enforce_permission("crm", "write")
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
                matter_id = _selected_matter_id(fallback_to_active=True)
                if matter_id and not can_access_matter(matter_id):
                    abort(403)
                if matter_id and not db.session.get(Matter, matter_id):
                    flash("Selected matter does not exist.", "warning")
                    return redirect(url_for("crm_lead_detail", lead_id=lead_id))
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
                if matter_id:
                    set_active_matter_context(matter_id)
                audit("intake_create", "IntakeForm", intake.id)
                queued_check_id = None
                try:
                    queued_check_id = ConflictEngine.enqueue_semantic_scan(intake.id, requested_by=current_user.id)
                except Exception:  # pragma: no cover - resilience guard for queue/database contention
                    db.session.rollback()
                    current_app.logger.exception("Failed to queue semantic conflict scan for intake_id=%s", intake.id)
                if queued_check_id is not None:
                    audit(
                        "conflict_semantic_queued",
                        "ConflictCheck",
                        queued_check_id,
                        {"intake_id": intake.id, "source": "intake_create"},
                    )
                    flash("Intake form created. Semantic conflict scan queued.", "info")
                else:
                    flash("Intake form created.", "info")

            elif action == "engagement":
                matter_id = _selected_matter_id(fallback_to_active=True)
                if not matter_id:
                    flash("Select a matter for the engagement letter.", "warning")
                    return redirect(url_for("crm_lead_detail", lead_id=lead_id))
                if not can_access_matter(matter_id):
                    abort(403)
                if not db.session.get(Matter, matter_id):
                    flash("Selected matter does not exist.", "warning")
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
                set_active_matter_context(matter_id)
                audit("engagement_create", "EngagementLetter", letter.id)
                flash("Engagement letter created.", "info")

            elif action == "quote_create":
                title = normalize_query(request.form.get("quote_title", "")) or f"Quote for {lead.full_name}"
                fee_model = (request.form.get("fee_model") or "fixed").strip().lower()
                if fee_model not in QUOTE_FEE_MODELS:
                    flash("Invalid fee model.", "warning")
                    return redirect(url_for("crm_lead_detail", lead_id=lead_id))

                estimated_hours = request.form.get("estimated_hours", type=float)
                hourly_rate = request.form.get("hourly_rate", type=float)
                estimated_amount = _safe_float(request.form.get("estimated_amount", type=float), 0.0)
                if estimated_amount <= 0 and estimated_hours and hourly_rate:
                    estimated_amount = round(float(estimated_hours) * float(hourly_rate), 2)

                disbursement_estimate = _safe_float(request.form.get("disbursement_estimate", type=float), 0.0)
                tax_rate = _safe_float(request.form.get("tax_rate", type=float), 15.0)
                if estimated_amount <= 0:
                    flash("Estimated fee amount must be greater than 0.", "warning")
                    return redirect(url_for("crm_lead_detail", lead_id=lead_id))
                if disbursement_estimate < 0:
                    flash("Disbursement estimate cannot be negative.", "warning")
                    return redirect(url_for("crm_lead_detail", lead_id=lead_id))
                if tax_rate < 0:
                    flash("Tax rate cannot be negative.", "warning")
                    return redirect(url_for("crm_lead_detail", lead_id=lead_id))

                valid_until_raw = (request.form.get("valid_until") or "").strip()
                valid_until = None
                if valid_until_raw:
                    try:
                        valid_until = dt.date.fromisoformat(valid_until_raw)
                    except ValueError:
                        flash("Invalid quote validity date.", "warning")
                        return redirect(url_for("crm_lead_detail", lead_id=lead_id))

                now_utc = dt.datetime.utcnow()
                row = LeadQuote(
                    lead_id=lead.id,
                    title=title,
                    fee_model=fee_model,
                    currency=(request.form.get("currency") or "ZAR").strip().upper() or "ZAR",
                    estimated_amount=round(float(estimated_amount), 2),
                    estimated_hours=round(float(estimated_hours), 2) if estimated_hours and estimated_hours > 0 else None,
                    hourly_rate=round(float(hourly_rate), 2) if hourly_rate and hourly_rate > 0 else None,
                    disbursement_estimate=round(float(disbursement_estimate), 2),
                    tax_rate=round(float(tax_rate), 2),
                    scope_summary=(request.form.get("scope_summary") or "").strip() or None,
                    assumptions=(request.form.get("assumptions") or "").strip() or None,
                    valid_until=valid_until,
                    status="draft",
                    created_by=current_user.id,
                    created_at=now_utc,
                    updated_at=now_utc,
                )
                db.session.add(row)

                lead.updated_at = now_utc
                if lead.stage in {"new", "contacted", "qualified"}:
                    lead.stage = "proposal"
                db.session.commit()
                totals = _quote_financials(row)
                audit(
                    "crm_quote_create",
                    "LeadQuote",
                    row.id,
                    {
                        "lead_id": lead.id,
                        "fee_model": row.fee_model,
                        "status": row.status,
                        "grand_total": totals["grand_total"],
                        "currency": row.currency,
                    },
                )
                flash("Quote draft saved.", "info")

            return redirect(url_for("crm_lead_detail", lead_id=lead_id))

        followups = CRMFollowUp.query.filter_by(lead_id=lead.id).order_by(CRMFollowUp.due_at.asc()).all()
        quotes = LeadQuote.query.filter_by(lead_id=lead.id).order_by(LeadQuote.created_at.desc(), LeadQuote.id.desc()).all()
        intakes = IntakeForm.query.filter_by(lead_id=lead.id).order_by(IntakeForm.created_at.desc()).all()
        conflicts = (
            ConflictCheck.query.filter(ConflictCheck.intake_form_id.in_([i.id for i in intakes]))
            .order_by(ConflictCheck.created_at.desc())
            .all()
            if intakes
            else []
        )
        matter_query = Matter.query
        if not is_admin():
            scope_ids = visible_matter_ids()
            if scope_ids:
                matter_query = matter_query.filter(Matter.id.in_(scope_ids))
            else:
                matter_query = matter_query.filter(Matter.id == -1)
        matters = matter_query.order_by(Matter.opened_at.desc()).limit(200).all()
        letters = (
            EngagementLetter.query.filter(EngagementLetter.matter_id.in_([m.id for m in matters]))
            .order_by(EngagementLetter.created_at.desc())
            .limit(100)
            .all()
        )
        conflict_ids = [conflict.id for conflict in conflicts]
        semantic_hits_by_conflict: dict[int, list[ConflictSemanticHit]] = defaultdict(list)
        if conflict_ids:
            semantic_rows = (
                ConflictSemanticHit.query.filter(ConflictSemanticHit.conflict_check_id.in_(conflict_ids))
                .order_by(
                    ConflictSemanticHit.conflict_check_id.asc(),
                    ConflictSemanticHit.semantic_rank.asc(),
                    ConflictSemanticHit.similarity_score.desc(),
                )
                .all()
            )
            for row in semantic_rows:
                semantic_hits_by_conflict[int(row.conflict_check_id)].append(row)

        conflict_meta: dict[int, dict] = {}
        for conflict in conflicts:
            payload = {}
            if conflict.result_json:
                try:
                    payload = json.loads(conflict.result_json)
                except json.JSONDecodeError:
                    payload = {}
            direct_matches = payload.get("matches", []) if isinstance(payload.get("matches"), list) else []
            semantic_hits = semantic_hits_by_conflict.get(conflict.id, [])
            semantic_status = str(payload.get("semantic_status") or ("completed" if semantic_hits else "not_requested"))
            semantic_hit_count = int(payload.get("semantic_hit_count") or len(semantic_hits))
            top_semantic_score = max((float(row.similarity_score or 0.0) for row in semantic_hits), default=0.0)
            if top_semantic_score <= 0 and isinstance(payload.get("semantic_hits"), list):
                top_semantic_score = max(
                    (
                        float(item.get("similarity_score") or 0.0)
                        for item in payload.get("semantic_hits", [])
                        if isinstance(item, dict)
                    ),
                    default=0.0,
                )
            conflict_meta[conflict.id] = {
                "direct_match_count": len(direct_matches),
                "semantic_status": semantic_status,
                "semantic_hit_count": semantic_hit_count,
                "top_semantic_score": round(top_semantic_score, 2),
            }

        matter_lookup = {matter.id: matter for matter in matters}
        missing_matter_ids = {
            int(hit.matter_id)
            for hits in semantic_hits_by_conflict.values()
            for hit in hits
            if hit.matter_id and int(hit.matter_id) not in matter_lookup
        }
        if missing_matter_ids:
            for row in Matter.query.filter(Matter.id.in_(missing_matter_ids)).all():
                matter_lookup[row.id] = row

        quote_financials = {row.id: _quote_financials(row) for row in quotes}
        quote_status_counts = {status: 0 for status in QUOTE_STATUSES}
        for row in quotes:
            normalized = (row.status or "draft").strip().lower()
            if normalized in quote_status_counts:
                quote_status_counts[normalized] += 1
        followup_default_due_at = (dt.datetime.now() + dt.timedelta(days=1)).replace(second=0, microsecond=0)
        selected_matter_id = get_active_matter_id()
        if selected_matter_id and selected_matter_id not in {matter.id for matter in matters}:
            selected_matter = db.session.get(Matter, selected_matter_id)
            if selected_matter is not None and can_access_matter(selected_matter.id):
                matters = [selected_matter] + matters
                matter_lookup[selected_matter.id] = selected_matter

        return page(
            "Lead Detail",
            "crm/lead_detail.html",
            lead=lead,
            followups=followups,
            quotes=quotes,
            quote_financials=quote_financials,
            quote_statuses=QUOTE_STATUSES,
            quote_fee_models=QUOTE_FEE_MODELS,
            quote_status_counts=quote_status_counts,
            intakes=intakes,
            conflicts=conflicts,
            conflict_meta=conflict_meta,
            semantic_hits_by_conflict=semantic_hits_by_conflict,
            matters=matters,
            matter_lookup=matter_lookup,
            letters=letters,
            stages=LEAD_STAGES,
            followup_default_due_at=followup_default_due_at.strftime("%Y-%m-%dT%H:%M"),
            selected_matter_id=selected_matter_id,
        )

    @app.post("/crm/followups/<int:followup_id>/status")
    @login_required
    def crm_followup_status(followup_id: int):
        enforce_permission("crm", "write")
        followup = db.session.get(CRMFollowUp, followup_id)
        if not followup:
            abort(404)
        lead = db.session.get(CRMLead, followup.lead_id)
        if not lead:
            abort(404)

        next_status = (request.form.get("status") or "").strip().lower()
        allowed_statuses = {"open", "done", "cancelled"}
        if next_status not in allowed_statuses:
            flash("Invalid follow-up status.", "warning")
            return redirect(url_for("crm_lead_detail", lead_id=lead.id))

        due_raw = (request.form.get("due_at") or "").strip()
        due_at = followup.due_at
        if due_raw:
            try:
                due_at = dt.datetime.fromisoformat(due_raw)
            except ValueError:
                flash("Invalid follow-up due date.", "warning")
                return redirect(url_for("crm_lead_detail", lead_id=lead.id))

        followup.status = next_status
        followup.due_at = due_at
        lead.updated_at = dt.datetime.utcnow()
        db.session.commit()
        audit(
            "crm_followup_status_update",
            "CRMFollowUp",
            followup.id,
            {"status": followup.status, "lead_id": lead.id},
        )
        flash("Follow-up updated.", "info")
        next_path = _safe_next_path(request.form.get("next"), url_for("crm_lead_detail", lead_id=lead.id))
        return redirect(next_path)

    @app.post("/crm/quotes/<int:quote_id>/status")
    @login_required
    def crm_quote_status(quote_id: int):
        enforce_permission("crm", "write")
        row = db.session.get(LeadQuote, quote_id)
        if not row:
            abort(404)
        lead = db.session.get(CRMLead, row.lead_id)
        if not lead:
            abort(404)

        next_status = (request.form.get("status") or "").strip().lower()
        if next_status not in QUOTE_STATUSES:
            flash("Invalid quote status.", "warning")
            return redirect(url_for("crm_lead_detail", lead_id=lead.id))

        note = (request.form.get("status_note") or "").strip() or None
        now_utc = dt.datetime.utcnow()
        previous = (row.status or "draft").strip().lower()
        row.status = next_status
        row.updated_at = now_utc
        if note:
            row.status_note = note
        if next_status == "sent" and row.sent_at is None:
            row.sent_at = now_utc
        if next_status in {"accepted", "rejected", "expired"}:
            row.decided_at = now_utc
            row.decided_by = current_user.id
        if next_status == "accepted" and lead.stage != "retained":
            lead.stage = "retained"
        lead.updated_at = now_utc
        db.session.commit()

        totals = _quote_financials(row)
        audit(
            "crm_quote_status_update",
            "LeadQuote",
            row.id,
            {
                "lead_id": lead.id,
                "from": previous,
                "to": next_status,
                "currency": row.currency,
                "grand_total": totals["grand_total"],
            },
        )
        flash("Quote status updated.", "info")
        return redirect(url_for("crm_lead_detail", lead_id=lead.id))

    @app.post("/crm/conflicts/check")
    @login_required
    def crm_conflicts_check():
        enforce_permission("crm", "conflict_check")
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
            queued_check_id = None
            try:
                queued_check_id = ConflictEngine.enqueue_semantic_scan(
                    intake_id,
                    requested_by=current_user.id,
                    conflict_check_id=report.conflict_check_id,
                )
            except Exception:  # pragma: no cover - resilience guard for queue/database contention
                db.session.rollback()
                current_app.logger.exception("Failed to queue semantic conflict scan for intake_id=%s", intake_id)
            if queued_check_id is not None:
                audit(
                    "conflict_semantic_queued",
                    "ConflictCheck",
                    queued_check_id,
                    {"intake_id": intake_id, "source": "manual_conflict_run"},
                )
                flash("Semantic conflict scan queued in background.", "info")
        flash(f"Conflict check status: {report.status}", "info")
        intake = db.session.get(IntakeForm, intake_id)
        if intake and intake.lead_id:
            return redirect(url_for("crm_lead_detail", lead_id=intake.lead_id))
        return redirect(url_for("crm_leads"))

    @app.post("/crm/conflicts/<int:conflict_id>/override")
    @login_required
    def crm_conflict_override(conflict_id: int):
        enforce_permission("crm", "override")
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
        enforce_permission("crm", "export")
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
        enforce_permission("crm", "sign_engagement")
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
        onboarding_task_ids = create_engagement_signed_tasks(
            row.id,
            actor_user_id=current_user.id,
        )
        db.session.commit()
        audit(
            "engagement_signed",
            "EngagementLetter",
            row.id,
            {"signed_by": signer_name, "onboarding_task_ids": onboarding_task_ids},
        )
        if onboarding_task_ids:
            flash(
                f"Engagement signed and kickoff task(s) created: {', '.join(f'#{task_id}' for task_id in onboarding_task_ids)}.",
                "info",
            )
        else:
            flash("Engagement signed.", "info")
        return redirect(url_for("matter_workspace", matter_id=row.matter_id))
