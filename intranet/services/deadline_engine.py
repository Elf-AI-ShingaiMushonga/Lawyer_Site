from __future__ import annotations

import datetime as dt

from ..extensions import db
from ..types import DeadlineCalculationTrace


class DeadlineEngine:
    """Rules-based deadline calculations with explainability traces."""

    @staticmethod
    def calculate(
        matter_id: int,
        trigger_event_id: int | None,
        *,
        rule_id: int | None = None,
        base_date: dt.date | None = None,
    ) -> DeadlineCalculationTrace:
        from ..models import DeadlineRule, HolidayCalendar, MatterTimelineEvent

        if base_date is None:
            if trigger_event_id:
                event = db.session.get(MatterTimelineEvent, trigger_event_id)
                base_date = event.event_date if event else dt.date.today()
            else:
                base_date = dt.date.today()

        rule = None
        if rule_id is not None:
            rule = db.session.get(DeadlineRule, rule_id)
        if rule is None:
            rule = (
                DeadlineRule.query.filter_by(matter_id=matter_id, is_active=True)
                .order_by(DeadlineRule.id.desc())
                .first()
            )
        if rule is None:
            rule = (
                DeadlineRule.query.filter_by(matter_id=None, is_active=True)
                .order_by(DeadlineRule.id.desc())
                .first()
            )

        offset_days = int(rule.offset_days if rule else 0)
        due_date = base_date + dt.timedelta(days=offset_days)

        holiday_adjustments = 0
        adjusted = False
        if rule and rule.business_day_adjust:
            adjusted = True
            while due_date.weekday() >= 5 or DeadlineEngine._is_holiday(due_date, rule.jurisdiction, rule.office_id):
                due_date += dt.timedelta(days=1)
                holiday_adjustments += 1

        return DeadlineCalculationTrace(
            matter_id=matter_id,
            trigger_event_id=trigger_event_id,
            rule_name=(rule.name if rule else "manual"),
            base_date_iso=base_date.isoformat(),
            offset_days=offset_days,
            adjusted_for_business_day=adjusted,
            holiday_adjustments=holiday_adjustments,
            result_due_at_iso=due_date.isoformat(),
        )

    @staticmethod
    def _is_holiday(day: dt.date, jurisdiction: str | None, office_id: int | None) -> bool:
        from ..models import HolidayCalendar

        q = HolidayCalendar.query.filter(HolidayCalendar.holiday_date == day)
        if jurisdiction:
            q = q.filter((HolidayCalendar.jurisdiction == jurisdiction) | (HolidayCalendar.jurisdiction.is_(None)))
        if office_id:
            q = q.filter((HolidayCalendar.office_id == office_id) | (HolidayCalendar.office_id.is_(None)))
        return q.first() is not None
