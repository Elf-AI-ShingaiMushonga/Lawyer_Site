from __future__ import annotations

import datetime as dt
from ..timeutils import utc_now

from sqlalchemy import and_, or_

from ..extensions import db
from ..types import InvoiceBuildResult


class BillingEngine:
    """Invoice construction from approved time and expenses."""

    @staticmethod
    def generate_invoice(
        matter_id: int,
        period: tuple[dt.date, dt.date],
        *,
        created_by: int | None = None,
    ) -> InvoiceBuildResult:
        from ..models import ExpenseEntry, Invoice, InvoiceLine, Matter, TimeEntry

        period_start, period_end = period
        arrangement = BillingEngine._active_fee_arrangement(matter_id)
        time_rows = (
            TimeEntry.query.filter(
                TimeEntry.matter_id == matter_id,
                TimeEntry.status == "approved",
                TimeEntry.locked_at.is_(None),
                TimeEntry.start_at >= dt.datetime.combine(period_start, dt.time.min),
                TimeEntry.start_at <= dt.datetime.combine(period_end, dt.time.max),
                ~TimeEntry.id.in_(db.session.query(InvoiceLine.time_entry_id).filter(InvoiceLine.time_entry_id.isnot(None))),
            )
            .order_by(TimeEntry.start_at.asc())
            .all()
        )
        expense_rows = (
            ExpenseEntry.query.filter(
                ExpenseEntry.matter_id == matter_id,
                ExpenseEntry.status == "approved",
                ExpenseEntry.invoice_id.is_(None),
                ExpenseEntry.incurred_on >= period_start,
                ExpenseEntry.incurred_on <= period_end,
            )
            .order_by(ExpenseEntry.incurred_on.asc())
            .all()
        )

        if not time_rows and not expense_rows:
            return InvoiceBuildResult(invoice_id=None, line_count=0, subtotal=0.0, tax_total=0.0, total=0.0)

        matter = db.session.get(Matter, matter_id)
        invoice = Invoice(
            matter_id=matter_id,
            client_name=(matter.client_name if matter else "Client"),
            period_start=period_start,
            period_end=period_end,
            status="draft",
            subtotal=0.0,
            tax_total=0.0,
            total=0.0,
            created_by=(created_by or (time_rows[0].user_id if time_rows else expense_rows[0].user_id)),
        )
        db.session.add(invoice)
        db.session.flush()

        subtotal = 0.0
        for row in time_rows:
            rate = BillingEngine._resolve_rate(row, matter_id, arrangement=arrangement)
            billable_hours = float(row.rounded_hours if row.rounded_hours is not None else (row.hours or 0.0))
            amount = billable_hours * rate
            line = InvoiceLine(
                invoice_id=invoice.id,
                time_entry_id=row.id,
                description=row.narrative or "Time entry",
                hours=billable_hours,
                rate=rate,
                amount=amount,
                tax_amount=0.0,
                task_code=row.task_code,
                activity_code=row.activity_code,
            )
            db.session.add(line)
            subtotal += amount
            row.status = "billed"
            row.locked_at = utc_now()

        for exp in expense_rows:
            line = InvoiceLine(
                invoice_id=invoice.id,
                expense_id=exp.id,
                description=exp.description or exp.category,
                hours=0.0,
                rate=0.0,
                amount=float(exp.amount or 0),
                tax_amount=0.0,
                task_code=None,
                activity_code=None,
            )
            db.session.add(line)
            subtotal += float(exp.amount or 0)
            exp.invoice_id = invoice.id

        subtotal, arrangement_line_count = BillingEngine._apply_fee_arrangement(invoice.id, arrangement, subtotal)
        tax_rate = BillingEngine._resolve_tax_rate(matter_id)
        tax_total = round(subtotal * tax_rate, 2)
        total = round(subtotal + tax_total, 2)

        invoice.subtotal = round(subtotal, 2)
        invoice.tax_total = tax_total
        invoice.total = total
        db.session.commit()

        return InvoiceBuildResult(
            invoice_id=invoice.id,
            line_count=len(time_rows) + len(expense_rows) + arrangement_line_count,
            subtotal=invoice.subtotal,
            tax_total=invoice.tax_total,
            total=invoice.total,
        )

    @staticmethod
    def _resolve_rate(time_row, matter_id: int, arrangement=None) -> float:
        from ..models import RateCard

        if arrangement is not None:
            if str(arrangement.arrangement_type or "").strip().lower() == "blended":
                blended_rate = float(arrangement.blended_rate or 0.0)
                if blended_rate > 0:
                    return blended_rate

        rates = (
            RateCard.query.filter(
                or_(RateCard.matter_id == matter_id, RateCard.matter_id.is_(None)),
                or_(RateCard.user_id == time_row.user_id, RateCard.user_id.is_(None)),
                RateCard.is_active.is_(True),
            )
            .order_by(RateCard.matter_id.desc().nullslast(), RateCard.user_id.desc().nullslast(), RateCard.id.desc())
            .all()
        )
        if rates:
            return float(rates[0].rate_per_hour or 0)
        return 0.0

    @staticmethod
    def _active_fee_arrangement(matter_id: int):
        from ..models import FeeArrangement

        return FeeArrangement.query.filter_by(matter_id=matter_id).order_by(FeeArrangement.id.desc()).first()

    @staticmethod
    def _apply_fee_arrangement(invoice_id: int, arrangement, subtotal: float) -> tuple[float, int]:
        from ..models import InvoiceLine

        if arrangement is None:
            return round(subtotal, 2), 0

        arrangement_type = str(arrangement.arrangement_type or "").strip().lower()
        adjustment_amount = 0.0
        description = None

        if arrangement_type == "fixed" and arrangement.fixed_amount is not None:
            target = round(float(arrangement.fixed_amount), 2)
            adjustment_amount = round(target - subtotal, 2)
            description = "Fee arrangement adjustment (fixed)"
        elif arrangement_type == "capped" and arrangement.cap_amount is not None:
            cap = round(float(arrangement.cap_amount), 2)
            if subtotal > cap:
                adjustment_amount = round(cap - subtotal, 2)
                description = "Fee arrangement adjustment (cap)"

        if not description or abs(adjustment_amount) < 0.01:
            return round(subtotal, 2), 0

        db.session.add(
            InvoiceLine(
                invoice_id=invoice_id,
                description=description,
                hours=0.0,
                rate=0.0,
                amount=adjustment_amount,
                tax_amount=0.0,
                task_code=None,
                activity_code=None,
            )
        )
        return round(subtotal + adjustment_amount, 2), 1

    @staticmethod
    def _resolve_tax_rate(matter_id: int) -> float:
        from ..models import Matter, TaxRule

        matter = db.session.get(Matter, matter_id)
        jurisdiction = matter.jurisdiction if matter else "ZA"
        rule = (
            TaxRule.query.filter_by(jurisdiction=jurisdiction, is_active=True)
            .order_by(TaxRule.id.desc())
            .first()
        )
        return float((rule.rate_percent or 0) / 100 if rule else 0)
