from __future__ import annotations

import datetime as dt
from urllib.parse import urlsplit

from flask import flash, redirect, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import func, or_
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
    ConflictCheck,
    Deadline,
    Contact,
    DocumentFile,
    Invoice,
    KnowledgeBase,
    Matter,
    MatterMember,
    PortalMessageThread,
    Task,
    TaskAssignee,
    TrustReconciliationRun,
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
        has_any_assignee = db.session.query(TaskAssignee.id).filter(TaskAssignee.task_id == Task.id).exists()
        urgent_unassigned_scope = Task.query.filter(
            Task.status != "Done",
            Task.assigned_to.is_(None),
            ~has_any_assignee,
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
            "assigned_open_tasks": Task.query.filter(my_assignment_clause, Task.status != "Done").count(),
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
            task_matter_map=task_matter_map,
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
        invoice_scope = Invoice.query
        deadline_scope = Deadline.query.filter(
            Deadline.due_at >= dt.date.today(),
            Deadline.due_at <= (dt.date.today() + dt.timedelta(days=14)),
        )
        if not is_admin():
            if scoped_ids:
                open_task_scope = open_task_scope.filter(Task.matter_id.in_(scoped_ids))
                document_scope = document_scope.filter(DocumentFile.matter_id.in_(scoped_ids))
                invoice_scope = invoice_scope.filter(Invoice.matter_id.in_(scoped_ids))
                deadline_scope = deadline_scope.filter(Deadline.matter_id.in_(scoped_ids))
            else:
                open_task_scope = open_task_scope.filter(Task.id == -1)
                document_scope = document_scope.filter(DocumentFile.id == -1)
                invoice_scope = invoice_scope.filter(Invoice.id == -1)
                deadline_scope = deadline_scope.filter(Deadline.id == -1)

        trust_visible = current_user.role in {"admin", "lawyer"}
        ops_visible = current_user.role == "admin"
        crm_conflict_count = ConflictCheck.query.count() if trust_visible else None
        portal_thread_count = PortalMessageThread.query.count() if is_admin() else None
        integration_event_count = (
            AuditLog.query.filter(
                or_(
                    AuditLog.action.like("office365_%"),
                    AuditLog.action.like("third_party_%"),
                    AuditLog.action.like("mobile_%"),
                )
            ).count()
            if is_admin()
            else None
        )

        story_stats = {
            "matter_count": matter_scope.count(),
            "open_task_count": open_task_scope.count(),
            "document_count": document_scope.count(),
            "deadline_next_14_count": deadline_scope.count(),
            "invoice_count": invoice_scope.count(),
            "contact_count": Contact.query.count(),
            "knowledge_count": KnowledgeBase.query.count(),
            "announcement_count": Announcement.query.count(),
            "conflict_count": crm_conflict_count,
            "trust_reconciliation_count": TrustReconciliationRun.query.count() if trust_visible else None,
            "portal_thread_count": portal_thread_count,
            "integration_event_count": integration_event_count,
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
            "calendar": url_for("calendar_my"),
            "workflow": url_for("task_templates"),
            "dms": url_for("matter_dms", matter_id=flagship_matter.id) if flagship_matter else url_for("matters"),
            "billing": url_for("billing_invoices"),
            "trust": url_for("trust_ledger") if trust_visible else url_for("trust_security"),
            "crm": url_for("crm_leads"),
            "portal": url_for("portal_login"),
            "integrations": url_for("integrations_office365"),
            "mobile": url_for("mobile_hub"),
            "analytics": url_for("analytics_utilization"),
            "ops": url_for("ops_backup_status") if ops_visible else url_for("trust_security"),
            "knowledge": url_for("kb"),
            "search": url_for("search", q="POPIA"),
            "audit": url_for("admin_audit") if is_admin() else url_for("trust_security"),
        }

        story_sequence = [
            {
                "title": "Executive snapshot",
                "description": "Frame current workload, open risks, and overdue pressure at firm level.",
                "href": story_links["dashboard"],
            },
            {
                "title": "Matter workspace and stage control",
                "description": "Open a flagship matter and show stage, risk, parties, and internal notes in one workspace.",
                "href": story_links["matter_detail"],
            },
            {
                "title": "Calendar and deadline execution",
                "description": "Show critical deadlines, acknowledgements, and team-level milestone visibility.",
                "href": story_links["calendar"],
            },
            {
                "title": "Task workflow automation",
                "description": "Demonstrate templates, dependencies, checklists, and multi-assignee execution.",
                "href": story_links["workflow"],
            },
            {
                "title": "Document lifecycle and productions",
                "description": "Walk through DMS versioning, lock controls, OCR discovery, and production sets.",
                "href": story_links["dms"],
            },
            {
                "title": "Time capture, billing, and collections",
                "description": "Review per-transaction billing, statements, tax invoices, and settled payment records.",
                "href": story_links["billing"],
            },
            {
                "title": "Trust accounting and compliance",
                "description": "Show bank statement imports, cashbook/trial balance views, and Section 86 automation.",
                "href": story_links["trust"],
            },
            {
                "title": "CRM intake and client portal",
                "description": "Show lead-to-conflict workflows, engagement progress, and curated client collaboration.",
                "href": story_links["crm"],
            },
            {
                "title": "Integrations and mobile operations",
                "description": "Show Office365 exports, third-party import/export, and mobile fee/task capture.",
                "href": story_links["integrations"],
            },
            {
                "title": "Analytics and governance closeout",
                "description": "Finish with utilization/profitability indicators and auditable operational controls.",
                "href": story_links["audit"],
            },
        ]

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
                    "Operational KPIs, integration telemetry, and adoption metrics",
                    "Backup/restore controls, trust compliance, and incident oversight",
                ],
            },
            "partner": {
                "title": "Partner narrative",
                "focus": [
                    "Portfolio-level risk posture and business impact",
                    "Financial confidence across billing, trust, and reconciliations",
                    "Client readiness across flagship matters and portal touchpoints",
                ],
            },
            "associate": {
                "title": "Associate narrative",
                "focus": [
                    "Execution detail across tasks, evidence, deadlines, and stage transitions",
                    "Matter-level accountability and delivery velocity",
                    "Knowledge reuse, DMS versioning, and reduced drafting cycle time",
                ],
            },
            "paralegal": {
                "title": "Delivery-efficiency narrative",
                "focus": [
                    "Document readiness, filing timelines, and production controls",
                    "Evidence integrity with OCR retrieval and lock discipline",
                    "Calendar/task execution consistency across matters",
                ],
            },
            "staff": {
                "title": "Operations-consistency narrative",
                "focus": [
                    "Intake quality, lead progression, and communication consistency",
                    "Cross-team visibility of priorities plus mobile quick-capture workflows",
                    "Governed process handoffs into billing, portal, and analytics views",
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
            story_sequence=story_sequence,
            story_pack_matters=story_pack_rows,
        )
