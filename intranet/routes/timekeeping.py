from __future__ import annotations

import datetime as dt

from flask import abort, flash, jsonify, redirect, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import and_, or_

from ..extensions import db
from ..helpers import audit, can_access_matter
from ..models import Matter, TimeEntry, TimeRoundingPolicy, TimeTimer, TimeValidationEvent
from ..templates import page


def _active_timer_for_user(user_id: int) -> TimeTimer | None:
    return TimeTimer.query.filter_by(user_id=user_id, status="running").order_by(TimeTimer.started_at.desc()).first()


def _round_hours(hours: float, increment: float) -> float:
    if increment <= 0:
        return hours
    steps = round(hours / increment)
    return round(steps * increment, 4)


def _policy_for_matter(matter_id: int) -> TimeRoundingPolicy | None:
    matter = db.session.get(Matter, matter_id)
    if matter is None:
        return None
    policy = TimeRoundingPolicy.query.filter_by(matter_id=matter_id, is_active=True).order_by(TimeRoundingPolicy.id.desc()).first()
    if policy:
        return policy
    return TimeRoundingPolicy.query.filter_by(client_name=matter.client_name, is_active=True).order_by(TimeRoundingPolicy.id.desc()).first()


def _validate_time_entry(entry: TimeEntry, policy: TimeRoundingPolicy | None) -> list[str]:
    issues: list[str] = []
    if policy and policy.min_narrative_length and len((entry.narrative or "").strip()) < int(policy.min_narrative_length):
        issues.append(f"Narrative must be at least {policy.min_narrative_length} characters")
    if policy and policy.require_activity_code and not (entry.activity_code or "").strip():
        issues.append("Activity code required")

    overlap = (
        TimeEntry.query.filter(
            TimeEntry.user_id == entry.user_id,
            TimeEntry.id != entry.id,
            TimeEntry.start_at < (entry.end_at or entry.start_at),
            or_(TimeEntry.end_at.is_(None), TimeEntry.end_at > entry.start_at),
        )
        .limit(1)
        .first()
    )
    if overlap:
        issues.append(f"Overlaps with entry #{overlap.id}")

    if policy and policy.daily_hour_cap:
        day_start = dt.datetime.combine(entry.start_at.date(), dt.time.min)
        day_end = dt.datetime.combine(entry.start_at.date(), dt.time.max)
        day_total = (
            db.session.query(db.func.coalesce(db.func.sum(TimeEntry.rounded_hours), 0.0))
            .filter(TimeEntry.user_id == entry.user_id, TimeEntry.start_at >= day_start, TimeEntry.start_at <= day_end)
            .scalar()
            or 0.0
        )
        if float(day_total) > float(policy.daily_hour_cap):
            issues.append(f"Daily cap exceeded ({policy.daily_hour_cap}h)")

    return issues


def register_timekeeping_routes(app):
    @app.get("/time/timers")
    @login_required
    def time_timers():
        timers = TimeTimer.query.filter_by(user_id=current_user.id).order_by(TimeTimer.updated_at.desc()).limit(50).all()
        return page("Timers", "timekeeping/timers.html", timers=timers)

    @app.post("/time/timers/start")
    @login_required
    def time_timer_start():
        matter_id = request.form.get("matter_id", type=int)
        if matter_id and not can_access_matter(matter_id):
            abort(403)

        running = _active_timer_for_user(current_user.id)
        if running:
            running.status = "paused"
            running.paused_at = dt.datetime.utcnow()

        timer = TimeTimer(
            user_id=current_user.id,
            matter_id=matter_id,
            task_id=request.form.get("task_id", type=int),
            label=(request.form.get("label") or "").strip() or None,
            started_at=dt.datetime.utcnow(),
            status="running",
        )
        db.session.add(timer)
        db.session.commit()
        audit("timer_start", "TimeTimer", timer.id)
        flash("Timer started.", "info")
        return redirect(url_for("time_timers"))

    @app.post("/time/timers/pause")
    @login_required
    def time_timer_pause():
        timer_id = request.form.get("timer_id", type=int)
        timer = db.session.get(TimeTimer, timer_id) if timer_id else _active_timer_for_user(current_user.id)
        if not timer or timer.user_id != current_user.id:
            flash("Timer not found.", "warning")
            return redirect(url_for("time_timers"))

        if timer.status == "running" and timer.started_at:
            elapsed = int((dt.datetime.utcnow() - timer.started_at).total_seconds())
            timer.elapsed_seconds = int(timer.elapsed_seconds or 0) + max(0, elapsed)
        timer.status = "paused"
        timer.paused_at = dt.datetime.utcnow()
        db.session.commit()
        audit("timer_pause", "TimeTimer", timer.id)
        flash("Timer paused.", "info")
        return redirect(url_for("time_timers"))

    @app.post("/time/timers/switch")
    @login_required
    def time_timer_switch():
        matter_id = request.form.get("matter_id", type=int)
        if matter_id and not can_access_matter(matter_id):
            abort(403)

        running = _active_timer_for_user(current_user.id)
        if running and running.started_at:
            elapsed = int((dt.datetime.utcnow() - running.started_at).total_seconds())
            running.elapsed_seconds = int(running.elapsed_seconds or 0) + max(0, elapsed)
            running.status = "paused"
            running.paused_at = dt.datetime.utcnow()

        timer = TimeTimer(
            user_id=current_user.id,
            matter_id=matter_id,
            task_id=request.form.get("task_id", type=int),
            label=(request.form.get("label") or "").strip() or None,
            started_at=dt.datetime.utcnow(),
            status="running",
        )
        db.session.add(timer)
        db.session.commit()
        audit("timer_switch", "TimeTimer", timer.id)
        flash("Timer switched.", "info")
        return redirect(url_for("time_timers"))

    @app.post("/time/offline-sync")
    @login_required
    def time_offline_sync():
        payload = request.get_json(silent=True) or {}
        entries = payload.get("entries")
        if not isinstance(entries, list):
            return jsonify({"ok": False, "error": "entries list is required"}), 400

        created = 0
        skipped = 0
        errors: list[dict] = []
        for idx, item in enumerate(entries):
            if not isinstance(item, dict):
                errors.append({"index": idx, "error": "entry must be an object"})
                continue

            matter_id = item.get("matter_id")
            if not isinstance(matter_id, int) or matter_id <= 0:
                errors.append({"index": idx, "error": "matter_id is required"})
                continue
            if not can_access_matter(matter_id):
                errors.append({"index": idx, "error": "access denied for matter"})
                continue

            start_raw = str(item.get("start_at") or "").strip()
            end_raw = str(item.get("end_at") or "").strip()
            if not start_raw or not end_raw:
                errors.append({"index": idx, "error": "start_at and end_at are required"})
                continue
            try:
                start_at = dt.datetime.fromisoformat(start_raw)
                end_at = dt.datetime.fromisoformat(end_raw)
            except ValueError:
                errors.append({"index": idx, "error": "invalid datetime format"})
                continue
            if end_at <= start_at:
                errors.append({"index": idx, "error": "end_at must be after start_at"})
                continue

            narrative = str(item.get("narrative") or "").strip() or None
            duplicate = (
                TimeEntry.query.filter(
                    TimeEntry.user_id == current_user.id,
                    TimeEntry.matter_id == matter_id,
                    TimeEntry.start_at == start_at,
                    TimeEntry.end_at == end_at,
                    TimeEntry.narrative == narrative,
                )
                .limit(1)
                .first()
            )
            if duplicate is not None:
                skipped += 1
                continue

            hours = (end_at - start_at).total_seconds() / 3600.0
            policy = _policy_for_matter(matter_id)
            rounded = _round_hours(hours, float(policy.increment_hours if policy else 0.1))
            task_id_raw = item.get("task_id")
            is_billable_raw = item.get("is_billable", True)
            if isinstance(is_billable_raw, str):
                is_billable = is_billable_raw.strip().lower() in {"1", "true", "yes", "on"}
            else:
                is_billable = bool(is_billable_raw)
            entry = TimeEntry(
                user_id=current_user.id,
                matter_id=matter_id,
                task_id=(int(task_id_raw) if isinstance(task_id_raw, int) or str(task_id_raw).isdigit() else None),
                start_at=start_at,
                end_at=end_at,
                hours=round(hours, 4),
                rounded_hours=rounded,
                narrative=narrative,
                task_code=str(item.get("task_code") or "").strip() or None,
                activity_code=str(item.get("activity_code") or "").strip() or None,
                is_billable=is_billable,
                status="draft",
            )
            db.session.add(entry)
            db.session.flush()
            issues = _validate_time_entry(entry, policy)
            for issue in issues:
                db.session.add(TimeValidationEvent(time_entry_id=entry.id, event_type="validation", message=issue))
            if issues:
                entry.status = "needs_review"
            created += 1

        db.session.commit()
        audit("time_offline_sync", "TimeEntry", None, {"created": created, "skipped": skipped, "errors": len(errors)})
        return jsonify({"ok": True, "created": created, "skipped": skipped, "errors": errors}), 200

    @app.route("/time/entries", methods=["GET", "POST"])
    @login_required
    def time_entries():
        if request.method == "POST":
            matter_id = request.form.get("matter_id", type=int)
            if not matter_id or not can_access_matter(matter_id):
                abort(403)

            start_raw = (request.form.get("start_at") or "").strip()
            end_raw = (request.form.get("end_at") or "").strip()
            narrative = (request.form.get("narrative") or "").strip()

            try:
                start_at = dt.datetime.fromisoformat(start_raw)
                end_at = dt.datetime.fromisoformat(end_raw) if end_raw else None
            except ValueError:
                flash("Invalid datetime. Use ISO format.", "warning")
                return redirect(url_for("time_entries"))

            if end_at and end_at <= start_at:
                flash("End time must be after start time.", "warning")
                return redirect(url_for("time_entries"))

            hours = ((end_at - start_at).total_seconds() / 3600.0) if end_at else 0.0
            policy = _policy_for_matter(matter_id)
            rounded = _round_hours(hours, float(policy.increment_hours if policy else 0.1))

            entry = TimeEntry(
                user_id=current_user.id,
                matter_id=matter_id,
                task_id=request.form.get("task_id", type=int),
                start_at=start_at,
                end_at=end_at,
                hours=round(hours, 4),
                rounded_hours=rounded,
                narrative=narrative or None,
                task_code=(request.form.get("task_code") or "").strip() or None,
                activity_code=(request.form.get("activity_code") or "").strip() or None,
                is_billable=(request.form.get("is_billable") or "").lower() in {"1", "true", "yes", "on"},
                status="draft",
            )
            db.session.add(entry)
            db.session.flush()

            issues = _validate_time_entry(entry, policy)
            for issue in issues:
                db.session.add(TimeValidationEvent(time_entry_id=entry.id, event_type="validation", message=issue))

            if issues:
                entry.status = "needs_review"
            db.session.commit()
            audit("time_entry_create", "TimeEntry", entry.id, {"issues": issues})
            flash("Time entry saved." if not issues else "Time entry saved with validation issues.", "info")
            return redirect(url_for("time_entries"))

        entries = TimeEntry.query.filter_by(user_id=current_user.id).order_by(TimeEntry.start_at.desc()).limit(200).all()
        validations = TimeValidationEvent.query.order_by(TimeValidationEvent.created_at.desc()).limit(200).all()
        matters = Matter.query.order_by(Matter.opened_at.desc()).limit(200).all()
        return page("Time Entries", "timekeeping/entries.html", entries=entries, validations=validations, matters=matters)

    @app.route("/time/review", methods=["GET", "POST"])
    @login_required
    def time_review():
        if request.method == "POST":
            entry_id = request.form.get("entry_id", type=int)
            state = (request.form.get("state") or "approved").strip().lower()
            entry = db.session.get(TimeEntry, entry_id) if entry_id else None
            if not entry:
                flash("Entry not found.", "warning")
                return redirect(url_for("time_review"))
            if not can_access_matter(entry.matter_id):
                abort(403)

            if state not in {"approved", "rejected", "draft"}:
                flash("Invalid state.", "warning")
                return redirect(url_for("time_review"))

            entry.status = state
            if state == "approved":
                entry.approved_by = current_user.id
                entry.approved_at = dt.datetime.utcnow()
            db.session.commit()
            audit("time_entry_review", "TimeEntry", entry.id, {"state": state})
            flash("Review saved.", "info")
            return redirect(url_for("time_review"))

        rows = TimeEntry.query.order_by(TimeEntry.created_at.desc()).limit(300).all()
        scoped_rows = [row for row in rows if can_access_matter(row.matter_id)]
        return page("Time Review", "timekeeping/review.html", entries=scoped_rows)

    @app.post("/time/entries/<int:entry_id>/lock")
    @login_required
    def time_entry_lock(entry_id: int):
        entry = db.session.get(TimeEntry, entry_id)
        if not entry:
            abort(404)
        if not can_access_matter(entry.matter_id):
            abort(403)
        if entry.status != "approved":
            flash("Only approved entries can be locked.", "warning")
            return redirect(url_for("time_review"))

        entry.locked_at = dt.datetime.utcnow()
        db.session.commit()
        audit("time_entry_lock", "TimeEntry", entry.id)
        flash("Time entry locked.", "info")
        return redirect(url_for("time_review"))
