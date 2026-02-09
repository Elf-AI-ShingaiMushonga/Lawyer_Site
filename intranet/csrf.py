from __future__ import annotations

import secrets

from flask import abort, request, session

CSRF_SESSION_KEY = "_csrf_token"


def _get_or_create_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if token:
        return token
    token = secrets.token_urlsafe(32)
    session[CSRF_SESSION_KEY] = token
    return token


def register_csrf_protection(app):
    app.jinja_env.globals["csrf_token"] = _get_or_create_token

    @app.before_request
    def enforce_csrf():
        if request.method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
            return
        sent_token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        expected_token = session.get(CSRF_SESSION_KEY)
        if not sent_token or not expected_token or sent_token != expected_token:
            abort(400)
