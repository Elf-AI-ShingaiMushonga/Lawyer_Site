from __future__ import annotations

import datetime as dt

from flask import redirect, flash, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from ..config import is_valid_email
from ..extensions import db
from ..helpers import audit, is_admin
from ..models import Announcement, Matter, MatterMember, Task, User
from ..templates import page


def register_auth_routes(app):
    @app.get("/")
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))

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

        recent_matters_query = Matter.query
        if not is_admin():
            recent_matters_query = (
                recent_matters_query.join(MatterMember, MatterMember.matter_id == Matter.id).filter(
                    MatterMember.user_id == current_user.id
                )
            )
        recent_matters = recent_matters_query.order_by(Matter.opened_at.desc()).limit(8).all()

        return page(
            "Dashboard",
            "auth/dashboard.html",
            anns=anns,
            my_tasks=my_tasks,
            recent_matters=recent_matters,
        )
