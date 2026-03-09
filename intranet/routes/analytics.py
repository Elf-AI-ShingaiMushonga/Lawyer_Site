from __future__ import annotations

import datetime as dt

from flask import abort, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_

from ..extensions import db
from ..helpers import is_admin
from ..models import (
    BurnoutSignal,
    Invoice,
    Matter,
    PaymentAllocation,
    Task,
    TaskAssignee,
    TimeEntry,
    User,
    WorkloadForecast,
)
from ..policies import visible_matter_ids
from ..roles import role_is_lawyer
from ..services.analytics_engine import AnalyticsEngine
from ..templates import page
from ..timeutils import utc_now


AVAILABLE_HOURS_PER_MONTH = 8.0 * 22.0


def _analytics_allowed() -> None:
    if not role_is_lawyer(getattr(current_user, "role", None)):
        abort(403)


def _analytics_scope_ids() -> list[int] | None:
    if is_admin():
        return None
    return visible_matter_ids()


def _analytics_scope_label(scope_ids: list[int] | None) -> str:
    if scope_ids is None:
        return "Firm-wide"
    return "Visible matters"


def _analytics_snapshot(scope_ids: list[int] | None):
    return AnalyticsEngine.compute_snapshot(
        dt.date.today(),
        matter_scope_ids=scope_ids,
        persist=False,
    )


def _analytics_workload_rows(scope_ids: list[int] | None) -> list[dict[str, object]]:
    users = [current_user] if not is_admin() else User.query.order_by(User.full_name.asc()).limit(500).all()
    user_ids = [user.id for user in users]
    open_tasks_by_user: dict[int, int] = {}
    hours_7d_by_user: dict[int, float] = {}

    if user_ids:
        task_query = (
            db.session.query(TaskAssignee.user_id, func.count(func.distinct(TaskAssignee.task_id)))
            .join(Task, Task.id == TaskAssignee.task_id)
            .filter(
                TaskAssignee.user_id.in_(user_ids),
                Task.status != "Done",
            )
        )
        legacy_has_assignee = db.session.query(TaskAssignee.id).filter(TaskAssignee.task_id == Task.id).exists()
        legacy_task_query = db.session.query(Task.assigned_to, func.count(Task.id)).filter(
            Task.assigned_to.in_(user_ids),
            Task.status != "Done",
            ~legacy_has_assignee,
        )
        hours_query = db.session.query(TimeEntry.user_id, func.coalesce(func.sum(TimeEntry.rounded_hours), 0.0)).filter(
            TimeEntry.user_id.in_(user_ids),
            TimeEntry.start_at >= utc_now() - dt.timedelta(days=7),
        )
        if scope_ids is not None:
            if scope_ids:
                task_query = task_query.filter(Task.matter_id.in_(scope_ids))
                legacy_task_query = legacy_task_query.filter(Task.matter_id.in_(scope_ids))
                hours_query = hours_query.filter(TimeEntry.matter_id.in_(scope_ids))
            else:
                task_query = task_query.filter(Task.id == -1)
                legacy_task_query = legacy_task_query.filter(Task.id == -1)
                hours_query = hours_query.filter(TimeEntry.id == -1)

        for user_id, count in task_query.group_by(TaskAssignee.user_id).all():
            if user_id is None:
                continue
            open_tasks_by_user[int(user_id)] = int(count or 0)
        for user_id, count in legacy_task_query.group_by(Task.assigned_to).all():
            if user_id is None:
                continue
            open_tasks_by_user[int(user_id)] = open_tasks_by_user.get(int(user_id), 0) + int(count or 0)
        hours_7d_by_user = {
            int(user_id): float(hours or 0.0)
            for user_id, hours in hours_query.group_by(TimeEntry.user_id).all()
            if user_id is not None
        }

    rows: list[dict[str, object]] = []
    for user in users:
        open_tasks = open_tasks_by_user.get(user.id, 0)
        hours_7d = round(hours_7d_by_user.get(user.id, 0.0), 2)
        tone = "positive"
        pressure_label = "Stable"
        if open_tasks >= 12 or hours_7d >= 45.0:
            tone = "danger"
            pressure_label = "Critical"
        elif open_tasks >= 7 or hours_7d >= 30.0:
            tone = "warning"
            pressure_label = "Watch"
        rows.append(
            {
                "user": user,
                "open_tasks": open_tasks,
                "hours_7d": hours_7d,
                "tone": tone,
                "pressure_label": pressure_label,
            }
        )
    rows.sort(key=lambda row: (-int(row["open_tasks"]), -float(row["hours_7d"]), str(row["user"].full_name).lower()))
    return rows


def _analytics_profitability_rows(scope_ids: list[int] | None) -> list[dict[str, object]]:
    matters_query = Matter.query
    if scope_ids is not None:
        if not scope_ids:
            matters_query = matters_query.filter(Matter.id == -1)
        else:
            matters_query = matters_query.filter(Matter.id.in_(scope_ids))
    matters = matters_query.order_by(Matter.opened_at.desc()).limit(200).all()
    matter_ids = [matter.id for matter in matters]
    billed_by_matter: dict[int, float] = {}
    collected_by_matter: dict[int, float] = {}
    if matter_ids:
        billed_by_matter = {
            int(matter_id): float(amount or 0.0)
            for matter_id, amount in (
                db.session.query(Invoice.matter_id, func.coalesce(func.sum(Invoice.total), 0.0))
                .filter(Invoice.matter_id.in_(matter_ids))
                .group_by(Invoice.matter_id)
                .all()
            )
        }
        collected_by_matter = {
            int(matter_id): float(amount or 0.0)
            for matter_id, amount in (
                db.session.query(Invoice.matter_id, func.coalesce(func.sum(PaymentAllocation.amount), 0.0))
                .join(PaymentAllocation, PaymentAllocation.invoice_id == Invoice.id)
                .filter(
                    Invoice.matter_id.in_(matter_ids),
                    or_(PaymentAllocation.status == "settled", PaymentAllocation.status.is_(None)),
                )
                .group_by(Invoice.matter_id)
                .all()
            )
        }

    rows: list[dict[str, object]] = []
    for matter in matters:
        billed = round(billed_by_matter.get(matter.id, 0.0), 2)
        collected = round(collected_by_matter.get(matter.id, 0.0), 2)
        realization = round((collected / billed) if billed > 0 else 0.0, 4)
        collection_gap = round(max(billed - collected, 0.0), 2)
        tone = "positive"
        if billed > 0 and realization < 0.7:
            tone = "danger"
        elif billed > 0 and realization < 0.9:
            tone = "warning"
        rows.append(
            {
                "matter": matter,
                "billed": billed,
                "collected": collected,
                "realization": realization,
                "collection_gap": collection_gap,
                "tone": tone,
            }
        )
    rows.sort(key=lambda row: (-float(row["collection_gap"]), -float(row["billed"]), str(row["matter"].matter_no).lower()))
    return rows


def _analytics_forecast_rows() -> tuple[list[WorkloadForecast], dict[int, User]]:
    rows_query = WorkloadForecast.query
    if not is_admin():
        rows_query = rows_query.filter(WorkloadForecast.user_id == current_user.id)
    rows = rows_query.order_by(WorkloadForecast.as_of_date.desc()).limit(200).all()
    user_ids = sorted({row.user_id for row in rows})
    users_by_id = {user.id: user for user in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}
    return rows, users_by_id


def _analytics_burnout_rows() -> tuple[list[BurnoutSignal], dict[int, User]]:
    rows_query = BurnoutSignal.query
    if not is_admin():
        rows_query = rows_query.filter(BurnoutSignal.user_id == current_user.id)
    rows = rows_query.order_by(BurnoutSignal.as_of_date.desc()).limit(200).all()
    user_ids = sorted({row.user_id for row in rows})
    users_by_id = {user.id: user for user in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}
    return rows, users_by_id


def _build_analytics_console(
    active_key: str,
    *,
    scope_ids: list[int] | None,
    snapshot,
    workload_rows: list[dict[str, object]] | None = None,
    profitability_rows: list[dict[str, object]] | None = None,
    forecast_rows: list[WorkloadForecast] | None = None,
    burnout_rows: list[BurnoutSignal] | None = None,
) -> dict[str, object]:
    workload_rows = workload_rows or []
    profitability_rows = profitability_rows or []
    forecast_rows = forecast_rows or []
    burnout_rows = burnout_rows or []

    collection_gap = round(max(float(snapshot.metrics.get("billed", 0.0)) - float(snapshot.metrics.get("collected", 0.0)), 0.0), 2)
    workload_pressure_count = sum(1 for row in workload_rows if str(row.get("tone")) in {"warning", "danger"})
    forecast_pressure_count = sum(
        1
        for row in forecast_rows
        if float(getattr(row, "predicted_hours", 0.0) or 0.0) >= 40.0 and float(getattr(row, "confidence", 0.0) or 0.0) >= 0.6
    )
    burnout_alert_count = sum(
        1
        for row in burnout_rows
        if float(getattr(row, "score", 0.0) or 0.0) >= 0.7 or str(getattr(row, "status", "")).lower() in {"open", "watch", "critical"}
    )
    low_realization_count = sum(
        1 for row in profitability_rows if float(row.get("billed", 0.0) or 0.0) > 0 and float(row.get("realization", 0.0) or 0.0) < 0.9
    )

    watchlist: list[dict[str, str]] = []
    if snapshot.utilization < 0.65:
        watchlist.append(
            {
                "tone": "warning",
                "title": "Utilization below target",
                "summary": "Billable output is below the operating floor for this scope. Review capture discipline and staffing allocation.",
                "href": url_for("analytics_utilization"),
                "button_label": "Open Utilization",
                "badge": f"{snapshot.utilization * 100:.1f}%",
            }
        )
    elif snapshot.utilization > 0.92:
        watchlist.append(
            {
                "tone": "danger",
                "title": "Utilization is running hot",
                "summary": "Current output suggests sustained overload. Cross-check task ownership, deadlines, and staffing before quality slips.",
                "href": url_for("analytics_workload"),
                "button_label": "Review Workload",
                "badge": f"{snapshot.utilization * 100:.1f}%",
            }
        )
    if snapshot.realization < 0.9 and collection_gap > 0:
        watchlist.append(
            {
                "tone": "danger",
                "title": "Collection leakage is visible",
                "summary": "Collected value is trailing billed value. Use the profitability view to target matters with the largest gaps.",
                "href": url_for("analytics_profitability"),
                "button_label": "Open Profitability",
                "badge": f"R {collection_gap:,.2f}",
            }
        )
    if workload_pressure_count:
        watchlist.append(
            {
                "tone": "warning",
                "title": "Capacity pressure detected",
                "summary": f"{workload_pressure_count} team member(s) have a meaningful open-task or recent-hours load signal.",
                "href": url_for("analytics_workload"),
                "button_label": "Open Workload",
                "badge": f"{workload_pressure_count} team member(s)",
            }
        )
    if forecast_pressure_count:
        watchlist.append(
            {
                "tone": "warning",
                "title": "Upcoming capacity spike",
                "summary": f"{forecast_pressure_count} forecast row(s) show high expected hours with meaningful confidence.",
                "href": url_for("analytics_forecast"),
                "button_label": "Open Forecast",
                "badge": f"{forecast_pressure_count} alert(s)",
            }
        )
    if burnout_alert_count:
        watchlist.append(
            {
                "tone": "danger",
                "title": "Burnout risk needs review",
                "summary": f"{burnout_alert_count} burnout signal(s) exceed the watch threshold and should be paired with workload decisions.",
                "href": url_for("analytics_burnout"),
                "button_label": "Open Burnout",
                "badge": f"{burnout_alert_count} signal(s)",
            }
        )
    if low_realization_count:
        watchlist.append(
            {
                "tone": "warning",
                "title": "Low-realization matters are accumulating",
                "summary": f"{low_realization_count} matter(s) have billed value but are converting poorly into cash collection.",
                "href": url_for("analytics_profitability"),
                "button_label": "Inspect Matters",
                "badge": f"{low_realization_count} matter(s)",
            }
        )

    nav = [
        {
            "key": "home",
            "label": "Overview",
            "summary": "Cross-report operating pulse across revenue, capacity, and risk.",
            "href": url_for("analytics_home"),
            "badge": f"{len(watchlist)} issue(s)",
        },
        {
            "key": "utilization",
            "label": "Utilization",
            "summary": "Billable output versus available time.",
            "href": url_for("analytics_utilization"),
            "badge": f"{snapshot.utilization * 100:.1f}%",
        },
        {
            "key": "realization",
            "label": "Realization",
            "summary": "Billed value converted into settled cash.",
            "href": url_for("analytics_realization"),
            "badge": f"{snapshot.realization * 100:.1f}%",
        },
        {
            "key": "ehr",
            "label": "EHR",
            "summary": "Collected value per billable hour.",
            "href": url_for("analytics_ehr"),
            "badge": f"R {snapshot.effective_hourly_rate:,.2f}",
        },
        {
            "key": "workload",
            "label": "Workload",
            "summary": "Task and effort concentration by person.",
            "href": url_for("analytics_workload"),
            "badge": f"{workload_pressure_count} flagged",
        },
        {
            "key": "profitability",
            "label": "Profitability",
            "summary": "Matter-level collection gap and realization quality.",
            "href": url_for("analytics_profitability"),
            "badge": f"{low_realization_count} flagged",
        },
        {
            "key": "forecast",
            "label": "Forecast",
            "summary": "Forward-looking capacity pressure.",
            "href": url_for("analytics_forecast"),
            "badge": f"{forecast_pressure_count} alerts",
        },
        {
            "key": "burnout",
            "label": "Burnout",
            "summary": "Delivery strain and wellbeing risk indicators.",
            "href": url_for("analytics_burnout"),
            "badge": f"{burnout_alert_count} alerts",
        },
    ]
    for item in nav:
        item["active"] = item["key"] == active_key

    return {
        "headline": "Analytics Command Center",
        "scope_label": _analytics_scope_label(scope_ids),
        "as_of_label": snapshot.as_of_date,
        "summary_cards": [
            {
                "label": "Utilization",
                "value": f"{snapshot.utilization * 100:.1f}%",
                "meta": "Billable hours against a 176-hour monthly operating baseline.",
            },
            {
                "label": "Realization",
                "value": f"{snapshot.realization * 100:.1f}%",
                "meta": "Settled cash captured from billed value.",
            },
            {
                "label": "Effective Hourly Rate",
                "value": f"R {snapshot.effective_hourly_rate:,.2f}",
                "meta": "Collected value per billable hour.",
            },
            {
                "label": "Billable Hours",
                "value": f"{float(snapshot.metrics.get('billable_hours', 0.0)):.2f}",
                "meta": "Billable hours currently within scope.",
            },
            {
                "label": "Collection Gap",
                "value": f"R {collection_gap:,.2f}",
                "meta": "Billed value that has not yet settled.",
            },
            {
                "label": "Risk Signals",
                "value": str(workload_pressure_count + forecast_pressure_count + burnout_alert_count),
                "meta": "Combined workload, forecast, and burnout alerts.",
            },
        ],
        "watchlist": watchlist,
        "nav": nav,
        "report_actions": [
            {
                "title": "Time Review",
                "summary": "Open the capture and review flows to fix utilization problems quickly.",
                "href": url_for("time_review"),
                "badge": "Time",
            },
            {
                "title": "Billing Pipeline",
                "summary": "Move draft, pending, and outstanding invoices before realization drops further.",
                "href": url_for("billing_invoices"),
                "badge": "Revenue",
            },
            {
                "title": "Matter Search",
                "summary": "Pivot from flagged metrics into the underlying matters and documents.",
                "href": url_for("search"),
                "badge": "Search",
            },
            {
                "title": "Dashboard",
                "summary": "Return to the operational cockpit once the analytics decision is made.",
                "href": url_for("dashboard"),
                "badge": "Ops",
            },
        ],
    }


def register_analytics_routes(app):
    @app.get("/analytics")
    @login_required
    def analytics_home():
        _analytics_allowed()
        scope_ids = _analytics_scope_ids()
        snapshot = _analytics_snapshot(scope_ids)
        workload_rows = _analytics_workload_rows(scope_ids)
        profitability_rows = _analytics_profitability_rows(scope_ids)
        forecast_rows, forecast_users_by_id = _analytics_forecast_rows()
        burnout_rows, burnout_users_by_id = _analytics_burnout_rows()
        analytics = _build_analytics_console(
            "home",
            scope_ids=scope_ids,
            snapshot=snapshot,
            workload_rows=workload_rows,
            profitability_rows=profitability_rows,
            forecast_rows=forecast_rows,
            burnout_rows=burnout_rows,
        )
        return page(
            "Analytics",
            "analytics/index.html",
            analytics=analytics,
            snapshot=snapshot,
            workload_rows=workload_rows[:8],
            profitability_rows=profitability_rows[:8],
            forecast_rows=forecast_rows[:8],
            burnout_rows=burnout_rows[:8],
            forecast_users_by_id=forecast_users_by_id,
            burnout_users_by_id=burnout_users_by_id,
            available_hours_per_month=AVAILABLE_HOURS_PER_MONTH,
        )

    @app.get("/analytics/utilization")
    @login_required
    def analytics_utilization():
        _analytics_allowed()
        scope_ids = _analytics_scope_ids()
        snapshot = _analytics_snapshot(scope_ids)
        analytics = _build_analytics_console("utilization", scope_ids=scope_ids, snapshot=snapshot)
        return page(
            "Utilization",
            "analytics/utilization.html",
            analytics=analytics,
            snapshot=snapshot,
            collection_gap=round(max(float(snapshot.metrics.get("billed", 0.0)) - float(snapshot.metrics.get("collected", 0.0)), 0.0), 2),
            available_hours_per_month=AVAILABLE_HOURS_PER_MONTH,
        )

    @app.get("/analytics/realization")
    @login_required
    def analytics_realization():
        _analytics_allowed()
        scope_ids = _analytics_scope_ids()
        snapshot = _analytics_snapshot(scope_ids)
        profitability_rows = _analytics_profitability_rows(scope_ids)
        analytics = _build_analytics_console(
            "realization",
            scope_ids=scope_ids,
            snapshot=snapshot,
            profitability_rows=profitability_rows,
        )
        return page(
            "Realization",
            "analytics/realization.html",
            analytics=analytics,
            snapshot=snapshot,
            profitability_rows=profitability_rows[:12],
            collection_gap=round(max(float(snapshot.metrics.get("billed", 0.0)) - float(snapshot.metrics.get("collected", 0.0)), 0.0), 2),
        )

    @app.get("/analytics/ehr")
    @login_required
    def analytics_ehr():
        _analytics_allowed()
        scope_ids = _analytics_scope_ids()
        snapshot = _analytics_snapshot(scope_ids)
        analytics = _build_analytics_console("ehr", scope_ids=scope_ids, snapshot=snapshot)
        return page(
            "Effective Hourly Rate",
            "analytics/ehr.html",
            analytics=analytics,
            snapshot=snapshot,
            available_hours_per_month=AVAILABLE_HOURS_PER_MONTH,
        )

    @app.get("/analytics/workload")
    @login_required
    def analytics_workload():
        _analytics_allowed()
        scope_ids = _analytics_scope_ids()
        snapshot = _analytics_snapshot(scope_ids)
        rows = _analytics_workload_rows(scope_ids)
        analytics = _build_analytics_console(
            "workload",
            scope_ids=scope_ids,
            snapshot=snapshot,
            workload_rows=rows,
        )
        return page("Workload", "analytics/workload.html", analytics=analytics, rows=rows)

    @app.get("/analytics/profitability")
    @login_required
    def analytics_profitability():
        _analytics_allowed()
        scope_ids = _analytics_scope_ids()
        snapshot = _analytics_snapshot(scope_ids)
        rows = _analytics_profitability_rows(scope_ids)
        analytics = _build_analytics_console(
            "profitability",
            scope_ids=scope_ids,
            snapshot=snapshot,
            profitability_rows=rows,
        )
        return page("Profitability", "analytics/profitability.html", analytics=analytics, rows=rows)

    @app.get("/analytics/forecast")
    @login_required
    def analytics_forecast():
        _analytics_allowed()
        scope_ids = _analytics_scope_ids()
        snapshot = _analytics_snapshot(scope_ids)
        rows, users_by_id = _analytics_forecast_rows()
        analytics = _build_analytics_console(
            "forecast",
            scope_ids=scope_ids,
            snapshot=snapshot,
            forecast_rows=rows,
        )
        return page("Forecast", "analytics/forecast.html", analytics=analytics, rows=rows, users_by_id=users_by_id)

    @app.get("/analytics/burnout")
    @login_required
    def analytics_burnout():
        _analytics_allowed()
        scope_ids = _analytics_scope_ids()
        snapshot = _analytics_snapshot(scope_ids)
        rows, users_by_id = _analytics_burnout_rows()
        analytics = _build_analytics_console(
            "burnout",
            scope_ids=scope_ids,
            snapshot=snapshot,
            burnout_rows=rows,
        )
        return page("Burnout Signals", "analytics/burnout.html", analytics=analytics, rows=rows, users_by_id=users_by_id)
