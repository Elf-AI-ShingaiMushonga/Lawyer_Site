from __future__ import annotations

from flask import flash, redirect, request, session, url_for
from flask_login import current_user, logout_user

from .extensions import db
from sqlalchemy.exc import OperationalError

from .helpers import audit, validate_user_session
from .templates import page

MFA_REQUIRED_ROLES = {"admin", "lawyer", "paralegal", "staff"}
MFA_ENROLLMENT_ALLOWLIST = {
    "auth_mfa_setup",
    "auth_mfa_backup_codes",
    "auth_mfa_verify",
    "auth_sessions",
    "auth_session_revoke",
    "logout",
    "static",
}


def register_security_handlers(app):
    @app.before_request
    def enforce_mfa_enrollment():
        if not current_user.is_authenticated:
            return None
        if getattr(current_user, "role", "") not in MFA_REQUIRED_ROLES:
            return None
        if bool(getattr(current_user, "mfa_enabled", False)):
            return None
        endpoint = request.endpoint or ""
        if endpoint in MFA_ENROLLMENT_ALLOWLIST:
            return None
        flash("MFA enrollment is required before accessing other modules.", "warning")
        return redirect(url_for("auth_mfa_setup"))

    @app.before_request
    def enforce_active_session():
        if not current_user.is_authenticated:
            return None
        endpoint = request.endpoint or ""
        if endpoint in {"logout", "static"}:
            return None

        ttl = int(app.config.get("SESSION_TTL_MINUTES", 8 * 60))
        touch_interval = int(app.config.get("SESSION_TOUCH_INTERVAL_SECONDS", 60))
        ok, reason = validate_user_session(
            ttl_minutes=ttl,
            touch_interval_seconds=touch_interval,
        )
        if ok:
            return None

        if reason in {"revoked", "expired"}:
            audit("session_invalidated", "User", current_user.id, {"reason": reason})
        session.pop("_session_token", None)
        session.pop("mfa_verified_at", None)
        logout_user()
        if reason == "revoked":
            flash("Your session was revoked. Please sign in again.", "warning")
        else:
            flash("Your session expired due to inactivity. Please sign in again.", "warning")
        return redirect(url_for("login"))

    @app.after_request
    def set_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "font-src 'self' https://cdn.jsdelivr.net; img-src 'self' data:; frame-ancestors 'none'; "
            "base-uri 'self'; object-src 'none'; form-action 'self'",
        )
        if app.config.get("IS_PRODUCTION"):
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    @app.errorhandler(400)
    def bad_request(_err):
        return page("Bad Request", "errors/400.html"), 400

    @app.errorhandler(403)
    def forbidden(_err):
        return page("Forbidden", "errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(_err):
        return page("Not Found", "errors/404.html"), 404

    @app.errorhandler(413)
    def payload_too_large(_err):
        max_mb = int(app.config["MAX_CONTENT_LENGTH"] / (1024 * 1024))
        return page("File Too Large", "errors/413.html", max_mb=max_mb), 413

    @app.errorhandler(429)
    def too_many_requests(err):
        message = getattr(err, "description", "Too many requests. Please wait and try again.")
        return page("Too Many Requests", "errors/429.html", message=message), 429

    @app.errorhandler(500)
    def internal_error(err):
        db.session.rollback()
        app.logger.exception("Unhandled exception: %s", err)
        return page("Server Error", "errors/500.html"), 500

    @app.errorhandler(OperationalError)
    def database_unavailable(err):
        db.session.rollback()
        app.logger.exception("Database operational error: %s", err)
        return page("Service Unavailable", "errors/503.html"), 503
