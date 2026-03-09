from __future__ import annotations

from flask import abort, flash, redirect, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from ..config import ROLE_OPTIONS, VALID_ROLES, is_valid_email
from ..extensions import db
from ..helpers import audit, is_admin, normalize_query
from ..models import (
    Announcement,
    AuditLog,
    ContractTemplate,
    DeadlineRule,
    DocumentTemplate,
    GovernanceIncident,
    LegalHold,
    MatterTemplate,
    Office,
    PortalUser,
    PracticeArea,
    RateCard,
    RetentionPolicy,
    TaskTemplate,
    TrustApprovalRequest,
    User,
)
from ..roles import canonical_role, role_display_name
from ..templates import page


def _build_admin_console_context() -> dict[str, object]:
    total_users = User.query.count()
    active_users = User.query.filter(User.is_active.is_(True)).count()
    inactive_users = max(0, total_users - active_users)
    mfa_enabled_users = User.query.filter(User.is_active.is_(True), User.mfa_enabled.is_(True)).count()
    mfa_gap = max(0, active_users - mfa_enabled_users)
    portal_users = PortalUser.query.count()
    active_portal_users = PortalUser.query.filter(PortalUser.is_active.is_(True)).count()

    setup_checks = [
        {
            "label": "Offices",
            "count": Office.query.filter(Office.is_active.is_(True)).count(),
            "href": url_for("admin_settings_offices"),
            "button_label": "Open Offices",
            "configured_label": "Configured",
            "missing_label": "No offices",
        },
        {
            "label": "Practice Areas",
            "count": PracticeArea.query.filter(PracticeArea.is_active.is_(True)).count(),
            "href": url_for("admin_settings_practice_areas"),
            "button_label": "Open Practice Areas",
            "configured_label": "Configured",
            "missing_label": "Needs seeding",
        },
        {
            "label": "Rate Cards",
            "count": RateCard.query.filter(RateCard.is_active.is_(True)).count(),
            "href": url_for("admin_settings_rates"),
            "button_label": "Open Rates",
            "configured_label": "Configured",
            "missing_label": "No active rates",
        },
        {
            "label": "Matter Archetypes",
            "count": MatterTemplate.query.count(),
            "href": url_for("admin_templates_matters"),
            "button_label": "Open Archetypes",
            "configured_label": "Configured",
            "missing_label": "No archetypes",
        },
        {
            "label": "Task Templates",
            "count": TaskTemplate.query.count(),
            "href": url_for("admin_templates_tasks"),
            "button_label": "Open Task Templates",
            "configured_label": "Configured",
            "missing_label": "No task templates",
        },
        {
            "label": "Document Templates",
            "count": DocumentTemplate.query.count(),
            "href": url_for("admin_templates_documents"),
            "button_label": "Open Document Templates",
            "configured_label": "Configured",
            "missing_label": "No document templates",
        },
        {
            "label": "Contract Templates",
            "count": ContractTemplate.query.filter(ContractTemplate.is_active.is_(True)).count(),
            "href": url_for("admin_templates_contracts"),
            "button_label": "Open Contract Templates",
            "configured_label": "Configured",
            "missing_label": "No active contracts",
        },
        {
            "label": "Deadline Rules",
            "count": DeadlineRule.query.filter(DeadlineRule.is_active.is_(True)).count(),
            "href": url_for("admin_rules_deadlines"),
            "button_label": "Open Deadline Rules",
            "configured_label": "Configured",
            "missing_label": "No active rules",
        },
        {
            "label": "Retention Policies",
            "count": RetentionPolicy.query.filter(RetentionPolicy.is_active.is_(True)).count(),
            "href": url_for("admin_rules_retention"),
            "button_label": "Open Retention Policies",
            "configured_label": "Configured",
            "missing_label": "No active policies",
        },
    ]
    for item in setup_checks:
        count = int(item["count"] or 0)
        item["configured"] = count > 0
        item["status_label"] = item["configured_label"] if count > 0 else item["missing_label"]
        item["tone"] = "positive" if count > 0 else "critical"

    open_incidents = GovernanceIncident.query.filter(GovernanceIncident.status == "Open").count()
    active_legal_holds = LegalHold.query.filter(LegalHold.is_active.is_(True)).count()
    pending_trust_approvals = TrustApprovalRequest.query.filter(TrustApprovalRequest.status == "pending").count()
    announcement_count = Announcement.query.count()

    watchlist: list[dict[str, str]] = []
    if mfa_gap:
        watchlist.append(
            {
                "tone": "critical",
                "title": "Internal MFA gap",
                "summary": f"{mfa_gap} active internal user(s) do not have MFA enabled.",
                "href": url_for("admin_users"),
                "button_label": "Review Users",
                "badge": f"{mfa_gap} user(s)",
            }
        )
    if inactive_users:
        watchlist.append(
            {
                "tone": "watch",
                "title": "Inactive account backlog",
                "summary": f"{inactive_users} internal account(s) are inactive and may need review or offboarding confirmation.",
                "href": url_for("admin_users"),
                "button_label": "Review Access",
                "badge": f"{inactive_users} inactive",
            }
        )
    if open_incidents:
        watchlist.append(
            {
                "tone": "critical",
                "title": "Open governance incidents",
                "summary": f"{open_incidents} incident/change record(s) are still open.",
                "href": url_for("trust_incidents"),
                "button_label": "Open Incidents",
                "badge": f"{open_incidents} open",
            }
        )
    if pending_trust_approvals:
        watchlist.append(
            {
                "tone": "watch",
                "title": "Pending trust approvals",
                "summary": f"{pending_trust_approvals} trust approval request(s) are waiting for a decision.",
                "href": url_for("trust_ledger"),
                "button_label": "Open Trust Ledger",
                "badge": f"{pending_trust_approvals} pending",
            }
        )
    if active_legal_holds:
        watchlist.append(
            {
                "tone": "watch",
                "title": "Active legal holds",
                "summary": f"{active_legal_holds} matter(s) are currently on legal hold and require records discipline.",
                "href": url_for("admin_rules_legal_holds"),
                "button_label": "Review Holds",
                "badge": f"{active_legal_holds} hold(s)",
            }
        )
    missing_setup = [item for item in setup_checks if not bool(item["configured"])]
    if missing_setup:
        top_gap = missing_setup[0]
        watchlist.append(
            {
                "tone": "critical",
                "title": "Configuration gap",
                "summary": f"{top_gap['label']} is not configured yet. Complete setup before relying on the related workflows.",
                "href": str(top_gap["href"]),
                "button_label": str(top_gap["button_label"]),
                "badge": str(top_gap["status_label"]),
            }
        )

    recent_logs = AuditLog.query.order_by(AuditLog.at.desc()).limit(8).all()
    actor_ids = sorted({int(row.actor_user_id) for row in recent_logs if row.actor_user_id is not None})
    actors_by_id = {row.id: row for row in User.query.filter(User.id.in_(actor_ids)).all()} if actor_ids else {}

    quick_actions = [
        {"title": "User Provisioning", "summary": "Create users, fix role drift, and close MFA gaps.", "href": url_for("admin_users"), "badge": "Identity"},
        {"title": "Automation Studio", "summary": "Tune templates, archetypes, and workflow builders.", "href": url_for("admin_automation"), "badge": "Automation"},
        {"title": "Audit Trail", "summary": "Review sensitive changes and governance activity.", "href": url_for("admin_audit"), "badge": "Audit"},
        {"title": "Trust & Incidents", "summary": "Watch incidents, trust approvals, and control posture.", "href": url_for("trust_incidents"), "badge": "Risk"},
        {"title": "Firm Settings", "summary": "Set global metadata lists, digest settings, and defaults.", "href": url_for("admin_settings_firm"), "badge": "Config"},
        {"title": "Portal Users", "summary": "Manage client-facing users and portal access posture.", "href": url_for("admin_portal_users"), "badge": "Portal"},
    ]

    launchpads = [
        {
            "title": "Identity & Access",
            "summary": "Internal users, portal identities, and federated access.",
            "actions": [
                {"label": "Internal Users", "href": url_for("admin_users")},
                {"label": "Portal Users", "href": url_for("admin_portal_users")},
                {"label": "SSO Apps", "href": url_for("admin_sso_apps")},
            ],
        },
        {
            "title": "Governance & Risk",
            "summary": "Auditability, incidents, legal holds, and trust controls.",
            "actions": [
                {"label": "Audit Log", "href": url_for("admin_audit")},
                {"label": "Incident Register", "href": url_for("trust_incidents")},
                {"label": "Legal Holds", "href": url_for("admin_rules_legal_holds")},
                {"label": "Trust Rules", "href": url_for("admin_rules_trust")},
            ],
        },
        {
            "title": "Configuration",
            "summary": "Firm defaults, offices, rates, and practice taxonomy.",
            "actions": [
                {"label": "Firm Settings", "href": url_for("admin_settings_firm")},
                {"label": "Offices", "href": url_for("admin_settings_offices")},
                {"label": "Practice Areas", "href": url_for("admin_settings_practice_areas")},
                {"label": "Rates", "href": url_for("admin_settings_rates")},
            ],
        },
        {
            "title": "Template Factory",
            "summary": "Matter, contract, document, and task templates.",
            "actions": [
                {"label": "Matter Archetypes", "href": url_for("admin_templates_matters")},
                {"label": "Contract Templates", "href": url_for("admin_templates_contracts")},
                {"label": "Document Templates", "href": url_for("admin_templates_documents")},
                {"label": "Task Templates", "href": url_for("admin_templates_tasks")},
            ],
        },
    ]

    return {
        "summary": {
            "total_users": total_users,
            "active_users": active_users,
            "mfa_enabled_users": mfa_enabled_users,
            "mfa_coverage_pct": round((mfa_enabled_users / active_users) * 100, 1) if active_users else 0.0,
            "portal_users": portal_users,
            "active_portal_users": active_portal_users,
            "open_incidents": open_incidents,
            "pending_trust_approvals": pending_trust_approvals,
            "active_legal_holds": active_legal_holds,
            "announcement_count": announcement_count,
            "configured_checks": sum(1 for item in setup_checks if bool(item["configured"])),
            "setup_check_total": len(setup_checks),
            "active_template_count": sum(
                int(item["count"] or 0)
                for item in setup_checks
                if item["label"] in {"Matter Archetypes", "Task Templates", "Document Templates", "Contract Templates"}
            ),
        },
        "watchlist": watchlist,
        "setup_checks": setup_checks,
        "recent_logs": recent_logs,
        "actors_by_id": actors_by_id,
        "quick_actions": quick_actions,
        "launchpads": launchpads,
    }


def register_admin_routes(app):
    @app.get("/admin")
    @login_required
    def admin():
        if not is_admin():
            abort(403)
        console = _build_admin_console_context()
        return page("Admin", "admin/index.html", console=console)

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
