from __future__ import annotations

import datetime as dt
import os
import uuid

import sqlalchemy as sa
from flask import abort, flash, redirect, request, send_from_directory, url_for
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from ..config import ALLOWED_DOC_EXT, MATTER_STATUSES, is_valid_email
from ..extensions import db
from ..helpers import (
    allowed_doc,
    audit,
    can_access_matter,
    is_admin,
    matter_activity,
    normalize_query,
    sha256_file,
)
from ..models import (
    DocumentFile,
    Matter,
    MatterActivity,
    MatterMember,
    MatterTimelineEvent,
    Task,
    TaskAssignee,
    TaskTemplate,
    TaskTemplateItem,
    User,
)
from ..policies import enforce_data_residency, visible_matter_ids
from ..templates import page

RISK_LEVELS = {"Low", "Medium", "High", "Critical"}
BUDGET_STATUSES = {"On Track", "Watch", "Over Budget", "Needs Review"}
TIMELINE_EVENT_TYPES = {"Milestone", "Filing", "Hearing", "Client Update", "Internal Review", "Delivery"}
DOC_CATEGORIES = {"Pleading", "Evidence", "Contract", "Advisory", "Correspondence", "Court Filing", "General"}
DOC_LIFECYCLE_STAGES = {"Draft", "For Review", "Final", "Executed"}


def register_matter_routes(app):
    @app.get("/matters")
    @login_required
    def matters():
        q = normalize_query(request.args.get("q", ""))
        sort = normalize_query(request.args.get("sort", "opened_desc")).lower() or "opened_desc"
        page_number = request.args.get("page", default=1, type=int) or 1
        if page_number < 1:
            page_number = 1
        risk_rank = sa.case(
            (Matter.risk_level == "Critical", 4),
            (Matter.risk_level == "High", 3),
            (Matter.risk_level == "Medium", 2),
            (Matter.risk_level == "Low", 1),
            else_=0,
        )
        sort_order = {
            "opened_desc": (Matter.opened_at.desc(), Matter.id.desc()),
            "opened_asc": (Matter.opened_at.asc(), Matter.id.asc()),
            "updated_desc": (Matter.last_updated_at.desc(), Matter.id.desc()),
            "updated_asc": (Matter.last_updated_at.asc(), Matter.id.asc()),
            "matter_no_asc": (Matter.matter_no.asc(), Matter.id.asc()),
            "matter_no_desc": (Matter.matter_no.desc(), Matter.id.desc()),
            "client_asc": (Matter.client_name.asc(), Matter.id.asc()),
            "client_desc": (Matter.client_name.desc(), Matter.id.desc()),
            "title_asc": (Matter.title.asc(), Matter.id.asc()),
            "title_desc": (Matter.title.desc(), Matter.id.desc()),
            "risk_desc": (risk_rank.desc(), Matter.opened_at.desc()),
            "risk_asc": (risk_rank.asc(), Matter.opened_at.desc()),
            "status_asc": (Matter.status.asc(), Matter.opened_at.desc()),
            "status_desc": (Matter.status.desc(), Matter.opened_at.desc()),
        }
        sort_options = (
            ("opened_desc", "Opened date (newest)"),
            ("opened_asc", "Opened date (oldest)"),
            ("updated_desc", "Last updated (newest)"),
            ("updated_asc", "Last updated (oldest)"),
            ("matter_no_asc", "Matter number (A-Z)"),
            ("matter_no_desc", "Matter number (Z-A)"),
            ("client_asc", "Client name (A-Z)"),
            ("client_desc", "Client name (Z-A)"),
            ("title_asc", "Title (A-Z)"),
            ("title_desc", "Title (Z-A)"),
            ("risk_desc", "Risk (Critical -> Low)"),
            ("risk_asc", "Risk (Low -> Critical)"),
            ("status_asc", "Status (A-Z)"),
            ("status_desc", "Status (Z-A)"),
        )
        if sort not in sort_order:
            sort = "opened_desc"
        base = Matter.query
        if not is_admin():
            ids = visible_matter_ids()
            if not ids:
                base = base.filter(Matter.id == -1)
            else:
                base = base.filter(Matter.id.in_(ids))
        if q:
            like = f"%{q}%"
            base = base.filter((Matter.matter_no.ilike(like)) | (Matter.title.ilike(like)) | (Matter.client_name.ilike(like)))
        pagination = base.order_by(*sort_order[sort]).paginate(page=page_number, per_page=50, error_out=False)
        ms = pagination.items
        status_counts = {"Open": 0, "On Hold": 0, "Closed": 0}
        for status, count in base.with_entities(Matter.status, sa.func.count(Matter.id)).group_by(Matter.status).all():
            if status in status_counts:
                status_counts[status] = int(count)
        return page(
            "Matters",
            "matters/list.html",
            ms=ms,
            q=q,
            sort=sort,
            sort_options=sort_options,
            pagination=pagination,
            status_counts=status_counts,
        )

    @app.route("/matters/new", methods=["GET", "POST"])
    @login_required
    def matter_create():
        if request.method == "POST":
            matter_no = normalize_query(request.form.get("matter_no", "")).upper()
            title = normalize_query(request.form.get("title", ""))
            client_name = normalize_query(request.form.get("client_name", ""))
            status = normalize_query(request.form.get("status", "Open")) or "Open"
            description = (request.form.get("description") or "").strip()
            objective = (request.form.get("objective") or "").strip()
            risk_level = normalize_query(request.form.get("risk_level", "Medium")) or "Medium"
            budget_status = normalize_query(request.form.get("budget_status", "On Track")) or "On Track"
            last_update_note = (request.form.get("last_update_note") or "").strip()

            if not matter_no or not title or not client_name:
                flash("Matter number, title, and client name are required.", "warning")
                return redirect(url_for("matter_create"))

            if Matter.query.filter_by(matter_no=matter_no).first():
                flash("Matter number already exists.", "warning")
                return redirect(url_for("matter_create"))
            if status not in MATTER_STATUSES:
                flash("Invalid matter status.", "warning")
                return redirect(url_for("matter_create"))
            if risk_level not in RISK_LEVELS:
                flash("Invalid risk level.", "warning")
                return redirect(url_for("matter_create"))
            if budget_status not in BUDGET_STATUSES:
                flash("Invalid budget status.", "warning")
                return redirect(url_for("matter_create"))

            now = dt.datetime.utcnow()

            m = Matter(
                matter_no=matter_no,
                title=title,
                client_name=client_name,
                status=status,
                description=description,
                objective=objective or None,
                risk_level=risk_level,
                budget_status=budget_status,
                last_update_note=last_update_note or None,
                last_updated_at=now,
                created_by=current_user.id,
            )
            db.session.add(m)
            db.session.flush()
            db.session.add(MatterMember(matter_id=m.id, user_id=current_user.id, role_in_matter="Responsible"))
            db.session.add(
                MatterTimelineEvent(
                    matter_id=m.id,
                    event_date=now.date(),
                    event_type="Milestone",
                    title="Matter opened",
                    description="Matter intake completed and baseline plan created.",
                    is_milestone=True,
                    created_by=current_user.id,
                )
            )
            db.session.add(
                MatterActivity(
                    matter_id=m.id,
                    actor_user_id=current_user.id,
                    action="Matter opened",
                    details=f"{m.matter_no} - {m.title}",
                )
            )
            db.session.commit()

            audit("matter_create", "Matter", m.id, {"matter_no": m.matter_no, "risk_level": m.risk_level})
            flash("Matter created.", "info")
            return redirect(url_for("matter_detail", matter_id=m.id))

        return page(
            "New Matter",
            "matters/new.html",
            risk_levels=sorted(RISK_LEVELS),
            budget_statuses=sorted(BUDGET_STATUSES),
        )

    @app.post("/matters/<int:matter_id>/summary")
    @login_required
    def matter_summary_update(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        objective = (request.form.get("objective") or "").strip()
        risk_level = normalize_query(request.form.get("risk_level", m.risk_level)) or m.risk_level
        budget_status = normalize_query(request.form.get("budget_status", m.budget_status)) or m.budget_status
        status = normalize_query(request.form.get("status", m.status)) or m.status
        last_update_note = (request.form.get("last_update_note") or "").strip()
        outcome_summary = (request.form.get("outcome_summary") or "").strip()

        if risk_level not in RISK_LEVELS:
            flash("Invalid risk level.", "warning")
            return redirect(url_for("matter_detail", matter_id=matter_id))
        if budget_status not in BUDGET_STATUSES:
            flash("Invalid budget status.", "warning")
            return redirect(url_for("matter_detail", matter_id=matter_id))
        if status not in MATTER_STATUSES:
            flash("Invalid status.", "warning")
            return redirect(url_for("matter_detail", matter_id=matter_id))

        previous_status = m.status
        m.objective = objective or None
        m.risk_level = risk_level
        m.budget_status = budget_status
        m.status = status
        m.last_update_note = last_update_note or None
        m.outcome_summary = outcome_summary or None
        m.last_updated_at = dt.datetime.utcnow()
        if previous_status != "Closed" and status == "Closed":
            m.closed_at = m.last_updated_at
        if previous_status == "Closed" and status != "Closed":
            m.closed_at = None
        db.session.commit()

        audit(
            "matter_summary_update",
            "Matter",
            m.id,
            {"status": m.status, "risk_level": m.risk_level, "budget_status": m.budget_status},
        )
        matter_activity(
            m.id,
            "Executive summary updated",
            f"Status {m.status}, risk {m.risk_level}, budget {m.budget_status}",
        )
        flash("Matter summary updated.", "info")
        return redirect(url_for("matter_detail", matter_id=m.id))

    @app.post("/matters/<int:matter_id>/timeline")
    @login_required
    def matter_timeline_add(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        title = normalize_query(request.form.get("title", ""))
        event_type = normalize_query(request.form.get("event_type", "Milestone")) or "Milestone"
        event_date_raw = normalize_query(request.form.get("event_date", ""))
        description = (request.form.get("description") or "").strip()
        is_milestone = (request.form.get("is_milestone") or "").strip().lower() in {"1", "true", "yes", "on"}

        if not title:
            flash("Timeline title is required.", "warning")
            return redirect(url_for("matter_detail", matter_id=m.id))
        if event_type not in TIMELINE_EVENT_TYPES:
            flash("Invalid timeline event type.", "warning")
            return redirect(url_for("matter_detail", matter_id=m.id))
        try:
            event_date = dt.date.fromisoformat(event_date_raw) if event_date_raw else dt.date.today()
        except ValueError:
            flash("Timeline date must be YYYY-MM-DD.", "warning")
            return redirect(url_for("matter_detail", matter_id=m.id))

        event = MatterTimelineEvent(
            matter_id=m.id,
            event_date=event_date,
            event_type=event_type,
            title=title,
            description=description or None,
            is_milestone=is_milestone,
            created_by=current_user.id,
        )
        m.last_updated_at = dt.datetime.utcnow()
        db.session.add(event)
        db.session.commit()

        audit(
            "matter_timeline_add",
            "MatterTimelineEvent",
            event.id,
            {"matter_id": m.id, "event_type": event.event_type, "event_date": str(event.event_date)},
        )
        matter_activity(m.id, f"Timeline event added: {event.title}", event.event_type)
        flash("Timeline event added.", "info")
        return redirect(url_for("matter_detail", matter_id=m.id))

    @app.get("/matters/<int:matter_id>")
    @login_required
    def matter_detail(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)

        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        members = (
            db.session.query(User, MatterMember)
            .join(MatterMember, MatterMember.user_id == User.id)
            .filter(MatterMember.matter_id == matter_id)
            .all()
        )
        tasks = Task.query.filter_by(matter_id=matter_id).order_by(Task.status.asc(), Task.due_date.asc().nullslast()).limit(50).all()
        docs = DocumentFile.query.filter_by(matter_id=matter_id).order_by(DocumentFile.uploaded_at.desc()).limit(30).all()
        timeline = (
            MatterTimelineEvent.query.filter_by(matter_id=matter_id)
            .order_by(MatterTimelineEvent.event_date.desc(), MatterTimelineEvent.created_at.desc())
            .limit(80)
            .all()
        )
        activity_items = (
            db.session.query(MatterActivity, User)
            .outerjoin(User, MatterActivity.actor_user_id == User.id)
            .filter(MatterActivity.matter_id == matter_id)
            .order_by(MatterActivity.created_at.desc())
            .limit(80)
            .all()
        )

        today = dt.date.today()
        overdue_tasks = [t for t in tasks if t.status != "Done" and t.due_date and t.due_date < today]
        due_soon_tasks = [t for t in tasks if t.status != "Done" and t.due_date and today <= t.due_date <= (today + dt.timedelta(days=7))]

        return page(
            f"Matter {m.matter_no}",
            "matters/detail.html",
            m=m,
            members=members,
            tasks=tasks,
            docs=docs,
            timeline=timeline,
            activity_items=activity_items,
            overdue_tasks=overdue_tasks,
            due_soon_tasks=due_soon_tasks,
            risk_levels=sorted(RISK_LEVELS),
            budget_statuses=sorted(BUDGET_STATUSES),
            matter_statuses=sorted(MATTER_STATUSES),
            timeline_event_types=sorted(TIMELINE_EVENT_TYPES),
            today=today.isoformat(),
        )

    @app.route("/matters/<int:matter_id>/team", methods=["GET", "POST"])
    @login_required
    def matter_team(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        if request.method == "POST":
            email = normalize_query(request.form.get("email", "")).lower()
            role_in_matter = normalize_query(request.form.get("role_in_matter", "")) or "Team"
            if not is_valid_email(email):
                flash("Provide a valid email address.", "warning")
                return redirect(url_for("matter_team", matter_id=matter_id))
            u = User.query.filter_by(email=email).first()
            if not u:
                flash("No such user. Admin must create them first.", "warning")
                return redirect(url_for("matter_team", matter_id=matter_id))
            if MatterMember.query.filter_by(matter_id=matter_id, user_id=u.id).first():
                flash("User already in team.", "warning")
                return redirect(url_for("matter_team", matter_id=matter_id))
            db.session.add(MatterMember(matter_id=matter_id, user_id=u.id, role_in_matter=role_in_matter))
            db.session.commit()
            audit("matter_team_add", "Matter", matter_id, {"user_id": u.id, "role_in_matter": role_in_matter})
            matter_activity(matter_id, "Team member added", f"{u.full_name} ({role_in_matter})")
            flash("Added to matter team.", "info")
            return redirect(url_for("matter_team", matter_id=matter_id))

        members = (
            db.session.query(User, MatterMember)
            .join(MatterMember, MatterMember.user_id == User.id)
            .filter(MatterMember.matter_id == matter_id)
            .all()
        )
        users = User.query.order_by(User.full_name.asc()).limit(500).all()

        return page("Matter Team", "matters/team.html", m=m, members=members, users=users)

    @app.route("/matters/<int:matter_id>/tasks", methods=["GET", "POST"])
    @login_required
    def matter_tasks(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        if request.method == "POST":
            template_id = request.form.get("template_id", type=int)
            template = db.session.get(TaskTemplate, template_id) if template_id else None
            if template_id and template is None:
                flash("Selected task template was not found.", "warning")
                return redirect(url_for("matter_tasks", matter_id=matter_id))

            template_items: list[TaskTemplateItem] = []
            if template is not None:
                template_items = (
                    TaskTemplateItem.query.filter_by(task_template_id=template.id)
                    .order_by(TaskTemplateItem.position.asc())
                    .all()
                )
            template_primary_item = template_items[0] if template_items else None

            title = normalize_query(request.form.get("title", ""))
            description = (request.form.get("description") or "").strip()
            if not title and template_primary_item is not None:
                title = normalize_query(template_primary_item.title or "")
            if not description and template_primary_item is not None and template_primary_item.description:
                description = template_primary_item.description.strip()
            if not description and len(template_items) > 1:
                checklist_lines = "\n".join(f"- {item.title}" for item in template_items[1:] if item.title)
                if checklist_lines:
                    description = f"Template checklist:\n{checklist_lines}"

            due = normalize_query(request.form.get("due_date", ""))
            assigned_to_email = normalize_query(request.form.get("assigned_to", "")).lower()
            save_as_template = (request.form.get("save_as_template") or "").strip().lower() in {"1", "true", "yes", "on"}
            template_name = normalize_query(request.form.get("template_name", ""))

            if not title:
                flash("Task title is required.", "warning")
                return redirect(url_for("matter_tasks", matter_id=matter_id))
            if save_as_template and not template_name:
                flash("Template name is required when saving this task as a template.", "warning")
                return redirect(url_for("matter_tasks", matter_id=matter_id))

            due_date = None
            if due:
                try:
                    due_date = dt.date.fromisoformat(due)
                except ValueError:
                    flash("Invalid due date. Use YYYY-MM-DD.", "warning")
                    return redirect(url_for("matter_tasks", matter_id=matter_id))
            elif template is not None and template.sla_hours:
                due_days = max(1, (int(template.sla_hours) + 23) // 24)
                due_date = dt.date.today() + dt.timedelta(days=due_days)

            assignee_ids: list[int] = []
            seen_assignees: set[int] = set()
            for raw_user_id in request.form.getlist("assignee_user_ids"):
                raw_user_id = (raw_user_id or "").strip()
                if not raw_user_id:
                    continue
                try:
                    user_id = int(raw_user_id)
                except (TypeError, ValueError):
                    flash("One or more assignee selections are invalid.", "warning")
                    return redirect(url_for("matter_tasks", matter_id=matter_id))
                if user_id in seen_assignees:
                    continue
                if db.session.get(User, user_id) is None:
                    flash("One or more selected assignees could not be found.", "warning")
                    return redirect(url_for("matter_tasks", matter_id=matter_id))
                assignee_ids.append(user_id)
                seen_assignees.add(user_id)

            if assigned_to_email:
                u = User.query.filter_by(email=assigned_to_email).first()
                if not u:
                    flash("Assigned-to user not found.", "warning")
                    return redirect(url_for("matter_tasks", matter_id=matter_id))
                if u.id not in seen_assignees:
                    assignee_ids.append(u.id)
                    seen_assignees.add(u.id)

            assigned_to = assignee_ids[0] if assignee_ids else None
            requires_two_person_review = (request.form.get("requires_two_person_review") or "").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            task_priority = (template.priority if template is not None else "Medium") or "Medium"
            task_sla_hours = template.sla_hours if template is not None else None
            task_recurrence_rule = template.recurrence_rule if template is not None else None

            t = Task(
                matter_id=matter_id,
                title=title,
                description=description,
                due_date=due_date,
                assigned_to=assigned_to,
                created_by=current_user.id,
                requires_two_person_review=requires_two_person_review,
                priority=task_priority,
                sla_hours=task_sla_hours,
                recurrence_rule=task_recurrence_rule,
            )
            db.session.add(t)
            db.session.flush()
            for user_id in assignee_ids:
                db.session.add(TaskAssignee(task_id=t.id, user_id=user_id, assigned_by=current_user.id))

            template_saved_name: str | None = None
            template_saved_id: int | None = None
            if save_as_template:
                template_row = TaskTemplate.query.filter_by(name=template_name).first()
                if template_row is None:
                    template_row = TaskTemplate(
                        name=template_name,
                        matter_type=(m.case_type or m.practice_area or "").strip() or None,
                        priority=task_priority,
                        sla_hours=task_sla_hours,
                        recurrence_rule=task_recurrence_rule,
                        created_by=current_user.id,
                    )
                    db.session.add(template_row)
                    db.session.flush()
                else:
                    if template_row.matter_type is None:
                        template_row.matter_type = (m.case_type or m.practice_area or "").strip() or None
                    template_row.priority = task_priority
                    template_row.sla_hours = task_sla_hours
                    template_row.recurrence_rule = task_recurrence_rule
                    TaskTemplateItem.query.filter_by(task_template_id=template_row.id).delete(synchronize_session=False)
                db.session.add(
                    TaskTemplateItem(
                        task_template_id=template_row.id,
                        title=title,
                        description=description or None,
                        position=1,
                    )
                )
                template_saved_name = template_row.name
                template_saved_id = template_row.id

            db.session.commit()
            audit(
                "task_create",
                "Task",
                t.id,
                {"matter_id": matter_id, "assignee_count": len(assignee_ids), "template_id": template.id if template else None},
            )
            if template_saved_name:
                audit("task_template_save_from_task", "TaskTemplate", template_saved_id, {"name": template_saved_name})
            matter_activity(matter_id, f"Task created: {t.title}", f"Due {t.due_date}" if t.due_date else "No due date")
            if template_saved_name:
                flash(f"Task created and template '{template_saved_name}' saved.", "info")
            else:
                flash("Task created.", "info")
            return redirect(url_for("matter_tasks", matter_id=matter_id))

        tasks = Task.query.filter_by(matter_id=matter_id).order_by(Task.status.asc(), Task.due_date.asc().nullslast()).limit(200).all()
        users = User.query.order_by(User.full_name.asc()).limit(500).all()
        users_map = {u.id: u for u in users}
        task_templates = TaskTemplate.query.order_by(TaskTemplate.name.asc()).limit(250).all()
        template_primary_items: dict[int, TaskTemplateItem] = {}
        template_ids = [row.id for row in task_templates]
        if template_ids:
            template_items = (
                TaskTemplateItem.query.filter(TaskTemplateItem.task_template_id.in_(template_ids))
                .order_by(TaskTemplateItem.task_template_id.asc(), TaskTemplateItem.position.asc())
                .all()
            )
            for item in template_items:
                if item.task_template_id not in template_primary_items:
                    template_primary_items[item.task_template_id] = item

        task_assignees_map: dict[int, list[User]] = {task.id: [] for task in tasks}
        task_ids = [task.id for task in tasks]
        if task_ids:
            assignment_rows = (
                db.session.query(TaskAssignee, User)
                .join(User, User.id == TaskAssignee.user_id)
                .filter(TaskAssignee.task_id.in_(task_ids))
                .order_by(TaskAssignee.task_id.asc(), User.full_name.asc())
                .all()
            )
            for assignment, user in assignment_rows:
                task_assignees_map.setdefault(assignment.task_id, []).append(user)
        for task in tasks:
            if task_assignees_map.get(task.id):
                continue
            if task.assigned_to is None:
                continue
            fallback_user = users_map.get(task.assigned_to)
            if fallback_user is not None:
                task_assignees_map[task.id] = [fallback_user]

        return page(
            "Matter Tasks",
            "matters/tasks.html",
            m=m,
            tasks=tasks,
            users_map=users_map,
            users=users,
            task_assignees_map=task_assignees_map,
            task_templates=task_templates,
            template_primary_items=template_primary_items,
        )

    @app.post("/tasks/<int:task_id>/status")
    @login_required
    def task_update(task_id: int):
        t = db.session.get(Task, task_id)
        if not t:
            abort(404)
        if not can_access_matter(t.matter_id):
            abort(403)
        status = normalize_query(request.form.get("status", "Todo")) or "Todo"
        if status not in {"Todo", "Doing", "Done"}:
            abort(400)
        t.status = status
        db.session.commit()
        audit("task_status", "Task", t.id, {"status": status, "matter_id": t.matter_id})
        matter_activity(t.matter_id, f"Task status changed: {t.title}", f"Now {status}")
        return redirect(url_for("matter_tasks", matter_id=t.matter_id))

    @app.route("/matters/<int:matter_id>/documents", methods=["GET", "POST"])
    @login_required
    def matter_documents(matter_id: int):
        if not can_access_matter(matter_id):
            abort(403)
        m = db.session.get(Matter, matter_id)
        if not m:
            abort(404)

        if request.method == "POST":
            if "file" not in request.files:
                flash("No file uploaded.", "warning")
                return redirect(url_for("matter_documents", matter_id=matter_id))
            f = request.files["file"]
            if not f or not f.filename:
                flash("No file selected.", "warning")
                return redirect(url_for("matter_documents", matter_id=matter_id))
            if not allowed_doc(f.filename):
                flash("File type not allowed.", "warning")
                return redirect(url_for("matter_documents", matter_id=matter_id))
            enforce_data_residency("primary_storage")

            safe = secure_filename(f.filename)
            if not safe:
                flash("Invalid filename.", "warning")
                return redirect(url_for("matter_documents", matter_id=matter_id))
            stored = f"{matter_id}_{uuid.uuid4().hex}_{safe}"
            dest = os.path.join(app.config["UPLOAD_DIR"], stored)
            f.save(dest)

            category = normalize_query(request.form.get("category", "General")) or "General"
            doc_version = normalize_query(request.form.get("doc_version", ""))
            lifecycle_stage = normalize_query(request.form.get("lifecycle_stage", "Draft")) or "Draft"
            owner_name = normalize_query(request.form.get("owner_name", ""))
            is_privileged = (request.form.get("is_privileged") or "").strip().lower() in {"1", "true", "yes", "on"}

            if category not in DOC_CATEGORIES:
                if os.path.isfile(dest):
                    os.remove(dest)
                flash("Invalid document category.", "warning")
                return redirect(url_for("matter_documents", matter_id=matter_id))
            if lifecycle_stage not in DOC_LIFECYCLE_STAGES:
                if os.path.isfile(dest):
                    os.remove(dest)
                flash("Invalid document stage.", "warning")
                return redirect(url_for("matter_documents", matter_id=matter_id))

            doc = DocumentFile(
                matter_id=matter_id,
                original_filename=safe,
                stored_filename=stored,
                sha256=sha256_file(dest),
                content_type=f.mimetype,
                category=category,
                doc_version=doc_version or None,
                lifecycle_stage=lifecycle_stage,
                owner_name=owner_name or None,
                is_privileged=is_privileged,
                uploaded_by=current_user.id,
            )
            db.session.add(doc)
            db.session.commit()
            audit(
                "document_upload",
                "DocumentFile",
                doc.id,
                {
                    "matter_id": matter_id,
                    "filename": safe,
                    "category": doc.category,
                    "lifecycle_stage": doc.lifecycle_stage,
                    "is_privileged": doc.is_privileged,
                },
            )
            matter_activity(
                matter_id,
                f"Document uploaded: {doc.original_filename}",
                f"{doc.category} / {doc.lifecycle_stage}",
            )
            flash("Uploaded.", "info")
            return redirect(url_for("matter_documents", matter_id=matter_id))

        docs = DocumentFile.query.filter_by(matter_id=matter_id).order_by(DocumentFile.uploaded_at.desc()).limit(200).all()

        return page(
            "Documents",
            "matters/documents.html",
            m=m,
            docs=docs,
            allowed=sorted(ALLOWED_DOC_EXT),
            doc_categories=sorted(DOC_CATEGORIES),
            doc_stages=sorted(DOC_LIFECYCLE_STAGES),
        )

    @app.get("/documents/<int:doc_id>/download")
    @login_required
    def doc_download(doc_id: int):
        d = db.session.get(DocumentFile, doc_id)
        if not d:
            abort(404)
        if not can_access_matter(d.matter_id):
            abort(403)
        enforce_data_residency("exports")
        file_path = os.path.join(app.config["UPLOAD_DIR"], d.stored_filename)
        if not os.path.isfile(file_path):
            abort(404)
        inline = (request.args.get("inline") or "").strip().lower() in {"1", "true", "yes", "on"}
        if inline:
            audit("document_preview", "DocumentFile", d.id, {"matter_id": d.matter_id})
            matter_activity(d.matter_id, f"Document previewed: {d.original_filename}")
            return send_from_directory(
                app.config["UPLOAD_DIR"],
                d.stored_filename,
                as_attachment=False,
                download_name=d.original_filename,
                mimetype=d.content_type or None,
            )
        audit("document_download", "DocumentFile", d.id, {"matter_id": d.matter_id})
        matter_activity(d.matter_id, f"Document downloaded: {d.original_filename}")
        return send_from_directory(app.config["UPLOAD_DIR"], d.stored_filename, as_attachment=True, download_name=d.original_filename)
