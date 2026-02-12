from __future__ import annotations

import datetime as dt
from urllib.parse import urlsplit

from flask import flash, redirect, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from ..config import is_valid_email
from ..extensions import db, limiter
from ..helpers import (
    audit,
    can_access_matter,
    is_admin,
    register_trusted_device,
    register_user_session,
    revoke_current_session,
)
from ..mfa import check_backup_code, verify_totp
from ..models import (
    Announcement,
    AuditLog,
    Contact,
    DocumentFile,
    KnowledgeBase,
    Matter,
    MatterMember,
    Task,
    User,
    UserMFABackupCode,
)
from ..policies import visible_matter_ids
from ..templates import page

MFA_REQUIRED_ROLES = {"admin", "lawyer", "paralegal", "staff"}


def has_any_users() -> bool:
    return db.session.query(User.id).first() is not None


def _safe_next_path(next_path: str | None, fallback: str) -> str:
    if not next_path:
        return fallback
    parsed = urlsplit(next_path)
    if parsed.scheme or parsed.netloc:
        return fallback
    if not parsed.path.startswith("/"):
        return fallback
    return next_path


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
            start_live_demo = (request.form.get("start_live_demo") or "").strip().lower() in {"1", "true", "yes", "on"}
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
            if start_live_demo:
                session["client_story_mode"] = True
                return redirect(url_for("client_story"))
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

    @app.post("/story-mode")
    @login_required
    def toggle_story_mode():
        enabled = (request.form.get("enabled") or "").strip().lower() in {"1", "true", "yes", "on"}
        session["client_story_mode"] = enabled
        flash("Client story mode enabled." if enabled else "Client story mode disabled.", "info")
        next_path = _safe_next_path(request.form.get("next"), url_for("dashboard"))
        return redirect(next_path)

    @app.get("/dashboard")
    @login_required
    def dashboard():
        anns = Announcement.query.order_by(Announcement.created_at.desc()).limit(5).all()
        today = dt.date.today()

        my_tasks = (
            Task.query.filter(Task.assigned_to == current_user.id)
            .order_by(Task.status.asc(), Task.due_date.asc().nullslast(), Task.created_at.desc())
            .limit(8)
            .all()
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
        urgent_unassigned_scope = Task.query.filter(
            Task.status != "Done",
            Task.assigned_to.is_(None),
            Task.due_date.isnot(None),
            Task.due_date <= (today + dt.timedelta(days=3)),
        )
        if not is_admin():
            if scoped_ids:
                overdue_task_scope = overdue_task_scope.filter(Task.matter_id.in_(scoped_ids))
                due_week_scope = due_week_scope.filter(Task.matter_id.in_(scoped_ids))
                urgent_unassigned_scope = urgent_unassigned_scope.filter(Task.matter_id.in_(scoped_ids))
            else:
                overdue_task_scope = overdue_task_scope.filter(Task.id == -1)
                due_week_scope = due_week_scope.filter(Task.id == -1)
                urgent_unassigned_scope = urgent_unassigned_scope.filter(Task.id == -1)

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

        stats = {
            "matter_count": matter_scope.count(),
            "assigned_open_tasks": Task.query.filter(Task.assigned_to == current_user.id, Task.status != "Done").count(),
            "document_count": document_scope.count(),
            "announcement_count": Announcement.query.count(),
            "overdue_tasks": overdue_task_scope.count(),
            "due_this_week": due_week_scope.count(),
            "urgent_unassigned": urgent_unassigned_scope.count(),
        }

        return page(
            "Dashboard",
            "auth/dashboard.html",
            anns=anns,
            my_tasks=my_tasks,
            recent_matters=recent_matters,
            stats=stats,
            at_risk_matters=at_risk_matters,
        )

    @app.get("/story")
    @login_required
    def client_story():
        matter_scope = Matter.query
        scoped_ids: list[int] | None = None
        if not is_admin():
            scoped_ids = visible_matter_ids()
            if scoped_ids:
                matter_scope = matter_scope.filter(Matter.id.in_(scoped_ids))
            else:
                matter_scope = matter_scope.filter(Matter.id == -1)

        flagship_matter = matter_scope.filter(Matter.matter_no == "2026-LIT-0142").first()
        if flagship_matter and not can_access_matter(flagship_matter.id):
            flagship_matter = None
        if not flagship_matter:
            flagship_matter = matter_scope.order_by(Matter.opened_at.desc()).first()

        open_task_scope = Task.query.filter(Task.status != "Done")
        document_scope = DocumentFile.query
        if not is_admin():
            if scoped_ids:
                open_task_scope = open_task_scope.filter(Task.matter_id.in_(scoped_ids))
                document_scope = document_scope.filter(DocumentFile.matter_id.in_(scoped_ids))
            else:
                open_task_scope = open_task_scope.filter(Task.id == -1)
                document_scope = document_scope.filter(DocumentFile.id == -1)

        story_stats = {
            "matter_count": matter_scope.count(),
            "open_task_count": open_task_scope.count(),
            "document_count": document_scope.count(),
            "contact_count": Contact.query.count(),
            "knowledge_count": KnowledgeBase.query.count(),
            "announcement_count": Announcement.query.count(),
            "audit_count": AuditLog.query.count() if is_admin() else None,
        }

        flagship_metrics = {
            "task_count": Task.query.filter(Task.matter_id == flagship_matter.id).count() if flagship_matter else 0,
            "document_count": DocumentFile.query.filter(DocumentFile.matter_id == flagship_matter.id).count()
            if flagship_matter
            else 0,
            "team_count": MatterMember.query.filter(MatterMember.matter_id == flagship_matter.id).count() if flagship_matter else 0,
        }

        story_links = {
            "dashboard": url_for("dashboard"),
            "matters": url_for("matters", q=(flagship_matter.matter_no if flagship_matter else "")),
            "matter_detail": url_for("matter_detail", matter_id=flagship_matter.id) if flagship_matter else url_for("matters"),
            "tasks": url_for("matter_tasks", matter_id=flagship_matter.id) if flagship_matter else url_for("matters"),
            "documents": url_for("matter_documents", matter_id=flagship_matter.id) if flagship_matter else url_for("matters"),
            "knowledge": url_for("kb"),
            "search": url_for("search", q="POPIA"),
            "audit": url_for("admin_audit") if is_admin() else url_for("kb"),
        }

        story_pack_nos = ["2026-LIT-0142", "2026-CORP-0033", "2026-EMP-0071"]
        story_pack_rows = (
            matter_scope.filter(Matter.matter_no.in_(story_pack_nos))
            .order_by(Matter.opened_at.desc())
            .limit(3)
            .all()
        )
        if not story_pack_rows:
            story_pack_rows = matter_scope.order_by(Matter.opened_at.desc()).limit(3).all()

        role_guides = {
            "admin": {
                "title": "Governance-first narrative",
                "focus": [
                    "Platform-wide control posture and auditability",
                    "Operational KPIs and adoption metrics",
                    "Incident/change handling and oversight",
                ],
            },
            "partner": {
                "title": "Partner narrative",
                "focus": [
                    "Portfolio-level risk posture and business impact",
                    "Client readiness across flagship matters",
                    "Governance confidence before board/client reviews",
                ],
            },
            "associate": {
                "title": "Associate narrative",
                "focus": [
                    "Execution detail across tasks, evidence, and deadlines",
                    "Matter-level accountability and delivery velocity",
                    "Knowledge reuse and reduced drafting cycle time",
                ],
            },
            "paralegal": {
                "title": "Delivery-efficiency narrative",
                "focus": [
                    "Document readiness and filing timelines",
                    "Evidence integrity and retrieval speed",
                    "Daily execution workflow consistency",
                ],
            },
            "staff": {
                "title": "Operations-consistency narrative",
                "focus": [
                    "Intake quality and communication consistency",
                    "Cross-team visibility of priorities",
                    "Governed process handoffs",
                ],
            },
        }
        role_key = current_user.role
        if current_user.role == "lawyer":
            role_key = "partner" if current_user.email.startswith("partner@") else "associate"
        role_story = role_guides.get(role_key, role_guides["associate"])

        return page(
            "Client Story",
            "auth/story.html",
            story_stats=story_stats,
            flagship_matter=flagship_matter,
            flagship_metrics=flagship_metrics,
            story_links=story_links,
            is_admin_user=is_admin(),
            role_story=role_story,
            story_pack_matters=story_pack_rows,
        )
