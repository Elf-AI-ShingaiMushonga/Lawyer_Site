from __future__ import annotations

import datetime as dt
from ..timeutils import utc_now

from flask import abort, flash, redirect, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..helpers import audit, can_access_matter, enforce_case_team_role, matter_activity, normalize_query
from ..models import Task, TaskApproval, TaskAssignee, TaskChecklistItem, TaskDependency, TaskTemplate, TaskTemplateItem
from ..templates import page


def _depends_graph(matter_task_ids: list[int]) -> dict[int, list[int]]:
    graph: dict[int, list[int]] = {task_id: [] for task_id in matter_task_ids}
    deps = TaskDependency.query.filter(TaskDependency.task_id.in_(matter_task_ids)).all()
    for dep in deps:
        graph.setdefault(dep.task_id, []).append(dep.depends_on_task_id)
    return graph


def _has_cycle(task_id: int, depends_on_id: int) -> bool:
    task = db.session.get(Task, task_id)
    if task is None:
        return True
    task_ids = [row.id for row in Task.query.filter_by(matter_id=task.matter_id).all()]
    graph = _depends_graph(task_ids)
    graph.setdefault(task_id, []).append(depends_on_id)

    visiting: set[int] = set()
    visited: set[int] = set()

    def dfs(node: int) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        for nxt in graph.get(node, []):
            if dfs(nxt):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    for n in graph:
        if dfs(n):
            return True
    return False


def register_workflow_routes(app):
    @app.route("/tasks/templates", methods=["GET", "POST"])
    @login_required
    def task_templates():
        enforce_case_team_role()
        if request.method == "POST":
            name = normalize_query(request.form.get("name", ""))
            if not name:
                flash("Template name required.", "warning")
                return redirect(url_for("task_templates"))

            t = TaskTemplate.query.filter_by(name=name).first()
            matter_type = (request.form.get("matter_type") or "").strip() or None
            priority = (request.form.get("priority") or "Medium").strip() or "Medium"
            sla_hours = request.form.get("sla_hours", type=int)
            recurrence_rule = (request.form.get("recurrence_rule") or "").strip() or None
            if t is None:
                t = TaskTemplate(
                    name=name,
                    matter_type=matter_type,
                    priority=priority,
                    sla_hours=sla_hours,
                    recurrence_rule=recurrence_rule,
                    created_by=current_user.id,
                )
                db.session.add(t)
                db.session.flush()
            else:
                t.matter_type = matter_type
                t.priority = priority
                t.sla_hours = sla_hours
                t.recurrence_rule = recurrence_rule

            TaskTemplateItem.query.filter_by(task_template_id=t.id).delete(synchronize_session=False)
            for i, line in enumerate((request.form.get("items") or "").splitlines(), start=1):
                item = line.strip()
                if item:
                    db.session.add(TaskTemplateItem(task_template_id=t.id, title=item, position=i))
            db.session.commit()
            audit("task_template_save", "TaskTemplate", t.id)
            flash("Template saved.", "info")
            return redirect(url_for("task_templates"))

        templates = TaskTemplate.query.order_by(TaskTemplate.created_at.desc()).all()
        items = TaskTemplateItem.query.order_by(TaskTemplateItem.task_template_id.asc(), TaskTemplateItem.position.asc()).all()
        return page("Task Templates", "workflow/templates.html", templates=templates, items=items)

    @app.route("/tasks/<int:task_id>/dependencies", methods=["GET", "POST"])
    @login_required
    def task_dependencies(task_id: int):
        t = db.session.get(Task, task_id)
        if not t:
            abort(404)
        if not can_access_matter(t.matter_id):
            abort(403)
        enforce_case_team_role()

        if request.method == "POST":
            depends_on_id = request.form.get("depends_on_task_id", type=int)
            if not depends_on_id:
                flash("Dependency task id required.", "warning")
                return redirect(url_for("task_dependencies", task_id=task_id))
            if depends_on_id == task_id:
                flash("Task cannot depend on itself.", "warning")
                return redirect(url_for("task_dependencies", task_id=task_id))

            depends_on_task = db.session.get(Task, depends_on_id)
            if not depends_on_task or depends_on_task.matter_id != t.matter_id:
                flash("Dependency must be from same matter.", "warning")
                return redirect(url_for("task_dependencies", task_id=task_id))

            if _has_cycle(task_id, depends_on_id):
                flash("Dependency would create a cycle.", "warning")
                return redirect(url_for("task_dependencies", task_id=task_id))

            if not TaskDependency.query.filter_by(task_id=task_id, depends_on_task_id=depends_on_id).first():
                db.session.add(TaskDependency(task_id=task_id, depends_on_task_id=depends_on_id))
                db.session.commit()
                audit("task_dependency_add", "Task", task_id, {"depends_on": depends_on_id})
                matter_activity(t.matter_id, f"Task dependency added for #{task_id}", f"depends on #{depends_on_id}")
            flash("Dependency added.", "info")
            return redirect(url_for("task_dependencies", task_id=task_id))

        deps = TaskDependency.query.filter_by(task_id=task_id).all()
        options = Task.query.filter(Task.matter_id == t.matter_id, Task.id != t.id).order_by(Task.id.desc()).all()
        return page("Task Dependencies", "workflow/dependencies.html", t=t, deps=deps, options=options)

    @app.route("/tasks/<int:task_id>/checklist", methods=["GET", "POST"])
    @login_required
    def task_checklist(task_id: int):
        t = db.session.get(Task, task_id)
        if not t:
            abort(404)
        if not can_access_matter(t.matter_id):
            abort(403)
        enforce_case_team_role()

        if request.method == "POST":
            action = (request.form.get("action") or "add").strip()
            if action == "add":
                item_text = normalize_query(request.form.get("item_text", ""))
                if item_text:
                    pos = TaskChecklistItem.query.filter_by(task_id=task_id).count() + 1
                    db.session.add(TaskChecklistItem(task_id=task_id, item_text=item_text, position=pos, is_done=False))
                    db.session.commit()
                    audit("task_checklist_add", "Task", task_id)
                else:
                    flash("Checklist text required.", "warning")
            elif action == "toggle":
                item_id = request.form.get("item_id", type=int)
                item = db.session.get(TaskChecklistItem, item_id) if item_id else None
                if item and item.task_id == task_id:
                    item.is_done = not item.is_done
                    db.session.commit()
                    audit("task_checklist_toggle", "TaskChecklistItem", item.id, {"is_done": item.is_done})
            return redirect(url_for("task_checklist", task_id=task_id))

        items = TaskChecklistItem.query.filter_by(task_id=task_id).order_by(TaskChecklistItem.position.asc()).all()
        return page("Task Checklist", "workflow/checklist.html", t=t, items=items)

    @app.post("/tasks/<int:task_id>/approve")
    @login_required
    def task_approve(task_id: int):
        t = db.session.get(Task, task_id)
        if not t:
            abort(404)
        if not can_access_matter(t.matter_id):
            abort(403)
        enforce_case_team_role()

        state = (request.form.get("state") or "pending").strip().lower()
        notes = (request.form.get("notes") or "").strip()
        if state not in {"pending", "approved", "rejected"}:
            flash("Invalid approval state.", "warning")
            return redirect(url_for("task_dependencies", task_id=task_id))
        if state == "approved" and current_user.id == t.created_by:
            flash("Task creator cannot approve their own task.", "warning")
            return redirect(url_for("matter_tasks", matter_id=t.matter_id))

        approval = TaskApproval(
            task_id=task_id,
            requested_by=t.created_by,
            approver_user_id=current_user.id,
            state=state,
            notes=notes or None,
            decided_at=utc_now() if state in {"approved", "rejected"} else None,
        )
        db.session.add(approval)
        message = "Task approval updated."
        if state == "approved" and t.requires_two_person_review:
            prior_approvers = {
                row.approver_user_id
                for row in TaskApproval.query.filter_by(task_id=task_id, state="approved")
                .order_by(TaskApproval.id.asc())
                .all()
            }
            prior_approvers.add(current_user.id)
            if len(prior_approvers) < 2:
                t.approval_state = "pending"
                t.approved_by = None
                t.approved_at = None
                message = "First approval recorded. Second independent approver is required."
            else:
                t.approval_state = "approved"
                t.approved_by = current_user.id
                t.approved_at = utc_now()
        else:
            t.approval_state = state
            if state == "approved":
                t.approved_by = current_user.id
                t.approved_at = utc_now()
            elif state in {"pending", "rejected"}:
                t.approved_by = None
                t.approved_at = None
        db.session.commit()
        audit("task_approval", "Task", t.id, {"state": state})
        matter_activity(t.matter_id, f"Task approval state: {t.title}", state)
        flash(message, "info")
        return redirect(url_for("matter_tasks", matter_id=t.matter_id))

    @app.post("/tasks/<int:task_id>/recur")
    @login_required
    def task_recur(task_id: int):
        t = db.session.get(Task, task_id)
        if not t:
            abort(404)
        if not can_access_matter(t.matter_id):
            abort(403)
        enforce_case_team_role()

        rule = (t.recurrence_rule or "weekly").lower()
        if t.due_date is None:
            next_due = dt.date.today() + dt.timedelta(days=7)
        elif rule.startswith("month"):
            next_due = t.due_date + dt.timedelta(days=30)
        else:
            next_due = t.due_date + dt.timedelta(days=7)

        assignee_ids = [
            int(user_id)
            for (user_id,) in (
                db.session.query(TaskAssignee.user_id)
                .filter(TaskAssignee.task_id == t.id)
                .order_by(TaskAssignee.user_id.asc())
                .all()
            )
            if user_id is not None
        ]
        if not assignee_ids and t.assigned_to is not None:
            assignee_ids = [int(t.assigned_to)]

        clone = Task(
            matter_id=t.matter_id,
            title=t.title,
            description=t.description,
            status="Todo",
            due_date=next_due,
            assigned_to=assignee_ids[0] if assignee_ids else None,
            created_by=current_user.id,
            priority=t.priority,
            sla_hours=t.sla_hours,
            recurrence_rule=t.recurrence_rule,
            requires_two_person_review=t.requires_two_person_review,
        )
        db.session.add(clone)
        db.session.flush()
        for user_id in assignee_ids:
            db.session.add(TaskAssignee(task_id=clone.id, user_id=user_id, assigned_by=current_user.id))
        db.session.commit()
        audit("task_recur", "Task", clone.id, {"source_task_id": t.id, "next_due": next_due.isoformat()})
        matter_activity(t.matter_id, f"Recurring task created from #{t.id}", f"new task #{clone.id}")
        flash("Recurring task created.", "info")
        return redirect(url_for("matter_tasks", matter_id=t.matter_id))
