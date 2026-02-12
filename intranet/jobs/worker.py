from __future__ import annotations

import datetime as dt
import json

from ..db_context import set_db_access_context
from ..extensions import db
from .queue import complete_job, fail_job, lease_job


def _handle_send_notification(payload: dict) -> str:
    from ..models import Notification

    notification = db.session.get(Notification, int(payload.get("notification_id") or 0))
    if notification is None:
        return "notification missing"

    # Internal-only delivery model: mark as delivered without external provider.
    notification.status = "delivered"
    notification.delivered_at = dt.datetime.utcnow()
    db.session.commit()
    return "notification delivered"


def _handle_deadline_sweep(payload: dict) -> str:
    from ..models import Deadline

    now = dt.datetime.utcnow().date()
    critical = (
        Deadline.query.filter(
            Deadline.is_critical.is_(True),
            Deadline.status.in_(["open", "overdue"]),
            Deadline.due_at <= now,
        )
        .limit(100)
        .all()
    )
    for row in critical:
        if row.status != "overdue":
            row.status = "overdue"
    db.session.commit()
    return f"processed {len(critical)} critical deadlines"


def _handle_deadline_escalation_scan(payload: dict) -> str:
    from ..models import Deadline, Notification
    from ..services.notification_engine import NotificationEngine

    today = dt.datetime.utcnow().date()
    rows = (
        Deadline.query.filter(
            Deadline.is_critical.is_(True),
            Deadline.status.in_(["open", "overdue"]),
            Deadline.acknowledged_at.is_(None),
            Deadline.due_at <= today,
        )
        .order_by(Deadline.due_at.asc())
        .limit(200)
        .all()
    )

    created = 0
    for row in rows:
        subject_ref = f"deadline:{row.id}"
        already = Notification.query.filter_by(event_type="deadline_escalation", subject_ref=subject_ref).first()
        if already is not None:
            continue
        NotificationEngine.enqueue("deadline_escalation", row.created_by, subject_ref)
        created += 1
    return f"created escalations: {created}"


def _handle_deadline_digest(payload: dict) -> str:
    from sqlalchemy import func

    from ..models import Deadline, MatterMember, Notification
    from ..services.notification_engine import NotificationEngine

    today = dt.datetime.utcnow().date()
    week_end = today + dt.timedelta(days=7)
    members = db.session.query(MatterMember.user_id).distinct().all()

    queued = 0
    for (user_id,) in members:
        matter_ids = (
            db.session.query(MatterMember.matter_id)
            .filter(MatterMember.user_id == user_id)
            .all()
        )
        matter_ids = [row[0] for row in matter_ids]
        if not matter_ids:
            continue

        today_count = int(
            db.session.query(func.count(Deadline.id))
            .filter(Deadline.matter_id.in_(matter_ids), Deadline.due_at == today, Deadline.status != "acknowledged")
            .scalar()
            or 0
        )
        week_count = int(
            db.session.query(func.count(Deadline.id))
            .filter(
                Deadline.matter_id.in_(matter_ids),
                Deadline.due_at >= today,
                Deadline.due_at <= week_end,
                Deadline.status != "acknowledged",
            )
            .scalar()
            or 0
        )
        if today_count == 0 and week_count == 0:
            continue

        subject_ref = f"deadline_digest:user:{user_id}:today:{today_count}:week:{week_count}:asof:{today.isoformat()}"
        already = Notification.query.filter_by(event_type="deadline_digest", subject_ref=subject_ref).first()
        if already is not None:
            continue
        NotificationEngine.enqueue("deadline_digest", int(user_id), subject_ref)
        queued += 1

    return f"queued digests: {queued}"


def _handle_retention_archive_sweep(payload: dict) -> str:
    from ..models import LegalHold, Matter, RetentionPolicy

    now = dt.datetime.utcnow()
    batch_size = max(1, int(payload.get("batch_size") or 200))

    # Seed archival due dates for closed matters that have no schedule yet.
    seeded = 0
    closed_without_due = (
        Matter.query.filter(
            Matter.status == "Closed",
            Matter.archival_due_at.is_(None),
            Matter.archival_status.in_(["active", "archive_pending", "legal_hold_blocked"]),
        )
        .limit(batch_size)
        .all()
    )
    for matter in closed_without_due:
        hold_active = (
            LegalHold.query.filter(LegalHold.matter_id == matter.id, LegalHold.is_active.is_(True)).first() is not None
        )
        if hold_active:
            matter.archival_status = "legal_hold_blocked"
            matter.archival_due_at = None
            continue

        policy = (
            RetentionPolicy.query.filter(
                RetentionPolicy.is_active.is_(True),
                (RetentionPolicy.jurisdiction.is_(None)) | (RetentionPolicy.jurisdiction == matter.jurisdiction),
            )
            .order_by(RetentionPolicy.id.desc())
            .first()
        )
        archive_days = int(policy.archive_after_days or 30) if policy else 30
        reference = matter.closed_at or now
        matter.archival_status = "archive_pending"
        matter.archival_due_at = reference + dt.timedelta(days=max(1, archive_days))
        seeded += 1

    archived = 0
    blocked = 0
    due_rows = (
        Matter.query.filter(
            Matter.status == "Closed",
            Matter.archival_status.in_(["archive_pending", "legal_hold_blocked"]),
            Matter.archival_due_at.isnot(None),
            Matter.archival_due_at <= now,
        )
        .limit(batch_size)
        .all()
    )
    for matter in due_rows:
        hold_active = (
            LegalHold.query.filter(LegalHold.matter_id == matter.id, LegalHold.is_active.is_(True)).first() is not None
        )
        if hold_active:
            matter.archival_status = "legal_hold_blocked"
            matter.archival_due_at = None
            blocked += 1
            continue
        matter.archival_status = "archived"
        matter.archival_due_at = None
        archived += 1

    db.session.commit()
    return f"retention sweep: seeded={seeded}, archived={archived}, blocked={blocked}"


def _handle_analytics_snapshot(payload: dict) -> str:
    from ..services.analytics_engine import AnalyticsEngine

    as_of = payload.get("as_of_date")
    if as_of:
        d = dt.date.fromisoformat(as_of)
    else:
        d = dt.date.today()
    AnalyticsEngine.compute_snapshot(d)
    return "analytics snapshot computed"


def _handle_workload_forecast(payload: dict) -> str:
    from sqlalchemy import func

    from ..models import TimeEntry, WorkloadForecast

    as_of_raw = payload.get("as_of_date")
    as_of_date = dt.date.fromisoformat(as_of_raw) if as_of_raw else dt.date.today()
    lookback_days = max(7, int(payload.get("lookback_days") or 30))
    start_dt = dt.datetime.combine(as_of_date - dt.timedelta(days=lookback_days), dt.time.min)
    end_dt = dt.datetime.combine(as_of_date, dt.time.max)

    grouped = (
        db.session.query(
            TimeEntry.user_id,
            func.coalesce(func.sum(TimeEntry.hours), 0.0),
            func.count(func.distinct(func.date(TimeEntry.start_at))),
        )
        .filter(TimeEntry.start_at >= start_dt, TimeEntry.start_at <= end_dt)
        .group_by(TimeEntry.user_id)
        .all()
    )

    upserted = 0
    for user_id, total_hours, active_days in grouped:
        uid = int(user_id or 0)
        if uid <= 0:
            continue
        total = float(total_hours or 0.0)
        active = int(active_days or 0)
        avg_daily = total / max(1, active)
        predicted = round(avg_daily * 1.1, 2)
        confidence = round(min(0.95, 0.4 + min(active, lookback_days) / max(1.0, lookback_days * 1.5)), 2)

        row = WorkloadForecast.query.filter_by(as_of_date=as_of_date, user_id=uid).first()
        if row is None:
            row = WorkloadForecast(as_of_date=as_of_date, user_id=uid, predicted_hours=predicted)
            db.session.add(row)
        row.predicted_hours = predicted
        row.confidence = confidence
        row.features_json = json.dumps(
            {
                "lookback_days": lookback_days,
                "active_days": active,
                "total_hours": round(total, 2),
                "avg_daily_hours": round(avg_daily, 2),
            }
        )
        upserted += 1

    db.session.commit()
    return f"workload forecasts upserted: {upserted}"


def _handle_burnout_heuristics(payload: dict) -> str:
    from ..models import BurnoutSignal, TimeEntry

    as_of_raw = payload.get("as_of_date")
    as_of_date = dt.date.fromisoformat(as_of_raw) if as_of_raw else dt.date.today()
    window_days = max(7, int(payload.get("window_days") or 14))
    start_dt = dt.datetime.combine(as_of_date - dt.timedelta(days=window_days), dt.time.min)
    end_dt = dt.datetime.combine(as_of_date, dt.time.max)

    entries = (
        TimeEntry.query.filter(TimeEntry.start_at >= start_dt, TimeEntry.start_at <= end_dt)
        .order_by(TimeEntry.start_at.asc())
        .all()
    )
    if not entries:
        return "burnout signals upserted: 0"

    per_user_day: dict[tuple[int, dt.date], float] = {}
    for entry in entries:
        uid = int(entry.user_id or 0)
        if uid <= 0:
            continue
        day = entry.start_at.date() if entry.start_at else as_of_date
        key = (uid, day)
        per_user_day[key] = float(per_user_day.get(key, 0.0)) + float(entry.hours or 0.0)

    per_user_daily: dict[int, list[float]] = {}
    for (uid, _day), hrs in per_user_day.items():
        per_user_daily.setdefault(uid, []).append(float(hrs))

    upserted = 0
    for uid, day_hours in per_user_daily.items():
        total_hours = float(sum(day_hours))
        avg_daily = total_hours / max(1, window_days)
        extreme_days = sum(1 for hrs in day_hours if hrs >= 10.0)
        score = round(min(100.0, (avg_daily * 6.0) + (extreme_days * 8.0)), 2)
        status = "open" if score >= 65 else "resolved"
        reason = f"{round(avg_daily, 2)}h/day avg over {window_days}d; {extreme_days} day(s) >=10h"

        row = BurnoutSignal.query.filter_by(as_of_date=as_of_date, user_id=uid).first()
        if row is None:
            row = BurnoutSignal(user_id=uid, as_of_date=as_of_date, score=score, status=status)
            db.session.add(row)
        row.score = score
        row.status = status
        row.reason = reason
        upserted += 1

    db.session.commit()
    return f"burnout signals upserted: {upserted}"


def _handle_suspicious_activity_scan(payload: dict) -> str:
    from sqlalchemy import func

    from ..models import AuditLog, SuspiciousActivityAlert

    def _create_alert(alert_type: str, severity: str, details: dict) -> bool:
        details_json = json.dumps(details, sort_keys=True)
        exists = SuspiciousActivityAlert.query.filter_by(
            alert_type=alert_type,
            status="open",
            details_json=details_json,
        ).first()
        if exists is not None:
            return False
        db.session.add(
            SuspiciousActivityAlert(
                alert_type=alert_type,
                severity=severity,
                status="open",
                details_json=details_json,
            )
        )
        return True

    now = dt.datetime.utcnow()
    created = 0

    # Repeated denied matter access attempts by a single actor.
    denied_rows = (
        db.session.query(AuditLog.actor_user_id, func.count(AuditLog.id))
        .filter(
            AuditLog.action == "matter_access_denied",
            AuditLog.actor_user_id.isnot(None),
            AuditLog.at >= now - dt.timedelta(hours=1),
        )
        .group_by(AuditLog.actor_user_id)
        .having(func.count(AuditLog.id) >= 5)
        .all()
    )
    for actor_user_id, attempts in denied_rows:
        if _create_alert(
            "repeated_denied_matter_access",
            "high" if int(attempts) >= 15 else "medium",
            {"actor_user_id": int(actor_user_id), "attempts_1h": int(attempts)},
        ):
            created += 1

    # Unusually high export volume in a short window.
    export_rows = (
        db.session.query(AuditLog.actor_user_id, func.count(AuditLog.id))
        .filter(
            AuditLog.action.in_(["production_export", "invoice_pdf_generate", "invoice_ledes_export"]),
            AuditLog.actor_user_id.isnot(None),
            AuditLog.at >= now - dt.timedelta(minutes=15),
        )
        .group_by(AuditLog.actor_user_id)
        .having(func.count(AuditLog.id) >= 4)
        .all()
    )
    for actor_user_id, export_count in export_rows:
        if _create_alert(
            "mass_exports",
            "high",
            {"actor_user_id": int(actor_user_id), "exports_15m": int(export_count)},
        ):
            created += 1

    # Download spike detector for DMS + receipts.
    download_rows = (
        db.session.query(AuditLog.actor_user_id, func.count(AuditLog.id))
        .filter(
            AuditLog.action.in_(["document_download", "expense_receipt_download"]),
            AuditLog.actor_user_id.isnot(None),
            AuditLog.at >= now - dt.timedelta(minutes=15),
        )
        .group_by(AuditLog.actor_user_id)
        .having(func.count(AuditLog.id) >= 25)
        .all()
    )
    for actor_user_id, download_count in download_rows:
        if _create_alert(
            "abnormal_download_spike",
            "high",
            {"actor_user_id": int(actor_user_id), "downloads_15m": int(download_count)},
        ):
            created += 1

    db.session.commit()
    open_alerts = int(
        db.session.query(func.count(SuspiciousActivityAlert.id))
        .filter(SuspiciousActivityAlert.status == "open")
        .scalar()
        or 0
    )
    return f"created alerts: {created}, open alerts: {open_alerts}"


HANDLERS = {
    "send_notification": _handle_send_notification,
    "deadline_sweep": _handle_deadline_sweep,
    "deadline_escalation_scan": _handle_deadline_escalation_scan,
    "deadline_digest": _handle_deadline_digest,
    "retention_archive_sweep": _handle_retention_archive_sweep,
    "analytics_snapshot": _handle_analytics_snapshot,
    "workload_forecast": _handle_workload_forecast,
    "burnout_heuristics": _handle_burnout_heuristics,
    "suspicious_activity_scan": _handle_suspicious_activity_scan,
}


def run_worker_once(worker_id: str = "local-worker") -> bool:
    set_db_access_context(user_id=None, role="system", is_admin=False, service_account=True)
    job = lease_job(worker_id)
    if job is None:
        return False

    try:
        payload = json.loads(job.payload_json or "{}")
    except json.JSONDecodeError:
        payload = {}

    handler = HANDLERS.get(job.job_type)
    if handler is None:
        fail_job(job.id, f"no handler for {job.job_type}")
        return True

    try:
        message = handler(payload)
        complete_job(job.id, message=message)
    except Exception as exc:  # pragma: no cover - defensive runtime guard
        db.session.rollback()
        fail_job(job.id, f"{type(exc).__name__}: {exc}")
    return True
