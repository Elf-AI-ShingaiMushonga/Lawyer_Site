from __future__ import annotations

import datetime as dt

from sqlalchemy import func

from ..extensions import db
from ..types import AnalyticsSnapshot


class AnalyticsEngine:
    """Permission-safe KPI materialization service."""

    @staticmethod
    def compute_snapshot(
        as_of_date: dt.date,
        matter_scope_ids: list[int] | None = None,
        persist: bool = True,
    ) -> AnalyticsSnapshot:
        from ..models import AnalyticsMetricSnapshot, Invoice, PaymentAllocation, TimeEntry

        time_cutoff = dt.datetime.combine(as_of_date, dt.time.max)
        billable_query = db.session.query(func.coalesce(func.sum(TimeEntry.hours), 0.0)).filter(
            TimeEntry.is_billable.is_(True),
            TimeEntry.start_at <= time_cutoff,
        )
        billed_query = db.session.query(func.coalesce(func.sum(Invoice.total), 0.0)).filter(Invoice.created_at <= time_cutoff)
        collected_query = (
            db.session.query(func.coalesce(func.sum(PaymentAllocation.amount), 0.0))
            .join(Invoice, Invoice.id == PaymentAllocation.invoice_id)
            .filter(PaymentAllocation.allocated_at <= time_cutoff)
        )

        if matter_scope_ids is not None:
            if not matter_scope_ids:
                billable_hours = 0.0
                billed = 0.0
                collected = 0.0
            else:
                billable_hours = float(billable_query.filter(TimeEntry.matter_id.in_(matter_scope_ids)).scalar() or 0.0)
                billed = float(billed_query.filter(Invoice.matter_id.in_(matter_scope_ids)).scalar() or 0.0)
                collected = float(collected_query.filter(Invoice.matter_id.in_(matter_scope_ids)).scalar() or 0.0)
        else:
            billable_hours = float(billable_query.scalar() or 0.0)
            billed = float(billed_query.scalar() or 0.0)
            collected = float(collected_query.scalar() or 0.0)

        available_hours = max(1.0, 8.0 * 22.0)
        utilization = billable_hours / available_hours
        realization = (collected / billed) if billed > 0 else 0.0
        effective_hourly_rate = (collected / billable_hours) if billable_hours > 0 else 0.0

        metrics = {
            "utilization": round(utilization, 4),
            "realization": round(realization, 4),
            "effective_hourly_rate": round(effective_hourly_rate, 2),
            "billable_hours": round(billable_hours, 2),
            "billed": round(billed, 2),
            "collected": round(collected, 2),
        }

        if persist:
            scope_type = "firm" if matter_scope_ids is None else "restricted"
            metric_keys = list(metrics.keys())
            existing_rows = (
                AnalyticsMetricSnapshot.query.filter(
                    AnalyticsMetricSnapshot.as_of_date == as_of_date,
                    AnalyticsMetricSnapshot.scope_type == scope_type,
                    AnalyticsMetricSnapshot.scope_id.is_(None),
                    AnalyticsMetricSnapshot.metric_key.in_(metric_keys),
                ).all()
            )
            existing_by_key = {row.metric_key: row for row in existing_rows}
            for key, value in metrics.items():
                existing = existing_by_key.get(key)
                if existing is None:
                    existing = AnalyticsMetricSnapshot(
                        as_of_date=as_of_date,
                        metric_key=key,
                        scope_type=scope_type,
                        scope_id=None,
                    )
                    db.session.add(existing)
                    existing_by_key[key] = existing
                existing.value_num = value
            db.session.commit()

        return AnalyticsSnapshot(
            as_of_date=as_of_date.isoformat(),
            utilization=metrics["utilization"],
            realization=metrics["realization"],
            effective_hourly_rate=metrics["effective_hourly_rate"],
            metrics=metrics,
        )
