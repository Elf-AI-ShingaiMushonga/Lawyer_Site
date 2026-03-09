from __future__ import annotations

import datetime as dt
from ..timeutils import utc_now

from flask import abort, flash, redirect, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from ..extensions import db
from ..helpers import audit
from ..models import DirectorTeamMember, Matter, MatterMember, Task, TaskAssignee, TimeEntry, User
from ..roles import role_display_name, role_is_director
from ..services.director_team import director_team_member_ids, team_candidate_users_query, user_can_be_team_member
from ..templates import page


def _director_required() -> None:
    if not role_is_director(getattr(current_user, "role", None)):
        abort(403)


def _build_personnel_rows(team_members: list[User]) -> tuple[list[dict], dict]:
    user_ids = [int(user.id) for user in team_members]
    if not user_ids:
        return [], {
            "headcount": 0,
            "active_matters": 0,
            "open_tasks": 0,
            "overdue_tasks": 0,
            "due_7d": 0,
            "hours_7d": 0.0,
            "hours_30d": 0.0,
            "billable_30d": 0.0,
            "pending_time_entries": 0,
            "avg_utilization_pct_30d": 0.0,
        }

    today = dt.date.today()
    week_end = today + dt.timedelta(days=7)
    now_utc = utc_now()
    window_7d = now_utc - dt.timedelta(days=7)
    window_30d = now_utc - dt.timedelta(days=30)
    monthly_target_hours = 160.0

    matter_counts = {
        int(user_id): int(count or 0)
        for user_id, count in (
            db.session.query(MatterMember.user_id, func.count(func.distinct(MatterMember.matter_id)))
            .filter(MatterMember.user_id.in_(user_ids))
            .group_by(MatterMember.user_id)
            .all()
        )
        if user_id is not None
    }
    active_matter_counts = {
        int(user_id): int(count or 0)
        for user_id, count in (
            db.session.query(MatterMember.user_id, func.count(func.distinct(MatterMember.matter_id)))
            .join(Matter, Matter.id == MatterMember.matter_id)
            .filter(
                MatterMember.user_id.in_(user_ids),
                Matter.status != "Closed",
            )
            .group_by(MatterMember.user_id)
            .all()
        )
        if user_id is not None
    }

    has_assignee = db.session.query(TaskAssignee.id).filter(TaskAssignee.task_id == Task.id).exists()

    def _task_counts(*extra_filters):
        assignee_query = (
            db.session.query(TaskAssignee.user_id, func.count(func.distinct(TaskAssignee.task_id)))
            .join(Task, Task.id == TaskAssignee.task_id)
            .filter(TaskAssignee.user_id.in_(user_ids))
        )
        legacy_query = (
            db.session.query(Task.assigned_to, func.count(Task.id))
            .filter(
                Task.assigned_to.in_(user_ids),
                ~has_assignee,
            )
        )
        for predicate in extra_filters:
            assignee_query = assignee_query.filter(predicate)
            legacy_query = legacy_query.filter(predicate)
        counts: dict[int, int] = {}
        for user_id, count in assignee_query.group_by(TaskAssignee.user_id).all():
            if user_id is None:
                continue
            counts[int(user_id)] = int(count or 0)
        for user_id, count in legacy_query.group_by(Task.assigned_to).all():
            if user_id is None:
                continue
            counts[int(user_id)] = counts.get(int(user_id), 0) + int(count or 0)
        return counts

    open_task_counts = _task_counts(Task.status != "Done")
    overdue_task_counts = _task_counts(Task.status != "Done", Task.due_date.isnot(None), Task.due_date < today)
    due_7d_task_counts = _task_counts(
        Task.status != "Done",
        Task.due_date.isnot(None),
        Task.due_date >= today,
        Task.due_date <= week_end,
    )

    hours_7d = {
        int(user_id): float(hours or 0.0)
        for user_id, hours in (
            db.session.query(TimeEntry.user_id, func.coalesce(func.sum(TimeEntry.rounded_hours), 0.0))
            .filter(
                TimeEntry.user_id.in_(user_ids),
                TimeEntry.start_at >= window_7d,
            )
            .group_by(TimeEntry.user_id)
            .all()
        )
        if user_id is not None
    }
    hours_30d = {
        int(user_id): float(hours or 0.0)
        for user_id, hours in (
            db.session.query(TimeEntry.user_id, func.coalesce(func.sum(TimeEntry.rounded_hours), 0.0))
            .filter(
                TimeEntry.user_id.in_(user_ids),
                TimeEntry.start_at >= window_30d,
            )
            .group_by(TimeEntry.user_id)
            .all()
        )
        if user_id is not None
    }
    billable_30d = {
        int(user_id): float(hours or 0.0)
        for user_id, hours in (
            db.session.query(TimeEntry.user_id, func.coalesce(func.sum(TimeEntry.rounded_hours), 0.0))
            .filter(
                TimeEntry.user_id.in_(user_ids),
                TimeEntry.start_at >= window_30d,
                TimeEntry.is_billable.is_(True),
            )
            .group_by(TimeEntry.user_id)
            .all()
        )
        if user_id is not None
    }
    pending_time_counts = {
        int(user_id): int(count or 0)
        for user_id, count in (
            db.session.query(TimeEntry.user_id, func.count(TimeEntry.id))
            .filter(
                TimeEntry.user_id.in_(user_ids),
                TimeEntry.status.in_(["draft", "needs_review"]),
            )
            .group_by(TimeEntry.user_id)
            .all()
        )
        if user_id is not None
    }

    rows: list[dict] = []
    totals = {
        "headcount": len(team_members),
        "active_matters": 0,
        "open_tasks": 0,
        "overdue_tasks": 0,
        "due_7d": 0,
        "hours_7d": 0.0,
        "hours_30d": 0.0,
        "billable_30d": 0.0,
        "pending_time_entries": 0,
        "avg_utilization_pct_30d": 0.0,
    }

    for user in team_members:
        uid = int(user.id)
        user_hours_30d = float(hours_30d.get(uid, 0.0))
        utilization_pct = (user_hours_30d / monthly_target_hours) * 100.0 if monthly_target_hours > 0 else 0.0
        row = {
            "user": user,
            "matters": matter_counts.get(uid, 0),
            "active_matters": active_matter_counts.get(uid, 0),
            "open_tasks": open_task_counts.get(uid, 0),
            "overdue_tasks": overdue_task_counts.get(uid, 0),
            "due_7d": due_7d_task_counts.get(uid, 0),
            "hours_7d": round(float(hours_7d.get(uid, 0.0)), 2),
            "hours_30d": round(user_hours_30d, 2),
            "billable_30d": round(float(billable_30d.get(uid, 0.0)), 2),
            "pending_time_entries": pending_time_counts.get(uid, 0),
            "utilization_pct_30d": round(utilization_pct, 1),
        }
        rows.append(row)
        totals["active_matters"] += int(row["active_matters"])
        totals["open_tasks"] += int(row["open_tasks"])
        totals["overdue_tasks"] += int(row["overdue_tasks"])
        totals["due_7d"] += int(row["due_7d"])
        totals["hours_7d"] += float(row["hours_7d"])
        totals["hours_30d"] += float(row["hours_30d"])
        totals["billable_30d"] += float(row["billable_30d"])
        totals["pending_time_entries"] += int(row["pending_time_entries"])
        totals["avg_utilization_pct_30d"] += float(row["utilization_pct_30d"])

    if rows:
        totals["avg_utilization_pct_30d"] = round(totals["avg_utilization_pct_30d"] / len(rows), 1)
    totals["hours_7d"] = round(totals["hours_7d"], 2)
    totals["hours_30d"] = round(totals["hours_30d"], 2)
    totals["billable_30d"] = round(totals["billable_30d"], 2)
    return rows, totals


def register_director_personnel_routes(app):
    @app.route("/director/personnel", methods=["GET", "POST"])
    @login_required
    def director_personnel():
        _director_required()

        if request.method == "POST":
            action = str(request.form.get("action") or "").strip().lower()
            if action == "assign_member":
                member_user_id = request.form.get("member_user_id", type=int)
                if not member_user_id:
                    flash("Select a team member to assign.", "warning")
                    return redirect(url_for("director_personnel"))
                if int(member_user_id) == int(current_user.id):
                    flash("Directors cannot assign themselves as a team member.", "warning")
                    return redirect(url_for("director_personnel"))

                member = db.session.get(User, int(member_user_id))
                if member is None:
                    flash("Selected user was not found.", "warning")
                    return redirect(url_for("director_personnel"))
                if not user_can_be_team_member(member):
                    flash("Only attorney roles can be assigned to a director team.", "warning")
                    return redirect(url_for("director_personnel"))

                existing = DirectorTeamMember.query.filter_by(member_user_id=int(member_user_id)).first()
                if existing is not None and int(existing.director_id) != int(current_user.id):
                    other_director = db.session.get(User, int(existing.director_id))
                    owner_name = other_director.full_name if other_director is not None else "another director"
                    flash(f"{member.full_name} is already assigned to {owner_name}.", "warning")
                    return redirect(url_for("director_personnel"))
                if existing is not None:
                    flash("User is already part of your team.", "warning")
                    return redirect(url_for("director_personnel"))

                row = DirectorTeamMember(
                    director_id=int(current_user.id),
                    member_user_id=int(member_user_id),
                    assigned_by=int(current_user.id),
                )
                db.session.add(row)
                db.session.commit()
                audit(
                    "director_team_member_assign",
                    "DirectorTeamMember",
                    row.id,
                    {"member_user_id": int(member_user_id)},
                )
                flash("Team member assigned.", "info")
                return redirect(url_for("director_personnel"))

            if action == "remove_member":
                row_id = request.form.get("team_row_id", type=int)
                row = db.session.get(DirectorTeamMember, row_id) if row_id else None
                if row is None or int(row.director_id) != int(current_user.id):
                    flash("Team member link not found.", "warning")
                    return redirect(url_for("director_personnel"))
                member_user_id = int(row.member_user_id)
                db.session.delete(row)
                db.session.commit()
                audit(
                    "director_team_member_remove",
                    "DirectorTeamMember",
                    row_id,
                    {"member_user_id": member_user_id},
                )
                flash("Team member removed.", "info")
                return redirect(url_for("director_personnel"))

            flash("Unsupported personnel action.", "warning")
            return redirect(url_for("director_personnel"))

        team_pairs = (
            db.session.query(DirectorTeamMember, User)
            .join(User, User.id == DirectorTeamMember.member_user_id)
            .filter(DirectorTeamMember.director_id == int(current_user.id))
            .order_by(User.full_name.asc(), User.email.asc())
            .all()
        )
        team_members = [user for _, user in team_pairs]
        team_member_ids = {int(user.id) for user in team_members}

        candidate_rows = team_candidate_users_query().all()
        assigned_elsewhere_rows = (
            db.session.query(DirectorTeamMember.member_user_id)
            .filter(DirectorTeamMember.director_id != int(current_user.id))
            .all()
        )
        assigned_elsewhere_member_ids = {
            int(member_user_id)
            for (member_user_id,) in assigned_elsewhere_rows
            if member_user_id is not None
        }
        available_candidates = [
            user
            for user in candidate_rows
            if int(user.id) not in team_member_ids
            and int(user.id) != int(current_user.id)
            and int(user.id) not in assigned_elsewhere_member_ids
        ]

        personnel_rows, totals = _build_personnel_rows(team_members)
        return page(
            "Director Personnel",
            "director/personnel.html",
            team_pairs=team_pairs,
            available_candidates=available_candidates,
            personnel_rows=personnel_rows,
            totals=totals,
            role_display_name=role_display_name,
            team_member_ids=director_team_member_ids(int(current_user.id)),
        )
