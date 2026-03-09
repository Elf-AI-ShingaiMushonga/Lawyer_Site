from __future__ import annotations

import datetime as dt
from ..timeutils import utc_now

from sqlalchemy import func, or_

from ..extensions import db


def _round_hours(hours: float, increment: float = 0.1) -> float:
    safe_increment = float(increment or 0.0)
    if safe_increment <= 0:
        return round(float(hours or 0.0), 4)
    steps = round(float(hours or 0.0) / safe_increment)
    return round(steps * safe_increment, 4)


def _month_window(day: dt.date) -> tuple[dt.date, dt.date]:
    start = day.replace(day=1)
    if start.month == 12:
        next_month = dt.date(start.year + 1, 1, 1)
    else:
        next_month = dt.date(start.year, start.month + 1, 1)
    return start, next_month - dt.timedelta(days=1)


def _pick_default_matter_owner_id(matter_id: int) -> int | None:
    from ..models import Matter, MatterMember

    preferred_roles = ("lead", "responsible", "originating partner", "supervising partner")
    members = (
        MatterMember.query.filter_by(matter_id=matter_id)
        .order_by(MatterMember.id.asc())
        .all()
    )
    for preferred in preferred_roles:
        for member in members:
            role = (member.role_in_matter or "").strip().lower()
            if role == preferred:
                return int(member.user_id)
    if members:
        return int(members[0].user_id)

    matter = db.session.get(Matter, matter_id)
    if matter and matter.created_by:
        return int(matter.created_by)
    return None


def _recalculate_invoice_totals(invoice_id: int) -> None:
    from ..models import Invoice, InvoiceLine
    from ..services.billing_engine import BillingEngine

    invoice = db.session.get(Invoice, invoice_id)
    if invoice is None:
        return
    subtotal = (
        db.session.query(func.coalesce(func.sum(InvoiceLine.amount), 0.0))
        .filter(InvoiceLine.invoice_id == invoice.id)
        .scalar()
        or 0.0
    )
    subtotal_value = round(float(subtotal), 2)
    tax_rate = float(BillingEngine._resolve_tax_rate(invoice.matter_id) or 0.0)
    tax_total = round(subtotal_value * tax_rate, 2)
    invoice.subtotal = subtotal_value
    invoice.tax_total = tax_total
    invoice.total = round(subtotal_value + tax_total, 2)


def ensure_draft_billing_item_for_time_entry(
    entry_id: int,
    *,
    actor_user_id: int | None = None,
) -> int | None:
    from ..models import Invoice, InvoiceLine, Matter, TimeEntry
    from ..services.billing_engine import BillingEngine

    entry = db.session.get(TimeEntry, entry_id)
    if entry is None or not bool(entry.is_billable) or entry.matter_id is None:
        return None

    marker = f"[time_entry:{entry.id}]"
    existing_line = (
        db.session.query(InvoiceLine)
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .filter(
            Invoice.matter_id == entry.matter_id,
            Invoice.status == "draft",
            InvoiceLine.description.ilike(f"%{marker}%"),
        )
        .order_by(InvoiceLine.id.desc())
        .first()
    )
    if existing_line is not None:
        existing_invoice = db.session.get(Invoice, existing_line.invoice_id)
        if existing_invoice is not None and (existing_invoice.status or "").strip().lower() == "draft":
            arrangement = BillingEngine._active_fee_arrangement(entry.matter_id)
            rate = float(BillingEngine._resolve_rate(entry, entry.matter_id, arrangement=arrangement) or 0.0)
            billable_hours = float(
                entry.rounded_hours if entry.rounded_hours is not None else (entry.hours or 0.0)
            )
            narrative = (entry.narrative or "Time entry") or "Time entry"
            prefix_limit = max(1, 255 - len(marker) - 1)
            existing_line.description = f"{narrative[:prefix_limit]} {marker}"
            existing_line.hours = billable_hours
            existing_line.rate = rate
            existing_line.amount = round(billable_hours * rate, 2)
            existing_line.task_code = entry.task_code
            existing_line.activity_code = entry.activity_code
            _recalculate_invoice_totals(existing_invoice.id)
            return existing_invoice.id
        return existing_invoice.id if existing_invoice else None

    entry_day = (entry.start_at or utc_now()).date()
    draft_invoice = (
        Invoice.query.filter(
            Invoice.matter_id == entry.matter_id,
            Invoice.status == "draft",
            Invoice.period_start <= entry_day,
            Invoice.period_end >= entry_day,
        )
        .order_by(Invoice.created_at.desc(), Invoice.id.desc())
        .first()
    )
    if draft_invoice is None:
        matter = db.session.get(Matter, entry.matter_id)
        period_start, period_end = _month_window(entry_day)
        draft_invoice = Invoice(
            matter_id=entry.matter_id,
            client_name=(matter.client_name if matter else "Client"),
            period_start=period_start,
            period_end=period_end,
            status="draft",
            subtotal=0.0,
            tax_total=0.0,
            total=0.0,
            created_by=int(actor_user_id or entry.user_id),
        )
        db.session.add(draft_invoice)
        db.session.flush()

    arrangement = BillingEngine._active_fee_arrangement(entry.matter_id)
    rate = float(BillingEngine._resolve_rate(entry, entry.matter_id, arrangement=arrangement) or 0.0)
    billable_hours = float(entry.rounded_hours if entry.rounded_hours is not None else (entry.hours or 0.0))
    narrative = (entry.narrative or "Time entry") or "Time entry"
    prefix_limit = max(1, 255 - len(marker) - 1)
    db.session.add(
        InvoiceLine(
            invoice_id=draft_invoice.id,
            time_entry_id=None,
            description=f"{narrative[:prefix_limit]} {marker}",
            hours=billable_hours,
            rate=rate,
            amount=round(billable_hours * rate, 2),
            tax_amount=0.0,
            task_code=entry.task_code,
            activity_code=entry.activity_code,
        )
    )
    _recalculate_invoice_totals(draft_invoice.id)
    return draft_invoice.id


def capture_timer_to_draft_time_entry(
    timer_id: int,
    *,
    pause_reason: str = "manual_pause",
    actor_user_id: int | None = None,
    auto_create_billing_item: bool = True,
) -> tuple[int | None, int | None]:
    from ..models import TimeEntry, TimeTimer

    timer = db.session.get(TimeTimer, timer_id)
    if timer is None or timer.matter_id is None:
        return None, None

    elapsed_seconds = max(0, int(timer.elapsed_seconds or 0))
    if elapsed_seconds <= 0:
        return None, None

    end_at = timer.paused_at or utc_now()
    start_at = end_at - dt.timedelta(seconds=elapsed_seconds)
    if timer.started_at and timer.started_at < end_at:
        start_at = min(start_at, timer.started_at)

    reason_label = {
        "manual": "captured from timer stop",
        "manual_pause": "captured from timer stop",
        "idle_timeout": "captured after inactivity auto-pause",
        "cap_reached": "captured after single-run cap",
        "switch": "captured on timer switch",
        "matter_closed": "captured on matter close",
    }.get((pause_reason or "").strip().lower(), "captured from timer")

    narrative_seed = (timer.label or "Timer work").strip() or "Timer work"
    narrative = f"{narrative_seed} ({reason_label})."

    duplicate = (
        TimeEntry.query.filter(
            TimeEntry.user_id == timer.user_id,
            TimeEntry.matter_id == timer.matter_id,
            TimeEntry.task_id == timer.task_id,
            TimeEntry.start_at == start_at,
            TimeEntry.end_at == end_at,
            TimeEntry.narrative == narrative,
        )
        .order_by(TimeEntry.id.desc())
        .first()
    )
    if duplicate is not None:
        invoice_id = None
        if auto_create_billing_item:
            invoice_id = ensure_draft_billing_item_for_time_entry(
                duplicate.id,
                actor_user_id=actor_user_id,
            )
        return duplicate.id, invoice_id

    hours = round(float(elapsed_seconds) / 3600.0, 4)
    entry = TimeEntry(
        user_id=timer.user_id,
        matter_id=timer.matter_id,
        task_id=timer.task_id,
        start_at=start_at,
        end_at=end_at,
        hours=hours,
        rounded_hours=_round_hours(hours, 0.1),
        narrative=narrative,
        task_code=None,
        activity_code=None,
        is_billable=True,
        status="draft",
    )
    db.session.add(entry)
    db.session.flush()

    invoice_id = None
    if auto_create_billing_item:
        invoice_id = ensure_draft_billing_item_for_time_entry(
            entry.id,
            actor_user_id=actor_user_id,
        )

    return entry.id, invoice_id


def reconcile_invoice_payment_status(invoice_id: int) -> tuple[str | None, float]:
    from ..models import Invoice, PaymentAllocation

    invoice = db.session.get(Invoice, invoice_id)
    if invoice is None:
        return None, 0.0

    settled_total = (
        db.session.query(func.coalesce(func.sum(PaymentAllocation.amount), 0.0))
        .filter(
            PaymentAllocation.invoice_id == invoice.id,
            or_(PaymentAllocation.status == "settled", PaymentAllocation.status.is_(None)),
        )
        .scalar()
        or 0.0
    )
    settled = round(float(settled_total), 2)
    outstanding = max(0.0, round(float(invoice.total or 0.0) - settled, 2))

    next_status = invoice.status
    if outstanding <= 0.01:
        next_status = "paid"
    elif settled > 0:
        next_status = "part_paid"
    elif (invoice.approved_at is not None) or (str(invoice.status or "").lower() in {"approved", "part_paid", "paid"}):
        next_status = "approved"
    else:
        next_status = "draft"

    invoice.status = next_status
    return next_status, outstanding


def schedule_invoice_collection_followups(
    invoice_id: int,
    *,
    actor_user_id: int | None = None,
) -> list[int]:
    from ..models import Invoice, Task, TaskAssignee

    invoice = db.session.get(Invoice, invoice_id)
    if invoice is None:
        return []

    assignee_id = _pick_default_matter_owner_id(invoice.matter_id)
    created_by = int(actor_user_id or assignee_id or invoice.created_by or 0)
    if created_by <= 0:
        created_by = int(invoice.created_by)

    checkpoints = [
        (7, "first reminder"),
        (14, "second reminder"),
        (30, "escalation review"),
    ]
    created_task_ids: list[int] = []
    for day_offset, label in checkpoints:
        due_date = dt.date.today() + dt.timedelta(days=day_offset)
        title = f"Invoice #{invoice.id} collection follow-up ({label})"
        existing = (
            Task.query.filter_by(matter_id=invoice.matter_id, title=title, due_date=due_date)
            .order_by(Task.id.desc())
            .first()
        )
        if existing is not None:
            continue
        task = Task(
            matter_id=invoice.matter_id,
            title=title,
            description=(
                f"Auto-created collections follow-up for invoice #{invoice.id}. "
                f"Invoice total {float(invoice.total or 0.0):.2f}."
            ),
            status="Todo",
            due_date=due_date,
            assigned_to=assignee_id,
            created_by=created_by,
            priority="Medium",
        )
        db.session.add(task)
        db.session.flush()
        if assignee_id:
            db.session.add(
                TaskAssignee(
                    task_id=task.id,
                    user_id=assignee_id,
                    assigned_by=(actor_user_id or created_by),
                )
            )
        created_task_ids.append(task.id)
    return created_task_ids


def create_portal_upload_review_task(
    upload_id: int,
    *,
    actor_user_id: int | None = None,
) -> int | None:
    from ..models import PortalUpload, Task, TaskAssignee, User

    upload = db.session.get(PortalUpload, upload_id)
    if upload is None:
        return None

    marker = f"[portal_upload:{upload.id}]"
    existing = (
        Task.query.filter(
            Task.matter_id == upload.matter_id,
            Task.description.isnot(None),
            Task.description.ilike(f"%{marker}%"),
        )
        .order_by(Task.id.desc())
        .first()
    )
    if existing is not None:
        return existing.id

    assignee_id = _pick_default_matter_owner_id(upload.matter_id)
    created_by = int(actor_user_id or assignee_id or 0)
    if created_by <= 0:
        fallback_user = db.session.query(User.id).order_by(User.id.asc()).first()
        if fallback_user is None or fallback_user[0] is None:
            return None
        created_by = int(fallback_user[0])

    task = Task(
        matter_id=upload.matter_id,
        title=f"Review portal upload: {upload.filename}",
        description=(
            f"{marker} Review and classify the client-uploaded file '{upload.filename}', "
            "then confirm filing destination and client response."
        ),
        status="Todo",
        due_date=dt.date.today() + dt.timedelta(days=1),
        assigned_to=assignee_id,
        created_by=created_by,
        priority="High",
    )
    db.session.add(task)
    db.session.flush()
    if assignee_id:
        db.session.add(
            TaskAssignee(
                task_id=task.id,
                user_id=assignee_id,
                assigned_by=(actor_user_id or created_by),
            )
        )
    return task.id


def create_engagement_signed_tasks(
    engagement_id: int,
    *,
    actor_user_id: int | None = None,
) -> list[int]:
    from ..models import EngagementLetter, Task, TaskAssignee

    engagement = db.session.get(EngagementLetter, engagement_id)
    if engagement is None:
        return []

    marker = f"[engagement:{engagement.id}]"
    existing = (
        Task.query.filter(
            Task.matter_id == engagement.matter_id,
            Task.description.isnot(None),
            Task.description.ilike(f"%{marker}%"),
        )
        .first()
    )
    if existing is not None:
        return []

    assignee_id = _pick_default_matter_owner_id(engagement.matter_id)
    created_by = int(actor_user_id or assignee_id or engagement.created_by or 0)
    if created_by <= 0:
        created_by = int(engagement.created_by)

    checklist = [
        ("Engagement signed: open matter kickoff", 1),
        ("Confirm scope, staffing, and first client update", 3),
    ]
    created_task_ids: list[int] = []
    for title, day_offset in checklist:
        task = Task(
            matter_id=engagement.matter_id,
            title=title,
            description=f"{marker} Auto-created after engagement signature.",
            status="Todo",
            due_date=dt.date.today() + dt.timedelta(days=day_offset),
            assigned_to=assignee_id,
            created_by=created_by,
            priority="High" if day_offset == 1 else "Medium",
        )
        db.session.add(task)
        db.session.flush()
        if assignee_id:
            db.session.add(
                TaskAssignee(
                    task_id=task.id,
                    user_id=assignee_id,
                    assigned_by=(actor_user_id or created_by),
                )
            )
        created_task_ids.append(task.id)
    return created_task_ids


def auto_pause_running_timers_for_matter(
    matter_id: int,
    *,
    actor_user_id: int | None = None,
    pause_reason: str = "matter_closed",
) -> dict[str, int]:
    from ..models import TimeTimer

    now = utc_now()
    timers = (
        TimeTimer.query.filter_by(matter_id=matter_id, status="running")
        .order_by(TimeTimer.started_at.asc())
        .all()
    )
    paused_count = 0
    captured_count = 0
    for timer in timers:
        elapsed = max(0, int(timer.elapsed_seconds or 0))
        if timer.started_at:
            elapsed += max(0, int((now - timer.started_at).total_seconds()))
        timer.elapsed_seconds = elapsed
        timer.status = "paused"
        timer.paused_at = now
        paused_count += 1
        entry_id, _invoice_id = capture_timer_to_draft_time_entry(
            timer.id,
            pause_reason=pause_reason,
            actor_user_id=actor_user_id,
            auto_create_billing_item=True,
        )
        if entry_id is not None:
            captured_count += 1
    return {"paused": paused_count, "captured_entries": captured_count}
