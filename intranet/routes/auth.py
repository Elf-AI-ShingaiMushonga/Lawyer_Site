from __future__ import annotations

import datetime as dt

from flask import redirect, flash, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy.exc import IntegrityError

from ..config import is_valid_email
from ..extensions import db, limiter
from ..helpers import audit, is_admin
from ..models import Announcement, DocumentFile, Matter, MatterMember, Task, User
from ..templates import page


def has_any_users() -> bool:
    return db.session.query(User.id).first() is not None


def register_auth_routes(app):
    @app.get("/")
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        if has_any_users():
            return redirect(url_for("login"))
        return redirect(url_for("register"))

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
            if not is_valid_email(email):
                flash("Invalid credentials.", "warning")
                return redirect(url_for("login"))
            user = User.query.filter_by(email=email).first()
            if not user or not user.is_active or not user.check_password(password):
                flash("Invalid credentials.", "warning")
                return redirect(url_for("login"))
            login_user(user)
            user.last_login_at = dt.datetime.utcnow()
            db.session.commit()
            audit("login")
            return redirect(url_for("dashboard"))

        return page("Login", "auth/login.html")

    @app.post("/logout")
    @login_required
    def logout():
        audit("logout")
        logout_user()
        flash("Logged out.", "info")
        return redirect(url_for("login"))

    @app.get("/dashboard")
    @login_required
    def dashboard():
        anns = Announcement.query.order_by(Announcement.created_at.desc()).limit(5).all()

        my_tasks = (
            Task.query.filter(Task.assigned_to == current_user.id)
            .order_by(Task.status.asc(), Task.due_date.asc().nullslast(), Task.created_at.desc())
            .limit(8)
            .all()
        )

        matter_scope = Matter.query
        if not is_admin():
            matter_scope = (
                matter_scope.join(MatterMember, MatterMember.matter_id == Matter.id).filter(MatterMember.user_id == current_user.id)
            )
        recent_matters = matter_scope.order_by(Matter.opened_at.desc()).limit(8).all()

        document_scope = DocumentFile.query
        if not is_admin():
            visible_matter_ids = db.session.query(MatterMember.matter_id).filter(MatterMember.user_id == current_user.id)
            document_scope = document_scope.filter(DocumentFile.matter_id.in_(visible_matter_ids))

        stats = {
            "matter_count": matter_scope.count(),
            "assigned_open_tasks": Task.query.filter(Task.assigned_to == current_user.id, Task.status != "Done").count(),
            "document_count": document_scope.count(),
            "announcement_count": Announcement.query.count(),
        }

        return page(
            "Dashboard",
            "auth/dashboard.html",
            anns=anns,
            my_tasks=my_tasks,
            recent_matters=recent_matters,
            stats=stats,
        )
