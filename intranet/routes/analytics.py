from __future__ import annotations

import datetime as dt

from flask import abort
from flask_login import current_user, login_required
from sqlalchemy import func

from ..extensions import db
from ..helpers import is_admin
from ..models import (
    BurnoutSignal,
    Invoice,
    Matter,
    PaymentAllocation,
    Task,
    TimeEntry,
    User,
    WorkloadForecast,
)
from ..policies import visible_matter_ids
from ..services.analytics_engine import AnalyticsEngine
from ..templates import page


def _analytics_allowed() -> None:
    if current_user.role not in {"admin", "lawyer"}:
        abort(403)


def _analytics_scope_ids() -> list[int] | None:
    if is_admin():
        return None
    return visible_matter_ids()


def register_analytics_routes(app):
    @app.get("/analytics/utilization")
    @login_required
    def analytics_utilization():
        _analytics_allowed()
        snapshot = AnalyticsEngine.compute_snapshot(
            dt.date.today(),
            matter_scope_ids=_analytics_scope_ids(),
            persist=False,
        )
        return page("Utilization", "analytics/utilization.html", snapshot=snapshot)

    @app.get("/analytics/realization")
    @login_required
    def analytics_realization():
        _analytics_allowed()
        snapshot = AnalyticsEngine.compute_snapshot(
            dt.date.today(),
            matter_scope_ids=_analytics_scope_ids(),
            persist=False,
        )
        return page("Realization", "analytics/realization.html", snapshot=snapshot)

    @app.get("/analytics/ehr")
    @login_required
    def analytics_ehr():
        _analytics_allowed()
        snapshot = AnalyticsEngine.compute_snapshot(
            dt.date.today(),
            matter_scope_ids=_analytics_scope_ids(),
            persist=False,
        )
        return page("Effective Hourly Rate", "analytics/ehr.html", snapshot=snapshot)

    @app.get("/analytics/workload")
    @login_required
    def analytics_workload():
        _analytics_allowed()
        scope_ids = _analytics_scope_ids()
        users = [current_user] if not is_admin() else User.query.order_by(User.full_name.asc()).limit(500).all()
        user_ids = [u.id for u in users]
        open_tasks_by_user: dict[int, int] = {}
        hours_7d_by_user: dict[int, float] = {}

        if user_ids:
            task_query = db.session.query(Task.assigned_to, func.count(Task.id)).filter(
                Task.assigned_to.in_(user_ids),
                Task.status != "Done",
            )
            hours_query = db.session.query(TimeEntry.user_id, func.coalesce(func.sum(TimeEntry.rounded_hours), 0.0)).filter(
                TimeEntry.user_id.in_(user_ids),
                TimeEntry.start_at >= dt.datetime.utcnow() - dt.timedelta(days=7),
            )
            if scope_ids is not None:
                if scope_ids:
                    task_query = task_query.filter(Task.matter_id.in_(scope_ids))
                    hours_query = hours_query.filter(TimeEntry.matter_id.in_(scope_ids))
                else:
                    task_query = task_query.filter(Task.id == -1)
                    hours_query = hours_query.filter(TimeEntry.id == -1)

            open_tasks_by_user = {
                int(user_id): int(count)
                for user_id, count in task_query.group_by(Task.assigned_to).all()
                if user_id is not None
            }
            hours_7d_by_user = {
                int(user_id): float(hours or 0.0)
                for user_id, hours in hours_query.group_by(TimeEntry.user_id).all()
                if user_id is not None
            }

        rows = []
        for user in users:
            open_tasks = open_tasks_by_user.get(user.id, 0)
            hours_7d = hours_7d_by_user.get(user.id, 0.0)
            rows.append({"user": user, "open_tasks": open_tasks, "hours_7d": round(hours_7d, 2)})
        return page("Workload", "analytics/workload.html", rows=rows)

    @app.get("/analytics/profitability")
    @login_required
    def analytics_profitability():
        _analytics_allowed()
        scope_ids = _analytics_scope_ids()
        matters_query = Matter.query
        if scope_ids is not None:
            if not scope_ids:
                matters_query = matters_query.filter(Matter.id == -1)
            else:
                matters_query = matters_query.filter(Matter.id.in_(scope_ids))
        matters = matters_query.order_by(Matter.opened_at.desc()).limit(200).all()
        matter_ids = [m.id for m in matters]
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
                    .filter(Invoice.matter_id.in_(matter_ids))
                    .group_by(Invoice.matter_id)
                    .all()
                )
            }
        rows = []
        for matter in matters:
            billed = billed_by_matter.get(matter.id, 0.0)
            collected = collected_by_matter.get(matter.id, 0.0)
            rows.append(
                {
                    "matter": matter,
                    "billed": round(billed, 2),
                    "collected": round(collected, 2),
                    "realization": round((collected / billed) if billed > 0 else 0.0, 4),
                }
            )
        return page("Profitability", "analytics/profitability.html", rows=rows)

    @app.get("/analytics/forecast")
    @login_required
    def analytics_forecast():
        _analytics_allowed()
        rows_query = WorkloadForecast.query
        if not is_admin():
            rows_query = rows_query.filter(WorkloadForecast.user_id == current_user.id)
        rows = rows_query.order_by(WorkloadForecast.as_of_date.desc()).limit(200).all()
        user_ids = sorted({row.user_id for row in rows})
        users_by_id = {u.id: u for u in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}
        return page("Forecast", "analytics/forecast.html", rows=rows, users_by_id=users_by_id)

    @app.get("/analytics/burnout")
    @login_required
    def analytics_burnout():
        _analytics_allowed()
        rows_query = BurnoutSignal.query
        if not is_admin():
            rows_query = rows_query.filter(BurnoutSignal.user_id == current_user.id)
        rows = rows_query.order_by(BurnoutSignal.as_of_date.desc()).limit(200).all()
        user_ids = sorted({row.user_id for row in rows})
        users_by_id = {u.id: u for u in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}
        return page("Burnout Signals", "analytics/burnout.html", rows=rows, users_by_id=users_by_id)
