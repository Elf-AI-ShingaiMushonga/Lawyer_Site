from __future__ import annotations

from .extensions import db
from sqlalchemy.exc import OperationalError

from .templates import page


def register_security_handlers(app):
    @app.after_request
    def set_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "font-src 'self' https://cdn.jsdelivr.net; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'",
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
