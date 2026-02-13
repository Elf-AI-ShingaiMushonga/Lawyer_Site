from __future__ import annotations

import datetime as dt
from urllib.parse import urlparse

from flask import abort, flash, redirect, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..helpers import audit, can_access_matter
from ..models import Deadline, Matter, MatterTimelineEvent
from ..policies import visible_matter_ids
from ..templates import page


CALENDAR_FILTERS = {"all", "overdue", "today", "next7", "critical", "open", "ack"}


def _normalize_calendar_filter(value: str | None) -> str:
    key = (value or "all").strip().lower()
    return key if key in CALENDAR_FILTERS else "all"


def _deadline_matches_filter(deadline: Deadline, filter_key: str, today: dt.date, week_end: dt.date) -> bool:
    if filter_key == "overdue":
        return deadline.status != "acknowledged" and deadline.due_at < today
    if filter_key == "today":
        return deadline.due_at == today
    if filter_key == "next7":
        return today <= deadline.due_at <= week_end
    if filter_key == "critical":
        return bool(deadline.is_critical)
    if filter_key == "open":
        return deadline.status != "acknowledged"
    if filter_key == "ack":
        return deadline.status == "acknowledged"
    return True


def _deadline_stats(deadlines: list[Deadline], today: dt.date, week_end: dt.date) -> dict[str, int]:
    return {
        "total": len(deadlines),
        "overdue": sum(1 for d in deadlines if d.status != "acknowledged" and d.due_at < today),
        "today": sum(1 for d in deadlines if d.due_at == today),
        "next7": sum(1 for d in deadlines if today <= d.due_at <= week_end),
        "critical": sum(1 for d in deadlines if d.is_critical),
        "open": sum(1 for d in deadlines if d.status != "acknowledged"),
        "ack": sum(1 for d in deadlines if d.status == "acknowledged"),
    }


def _safe_calendar_redirect(default_url: str) -> str:
    next_raw = (request.form.get("next") or request.args.get("next") or "").strip()
    if next_raw.startswith("/") and not next_raw.startswith("//"):
        return next_raw

    ref = (request.referrer or "").strip()
    if ref:
        parsed = urlparse(ref)
        if (not parsed.netloc or parsed.netloc == request.host) and parsed.path.startswith("/"):
            return f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path

    return default_url


def _parse_date_arg(value: str | None, fallback: dt.date) -> dt.date:
    raw = (value or "").strip()
    if not raw:
        return fallback
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        return fallback


def register_calendar_routes(app):
    @app.get("/calendar/my")
    @login_required
    def calendar_my():
        today = dt.date.today()
        week_end = today + dt.timedelta(days=7)
        active_filter = _normalize_calendar_filter(request.args.get("filter"))
        matter_ids = visible_matter_ids()
        all_deadlines = (
            Deadline.query.filter(Deadline.matter_id.in_(matter_ids))
            .order_by(Deadline.due_at.asc())
            .limit(200)
            .all()
            if matter_ids
            else []
        )
        deadlines = [d for d in all_deadlines if _deadline_matches_filter(d, active_filter, today, week_end)]
        stats = _deadline_stats(all_deadlines, today, week_end)
        matter_ids_for_view = sorted({int(d.matter_id) for d in all_deadlines if d.matter_id is not None})
        matter_by_id = (
            {row.id: row for row in Matter.query.filter(Matter.id.in_(matter_ids_for_view)).all()}
            if matter_ids_for_view
            else {}
        )
        return page(
            "My Calendar",
            "calendar/my.html",
            deadlines=deadlines,
            active_filter=active_filter,
            stats=stats,
            today=today,
            matter_by_id=matter_by_id,
        )

    @app.get("/calendar/team")
    @login_required
    def calendar_team():
        today = dt.date.today()
        week_end = today + dt.timedelta(days=7)
        active_filter = _normalize_calendar_filter(request.args.get("filter"))
        if current_user.role == "admin":
            all_deadlines = Deadline.query.order_by(Deadline.due_at.asc()).limit(300).all()
        else:
            matter_ids = visible_matter_ids()
            all_deadlines = (
                Deadline.query.filter(Deadline.matter_id.in_(matter_ids)).order_by(Deadline.due_at.asc()).limit(300).all()
                if matter_ids
                else []
            )
        deadlines = [d for d in all_deadlines if _deadline_matches_filter(d, active_filter, today, week_end)]
        stats = _deadline_stats(all_deadlines, today, week_end)
        matter_ids_for_view = sorted({int(d.matter_id) for d in all_deadlines if d.matter_id is not None})
        matter_by_id = (
            {row.id: row for row in Matter.query.filter(Matter.id.in_(matter_ids_for_view)).all()}
            if matter_ids_for_view
            else {}
        )
        return page(
            "Team Calendar",
            "calendar/team.html",
            deadlines=deadlines,
            active_filter=active_filter,
            stats=stats,
            today=today,
            matter_by_id=matter_by_id,
        )

    @app.get("/calendar/milestones/report")
    @login_required
    def calendar_milestone_report():
        today = dt.date.today()
        default_start = today
        default_end = today + dt.timedelta(days=90)
        range_start = _parse_date_arg(request.args.get("start"), default_start)
        range_end = _parse_date_arg(request.args.get("end"), default_end)
        if range_end < range_start:
            range_end = range_start

        requested_scope = (request.args.get("scope") or "my").strip().lower()
        scope = "team" if requested_scope == "team" and current_user.role == "admin" else "my"

        matter_query = Matter.query
        if scope == "my" or current_user.role != "admin":
            scoped_ids = visible_matter_ids()
            if scoped_ids:
                matter_query = matter_query.filter(Matter.id.in_(scoped_ids))
            else:
                matter_query = matter_query.filter(Matter.id == -1)
        matters = matter_query.order_by(Matter.last_updated_at.desc()).limit(500).all()
        matter_ids = [matter.id for matter in matters]

        milestones = (
            MatterTimelineEvent.query.filter(
                MatterTimelineEvent.matter_id.in_(matter_ids),
                MatterTimelineEvent.is_milestone.is_(True),
            ).all()
            if matter_ids
            else []
        )
        deadlines = (
            Deadline.query.filter(Deadline.matter_id.in_(matter_ids)).all()
            if matter_ids
            else []
        )

        milestones_by_matter: dict[int, list[MatterTimelineEvent]] = {}
        for milestone in milestones:
            milestones_by_matter.setdefault(int(milestone.matter_id), []).append(milestone)
        deadlines_by_matter: dict[int, list[Deadline]] = {}
        for deadline in deadlines:
            deadlines_by_matter.setdefault(int(deadline.matter_id), []).append(deadline)

        summaries: list[dict] = []
        for matter in matters:
            matter_milestones = milestones_by_matter.get(matter.id, [])
            matter_deadlines = deadlines_by_matter.get(matter.id, [])

            in_range_milestones = [
                row for row in matter_milestones if range_start <= row.event_date <= range_end
            ]
            upcoming_milestones = [row for row in matter_milestones if row.event_date >= today]
            open_deadlines = [row for row in matter_deadlines if row.status != "acknowledged"]
            overdue_deadlines = [row for row in open_deadlines if row.due_at < today]
            critical_open = [row for row in open_deadlines if row.is_critical]

            next_candidates = [row.event_date for row in upcoming_milestones]
            next_candidates.extend(row.due_at for row in open_deadlines if row.due_at >= today)
            next_key_date = min(next_candidates) if next_candidates else None

            summaries.append(
                {
                    "matter": matter,
                    "milestones_in_range": len(in_range_milestones),
                    "upcoming_milestones": len(upcoming_milestones),
                    "open_deadlines": len(open_deadlines),
                    "overdue_deadlines": len(overdue_deadlines),
                    "critical_open_deadlines": len(critical_open),
                    "next_key_date": next_key_date,
                }
            )

        summaries.sort(
            key=lambda row: (
                -int(row["overdue_deadlines"]),
                -int(row["critical_open_deadlines"]),
                row["next_key_date"] or dt.date.max,
                str(row["matter"].matter_no),
            )
        )

        totals = {
            "matters": len(summaries),
            "milestones_in_range": sum(int(row["milestones_in_range"]) for row in summaries),
            "upcoming_milestones": sum(int(row["upcoming_milestones"]) for row in summaries),
            "open_deadlines": sum(int(row["open_deadlines"]) for row in summaries),
            "overdue_deadlines": sum(int(row["overdue_deadlines"]) for row in summaries),
            "critical_open_deadlines": sum(int(row["critical_open_deadlines"]) for row in summaries),
        }

        return page(
            "Milestone Report",
            "calendar/milestone_report.html",
            summaries=summaries,
            totals=totals,
            range_start=range_start,
            range_end=range_end,
            scope=scope,
            today=today,
        )

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

        today = dt.date.today()
        week_end = today + dt.timedelta(days=7)
        active_filter = _normalize_calendar_filter(request.args.get("filter"))
        all_deadlines = Deadline.query.filter_by(matter_id=matter_id).order_by(Deadline.due_at.asc()).all()
        deadlines = [d for d in all_deadlines if _deadline_matches_filter(d, active_filter, today, week_end)]
        stats = _deadline_stats(all_deadlines, today, week_end)
        timeline = (
            MatterTimelineEvent.query.filter_by(matter_id=matter_id)
            .order_by(MatterTimelineEvent.event_date.asc())
            .limit(200)
            .all()
        )
        return page(
            "Matter Calendar",
            "calendar/matter.html",
            m=m,
            deadlines=deadlines,
            timeline=timeline,
            active_filter=active_filter,
            stats=stats,
            today=today,
        )

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
        return redirect(_safe_calendar_redirect(url_for("calendar_matter", matter_id=row.matter_id)))

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
        return redirect(_safe_calendar_redirect(url_for("calendar_matter", matter_id=row.matter_id)))
