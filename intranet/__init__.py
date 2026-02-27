from __future__ import annotations

import datetime as dt
import logging
import os
import secrets
import sys

from flask import Flask, request, session
from flask_login import current_user
from sqlalchemy.engine import make_url
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import BASE_DIR, PRODUCTION_ENV_VALUES, UPLOAD_DIR, env_bool, env_int
from .csrf import register_csrf_protection
from .db_context import apply_request_db_context
from .extensions import (
    HAS_FLASK_LIMITER,
    HAS_FLASK_MIGRATE,
    db,
    limiter,
    login_manager,
    migrate,
)
from .helpers import ACTIVE_MATTER_SESSION_KEY, resolve_active_matter, set_active_matter_context
from .models import User
from .routes import register_routes
from .schema_sync import sync_schema_compatibility
from .security import register_security_handlers


@login_manager.user_loader
def load_user(user_id: str):
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None


def _is_migration_cli_invocation() -> bool:
    argv = [part.strip().lower() for part in sys.argv[1:] if part.strip()]
    if "db" in argv:
        return True
    executable = os.path.basename((sys.argv[0] or "")).strip().lower()
    return "alembic" in executable


def create_app() -> Flask:
    app = Flask(__name__)

    app_env = os.environ.get("FLASK_ENV", "development").strip().lower()
    is_production = app_env in PRODUCTION_ENV_VALUES
    secret_key = os.environ.get("FLASK_SECRET_KEY")
    database_uri = os.environ.get("DATABASE_URL")
    backup_encryption_key = (os.environ.get("BACKUP_ENCRYPTION_KEY") or "").strip()
    rate_limit_storage_uri = os.environ.get("RATE_LIMIT_STORAGE_URI", "memory://")
    rate_limit_strategy = os.environ.get("RATE_LIMIT_STRATEGY", "fixed-window")
    auth_login_rate_limit = os.environ.get("AUTH_LOGIN_RATE_LIMIT", "10/minute")
    auth_register_rate_limit = os.environ.get("AUTH_REGISTER_RATE_LIMIT", "5/hour")
    portal_login_rate_limit = os.environ.get("PORTAL_LOGIN_RATE_LIMIT", "10/minute")
    sso_token_rate_limit = os.environ.get("AUTH_SSO_TOKEN_RATE_LIMIT", "60/minute")

    if is_production and not secret_key:
        raise RuntimeError("FLASK_SECRET_KEY must be set in production.")
    if is_production and not database_uri:
        raise RuntimeError("DATABASE_URL must be set in production.")
    if is_production and not backup_encryption_key:
        raise RuntimeError("BACKUP_ENCRYPTION_KEY must be set in production.")
    if is_production and not HAS_FLASK_MIGRATE:
        raise RuntimeError("Flask-Migrate dependency is required in production.")
    if is_production and not HAS_FLASK_LIMITER:
        raise RuntimeError("Flask-Limiter dependency is required in production.")
    if not secret_key:
        secret_key = secrets.token_urlsafe(32)
    if not database_uri:
        database_uri = f"sqlite:///{os.path.join(BASE_DIR, 'intranet.db')}"

    try:
        db_backend = make_url(database_uri).get_backend_name()
    except Exception as exc:
        raise RuntimeError("DATABASE_URL is invalid or cannot be parsed.") from exc
    if is_production and db_backend != "postgresql":
        raise RuntimeError("PostgreSQL is required in production. Set DATABASE_URL=postgresql+psycopg://...")

    upload_dir = os.environ.get("UPLOAD_DIR", UPLOAD_DIR)
    os.makedirs(upload_dir, exist_ok=True)
    max_upload_bytes = env_int("MAX_UPLOAD_BYTES", 50 * 1024 * 1024)
    session_ttl_minutes = env_int("SESSION_TTL_MINUTES", 8 * 60)
    session_touch_interval_seconds = env_int("SESSION_TOUCH_INTERVAL_SECONDS", 60)
    trust_proxy = env_bool("TRUST_PROXY", False)
    force_secure_cookie = env_bool("FORCE_SECURE_COOKIE", False)
    data_region = (os.environ.get("DATA_REGION") or "ZA").strip().upper() or "ZA"
    enable_schema_sync = env_bool("ENABLE_SCHEMA_COMPAT_SYNC", not is_production)
    allow_in_memory_ratelimit = env_bool("ALLOW_IN_MEMORY_RATELIMIT", False)
    trusted_proxy_hops = max(1, env_int("TRUSTED_PROXY_HOPS", 1))
    secure_cookie = is_production or force_secure_cookie
    timer_single_cap_minutes = max(5, env_int("TIMER_SINGLE_CAP_MINUTES", 4 * 60))
    timer_idle_prompt_seconds = max(5 * 60, env_int("TIMER_IDLE_PROMPT_SECONDS", 45 * 60))
    timer_idle_grace_seconds = max(30, env_int("TIMER_IDLE_GRACE_SECONDS", 60))

    app.config.update(
        SECRET_KEY=secret_key,
        SQLALCHEMY_DATABASE_URI=database_uri,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True},
        MAX_CONTENT_LENGTH=max_upload_bytes,
        UPLOAD_DIR=upload_dir,
        DATA_REGION=data_region,
        BACKUP_ENCRYPTION_KEY=backup_encryption_key or None,
        SESSION_TTL_MINUTES=session_ttl_minutes,
        SESSION_TOUCH_INTERVAL_SECONDS=max(1, session_touch_interval_seconds),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=secure_cookie,
        REMEMBER_COOKIE_HTTPONLY=True,
        REMEMBER_COOKIE_SAMESITE="Lax",
        REMEMBER_COOKIE_SECURE=secure_cookie,
        PERMANENT_SESSION_LIFETIME=dt.timedelta(minutes=session_ttl_minutes),
        PREFERRED_URL_SCHEME="https" if secure_cookie else "http",
        RATELIMIT_STORAGE_URI=rate_limit_storage_uri,
        RATELIMIT_STRATEGY=rate_limit_strategy,
        RATELIMIT_HEADERS_ENABLED=True,
        AUTH_LOGIN_RATE_LIMIT=auth_login_rate_limit,
        AUTH_REGISTER_RATE_LIMIT=auth_register_rate_limit,
        PORTAL_LOGIN_RATE_LIMIT=portal_login_rate_limit,
        AUTH_SSO_TOKEN_RATE_LIMIT=sso_token_rate_limit,
        TIMER_SINGLE_CAP_MINUTES=timer_single_cap_minutes,
        TIMER_IDLE_PROMPT_SECONDS=timer_idle_prompt_seconds,
        TIMER_IDLE_GRACE_SECONDS=timer_idle_grace_seconds,
        UFC_STRICT_INIT=env_bool("UFC_STRICT_INIT", False),
    )

    if db_backend != "sqlite":
        app.config["SQLALCHEMY_ENGINE_OPTIONS"]["pool_recycle"] = env_int("DB_POOL_RECYCLE_SECONDS", 1800)
        app.config["SQLALCHEMY_ENGINE_OPTIONS"]["pool_size"] = max(1, env_int("DB_POOL_SIZE", 5))
        app.config["SQLALCHEMY_ENGINE_OPTIONS"]["max_overflow"] = max(0, env_int("DB_MAX_OVERFLOW", 10))
        app.config["SQLALCHEMY_ENGINE_OPTIONS"]["pool_timeout"] = max(1, env_int("DB_POOL_TIMEOUT_SECONDS", 30))

    if trust_proxy:
        # Trust the configured number of proxy hops for client IP and proto handling.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=trusted_proxy_hops, x_proto=1, x_host=1)

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    app.logger.setLevel(getattr(logging, log_level, logging.INFO))
    configured_workers = max(1, env_int("GUNICORN_WORKERS", 1))
    if is_production and rate_limit_storage_uri == "memory://" and configured_workers > 1 and not allow_in_memory_ratelimit:
        raise RuntimeError(
            "RATE_LIMIT_STORAGE_URI=memory:// is unsafe with multiple workers. "
            "Use Redis or set ALLOW_IN_MEMORY_RATELIMIT=true to bypass."
        )
    if is_production and rate_limit_storage_uri == "memory://" and configured_workers > 1:
        app.logger.warning(
            "RATE_LIMIT_STORAGE_URI=memory:// with %s workers enables per-process limits. "
            "Use Redis for consistent rate limiting.",
            configured_workers,
        )
    if is_production and enable_schema_sync:
        app.logger.warning(
            "ENABLE_SCHEMA_COMPAT_SYNC=true in production. Prefer explicit migration runs for controlled schema changes."
        )
    app.config["IS_PRODUCTION"] = is_production

    @app.before_request
    def _bind_db_access_context():
        # Bind request-scoped identity context for PostgreSQL RLS policies.
        apply_request_db_context()

    @app.before_request
    def _capture_active_matter_context():
        if not current_user.is_authenticated:
            session.pop(ACTIVE_MATTER_SESSION_KEY, None)
            return

        candidates: list[int] = []
        view_args = request.view_args or {}
        for key in ("matter_id",):
            if key in view_args:
                try:
                    candidates.append(int(view_args.get(key)))
                except (TypeError, ValueError):
                    continue
        for key in ("matter_id", "matter_id_select"):
            if key not in request.values:
                continue
            try:
                candidates.append(int(request.values.get(key)))
            except (TypeError, ValueError):
                continue

        for matter_id in candidates:
            if set_active_matter_context(matter_id):
                break

    @app.context_processor
    def inject_ui_state():
        return {
            "active_matter": resolve_active_matter(),
        }

    db.init_app(app)
    migrate.init_app(app, db, directory=os.path.join(BASE_DIR, "migrations"))
    limiter.init_app(app)

    login_manager.init_app(app)
    login_manager.login_view = "login"
    login_manager.session_protection = "strong"

    register_csrf_protection(app)
    register_security_handlers(app)
    register_routes(app)

    run_schema_sync = enable_schema_sync and not _is_migration_cli_invocation()
    if enable_schema_sync and not run_schema_sync:
        app.logger.info("Skipping schema compatibility sync during migration CLI invocation.")
    if run_schema_sync:
        # Additive schema safety-net for environments without migration tooling.
        with app.app_context():
            sync_schema_compatibility()

    return app
