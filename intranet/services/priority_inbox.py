from __future__ import annotations

import datetime as dt
from ..timeutils import utc_now
import json

from sqlalchemy import and_, func, or_

from ..extensions import db
from ..models import (
    CRMFollowUp,
    CRMLead,
    FirmSetting,
    InvoiceLine,
    Matter,
    MatterMember,
    PortalMessage,
    PortalMessageThread,
    TimeEntry,
    User,
)
from ..roles import canonical_role, role_is_admin

PRIORITY_INBOX_CONFIG_KEY = "priority_inbox"
DEFAULT_PRIORITY_INBOX_CONFIG = {
    "portal_response_sla_hours": 4,
    "followup_horizon_hours": 24,
    "billing_capture_sla_hours": 48,
    "digest_enabled": True,
    "digest_interval_minutes": 60,
}


def _coerce_int(value, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _coerce_bool(value, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(int(value))
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def normalize_priority_inbox_config(raw: dict | None) -> dict:
    payload = raw if isinstance(raw, dict) else {}
    return {
        "portal_response_sla_hours": _coerce_int(
            payload.get("portal_response_sla_hours"),
            default=DEFAULT_PRIORITY_INBOX_CONFIG["portal_response_sla_hours"],
            minimum=1,
            maximum=168,
        ),
        "followup_horizon_hours": _coerce_int(
            payload.get("followup_horizon_hours"),
            default=DEFAULT_PRIORITY_INBOX_CONFIG["followup_horizon_hours"],
            minimum=1,
            maximum=168,
        ),
        "billing_capture_sla_hours": _coerce_int(
            payload.get("billing_capture_sla_hours"),
            default=DEFAULT_PRIORITY_INBOX_CONFIG["billing_capture_sla_hours"],
            minimum=1,
            maximum=336,
        ),
        "digest_enabled": _coerce_bool(
            payload.get("digest_enabled"),
            default=DEFAULT_PRIORITY_INBOX_CONFIG["digest_enabled"],
        ),
        "digest_interval_minutes": _coerce_int(
            payload.get("digest_interval_minutes"),
            default=DEFAULT_PRIORITY_INBOX_CONFIG["digest_interval_minutes"],
            minimum=15,
            maximum=1440,
        ),
    }


def load_priority_inbox_config() -> dict:
    row = FirmSetting.query.filter_by(setting_key=PRIORITY_INBOX_CONFIG_KEY).first()
    if row is None:
        return dict(DEFAULT_PRIORITY_INBOX_CONFIG)

    parsed = {}
    try:
        candidate = json.loads(row.setting_value_json or "{}")
        if isinstance(candidate, dict):
            parsed = candidate
    except json.JSONDecodeError:
        parsed = {}
    return normalize_priority_inbox_config(parsed)


def save_priority_inbox_config(raw: dict, *, updated_by: int | None) -> dict:
    payload = normalize_priority_inbox_config(raw)
    now = utc_now()
    row = FirmSetting.query.filter_by(setting_key=PRIORITY_INBOX_CONFIG_KEY).first()
    if row is None:
        row = FirmSetting(
            setting_key=PRIORITY_INBOX_CONFIG_KEY,
            setting_value_json=json.dumps(payload, sort_keys=True),
            updated_at=now,
            updated_by=updated_by,
        )
        db.session.add(row)
    else:
        row.setting_value_json = json.dumps(payload, sort_keys=True)
        row.updated_at = now
        row.updated_by = updated_by
    db.session.commit()
    return payload


def _matter_scope_for_user(user: User, *, scoped_matter_ids: list[int] | None) -> list[int] | None:
    if role_is_admin(getattr(user, "role", None)):
        return None
    if scoped_matter_ids is not None:
        scoped: list[int] = []
        for matter_id in scoped_matter_ids:
            try:
                parsed = int(matter_id)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                scoped.append(parsed)
        return scoped
    rows = db.session.query(MatterMember.matter_id).filter(MatterMember.user_id == user.id).all()
    return [int(row[0]) for row in rows if row[0] is not None]


def build_priority_inbox(
    user: User,
    *,
    now_utc: dt.datetime | None = None,
    scoped_matter_ids: list[int] | None = None,
    limit: int = 12,
    config: dict | None = None,
) -> dict:
    now_utc = now_utc or utc_now()
    cfg = normalize_priority_inbox_config(config or load_priority_inbox_config())
    matter_scope = _matter_scope_for_user(user, scoped_matter_ids=scoped_matter_ids)
    is_admin_user = role_is_admin(getattr(user, "role", None))

    followup_query = (
        db.session.query(CRMFollowUp, CRMLead)
        .join(CRMLead, CRMLead.id == CRMFollowUp.lead_id)
        .filter(CRMFollowUp.status == "open")
        .filter(CRMFollowUp.due_at <= now_utc + dt.timedelta(hours=cfg["followup_horizon_hours"]))
    )
    if not is_admin_user:
        followup_query = followup_query.filter(or_(CRMLead.assigned_to == user.id, CRMLead.created_by == user.id))
    followup_rows = followup_query.order_by(CRMFollowUp.due_at.asc()).limit(limit).all()
    crm_followup_watchlist = [
        {
            "followup_id": row.id,
            "lead_id": lead.id,
            "lead_name": lead.full_name,
            "lead_stage": lead.stage,
            "due_at": row.due_at,
            "note": row.note,
            "is_overdue": row.due_at < now_utc,
        }
        for row, lead in followup_rows
    ]

    latest_thread_message = (
        db.session.query(
            PortalMessage.thread_id.label("thread_id"),
            func.max(PortalMessage.created_at).label("latest_created_at"),
        )
        .group_by(PortalMessage.thread_id)
        .subquery()
    )
    portal_watchlist_query = (
        db.session.query(PortalMessageThread, PortalMessage, Matter)
        .join(latest_thread_message, latest_thread_message.c.thread_id == PortalMessageThread.id)
        .join(
            PortalMessage,
            and_(
                PortalMessage.thread_id == PortalMessageThread.id,
                PortalMessage.created_at == latest_thread_message.c.latest_created_at,
            ),
        )
        .join(Matter, Matter.id == PortalMessageThread.matter_id)
        .filter(PortalMessage.from_portal_user_id.isnot(None))
        .filter(PortalMessage.created_at <= now_utc - dt.timedelta(hours=cfg["portal_response_sla_hours"]))
    )
    if not is_admin_user:
        if matter_scope:
            portal_watchlist_query = portal_watchlist_query.filter(PortalMessageThread.matter_id.in_(matter_scope))
        else:
            portal_watchlist_query = portal_watchlist_query.filter(PortalMessageThread.id == -1)
    portal_watchlist_rows = portal_watchlist_query.order_by(PortalMessage.created_at.asc()).limit(limit).all()
    portal_response_watchlist = []
    for thread, message, matter in portal_watchlist_rows:
        wait_hours = max(0.0, (now_utc - message.created_at).total_seconds() / 3600.0)
        portal_response_watchlist.append(
            {
                "thread_id": thread.id,
                "matter_id": matter.id,
                "matter_no": matter.matter_no,
                "matter_title": matter.title,
                "subject": thread.subject,
                "last_message_at": message.created_at,
                "wait_hours": round(wait_hours, 1),
            }
        )

    unbilled_time_query = (
        db.session.query(TimeEntry, Matter, User)
        .join(Matter, Matter.id == TimeEntry.matter_id)
        .join(User, User.id == TimeEntry.user_id)
        .outerjoin(InvoiceLine, InvoiceLine.time_entry_id == TimeEntry.id)
        .filter(InvoiceLine.id.is_(None))
        .filter(TimeEntry.is_billable.is_(True))
        .filter(TimeEntry.status.in_(["approved", "locked"]))
        .filter(TimeEntry.end_at.isnot(None))
        .filter(TimeEntry.end_at <= now_utc - dt.timedelta(hours=cfg["billing_capture_sla_hours"]))
    )
    if not is_admin_user:
        if matter_scope:
            unbilled_time_query = unbilled_time_query.filter(TimeEntry.matter_id.in_(matter_scope))
        else:
            unbilled_time_query = unbilled_time_query.filter(TimeEntry.id == -1)
    if canonical_role(getattr(user, "role", None)) in {"operations_staff", "candidate_attorney"}:
        unbilled_time_query = unbilled_time_query.filter(TimeEntry.user_id == user.id)
    unbilled_rows = unbilled_time_query.order_by(TimeEntry.end_at.asc()).limit(limit).all()
    unbilled_time_watchlist = []
    unbilled_total_hours = 0.0
    for entry, matter, timekeeper in unbilled_rows:
        hours = float(entry.rounded_hours or entry.hours or 0.0)
        unbilled_total_hours += max(0.0, hours)
        unbilled_time_watchlist.append(
            {
                "time_entry_id": entry.id,
                "matter_id": matter.id,
                "matter_no": matter.matter_no,
                "matter_title": matter.title,
                "timekeeper_name": timekeeper.full_name,
                "captured_at": entry.end_at,
                "hours": round(hours, 2),
            }
        )

    return {
        "portal_response_watchlist": portal_response_watchlist,
        "crm_followup_watchlist": crm_followup_watchlist,
        "unbilled_time_watchlist": unbilled_time_watchlist,
        "portal_response_sla_hours": cfg["portal_response_sla_hours"],
        "followup_horizon_hours": cfg["followup_horizon_hours"],
        "billing_capture_sla_hours": cfg["billing_capture_sla_hours"],
        "digest_enabled": cfg["digest_enabled"],
        "digest_interval_minutes": cfg["digest_interval_minutes"],
        "followups_overdue": sum(1 for row in crm_followup_watchlist if row["is_overdue"]),
        "unbilled_total_hours": round(unbilled_total_hours, 2),
        "total_actions": len(portal_response_watchlist)
        + len(crm_followup_watchlist)
        + len(unbilled_time_watchlist),
    }
