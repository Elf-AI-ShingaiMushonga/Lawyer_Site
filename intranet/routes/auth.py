from __future__ import annotations

import datetime as dt

from flask import current_app, flash, redirect, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from ..config import is_valid_email
from ..extensions import db, limiter
from ..helpers import (
    audit,
    is_admin,
    register_trusted_device,
    register_user_session,
    revoke_current_session,
)
from ..mfa import check_backup_code, verify_totp
from ..models import (
    Announcement,
    Deadline,
    DocumentFile,
    Invoice,
    Matter,
    MatterPin,
    MatterRecentView,
    Task,
    TaskAssignee,
    TimeEntry,
    TimeTimer,
    User,
    UserMFABackupCode,
)
from ..policies import visible_matter_ids
from ..services.priority_inbox import build_priority_inbox
from ..templates import page

MFA_REQUIRED_ROLES = {"admin", "lawyer", "paralegal", "staff"}


def has_any_users() -> bool:
    return db.session.query(User.id).first() is not None


def register_auth_routes(app):
    @app.get("/")
    def index():
        return page(
            "ELF Demo Hub",
            "landing.html",
            intranet_login_url=url_for("login"),
            ufc_url=current_app.config.get("UFC_DEMO_PATH", "/ufc/"),
            ufc_enabled=bool(current_app.config.get("UFC_DEMO_ENABLED", False)),
        )

    @app.route("/register", methods=["GET", "POST"])
    @limiter.limit(lambda: app.config.get("AUTH_REGISTER_RATE_LIMIT", "5/hour"), methods=["POST"])
    def register():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        if has_any_users():
            return redirect(url_for("login"))

        if request.method == "POST":
            full_name = (request.form.get("full_name") or "").strip() or "Admin User"
            email = (request.form.get("email") or "").strip().lower()
            password = request.form.get("password") or ""
            confirm_password = request.form.get("confirm_password") or ""

            if not is_valid_email(email):
                flash("Enter a valid email address.", "warning")
                return redirect(url_for("register"))
            if len(password) < 12:
                flash("Password must be at least 12 characters.", "warning")
                return redirect(url_for("register"))
            if password != confirm_password:
                flash("Passwords do not match.", "warning")
                return redirect(url_for("register"))

            user = User(email=email, full_name=full_name, role="admin", password_hash="x")
            user.set_password(password)
            user.last_login_at = dt.datetime.utcnow()
            db.session.add(user)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("An account already exists. Please sign in.", "warning")
                return redirect(url_for("login"))

            login_user(user)
            session.permanent = True
            audit("bootstrap_admin_create", "User", user.id, {"email": email})
            flash("Administrator account created.", "info")
            return redirect(url_for("dashboard"))

        return page("Register", "auth/register.html")

    @app.route("/login", methods=["GET", "POST"])
    @limiter.limit(lambda: app.config.get("AUTH_LOGIN_RATE_LIMIT", "10/minute"), methods=["POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        if not has_any_users():
            return redirect(url_for("register"))

        if request.method == "POST":
            email = (request.form.get("email") or "").strip().lower()
            password = request.form.get("password") or ""
            mfa_code = (request.form.get("mfa_code") or "").strip()
            backup_code = (request.form.get("backup_code") or "").strip().upper()
            if not is_valid_email(email):
                flash("Invalid credentials.", "warning")
                return redirect(url_for("login"))
            user = User.query.filter_by(email=email).first()
            now = dt.datetime.utcnow()
            if user and user.locked_until and user.locked_until > now:
                flash("Account temporarily locked due to failed sign-in attempts.", "warning")
                return redirect(url_for("login"))

            if not user or not user.is_active or not user.check_password(password):
                if user:
                    user.failed_login_attempts = int(user.failed_login_attempts or 0) + 1
                    user.last_failed_login_at = now
                    if user.failed_login_attempts >= 5:
                        user.locked_until = now + dt.timedelta(minutes=15)
                    db.session.commit()
                flash("Invalid credentials.", "warning")
                return redirect(url_for("login"))

            if user.role in MFA_REQUIRED_ROLES and not user.mfa_enabled:
                login_user(user)
                session.permanent = True
                user.last_login_at = dt.datetime.utcnow()
                user.failed_login_attempts = 0
                user.locked_until = None
                db.session.commit()
                register_user_session(user.id)
                register_trusted_device(user.id)
                audit("login_mfa_enrollment_required", "User", user.id)
                flash("MFA enrollment is required before using the system.", "warning")
                return redirect(url_for("auth_mfa_setup"))

            if user.mfa_enabled:
                verified = False
                if mfa_code and user.mfa_secret and verify_totp(user.mfa_secret, mfa_code):
                    verified = True
                elif backup_code:
                    backups = UserMFABackupCode.query.filter_by(user_id=user.id, used_at=None).all()
                    for row in backups:
                        if check_backup_code(row.code_hash, backup_code):
                            row.used_at = now
                            verified = True
                            break
                if not verified:
                    flash("MFA code required or invalid.", "warning")
                    return redirect(url_for("login"))

            login_user(user)
            session.permanent = True
            if user.mfa_enabled:
                session["mfa_verified_at"] = dt.datetime.utcnow().isoformat()
            else:
                session.pop("mfa_verified_at", None)
            user.last_login_at = dt.datetime.utcnow()
            user.failed_login_attempts = 0
            user.locked_until = None
            db.session.commit()
            register_user_session(user.id)
            register_trusted_device(user.id)
            audit("login")
            return redirect(url_for("dashboard"))

        return page("Login", "auth/login.html")

    @app.post("/logout")
    @login_required
    def logout():
        revoke_current_session()
        session.pop("_session_token", None)
        session.pop("mfa_verified_at", None)
        audit("logout")
        logout_user()
        flash("Logged out.", "info")
        return redirect(url_for("login"))

    @app.get("/dashboard")
    @login_required
    def dashboard():
        anns = Announcement.query.order_by(Announcement.created_at.desc()).limit(5).all()
        today = dt.date.today()
        has_current_user_assignee = db.session.query(TaskAssignee.id).filter(
            TaskAssignee.task_id == Task.id,
            TaskAssignee.user_id == current_user.id,
        ).exists()
        my_assignment_clause = or_(Task.assigned_to == current_user.id, has_current_user_assignee)

        my_tasks = (
            Task.query.filter(my_assignment_clause)
            .order_by(Task.status.asc(), Task.due_date.asc().nullslast(), Task.created_at.desc())
            .limit(8)
            .all()
        )
        task_matter_ids = sorted({int(task.matter_id) for task in my_tasks if task.matter_id})
        task_matter_map = (
            {row.id: row for row in Matter.query.filter(Matter.id.in_(task_matter_ids)).all()}
            if task_matter_ids
            else {}
        )

        matter_scope = Matter.query
        scoped_ids: list[int] | None = None
        if not is_admin():
            scoped_ids = visible_matter_ids()
            if scoped_ids:
                matter_scope = matter_scope.filter(Matter.id.in_(scoped_ids))
            else:
                matter_scope = matter_scope.filter(Matter.id == -1)
        recent_matters = matter_scope.order_by(Matter.opened_at.desc()).limit(8).all()

        pin_query = (
            db.session.query(MatterPin, Matter)
            .join(Matter, Matter.id == MatterPin.matter_id)
            .filter(MatterPin.user_id == current_user.id)
        )
        recent_view_query = (
            db.session.query(MatterRecentView, Matter)
            .join(Matter, Matter.id == MatterRecentView.matter_id)
            .filter(MatterRecentView.user_id == current_user.id)
        )
        if not is_admin():
            if scoped_ids:
                pin_query = pin_query.filter(Matter.id.in_(scoped_ids))
                recent_view_query = recent_view_query.filter(Matter.id.in_(scoped_ids))
            else:
                pin_query = pin_query.filter(Matter.id == -1)
                recent_view_query = recent_view_query.filter(Matter.id == -1)

        pinned_pairs = pin_query.order_by(MatterPin.created_at.asc()).limit(8).all()
        pinned_matters = [matter for _, matter in pinned_pairs]
        pinned_matter_ids = {matter.id for matter in pinned_matters}

        recent_views = []
        for view_row, matter in recent_view_query.order_by(MatterRecentView.last_viewed_at.desc()).limit(12).all():
            if matter.id in pinned_matter_ids:
                continue
            recent_views.append(
                {
                    "matter": matter,
                    "last_viewed_at": view_row.last_viewed_at,
                    "view_count": int(view_row.view_count or 0),
                }
            )
            if len(recent_views) >= 8:
                break

        document_scope = DocumentFile.query
        if not is_admin():
            if scoped_ids:
                document_scope = document_scope.filter(DocumentFile.matter_id.in_(scoped_ids))
            else:
                document_scope = document_scope.filter(DocumentFile.id == -1)

        risk_matter_scope = Matter.query
        if not is_admin():
            if scoped_ids:
                risk_matter_scope = risk_matter_scope.filter(Matter.id.in_(scoped_ids))
            else:
                risk_matter_scope = risk_matter_scope.filter(Matter.id == -1)
        risk_matter_scope = risk_matter_scope.filter(Matter.status != "Closed")

        overdue_task_scope = Task.query.filter(Task.status != "Done", Task.due_date.isnot(None), Task.due_date < today)
        due_week_scope = Task.query.filter(
            Task.status != "Done",
            Task.due_date.isnot(None),
            Task.due_date >= today,
            Task.due_date <= (today + dt.timedelta(days=7)),
        )
        due_today_scope = Task.query.filter(Task.status != "Done", Task.due_date == today)
        has_any_assignee = db.session.query(TaskAssignee.id).filter(TaskAssignee.task_id == Task.id).exists()
        urgent_unassigned_scope = Task.query.filter(
            Task.status != "Done",
            Task.assigned_to.is_(None),
            ~has_any_assignee,
            Task.due_date.isnot(None),
            Task.due_date <= (today + dt.timedelta(days=3)),
        )
        deadline_scope = Deadline.query.filter(Deadline.status != "acknowledged")
        invoice_scope = Invoice.query
        if not is_admin():
            if scoped_ids:
                overdue_task_scope = overdue_task_scope.filter(Task.matter_id.in_(scoped_ids))
                due_week_scope = due_week_scope.filter(Task.matter_id.in_(scoped_ids))
                due_today_scope = due_today_scope.filter(Task.matter_id.in_(scoped_ids))
                urgent_unassigned_scope = urgent_unassigned_scope.filter(Task.matter_id.in_(scoped_ids))
                deadline_scope = deadline_scope.filter(Deadline.matter_id.in_(scoped_ids))
                invoice_scope = invoice_scope.filter(Invoice.matter_id.in_(scoped_ids))
            else:
                overdue_task_scope = overdue_task_scope.filter(Task.id == -1)
                due_week_scope = due_week_scope.filter(Task.id == -1)
                due_today_scope = due_today_scope.filter(Task.id == -1)
                urgent_unassigned_scope = urgent_unassigned_scope.filter(Task.id == -1)
                deadline_scope = deadline_scope.filter(Deadline.id == -1)
                invoice_scope = invoice_scope.filter(Invoice.id == -1)

        overdue_counts = {
            matter_id: count
            for matter_id, count in (
                overdue_task_scope.with_entities(Task.matter_id, func.count(Task.id))
                .group_by(Task.matter_id)
                .all()
            )
        }

        at_risk_matters = []
        for matter in risk_matter_scope.order_by(Matter.last_updated_at.desc(), Matter.opened_at.desc()).limit(40).all():
            overdue_for_matter = overdue_counts.get(matter.id, 0)
            if matter.risk_level in {"High", "Critical"} or overdue_for_matter > 0:
                at_risk_matters.append(
                    {
                        "matter": matter,
                        "overdue_tasks": overdue_for_matter,
                        "risk_driver": "Overdue tasks" if overdue_for_matter else f"{matter.risk_level} risk",
                    }
                )
            if len(at_risk_matters) >= 8:
                break

        priority_inbox = build_priority_inbox(current_user, scoped_matter_ids=scoped_ids)

        stats = {
            "matter_count": matter_scope.count(),
            "assigned_open_tasks": Task.query.filter(my_assignment_clause, Task.status != "Done").count(),
            "document_count": document_scope.count(),
            "announcement_count": Announcement.query.count(),
            "overdue_tasks": overdue_task_scope.count(),
            "due_today_tasks": due_today_scope.count(),
            "today_deadlines": deadline_scope.filter(Deadline.due_at == today).count(),
            "due_this_week": due_week_scope.count(),
            "urgent_unassigned": urgent_unassigned_scope.count(),
            "my_time_needs_review": TimeEntry.query.filter(
                TimeEntry.user_id == current_user.id,
                TimeEntry.status.in_(["draft", "needs_review"]),
            ).count(),
            "running_timers": TimeTimer.query.filter(
                TimeTimer.user_id == current_user.id,
                TimeTimer.status == "running",
            ).count(),
            "draft_invoices": invoice_scope.filter(Invoice.status == "draft").count(),
            "pinned_matters": len(pinned_matters),
            "recent_views": len(recent_views),
            "priority_actions": priority_inbox["total_actions"],
        }

        return page(
            "Dashboard",
            "auth/dashboard.html",
            anns=anns,
            my_tasks=my_tasks,
            recent_matters=recent_matters,
            stats=stats,
            at_risk_matters=at_risk_matters,
            task_matter_map=task_matter_map,
            pinned_matters=pinned_matters,
            recent_views=recent_views,
            priority_inbox=priority_inbox,
        )
