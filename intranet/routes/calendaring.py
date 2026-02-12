from __future__ import annotations

import datetime as dt

from flask import abort, flash, redirect, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..helpers import audit, can_access_matter
from ..models import Deadline, Matter, MatterMember, MatterTimelineEvent
from ..policies import visible_matter_ids
from ..templates import page


def register_calendar_routes(app):
    @app.get("/calendar/my")
    @login_required
    def calendar_my():
        matter_ids = visible_matter_ids()
        deadlines = (
            Deadline.query.filter(Deadline.matter_id.in_(matter_ids))
            .order_by(Deadline.due_at.asc())
            .limit(200)
            .all()
            if matter_ids
            else []
        )
        return page("My Calendar", "calendar/my.html", deadlines=deadlines)

    @app.get("/calendar/team")
    @login_required
    def calendar_team():
        if current_user.role == "admin":
            deadlines = Deadline.query.order_by(Deadline.due_at.asc()).limit(300).all()
        else:
            matter_ids = visible_matter_ids()
            deadlines = (
                Deadline.query.filter(Deadline.matter_id.in_(matter_ids)).order_by(Deadline.due_at.asc()).limit(300).all()
                if matter_ids
                else []
            )
        return page("Team Calendar", "calendar/team.html", deadlines=deadlines)

    @app.route("/calendar/matter/<int:matter_id>", methods=["GET", "POST"])
    @login_required
    def calendar_matter(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        if request.method == "POST":
            action = (request.form.get("action") or "create_deadline").strip().lower()
            if action == "create_deadline":
                title = (request.form.get("title") or "").strip()
                due_raw = (request.form.get("due_at") or "").strip()
                if not title or not due_raw:
                    flash("Deadline title and due date are required.", "warning")
                    return redirect(url_for("calendar_matter", matter_id=matter_id))
                try:
                    due_at = dt.date.fromisoformat(due_raw)
                except ValueError:
                    flash("Invalid deadline date format.", "warning")
                    return redirect(url_for("calendar_matter", matter_id=matter_id))

                row = Deadline(
                    matter_id=matter_id,
                    task_id=request.form.get("task_id", type=int),
                    title=title,
                    due_at=due_at,
                    is_critical=(request.form.get("is_critical") or "").strip().lower() in {"1", "true", "yes", "on"},
                    status="open",
                    created_by=current_user.id,
                )
                db.session.add(row)
                db.session.commit()
                audit("deadline_create", "Deadline", row.id, {"matter_id": matter_id})
                flash("Deadline added.", "info")
                return redirect(url_for("calendar_matter", matter_id=matter_id))

            if action == "schedule_hearing":
                title = (request.form.get("title") or "").strip() or "Court appearance"
                date_raw = (request.form.get("event_date") or "").strip()
                if not date_raw:
                    flash("Hearing date is required.", "warning")
                    return redirect(url_for("calendar_matter", matter_id=matter_id))
                try:
                    event_date = dt.date.fromisoformat(date_raw)
                except ValueError:
                    flash("Invalid hearing date format.", "warning")
                    return redirect(url_for("calendar_matter", matter_id=matter_id))

                event = MatterTimelineEvent(
                    matter_id=matter_id,
                    event_date=event_date,
                    event_type="Hearing",
                    title=title,
                    description=(request.form.get("description") or "").strip() or None,
                    is_milestone=True,
                    created_by=current_user.id,
                )
                db.session.add(event)
                db.session.commit()
                audit("hearing_schedule", "MatterTimelineEvent", event.id, {"matter_id": matter_id})
                flash("Hearing scheduled.", "info")
                return redirect(url_for("calendar_matter", matter_id=matter_id))

            flash("Unsupported calendar action.", "warning")
            return redirect(url_for("calendar_matter", matter_id=matter_id))

        deadlines = Deadline.query.filter_by(matter_id=matter_id).order_by(Deadline.due_at.asc()).all()
        timeline = (
            MatterTimelineEvent.query.filter_by(matter_id=matter_id)
            .order_by(MatterTimelineEvent.event_date.asc())
            .limit(200)
            .all()
        )
        return page("Matter Calendar", "calendar/matter.html", m=m, deadlines=deadlines, timeline=timeline)

    @app.post("/deadlines/<int:deadline_id>/override")
    @login_required
    def deadline_override(deadline_id: int):
        row = db.session.get(Deadline, deadline_id)
        if not row:
            abort(404)
        if not can_access_matter(row.matter_id):
            abort(403)

        new_due_raw = (request.form.get("due_at") or "").strip()
        reason = (request.form.get("reason") or "").strip()
        if not new_due_raw or not reason:
            flash("New due date and reason are required.", "warning")
            return redirect(url_for("calendar_matter", matter_id=row.matter_id))

        try:
            new_due = dt.date.fromisoformat(new_due_raw)
        except ValueError:
            flash("Invalid date format.", "warning")
            return redirect(url_for("calendar_matter", matter_id=row.matter_id))

        row.due_at = new_due
        row.override_reason = reason
        row.overridden_by = current_user.id
        row.overridden_at = dt.datetime.utcnow()
        db.session.commit()
        audit("deadline_override", "Deadline", row.id, {"reason": reason, "new_due": new_due.isoformat()})
        flash("Deadline override saved.", "info")
        return redirect(url_for("calendar_matter", matter_id=row.matter_id))

    @app.post("/deadlines/<int:deadline_id>/ack")
    @login_required
    def deadline_ack(deadline_id: int):
        row = db.session.get(Deadline, deadline_id)
        if not row:
            abort(404)
        if not can_access_matter(row.matter_id):
            abort(403)

        row.acknowledged_by = current_user.id
        row.acknowledged_at = dt.datetime.utcnow()
        row.status = "acknowledged"
        db.session.commit()
        audit("deadline_ack", "Deadline", row.id)
        flash("Deadline acknowledged.", "info")
        return redirect(url_for("calendar_matter", matter_id=row.matter_id))
