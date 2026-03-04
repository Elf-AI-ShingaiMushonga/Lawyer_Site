from __future__ import annotations

from flask import abort, flash, redirect, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from ..config import ROLE_OPTIONS, VALID_ROLES, is_valid_email
from ..extensions import db
from ..helpers import audit, is_admin, normalize_query
from ..models import Announcement, AuditLog, User
from ..roles import canonical_role, role_display_name
from ..templates import page


def register_admin_routes(app):
    @app.get("/admin")
    @login_required
    def admin():
        if not is_admin():
            abort(403)
        return page("Admin", "admin/index.html")

    @app.route("/admin/users", methods=["GET", "POST"])
    @login_required
    def admin_users():
        if not is_admin():
            abort(403)

        if request.method == "POST":
            action = normalize_query(request.form.get("action", "create")) or "create"
            if action == "create":
                email = normalize_query(request.form.get("email", "")).lower()
                full_name = normalize_query(request.form.get("full_name", "")) or "(Unnamed)"
                role = canonical_role(normalize_query(request.form.get("role", "junior_attorney")) or "junior_attorney")
                password = request.form.get("password") or ""
                confirm_password = request.form.get("confirm_password") or ""
                is_active = (request.form.get("is_active") or "").strip().lower() in {"1", "true", "yes", "on"}
                if not email or not password:
                    flash("Email and password required.", "warning")
                    return redirect(url_for("admin_users"))
                if not is_valid_email(email):
                    flash("Invalid email format.", "warning")
                    return redirect(url_for("admin_users"))
                if role not in VALID_ROLES:
                    flash("Invalid role.", "warning")
                    return redirect(url_for("admin_users"))
                if len(password) < 12:
                    flash("Password must be at least 12 characters.", "warning")
                    return redirect(url_for("admin_users"))
                if password != confirm_password:
                    flash("Passwords do not match.", "warning")
                    return redirect(url_for("admin_users"))
                if User.query.filter_by(email=email).first():
                    flash("User already exists.", "warning")
                    return redirect(url_for("admin_users"))
                u = User(email=email, full_name=full_name, role=role, password_hash="x")
                u.set_password(password)
                u.is_active = is_active
                db.session.add(u)
                try:
                    db.session.commit()
                except IntegrityError:
                    db.session.rollback()
                    flash("User already exists.", "warning")
                    return redirect(url_for("admin_users"))
                audit("user_create", "User", u.id, {"email": email, "role": role})
                flash("User created.", "info")
            elif action == "set_active":
                user_id = request.form.get("user_id", type=int)
                user = db.session.get(User, user_id) if user_id else None
                if not user:
                    flash("User not found.", "warning")
                    return redirect(url_for("admin_users"))
                user.is_active = (request.form.get("is_active") or "").strip().lower() in {"1", "true", "yes", "on"}
                db.session.commit()
                audit("user_status_update", "User", user.id, {"is_active": user.is_active})
                flash("User status updated.", "info")
            elif action == "set_role":
                user_id = request.form.get("user_id", type=int)
                role = canonical_role(normalize_query(request.form.get("role", "")))
                user = db.session.get(User, user_id) if user_id else None
                if not user or role not in VALID_ROLES:
                    flash("Invalid user or role.", "warning")
                    return redirect(url_for("admin_users"))
                user.role = role
                db.session.commit()
                audit("user_role_update", "User", user.id, {"role": role})
                flash("User role updated.", "info")
            else:
                flash("Unsupported admin user action.", "warning")
            return redirect(url_for("admin_users"))

        users = User.query.order_by(User.created_at.desc()).limit(500).all()
        return page(
            "Admin Users",
            "admin/users.html",
            users=users,
            role_options=ROLE_OPTIONS,
            role_display_name=role_display_name,
        )

    @app.route("/admin/announcements", methods=["GET", "POST"])
    @login_required
    def admin_announcements():
        if not is_admin():
            abort(403)

        if request.method == "POST":
            title = normalize_query(request.form.get("title", ""))
            body_text = (request.form.get("body") or "").strip()
            if not title or not body_text:
                flash("Title and body required.", "warning")
                return redirect(url_for("admin_announcements"))
            a = Announcement(title=title, body=body_text, created_by=current_user.id)
            db.session.add(a)
            db.session.commit()
            audit("announcement_create", "Announcement", a.id)
            flash("Announcement posted.", "info")
            return redirect(url_for("admin_announcements"))

        anns = Announcement.query.order_by(Announcement.created_at.desc()).limit(50).all()
        return page("Announcements", "admin/announcements.html", anns=anns)

    @app.get("/admin/audit")
    @login_required
    def admin_audit():
        if not is_admin():
            abort(403)
        logs = AuditLog.query.order_by(AuditLog.at.desc()).limit(200).all()
        return page("Audit Log", "admin/audit.html", logs=logs)
