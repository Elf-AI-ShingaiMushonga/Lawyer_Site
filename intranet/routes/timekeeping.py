from __future__ import annotations

import datetime as dt
from ..timeutils import utc_now
from typing import Any

from flask import abort, current_app, flash, jsonify, redirect, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import and_, or_
from werkzeug.utils import secure_filename

from ..extensions import db
from ..helpers import audit, can_access_matter, get_active_matter_id, is_admin, set_active_matter_context
from ..models import FeeArrangement, Matter, RateCard, Task, TimeEntry, TimeRoundingPolicy, TimeTimer, TimeValidationEvent
from ..policies import enforce_permission, visible_matter_ids
from ..services.assist_ai import suggest_time_entry_narrative
from ..services.timesheet_ai import parse_timesheet_image_entries
from ..services.workflow_automation import (
    capture_timer_to_draft_time_entry,
    ensure_draft_billing_item_for_time_entry,
)
from ..templates import page

_TIMESHEET_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
_TIMESHEET_IMAGE_MIME_PREFIX = "image/"


def _active_timer_for_user(user_id: int) -> TimeTimer | None:
    return TimeTimer.query.filter_by(user_id=user_id, status="running").order_by(TimeTimer.started_at.desc()).first()


def _single_timer_cap_seconds() -> int:
    cap_minutes = int(current_app.config.get("TIMER_SINGLE_CAP_MINUTES", 4 * 60) or 4 * 60)
    return max(5 * 60, cap_minutes * 60)


def _elapsed_seconds_for_timer(timer: TimeTimer, *, as_of: dt.datetime | None = None) -> int:
    total = max(0, int(timer.elapsed_seconds or 0))
    if timer.status == "running" and timer.started_at:
        now = as_of or utc_now()
        total += max(0, int((now - timer.started_at).total_seconds()))
    return total


def _pause_timer(
    timer: TimeTimer, *, as_of: dt.datetime | None = None, cap_seconds: int | None = None
) -> tuple[int, bool]:
    total = _elapsed_seconds_for_timer(timer, as_of=as_of)
    capped = False
    if cap_seconds is not None and total > cap_seconds:
        total = int(cap_seconds)
        capped = True
    timer.elapsed_seconds = max(0, int(total))
    timer.status = "paused"
    timer.paused_at = as_of or utc_now()
    return timer.elapsed_seconds, capped


def _request_prefers_html() -> bool:
    if request.path.startswith("/static/"):
        return False
    best = request.accept_mimetypes.best
    if best is None:
        return True
    return best in {"text/html", "application/xhtml+xml", "*/*"}


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


def _matter_is_closed(matter_id: int | None) -> bool:
    if not matter_id:
        return False
    row = db.session.get(Matter, int(matter_id))
    return row is not None and (row.status or "").strip().lower() == "closed"


def _scoped_matters_for_current_user(limit: int = 200) -> list[Matter]:
    query = Matter.query
    if not is_admin():
        scope_ids = visible_matter_ids()
        if scope_ids:
            query = query.filter(Matter.id.in_(scope_ids))
        else:
            return []
    return query.order_by(Matter.opened_at.desc()).limit(max(1, int(limit))).all()


def _build_time_task_options(*, matter_ids: list[int], include_task_id: int | None = None) -> dict[str, list[dict[str, object]]]:
    unique_matter_ids = sorted({int(matter_id) for matter_id in matter_ids if matter_id})
    if not unique_matter_ids:
        return {}

    filters: list[object] = [Task.matter_id.in_(unique_matter_ids)]
    if include_task_id:
        filters.append(or_(Task.status != "Done", Task.id == include_task_id))
    else:
        filters.append(Task.status != "Done")

    rows = (
        Task.query.filter(*filters)
        .order_by(Task.matter_id.asc(), Task.status.asc(), Task.due_date.asc().nullslast(), Task.id.desc())
        .limit(3000)
        .all()
    )

    payload: dict[str, list[dict[str, object]]] = {}
    counts_by_matter: dict[str, int] = {}
    for row in rows:
        matter_key = str(int(row.matter_id))
        count = counts_by_matter.get(matter_key, 0)
        if count >= 50 and int(row.id) != int(include_task_id or 0):
            continue
        payload.setdefault(matter_key, []).append(
            {
                "id": int(row.id),
                "title": row.title or f"Task {row.id}",
                "status": row.status or "",
                "due_date": row.due_date.isoformat() if row.due_date else "",
            }
        )
        counts_by_matter[matter_key] = count + 1

    return payload


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


def _parse_time_hhmm(raw: str | None) -> dt.time | None:
    candidate = (raw or "").strip()
    if not candidate:
        return None
    try:
        return dt.time.fromisoformat(candidate)
    except ValueError:
        return None


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


def _time_code_pair_label(task_code: str, activity_code: str) -> str:
    if task_code and activity_code:
        return f"{task_code} / {activity_code}"
    return task_code or activity_code


def _add_unique_string(target: list[str], seen: set[str], value: str, limit: int) -> None:
    if not value or value in seen:
        return
    target.append(value)
    seen.add(value)
    if len(target) > limit:
        removed = target.pop()
        seen.discard(removed)


def _add_unique_pair(
    target: list[dict[str, str]],
    seen: set[tuple[str, str]],
    *,
    task_code: str,
    activity_code: str,
    limit: int,
) -> dict[str, str] | None:
    if not task_code and not activity_code:
        return None
    pair_key = (task_code, activity_code)
    if pair_key in seen:
        return None

    pair = {
        "task_code": task_code,
        "activity_code": activity_code,
        "label": _time_code_pair_label(task_code, activity_code),
    }
    target.append(pair)
    seen.add(pair_key)
    if len(target) > limit:
        removed = target.pop()
        seen.discard((removed.get("task_code", ""), removed.get("activity_code", "")))
    return pair


def _build_time_code_assist(entries: list[TimeEntry]) -> dict[str, object]:
    max_codes = 12
    max_pairs = 10

    global_task_codes: list[str] = []
    global_activity_codes: list[str] = []
    global_pairs: list[dict[str, str]] = []
    global_task_seen: set[str] = set()
    global_activity_seen: set[str] = set()
    global_pair_seen: set[tuple[str, str]] = set()

    by_matter: dict[str, dict[str, object]] = {}
    per_matter_seen: dict[str, dict[str, set]] = {}

    for entry in entries:
        task_code = (entry.task_code or "").strip()
        activity_code = (entry.activity_code or "").strip()
        if not task_code and not activity_code:
            continue

        _add_unique_string(global_task_codes, global_task_seen, task_code, max_codes)
        _add_unique_string(global_activity_codes, global_activity_seen, activity_code, max_codes)
        _add_unique_pair(
            global_pairs,
            global_pair_seen,
            task_code=task_code,
            activity_code=activity_code,
            limit=max_pairs,
        )

        matter_key = str(entry.matter_id) if entry.matter_id is not None else ""
        if not matter_key:
            continue

        matter_bucket = by_matter.get(matter_key)
        if matter_bucket is None:
            matter_bucket = {
                "task_codes": [],
                "activity_codes": [],
                "pairs": [],
                "latest_pair": None,
            }
            by_matter[matter_key] = matter_bucket
            per_matter_seen[matter_key] = {
                "task_codes": set(),
                "activity_codes": set(),
                "pairs": set(),
            }

        matter_seen = per_matter_seen[matter_key]
        matter_task_codes = matter_bucket["task_codes"]
        matter_activity_codes = matter_bucket["activity_codes"]
        matter_pairs = matter_bucket["pairs"]
        _add_unique_string(matter_task_codes, matter_seen["task_codes"], task_code, max_codes)
        _add_unique_string(matter_activity_codes, matter_seen["activity_codes"], activity_code, max_codes)
        created_pair = _add_unique_pair(
            matter_pairs,
            matter_seen["pairs"],
            task_code=task_code,
            activity_code=activity_code,
            limit=max_pairs,
        )
        if matter_bucket["latest_pair"] is None:
            if created_pair is not None:
                matter_bucket["latest_pair"] = created_pair
            elif matter_pairs:
                matter_bucket["latest_pair"] = matter_pairs[0]

    return {
        "global": {
            "task_codes": global_task_codes,
            "activity_codes": global_activity_codes,
            "pairs": global_pairs,
        },
        "by_matter": by_matter,
    }


def register_timekeeping_routes(app):
    @app.before_request
    def timekeeping_timer_cap_guard():
        if not current_user.is_authenticated:
            return
        if request.endpoint == "static" or request.path.startswith("/static/"):
            return

        running = _active_timer_for_user(current_user.id)
        if running is None:
            return

        cap_seconds = _single_timer_cap_seconds()
        if _elapsed_seconds_for_timer(running) < cap_seconds:
            return

        _pause_timer(running, cap_seconds=cap_seconds)
        captured_entry_id, captured_invoice_id = capture_timer_to_draft_time_entry(
            running.id,
            pause_reason="cap_reached",
            actor_user_id=current_user.id,
            auto_create_billing_item=True,
        )
        db.session.commit()
        audit(
            "timer_auto_pause_cap",
            "TimeTimer",
            running.id,
            {
                "reason": "single_timer_cap",
                "cap_seconds": cap_seconds,
                "elapsed_seconds": running.elapsed_seconds,
                "captured_entry_id": captured_entry_id,
                "captured_invoice_id": captured_invoice_id,
            },
        )
        if _request_prefers_html():
            flash(
                f"Running timer auto-paused at {cap_seconds // 60} minutes to prevent accidental overrun.",
                "warning",
            )
            if captured_entry_id is not None:
                if captured_invoice_id is not None:
                    flash(
                        f"Captured draft entry #{captured_entry_id} and queued it on draft invoice #{captured_invoice_id}.",
                        "info",
                    )
                else:
                    flash(f"Captured draft entry #{captured_entry_id}.", "info")

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

    @app.post("/time/ai/narrative")
    @login_required
    def time_ai_narrative():
        payload_raw = request.get_json(silent=True) if request.is_json else request.form
        if isinstance(payload_raw, dict):
            payload = payload_raw
        elif hasattr(payload_raw, "to_dict"):
            payload = payload_raw.to_dict(flat=True)
        else:
            payload = {}

        matter_id_value = payload.get("matter_id")
        try:
            matter_id = int(matter_id_value)
        except (TypeError, ValueError):
            matter_id = 0
        if matter_id <= 0:
            return jsonify({"ok": False, "error": "matter_id is required."}), 400
        if not can_access_matter(matter_id):
            abort(403)

        matter = db.session.get(Matter, matter_id)
        if matter is None:
            return jsonify({"ok": False, "error": "Matter not found."}), 404

        start_at, start_error = _parse_iso_datetime(payload.get("start_at"))
        end_at, end_error = _parse_iso_datetime(payload.get("end_at"))
        if start_error or end_error:
            return jsonify({"ok": False, "error": start_error or end_error}), 400
        if start_at and end_at and end_at <= start_at:
            return jsonify({"ok": False, "error": "End time must be after start time."}), 400

        duration_hours: float | None = None
        if start_at and end_at:
            duration_hours = max(0.0, (end_at - start_at).total_seconds() / 3600.0)

        task_code = " ".join(str(payload.get("task_code") or "").split()).strip()[:40]
        activity_code = " ".join(str(payload.get("activity_code") or "").split()).strip()[:40]
        current_narrative = " ".join(str(payload.get("narrative") or "").split()).strip()[:500]
        matter_context = {
            "matter_id": int(matter.id),
            "matter_no": matter.matter_no or "",
            "title": matter.title or "",
            "client_name": matter.client_name or "",
            "status": matter.status or "",
            "risk_level": matter.risk_level or "",
            "budget_status": matter.budget_status or "",
            "objective": matter.objective or "",
        }

        started = utc_now()
        suggestion = suggest_time_entry_narrative(
            matter_context=matter_context,
            duration_hours=duration_hours,
            task_code=task_code,
            activity_code=activity_code,
            current_narrative=current_narrative,
        )
        elapsed_ms = int((utc_now() - started).total_seconds() * 1000)
        audit(
            "time_narrative_ai_suggest",
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

    @app.post("/time/entries/import-photo")
    @login_required
    def time_entries_import_photo():
        upload = request.files.get("timesheet_photo")
        default_matter_id = request.form.get("default_matter_id", type=int)

        def _entries_redirect() -> Response:
            params: dict[str, int] = {}
            if default_matter_id:
                params["matter_id"] = default_matter_id
            return redirect(url_for("time_entries", **params))

        if default_matter_id and not can_access_matter(default_matter_id):
            abort(403)

        if upload is None or not (upload.filename or "").strip():
            flash("Select a timesheet image to upload.", "warning")
            return _entries_redirect()

        filename = secure_filename(upload.filename or "").strip()
        extension = filename.rsplit(".", 1)[1].lower() if "." in filename else ""
        content_type = (upload.mimetype or "").strip().lower()
        if extension not in _TIMESHEET_IMAGE_EXTENSIONS:
            flash("Timesheet photo must be PNG, JPG, JPEG, or WEBP.", "warning")
            return _entries_redirect()
        if not content_type.startswith(_TIMESHEET_IMAGE_MIME_PREFIX):
            flash("Uploaded file is not recognized as an image.", "warning")
            return _entries_redirect()

        image_bytes = upload.read()
        if not image_bytes:
            flash("Uploaded image is empty.", "warning")
            return _entries_redirect()
        if len(image_bytes) > 12 * 1024 * 1024:
            flash("Image is too large. Please upload a file smaller than 12MB.", "warning")
            return _entries_redirect()

        parse_result = parse_timesheet_image_entries(
            image_bytes=image_bytes,
            mime_type=content_type,
            filename=filename,
        )
        parsed_entries_raw = parse_result.get("entries") if isinstance(parse_result, dict) else None
        parsed_entries = parsed_entries_raw if isinstance(parsed_entries_raw, list) else []
        source = str(parse_result.get("source") or "fallback") if isinstance(parse_result, dict) else "fallback"
        fallback_reason = str(parse_result.get("fallback_reason") or "") if isinstance(parse_result, dict) else ""
        fallback_detail = str(parse_result.get("fallback_detail") or "") if isinstance(parse_result, dict) else ""

        if not parsed_entries:
            if source == "fallback":
                reason = fallback_reason.replace("_", " ").strip() if fallback_reason else "no rows recognized"
                detail = f" ({fallback_detail})" if fallback_detail else ""
                flash(
                    f"No rows were imported from the timesheet image. Reason: {reason}{detail}.",
                    "warning",
                )
            else:
                flash("No legible timesheet rows were detected in the uploaded image.", "warning")
            return _entries_redirect()

        scoped_matters = _scoped_matters_for_current_user(limit=500)
        matter_by_id = {int(matter.id): matter for matter in scoped_matters}
        matter_by_no = {str(matter.matter_no or "").upper(): matter for matter in scoped_matters if matter.matter_no}

        if default_matter_id and default_matter_id not in matter_by_id:
            default_matter = db.session.get(Matter, default_matter_id)
            if default_matter and can_access_matter(default_matter.id):
                matter_by_id[int(default_matter.id)] = default_matter
                if default_matter.matter_no:
                    matter_by_no[str(default_matter.matter_no).upper()] = default_matter

        created = 0
        skipped = 0
        needs_review = 0
        skipped_reasons: list[str] = []
        last_matter_id: int | None = None
        now = utc_now()

        for index, item in enumerate(parsed_entries, start=1):
            row = item if isinstance(item, dict) else {}

            matter: Matter | None = None
            matter_id_raw = row.get("matter_id")
            try:
                matter_id_candidate = int(matter_id_raw) if matter_id_raw is not None else None
            except (TypeError, ValueError):
                matter_id_candidate = None
            if matter_id_candidate and matter_id_candidate in matter_by_id:
                matter = matter_by_id[matter_id_candidate]
            if matter is None:
                matter_no = " ".join(str(row.get("matter_no") or "").split()).strip().upper()
                if matter_no and matter_no in matter_by_no:
                    matter = matter_by_no[matter_no]
            if matter is None and default_matter_id and default_matter_id in matter_by_id:
                matter = matter_by_id[default_matter_id]
            if matter is None:
                skipped += 1
                skipped_reasons.append(f"row {index}: matter not resolved")
                continue
            if _matter_is_closed(matter.id):
                skipped += 1
                skipped_reasons.append(f"row {index}: matter {matter.matter_no} is closed")
                continue

            date_text = " ".join(str(row.get("date") or "").split()).strip()
            try:
                work_date = dt.date.fromisoformat(date_text) if date_text else now.date()
            except ValueError:
                work_date = now.date()

            start_time = _parse_time_hhmm(str(row.get("start_time") or ""))
            if start_time is None:
                start_time = dt.time(hour=9, minute=0)
            end_time = _parse_time_hhmm(str(row.get("end_time") or ""))

            hours_value: float | None = None
            try:
                if row.get("hours") is not None:
                    parsed_hours = float(row.get("hours"))
                    if parsed_hours > 0:
                        hours_value = min(parsed_hours, 24.0)
            except (TypeError, ValueError):
                hours_value = None

            start_at = dt.datetime.combine(work_date, start_time)
            if end_time is not None:
                end_at = dt.datetime.combine(work_date, end_time)
            elif hours_value is not None:
                end_at = start_at + dt.timedelta(hours=hours_value)
            else:
                skipped += 1
                skipped_reasons.append(f"row {index}: missing end time and hours")
                continue

            if end_at <= start_at:
                if hours_value is not None:
                    end_at = start_at + dt.timedelta(hours=hours_value)
                else:
                    skipped += 1
                    skipped_reasons.append(f"row {index}: invalid time range")
                    continue

            raw_hours = max(0.0, (end_at - start_at).total_seconds() / 3600.0)
            if raw_hours <= 0:
                skipped += 1
                skipped_reasons.append(f"row {index}: zero-duration entry")
                continue

            narrative = " ".join(str(row.get("narrative") or "").split()).strip()
            task_code = " ".join(str(row.get("task_code") or "").split()).strip() or None
            activity_code = " ".join(str(row.get("activity_code") or "").split()).strip() or None
            is_billable_raw: Any = row.get("is_billable")
            if isinstance(is_billable_raw, str):
                is_billable = is_billable_raw.strip().lower() in {"1", "true", "yes", "on", "billable"}
            else:
                is_billable = bool(is_billable_raw) if is_billable_raw is not None else True

            duplicate = (
                TimeEntry.query.filter(
                    TimeEntry.user_id == current_user.id,
                    TimeEntry.matter_id == matter.id,
                    TimeEntry.start_at == start_at,
                    TimeEntry.end_at == end_at,
                    TimeEntry.narrative == (narrative or None),
                )
                .limit(1)
                .first()
            )
            if duplicate is not None:
                skipped += 1
                skipped_reasons.append(f"row {index}: duplicate existing entry #{duplicate.id}")
                continue

            policy = _policy_for_matter(matter.id)
            rounded = _round_hours(raw_hours, float(policy.increment_hours if policy else 0.1))
            entry = TimeEntry(
                user_id=current_user.id,
                matter_id=matter.id,
                task_id=None,
                start_at=start_at,
                end_at=end_at,
                hours=round(raw_hours, 4),
                rounded_hours=rounded,
                narrative=narrative or None,
                task_code=task_code,
                activity_code=activity_code,
                is_billable=is_billable,
                status="draft",
            )
            db.session.add(entry)
            db.session.flush()
            issues = _validate_time_entry(entry, policy)
            for issue in issues:
                db.session.add(TimeValidationEvent(time_entry_id=entry.id, event_type="validation", message=issue))
            if source == "fallback":
                reason = fallback_reason or "fallback_used"
                db.session.add(
                    TimeValidationEvent(
                        time_entry_id=entry.id,
                        event_type="import_note",
                        message=f"Timesheet AI fallback used during import ({reason}).",
                    )
                )
            if issues:
                entry.status = "needs_review"
                needs_review += 1

            created += 1
            last_matter_id = matter.id

        if created > 0:
            db.session.commit()
            if last_matter_id:
                set_active_matter_context(last_matter_id)
        else:
            db.session.rollback()

        audit(
            "time_entries_import_photo",
            "TimeEntry",
            None,
            {
                "filename": filename,
                "source": source,
                "fallback_reason": fallback_reason,
                "created": created,
                "needs_review": needs_review,
                "skipped": skipped,
            },
        )

        if created > 0:
            review_suffix = f", {needs_review} flagged for review" if needs_review > 0 else ""
            flash(f"Imported {created} time entr{'y' if created == 1 else 'ies'}{review_suffix}.", "info")
        else:
            flash("No time entries were imported from the uploaded timesheet photo.", "warning")
        if skipped > 0:
            preview = "; ".join(skipped_reasons[:4])
            flash(f"Skipped {skipped} row(s): {preview}", "warning")

        return _entries_redirect()

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
        if not prefill_matter_id:
            prefill_matter_id = get_active_matter_id()
        if prefill_matter_id and not can_access_matter(prefill_matter_id):
            prefill_matter_id = None
        if prefill_matter_id and prefill_matter_id not in {matter.id for matter in matters}:
            selected = db.session.get(Matter, prefill_matter_id)
            if selected and can_access_matter(selected.id):
                matters = [selected] + matters
                matter_map[selected.id] = selected
        prefill_task_id = request.args.get("task_id", type=int)
        prefill_label = (request.args.get("label") or "").strip()
        timer_cap_minutes = max(5, int(app.config.get("TIMER_SINGLE_CAP_MINUTES", 4 * 60) or 4 * 60))
        timer_idle_prompt_seconds = max(
            5 * 60, int(app.config.get("TIMER_IDLE_PROMPT_SECONDS", 45 * 60) or 45 * 60)
        )
        timer_idle_grace_seconds = max(30, int(app.config.get("TIMER_IDLE_GRACE_SECONDS", 60) or 60))

        return page(
            "Timers",
            "timekeeping/timers.html",
            timers=timers,
            matters=matters,
            matter_map=matter_map,
            prefill_matter_id=prefill_matter_id,
            prefill_task_id=prefill_task_id,
            prefill_label=prefill_label,
            timer_cap_minutes=timer_cap_minutes,
            timer_idle_prompt_seconds=timer_idle_prompt_seconds,
            timer_idle_grace_seconds=timer_idle_grace_seconds,
        )

    @app.post("/time/timers/start")
    @login_required
    def time_timer_start():
        matter_id = request.form.get("matter_id", type=int)
        if matter_id and not can_access_matter(matter_id):
            abort(403)
        if _matter_is_closed(matter_id):
            flash("Cannot start a timer on a closed matter. Reopen the matter first.", "warning")
            return redirect(url_for("time_timers"))

        cap_seconds = _single_timer_cap_seconds()
        running = _active_timer_for_user(current_user.id)
        previous_timer_capped = False
        previous_entry_id = None
        previous_invoice_id = None
        if running:
            _, previous_timer_capped = _pause_timer(running, cap_seconds=cap_seconds)
            previous_entry_id, previous_invoice_id = capture_timer_to_draft_time_entry(
                running.id,
                pause_reason="switch",
                actor_user_id=current_user.id,
                auto_create_billing_item=True,
            )

        timer = TimeTimer(
            user_id=current_user.id,
            matter_id=matter_id,
            task_id=request.form.get("task_id", type=int),
            label=(request.form.get("label") or "").strip() or None,
            started_at=utc_now(),
            status="running",
        )
        db.session.add(timer)
        db.session.commit()
        if matter_id:
            set_active_matter_context(matter_id)
        audit("timer_start", "TimeTimer", timer.id)
        if previous_timer_capped:
            flash(
                f"Previous timer was capped at {cap_seconds // 60} minutes and auto-paused.",
                "warning",
            )
        if previous_entry_id:
            if previous_invoice_id:
                flash(
                    f"Previous timer captured as draft entry #{previous_entry_id} and queued on draft invoice #{previous_invoice_id}.",
                    "info",
                )
            else:
                flash(f"Previous timer captured as draft time entry #{previous_entry_id}.", "info")
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

        pause_reason = (request.form.get("pause_reason") or "").strip().lower()
        auto_capture = _as_bool(request.form.get("auto_capture"), default=True)
        auto_create_billing_item = _as_bool(request.form.get("auto_create_billing_item"), default=True)
        cap_seconds = _single_timer_cap_seconds()
        capped = False
        captured_entry_id = None
        captured_invoice_id = None
        if timer.status == "running":
            _, capped = _pause_timer(timer, cap_seconds=cap_seconds)
            if auto_capture:
                captured_entry_id, captured_invoice_id = capture_timer_to_draft_time_entry(
                    timer.id,
                    pause_reason=(pause_reason or ("cap_reached" if capped else "manual_pause")),
                    actor_user_id=current_user.id,
                    auto_create_billing_item=auto_create_billing_item,
                )
        else:
            timer.status = "paused"
            timer.paused_at = utc_now()
        db.session.commit()
        audit(
            "timer_pause",
            "TimeTimer",
            timer.id,
            {
                "reason": pause_reason or "manual",
                "capped": bool(capped),
                "elapsed_seconds": int(timer.elapsed_seconds or 0),
                "captured_entry_id": captured_entry_id,
                "captured_invoice_id": captured_invoice_id,
            },
        )
        if captured_entry_id is not None:
            if captured_invoice_id is not None:
                flash(
                    f"Timer captured as draft entry #{captured_entry_id} and queued on draft invoice #{captured_invoice_id}.",
                    "info",
                )
            else:
                flash(f"Timer captured as draft time entry #{captured_entry_id}.", "info")
        if pause_reason == "idle_timeout":
            flash("Timer auto-paused after inactivity. Resume it when you return.", "warning")
        elif pause_reason == "cap_reached" or capped:
            flash(
                f"Timer reached the single-session cap ({cap_seconds // 60} minutes) and was paused.",
                "warning",
            )
        else:
            flash("Timer paused.", "info")
        return redirect(url_for("time_timers"))

    @app.post("/time/timers/switch")
    @login_required
    def time_timer_switch():
        matter_id = request.form.get("matter_id", type=int)
        if matter_id and not can_access_matter(matter_id):
            abort(403)
        if _matter_is_closed(matter_id):
            flash("Cannot switch a timer onto a closed matter. Reopen the matter first.", "warning")
            return redirect(url_for("time_timers"))

        cap_seconds = _single_timer_cap_seconds()
        running = _active_timer_for_user(current_user.id)
        previous_timer_capped = False
        previous_entry_id = None
        previous_invoice_id = None
        if running:
            _, previous_timer_capped = _pause_timer(running, cap_seconds=cap_seconds)
            previous_entry_id, previous_invoice_id = capture_timer_to_draft_time_entry(
                running.id,
                pause_reason="switch",
                actor_user_id=current_user.id,
                auto_create_billing_item=True,
            )

        timer = TimeTimer(
            user_id=current_user.id,
            matter_id=matter_id,
            task_id=request.form.get("task_id", type=int),
            label=(request.form.get("label") or "").strip() or None,
            started_at=utc_now(),
            status="running",
        )
        db.session.add(timer)
        db.session.commit()
        if matter_id:
            set_active_matter_context(matter_id)
        audit("timer_switch", "TimeTimer", timer.id)
        if previous_timer_capped:
            flash(
                f"Previous timer was capped at {cap_seconds // 60} minutes and auto-paused.",
                "warning",
            )
        if previous_entry_id:
            if previous_invoice_id:
                flash(
                    f"Previous timer captured as draft entry #{previous_entry_id} and queued on draft invoice #{previous_invoice_id}.",
                    "info",
                )
            else:
                flash(f"Previous timer captured as draft time entry #{previous_entry_id}.", "info")
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
            if _matter_is_closed(matter_id):
                errors.append({"index": idx, "error": "matter is closed and cannot accept new time entries"})
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
        def _entries_redirect(*, matter_id: int | None = None, task_id: int | None = None) -> Response:
            params: dict[str, int] = {}
            if matter_id:
                params["matter_id"] = matter_id
            if task_id:
                params["task_id"] = task_id
            return redirect(url_for("time_entries", **params))

        if request.method == "POST":
            matter_id = request.form.get("matter_id", type=int)
            task_id = request.form.get("task_id", type=int)
            if not matter_id or not can_access_matter(matter_id):
                abort(403)
            if task_id:
                task = db.session.get(Task, task_id)
                if task is None:
                    flash("Selected task could not be found.", "warning")
                    return _entries_redirect(matter_id=matter_id)
                if int(task.matter_id) != int(matter_id):
                    flash("Selected task does not belong to the chosen matter.", "warning")
                    return _entries_redirect(matter_id=matter_id)
            if _matter_is_closed(matter_id):
                flash("Matter is closed. Reopen it before posting new time.", "warning")
                return _entries_redirect(matter_id=matter_id, task_id=task_id)

            start_raw = (request.form.get("start_at") or "").strip()
            end_raw = (request.form.get("end_at") or "").strip()
            narrative = (request.form.get("narrative") or "").strip()

            try:
                start_at = dt.datetime.fromisoformat(start_raw)
                end_at = dt.datetime.fromisoformat(end_raw) if end_raw else None
            except ValueError:
                flash("Invalid datetime. Use ISO format.", "warning")
                return _entries_redirect(matter_id=matter_id, task_id=task_id)

            if end_at and end_at <= start_at:
                flash("End time must be after start time.", "warning")
                return _entries_redirect(matter_id=matter_id, task_id=task_id)

            hours = ((end_at - start_at).total_seconds() / 3600.0) if end_at else 0.0
            policy = _policy_for_matter(matter_id)
            rounded = _round_hours(hours, float(policy.increment_hours if policy else 0.1))

            entry = TimeEntry(
                user_id=current_user.id,
                matter_id=matter_id,
                task_id=task_id,
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
            set_active_matter_context(matter_id)
            audit("time_entry_create", "TimeEntry", entry.id, {"issues": issues})
            flash("Time entry saved." if not issues else "Time entry saved with validation issues.", "info")
            return _entries_redirect(matter_id=matter_id, task_id=task_id)

        entries = TimeEntry.query.filter_by(user_id=current_user.id).order_by(TimeEntry.start_at.desc()).limit(200).all()
        time_code_assist = _build_time_code_assist(entries)
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
        if not prefill_matter_id:
            prefill_matter_id = get_active_matter_id()
        if prefill_matter_id and not can_access_matter(prefill_matter_id):
            prefill_matter_id = None
        prefill_task_id = request.args.get("task_id", type=int)
        prefill_task = db.session.get(Task, prefill_task_id) if prefill_task_id else None
        if prefill_task is None and prefill_task_id:
            prefill_task_id = None
        elif prefill_task is not None:
            if not can_access_matter(prefill_task.matter_id):
                prefill_task_id = None
            else:
                if prefill_matter_id and int(prefill_task.matter_id) != int(prefill_matter_id):
                    prefill_task_id = None
                elif not prefill_matter_id:
                    prefill_matter_id = int(prefill_task.matter_id)
        prefill_task_code = (request.args.get("task_code") or "").strip()
        prefill_activity_code = (request.args.get("activity_code") or "").strip()
        prefill_narrative = (request.args.get("narrative") or "").strip()
        prefill_is_billable = _as_bool(request.args.get("is_billable"), default=True)

        if prefill_matter_id and prefill_matter_id not in {matter.id for matter in matters}:
            selected = db.session.get(Matter, prefill_matter_id)
            if selected and can_access_matter(selected.id):
                matters = [selected] + matters
                matter_lookup[selected.id] = selected

        time_task_options = _build_time_task_options(
            matter_ids=[matter.id for matter in matters],
            include_task_id=prefill_task_id,
        )

        default_end_dt = utc_now().replace(second=0, microsecond=0)
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
            prefill_task_code=prefill_task_code,
            prefill_activity_code=prefill_activity_code,
            prefill_narrative=prefill_narrative,
            prefill_is_billable=prefill_is_billable,
            prefill_start_at=prefill_start_at,
            prefill_end_at=prefill_end_at,
            time_code_assist=time_code_assist,
            time_task_options=time_task_options,
        )

    @app.route("/time/review", methods=["GET", "POST"])
    @login_required
    def time_review():
        enforce_permission("time_entry", "review")
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
                entry.approved_at = utc_now()
                queued_invoice_id = ensure_draft_billing_item_for_time_entry(
                    entry.id,
                    actor_user_id=current_user.id,
                )
            else:
                queued_invoice_id = None
            db.session.commit()
            set_active_matter_context(entry.matter_id)
            audit(
                "time_entry_review",
                "TimeEntry",
                entry.id,
                {"state": state, "queued_invoice_id": queued_invoice_id},
            )
            if state == "approved" and queued_invoice_id is not None:
                flash(f"Review saved. Entry queued on draft invoice #{queued_invoice_id}.", "info")
            else:
                flash("Review saved.", "info")
            return redirect(url_for("time_review"))

        rows = TimeEntry.query.order_by(TimeEntry.created_at.desc()).limit(300).all()
        scoped_rows = [row for row in rows if can_access_matter(row.matter_id)]
        return page("Time Review", "timekeeping/review.html", entries=scoped_rows)

    @app.post("/time/entries/<int:entry_id>/lock")
    @login_required
    def time_entry_lock(entry_id: int):
        enforce_permission("time_entry", "lock")
        entry = db.session.get(TimeEntry, entry_id)
        if not entry:
            abort(404)
        if not can_access_matter(entry.matter_id):
            abort(403)
        if entry.status != "approved":
            flash("Only approved entries can be locked.", "warning")
            return redirect(url_for("time_review"))

        entry.locked_at = utc_now()
        db.session.commit()
        set_active_matter_context(entry.matter_id)
        audit("time_entry_lock", "TimeEntry", entry.id)
        flash("Time entry locked.", "info")
        return redirect(url_for("time_review"))
