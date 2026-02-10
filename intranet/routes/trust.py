from __future__ import annotations

import datetime as dt

from flask import abort, flash, redirect, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..helpers import audit, is_admin, normalize_query
from ..models import AuditLog, GovernanceIncident
from ..templates import page

INCIDENT_TYPES = {"Incident", "Change", "Outage", "Security Alert", "Compliance"}
INCIDENT_SEVERITIES = {"Low", "Medium", "High", "Critical"}


def register_trust_routes(app):
    @app.get("/trust/policy")
    @login_required
    def trust_policy():
        retention_controls = [
            {
                "record": "Matter records",
                "retention": "7 years after closure",
                "control": "Archive + restricted retrieval",
            },
            {
                "record": "Audit log",
                "retention": "24 months online + cold archive",
                "control": "Immutable export to centralized storage",
            },
            {
                "record": "Knowledge articles",
                "retention": "Until superseded + review every 12 months",
                "control": "Owner review workflow",
            },
            {
                "record": "Operational incidents",
                "retention": "5 years",
                "control": "Post-incident summary + closure evidence",
            },
        ]
        access_controls = [
            "Role-based access control (admin, lawyer, paralegal, staff).",
            "Matter-level membership restrictions for non-admin users.",
            "Session hardening (secure cookies, CSRF, rate limiting).",
            "Document retrieval events captured in audit trail.",
        ]
        return page(
            "Data Policy",
            "trust/policy.html",
            retention_controls=retention_controls,
            access_controls=access_controls,
        )

    @app.get("/trust/security")
    @login_required
    def trust_security():
        security_controls = [
            ("Authentication", "Password hashing, account activation controls, and session protection."),
            ("Transport", "TLS termination expected at Nginx/ALB with secure cookies in production."),
            ("Application", "CSRF protection, security headers, input validation, and rate limiting."),
            ("Data", "Database-backed audit logs and document integrity hashing (SHA-256)."),
            ("Operations", "Health endpoint, systemd service controls, and centralized log forwarding ready."),
        ]
        hardening_backlog = [
            "Integrate SSO + MFA for identity assurance.",
            "Move uploads to S3 with lifecycle policies and immutable backup.",
            "Enable managed Redis for distributed rate limiting.",
            "Attach runtime monitoring/alerting (APM + infra metrics).",
        ]
        return page(
            "Security Posture",
            "trust/security.html",
            security_controls=security_controls,
            hardening_backlog=hardening_backlog,
            now_utc=dt.datetime.utcnow(),
        )

    @app.route("/trust/incidents", methods=["GET", "POST"])
    @login_required
    def trust_incidents():
        if request.method == "POST":
            action = normalize_query(request.form.get("action", "create")) or "create"
            if not is_admin():
                abort(403)

            if action == "create":
                title = normalize_query(request.form.get("title", ""))
                incident_type = normalize_query(request.form.get("incident_type", "Incident")) or "Incident"
                severity = normalize_query(request.form.get("severity", "Medium")) or "Medium"
                summary = (request.form.get("summary") or "").strip()
                impact = (request.form.get("impact") or "").strip()
                if not title or not summary:
                    flash("Incident title and summary are required.", "warning")
                    return redirect(url_for("trust_incidents"))
                if incident_type not in INCIDENT_TYPES:
                    flash("Invalid incident type.", "warning")
                    return redirect(url_for("trust_incidents"))
                if severity not in INCIDENT_SEVERITIES:
                    flash("Invalid severity.", "warning")
                    return redirect(url_for("trust_incidents"))

                incident = GovernanceIncident(
                    title=title,
                    incident_type=incident_type,
                    severity=severity,
                    summary=summary,
                    impact=impact or None,
                    status="Open",
                    created_by=current_user.id,
                    updated_by=current_user.id,
                )
                db.session.add(incident)
                db.session.commit()
                audit("incident_create", "GovernanceIncident", incident.id, {"severity": severity, "type": incident_type})
                flash("Incident/change record created.", "info")
                return redirect(url_for("trust_incidents"))

            if action == "close":
                incident_id = request.form.get("incident_id", type=int)
                resolution = (request.form.get("resolution") or "").strip()
                incident = db.session.get(GovernanceIncident, incident_id) if incident_id else None
                if not incident:
                    flash("Incident record not found.", "warning")
                    return redirect(url_for("trust_incidents"))
                if not resolution:
                    flash("Provide a resolution note before closing.", "warning")
                    return redirect(url_for("trust_incidents"))

                incident.status = "Closed"
                incident.resolution = resolution
                incident.closed_at = dt.datetime.utcnow()
                incident.updated_by = current_user.id
                db.session.commit()
                audit("incident_close", "GovernanceIncident", incident.id)
                flash("Incident/change record closed.", "info")
                return redirect(url_for("trust_incidents"))

            flash("Unsupported incident action.", "warning")
            return redirect(url_for("trust_incidents"))

        status_filter = normalize_query(request.args.get("status", "all")).lower()
        incident_scope = GovernanceIncident.query
        if status_filter == "open":
            incident_scope = incident_scope.filter(GovernanceIncident.status == "Open")
        elif status_filter == "closed":
            incident_scope = incident_scope.filter(GovernanceIncident.status == "Closed")

        incidents = incident_scope.order_by(GovernanceIncident.opened_at.desc()).limit(200).all()
        open_count = GovernanceIncident.query.filter(GovernanceIncident.status == "Open").count()
        closed_count = GovernanceIncident.query.filter(GovernanceIncident.status == "Closed").count()
        recent_changes = AuditLog.query.order_by(AuditLog.at.desc()).limit(20).all()

        return page(
            "Incident and Change Log",
            "trust/incidents.html",
            incidents=incidents,
            open_count=open_count,
            closed_count=closed_count,
            status_filter=status_filter,
            incident_types=sorted(INCIDENT_TYPES),
            incident_severities=sorted(INCIDENT_SEVERITIES),
            recent_changes=recent_changes,
            admin_mode=is_admin(),
        )
