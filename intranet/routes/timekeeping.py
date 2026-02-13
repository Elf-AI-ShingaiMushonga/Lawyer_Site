from __future__ import annotations

import datetime as dt

from flask import abort, flash, jsonify, redirect, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import and_, or_

from ..extensions import db
from ..helpers import audit, can_access_matter, is_admin
from ..models import FeeArrangement, Matter, RateCard, TimeEntry, TimeRoundingPolicy, TimeTimer, TimeValidationEvent
from ..policies import visible_matter_ids
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


def _scoped_matters_for_current_user(limit: int = 200) -> list[Matter]:
    query = Matter.query
    if not is_admin():
        scope_ids = visible_matter_ids()
        if scope_ids:
            query = query.filter(Matter.id.in_(scope_ids))
        else:
            return []
    return query.order_by(Matter.opened_at.desc()).limit(max(1, int(limit))).all()


def _resolve_rate_for_prompt(
    *,
    matter_id: int,
    user_id: int,
    as_of_date: dt.date | None,
) -> tuple[RateCard | None, float]:
    query = RateCard.query.filter(
        or_(RateCard.matter_id == matter_id, RateCard.matter_id.is_(None)),
        or_(RateCard.user_id == user_id, RateCard.user_id.is_(None)),
        RateCard.is_active.is_(True),
    )
    if as_of_date is not None:
        query = query.filter(
            or_(RateCard.effective_from.is_(None), RateCard.effective_from <= as_of_date),
            or_(RateCard.effective_to.is_(None), RateCard.effective_to >= as_of_date),
        )
    rate_card = (
        query.order_by(
            RateCard.matter_id.desc().nullslast(),
            RateCard.user_id.desc().nullslast(),
            RateCard.effective_from.desc().nullslast(),
            RateCard.id.desc(),
        )
        .limit(1)
        .first()
    )
    if rate_card is None:
        return None, 0.0

    resolved_rate = float(rate_card.rate_per_hour or 0.0)
    return rate_card, resolved_rate


def _parse_iso_datetime(raw: str | None) -> tuple[dt.datetime | None, str | None]:
    candidate = (raw or "").strip()
    if not candidate:
        return None, None
    try:
        return dt.datetime.fromisoformat(candidate), None
    except ValueError:
        return None, "Invalid datetime format. Use ISO format such as 2026-03-01T09:00:00."


def _as_bool(raw: str | None, default: bool = True) -> bool:
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


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
    @app.get("/time/prompts")
    @login_required
    def time_prompts():
        matter_id = request.args.get("matter_id", type=int)
        if not matter_id:
            return jsonify({"ok": True, "prompts": []}), 200
        if not can_access_matter(matter_id):
            abort(403)

        matter = db.session.get(Matter, matter_id)
        if matter is None:
            return jsonify({"ok": False, "error": "Matter not found."}), 404

        start_at, start_error = _parse_iso_datetime(request.args.get("start_at"))
        end_at, end_error = _parse_iso_datetime(request.args.get("end_at"))
        if start_error or end_error:
            return jsonify({"ok": False, "error": start_error or end_error}), 400
        if start_at and end_at and end_at <= start_at:
            return jsonify({"ok": False, "error": "End time must be after start time."}), 400

        policy = _policy_for_matter(matter_id)
        has_narrative = "narrative" in request.args
        has_activity_code = "activity_code" in request.args
        has_task_code = "task_code" in request.args
        narrative = (request.args.get("narrative") or "").strip()
        activity_code = (request.args.get("activity_code") or "").strip()
        task_code = (request.args.get("task_code") or "").strip()
        is_billable = _as_bool(request.args.get("is_billable"), default=True)

        prompts: list[dict[str, str]] = []
        if policy and policy.require_activity_code and has_activity_code and not activity_code:
            prompts.append(
                {
                    "level": "warning",
                    "code": "missing_activity_code",
                    "message": "This matter requires an activity code before entry approval.",
                }
            )
        if policy and policy.min_narrative_length and has_narrative:
            min_chars = int(policy.min_narrative_length)
            if len(narrative) < min_chars:
                missing = min_chars - len(narrative)
                prompts.append(
                    {
                        "level": "warning",
                        "code": "narrative_too_short",
                        "message": f"Narrative is {missing} characters short of the {min_chars}-character minimum.",
                    }
                )
        if has_task_code and not task_code:
            prompts.append(
                {
                    "level": "info",
                    "code": "missing_task_code",
                    "message": "Add a task code for cleaner LEDES and realization reporting.",
                }
            )

        rounded_hours = None
        if start_at and end_at:
            raw_hours = max(0.0, (end_at - start_at).total_seconds() / 3600.0)
            increment = float(policy.increment_hours if policy else 0.1)
            rounded_hours = _round_hours(raw_hours, increment)
            if policy and policy.daily_hour_cap:
                day_start = dt.datetime.combine(start_at.date(), dt.time.min)
                day_end = dt.datetime.combine(start_at.date(), dt.time.max)
                existing_day_total = (
                    db.session.query(db.func.coalesce(db.func.sum(TimeEntry.rounded_hours), 0.0))
                    .filter(
                        TimeEntry.user_id == current_user.id,
                        TimeEntry.start_at >= day_start,
                        TimeEntry.start_at <= day_end,
                    )
                    .scalar()
                    or 0.0
                )
                projected = float(existing_day_total) + float(rounded_hours)
                cap = float(policy.daily_hour_cap)
                if projected > cap:
                    prompts.append(
                        {
                            "level": "warning",
                            "code": "daily_cap_risk",
                            "message": f"Projected rounded total {projected:.2f}h exceeds daily cap {cap:.2f}h.",
                        }
                    )

            overlap = (
                TimeEntry.query.filter(
                    TimeEntry.user_id == current_user.id,
                    TimeEntry.start_at < end_at,
                    or_(TimeEntry.end_at.is_(None), TimeEntry.end_at > start_at),
                )
                .order_by(TimeEntry.start_at.desc())
                .limit(1)
                .first()
            )
            if overlap is not None:
                prompts.append(
                    {
                        "level": "warning",
                        "code": "overlap_detected",
                        "message": f"Time range overlaps with existing entry #{overlap.id}.",
                    }
                )

        rate_card, resolved_rate = _resolve_rate_for_prompt(
            matter_id=matter_id,
            user_id=current_user.id,
            as_of_date=(start_at.date() if start_at else dt.date.today()),
        )
        arrangement = FeeArrangement.query.filter_by(matter_id=matter_id).order_by(FeeArrangement.id.desc()).first()
        if arrangement is not None:
            arrangement_type = str(arrangement.arrangement_type or "").strip().lower()
            if arrangement_type in {"fixed", "capped", "blended"}:
                prompts.append(
                    {
                        "level": "info",
                        "code": "fee_arrangement_active",
                        "message": (
                            f"Fee arrangement '{arrangement_type}' is active. Capture narrative cleanly for adjustment transparency."
                        ),
                    }
                )

        preview: dict[str, str | float] | None = None
        if is_billable:
            if rate_card is None or resolved_rate <= 0:
                prompts.append(
                    {
                        "level": "warning",
                        "code": "missing_rate_card",
                        "message": "No active rate card found for this matter/user scope. Billable entries may price at 0.00.",
                    }
                )
            elif rounded_hours is not None:
                estimated = round(float(rounded_hours) * float(resolved_rate), 2)
                preview = {
                    "currency": (rate_card.currency or "ZAR").upper(),
                    "hourly_rate": round(float(resolved_rate), 2),
                    "rounded_hours": round(float(rounded_hours), 4),
                    "estimated_fee": estimated,
                }
                prompts.append(
                    {
                        "level": "info",
                        "code": "fee_preview",
                        "message": (
                            f"Estimated fee: {preview['currency']} {preview['estimated_fee']:.2f} "
                            f"({preview['rounded_hours']:.2f}h at {preview['hourly_rate']:.2f}/h)."
                        ),
                    }
                )

        payload = {
            "ok": True,
            "matter_id": matter_id,
            "prompts": prompts,
            "policy": {
                "increment_hours": float(policy.increment_hours) if policy else 0.1,
                "min_narrative_length": int(policy.min_narrative_length) if policy else 0,
                "require_activity_code": bool(policy.require_activity_code) if policy else False,
                "daily_hour_cap": float(policy.daily_hour_cap) if policy and policy.daily_hour_cap else None,
            },
            "fee_preview": preview,
        }
        return jsonify(payload), 200

    @app.get("/time/timers")
    @login_required
    def time_timers():
        timers = TimeTimer.query.filter_by(user_id=current_user.id).order_by(TimeTimer.updated_at.desc()).limit(50).all()
        matters = _scoped_matters_for_current_user(limit=250)
        matter_map = {matter.id: matter for matter in matters}
        missing_matter_ids = {int(timer.matter_id) for timer in timers if timer.matter_id and int(timer.matter_id) not in matter_map}
        if missing_matter_ids:
            for matter in Matter.query.filter(Matter.id.in_(missing_matter_ids)).all():
                if is_admin() or can_access_matter(matter.id):
                    matter_map[matter.id] = matter

        prefill_matter_id = request.args.get("matter_id", type=int)
        if prefill_matter_id and not can_access_matter(prefill_matter_id):
            prefill_matter_id = None
        prefill_task_id = request.args.get("task_id", type=int)
        prefill_label = (request.args.get("label") or "").strip()

        return page(
            "Timers",
            "timekeeping/timers.html",
            timers=timers,
            matters=matters,
            matter_map=matter_map,
            prefill_matter_id=prefill_matter_id,
            prefill_task_id=prefill_task_id,
            prefill_label=prefill_label,
        )

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
        validations = (
            TimeValidationEvent.query.join(TimeEntry, TimeEntry.id == TimeValidationEvent.time_entry_id)
            .filter(TimeEntry.user_id == current_user.id)
            .order_by(TimeValidationEvent.created_at.desc())
            .limit(200)
            .all()
        )
        matters = _scoped_matters_for_current_user(limit=250)
        matter_lookup = {matter.id: matter for matter in matters}
        missing_matter_ids = {int(entry.matter_id) for entry in entries if entry.matter_id and int(entry.matter_id) not in matter_lookup}
        if missing_matter_ids:
            for matter in Matter.query.filter(Matter.id.in_(missing_matter_ids)).all():
                if is_admin() or can_access_matter(matter.id):
                    matter_lookup[matter.id] = matter

        prefill_matter_id = request.args.get("matter_id", type=int)
        if prefill_matter_id and not can_access_matter(prefill_matter_id):
            prefill_matter_id = None
        prefill_task_id = request.args.get("task_id", type=int)

        default_end_dt = dt.datetime.utcnow().replace(second=0, microsecond=0)
        default_start_dt = default_end_dt - dt.timedelta(minutes=30)
        prefill_start_at_dt, start_error = _parse_iso_datetime(request.args.get("start_at"))
        prefill_end_at_dt, end_error = _parse_iso_datetime(request.args.get("end_at"))
        prefill_start_at = (
            (prefill_start_at_dt if prefill_start_at_dt and not start_error else default_start_dt).isoformat(timespec="minutes")
        )
        prefill_end_at = (
            (prefill_end_at_dt if prefill_end_at_dt and not end_error else default_end_dt).isoformat(timespec="minutes")
        )

        return page(
            "Time Entries",
            "timekeeping/entries.html",
            entries=entries,
            validations=validations,
            matters=matters,
            matter_lookup=matter_lookup,
            prefill_matter_id=prefill_matter_id,
            prefill_task_id=prefill_task_id,
            prefill_start_at=prefill_start_at,
            prefill_end_at=prefill_end_at,
        )

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
