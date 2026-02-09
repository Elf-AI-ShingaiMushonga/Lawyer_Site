from __future__ import annotations

import datetime as dt
import logging
import os
import secrets

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import BASE_DIR, PRODUCTION_ENV_VALUES, UPLOAD_DIR, env_bool, env_int
from .csrf import register_csrf_protection
from .extensions import db, login_manager
from .models import User
from .routes import register_routes
from .security import register_security_handlers


@login_manager.user_loader
def load_user(user_id: str):
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None


def create_app() -> Flask:
    app = Flask(__name__)

    app_env = os.environ.get("FLASK_ENV", "development").strip().lower()
    is_production = app_env in PRODUCTION_ENV_VALUES
    secret_key = os.environ.get("FLASK_SECRET_KEY")

    if is_production and not secret_key:
        raise RuntimeError("FLASK_SECRET_KEY must be set in production.")
    if not secret_key:
        secret_key = secrets.token_urlsafe(32)

    database_uri = os.environ.get("DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'intranet.db')}")
    upload_dir = os.environ.get("UPLOAD_DIR", UPLOAD_DIR)
    os.makedirs(upload_dir, exist_ok=True)
    max_upload_bytes = env_int("MAX_UPLOAD_BYTES", 50 * 1024 * 1024)
    session_ttl_minutes = env_int("SESSION_TTL_MINUTES", 8 * 60)
    trust_proxy = env_bool("TRUST_PROXY", False)
    force_secure_cookie = env_bool("FORCE_SECURE_COOKIE", False)
    secure_cookie = is_production or force_secure_cookie

    app.config.update(
        SECRET_KEY=secret_key,
        SQLALCHEMY_DATABASE_URI=database_uri,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
        MAX_CONTENT_LENGTH=max_upload_bytes,
        UPLOAD_DIR=upload_dir,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=secure_cookie,
        REMEMBER_COOKIE_HTTPONLY=True,
        REMEMBER_COOKIE_SAMESITE="Lax",
        REMEMBER_COOKIE_SECURE=secure_cookie,
        PERMANENT_SESSION_LIFETIME=dt.timedelta(minutes=session_ttl_minutes),
        PREFERRED_URL_SCHEME="https" if secure_cookie else "http",
    )

    if not database_uri.startswith("sqlite"):
        app.config["SQLALCHEMY_ENGINE_OPTIONS"]["pool_recycle"] = env_int("DB_POOL_RECYCLE_SECONDS", 1800)

    if trust_proxy:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app.logger.setLevel(getattr(logging, log_level, logging.INFO))
    app.config["IS_PRODUCTION"] = is_production

    db.init_app(app)

    login_manager.init_app(app)
    login_manager.login_view = "login"
    login_manager.session_protection = "strong"

    register_csrf_protection(app)
    register_security_handlers(app)
    register_routes(app)

    return app
