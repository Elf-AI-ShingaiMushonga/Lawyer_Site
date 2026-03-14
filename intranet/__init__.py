from __future__ import annotations

import base64
import binascii
import datetime as dt
from .timeutils import utc_now
import importlib.util
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
from .models import Matter, TimeTimer, User
from .routes import register_routes
from .roles import (
    canonical_role,
    role_can_access_finance,
    role_display_name,
    role_is_admin,
    role_is_case,
    role_is_director,
    role_is_lawyer,
    role_is_support,
)
from .schema_sync import sync_schema_compatibility
from .security import register_security_handlers
from .services.workspace_hub import build_workspace_quick_actions, load_user_workspace_mode, workspace_mode_meta

HEALTH_ENDPOINTS = {"healthz", "readyz"}


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


def _validate_backup_encryption_key(raw_value: str) -> str:
    value = str(raw_value or "").strip()
    if not value:
        return ""
    if len(value) == 64:
        try:
            bytes.fromhex(value)
            return value
        except ValueError:
            pass
    padded = value + ("=" * (-len(value) % 4))
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("utf-8"))
    except (ValueError, binascii.Error) as exc:
        raise RuntimeError(
            "BACKUP_ENCRYPTION_KEY is invalid. Use 32-byte URL-safe base64 or 64-char hex."
        ) from exc
    if len(decoded) != 32:
        raise RuntimeError("BACKUP_ENCRYPTION_KEY must decode to exactly 32 bytes for AES-256.")
    return value


def create_app() -> Flask:
    app = Flask(__name__)

    app_env = os.environ.get("FLASK_ENV", "development").strip().lower()
    is_production = app_env in PRODUCTION_ENV_VALUES
    secret_key = os.environ.get("FLASK_SECRET_KEY")
    database_uri = os.environ.get("DATABASE_URL")
    backup_encryption_key = _validate_backup_encryption_key(os.environ.get("BACKUP_ENCRYPTION_KEY") or "")
    rate_limit_storage_uri = os.environ.get("RATE_LIMIT_STORAGE_URI", "memory://")
    rate_limit_strategy = os.environ.get("RATE_LIMIT_STRATEGY", "fixed-window")
    auth_login_rate_limit = os.environ.get("AUTH_LOGIN_RATE_LIMIT", "10/minute")
    auth_register_rate_limit = os.environ.get("AUTH_REGISTER_RATE_LIMIT", "5/hour")
    portal_login_rate_limit = os.environ.get("PORTAL_LOGIN_RATE_LIMIT", "10/minute")
    sso_token_rate_limit = os.environ.get("AUTH_SSO_TOKEN_RATE_LIMIT", "60/minute")
    ai_enabled = env_bool("AI_ENABLED", False)
    ai_provider = (os.environ.get("AI_PROVIDER") or "openai").strip().lower() or "openai"
    ai_semantic_search_enabled = env_bool("AI_SEMANTIC_SEARCH_ENABLED", ai_enabled)
    ai_openai_api_key = (os.environ.get("AI_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip() or None
    ai_openai_embed_model = (os.environ.get("AI_OPENAI_EMBED_MODEL") or "text-embedding-3-small").strip()
    ai_openai_text_model = (os.environ.get("AI_OPENAI_TEXT_MODEL") or "gpt-4o-mini").strip()
    ai_openai_embed_dimensions = max(0, env_int("AI_OPENAI_EMBED_DIMENSIONS", 1024))
    ai_openai_timeout_seconds = max(1, env_int("AI_OPENAI_TIMEOUT_SECONDS", 20))
    ai_openai_max_retries = max(0, env_int("AI_OPENAI_MAX_RETRIES", 2))
    ai_fallback_embedding_dimensions = max(32, env_int("AI_FALLBACK_EMBED_DIMENSIONS", 256))
    ai_embed_strict = env_bool("AI_EMBED_STRICT", False)
    ai_redact_before_embedding = env_bool("AI_REDACT_BEFORE_EMBEDDING", True)
    ai_operation_logging = env_bool("AI_OPERATION_LOGGING", True)
    ai_semantic_candidate_limit = max(50, env_int("AI_SEMANTIC_CANDIDATE_LIMIT", 600))

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
    if is_production and ai_enabled and ai_provider == "openai" and not ai_openai_api_key:
        raise RuntimeError(
            "OPENAI_API_KEY or AI_OPENAI_API_KEY must be set in production when AI_ENABLED=true and AI_PROVIDER=openai."
        )
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

    upload_dir = str(os.environ.get("UPLOAD_DIR", UPLOAD_DIR) or "").strip() or UPLOAD_DIR
    if not os.path.isabs(upload_dir):
        upload_dir = os.path.join(BASE_DIR, upload_dir)
    upload_dir = os.path.abspath(upload_dir)
    os.makedirs(upload_dir, mode=0o750, exist_ok=True)
    max_upload_bytes = env_int("MAX_UPLOAD_BYTES", 50 * 1024 * 1024)
    if max_upload_bytes <= 0:
        raise RuntimeError("MAX_UPLOAD_BYTES must be a positive integer.")
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
        AI_ENABLED=ai_enabled,
        AI_PROVIDER=ai_provider,
        AI_SEMANTIC_SEARCH_ENABLED=ai_semantic_search_enabled,
        AI_OPENAI_API_KEY=ai_openai_api_key,
        AI_OPENAI_EMBED_MODEL=ai_openai_embed_model,
        AI_OPENAI_TEXT_MODEL=ai_openai_text_model,
        AI_OPENAI_EMBED_DIMENSIONS=ai_openai_embed_dimensions,
        AI_OPENAI_TIMEOUT_SECONDS=ai_openai_timeout_seconds,
        AI_OPENAI_MAX_RETRIES=ai_openai_max_retries,
        AI_FALLBACK_EMBED_DIMENSIONS=ai_fallback_embedding_dimensions,
        AI_EMBED_STRICT=ai_embed_strict,
        AI_REDACT_BEFORE_EMBEDDING=ai_redact_before_embedding,
        AI_OPERATION_LOGGING=ai_operation_logging,
        AI_SEMANTIC_CANDIDATE_LIMIT=ai_semantic_candidate_limit,
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
        endpoint = request.endpoint or ""
        if endpoint in HEALTH_ENDPOINTS:
            return
        # Bind request-scoped identity context for PostgreSQL RLS policies.
        apply_request_db_context()

    @app.before_request
    def _capture_active_matter_context():
        endpoint = request.endpoint or ""
        if endpoint in HEALTH_ENDPOINTS:
            return
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
        active_matter = resolve_active_matter()
        active_timer_cue = None
        workspace_command_actions: list[dict[str, str]] = []
        workspace_command_mode = "practice"
        workspace_command_meta = workspace_mode_meta(workspace_command_mode)
        raw_role = ""
        role_value = ""
        role_label = ""
        role_slug = ""
        is_admin_role = False
        is_director_role = False
        is_lawyer_role = False
        is_case_role = False
        is_support_role = False
        can_access_finance = False
        if current_user.is_authenticated:
            raw_role = str(getattr(current_user, "role", "") or "")
            role_value = canonical_role(raw_role) or raw_role
            role_label = role_display_name(role_value) or role_value
            role_slug = role_value.lower().replace(" ", "-").replace("_", "-")
            is_admin_role = role_is_admin(raw_role)
            is_director_role = role_is_director(raw_role)
            is_lawyer_role = role_is_lawyer(raw_role)
            is_case_role = role_is_case(raw_role)
            is_support_role = role_is_support(raw_role)
            can_access_finance = role_can_access_finance(raw_role)
            workspace_command_mode = load_user_workspace_mode(current_user.id, raw_role)
            workspace_command_meta = workspace_mode_meta(workspace_command_mode)
            workspace_command_actions = build_workspace_quick_actions(
                raw_role,
                workspace_command_mode,
                active_matter=active_matter,
            )
        ai_status = {
            "available": False,
            "label": "AI Off",
            "tone": "tone-neutral",
            "tooltip": "AI features are disabled.",
        }
        ai_enabled = bool(app.config.get("AI_ENABLED", False))
        ai_provider = str(app.config.get("AI_PROVIDER") or "openai").strip().lower()
        ai_openai_key = (app.config.get("AI_OPENAI_API_KEY") or "").strip()
        if ai_enabled:
            if ai_provider != "openai":
                ai_status = {
                    "available": False,
                    "label": "AI Unavailable",
                    "tone": "tone-warning",
                    "tooltip": f"Unsupported AI provider configured: {ai_provider or 'unknown'}.",
                }
            elif not ai_openai_key:
                ai_status = {
                    "available": False,
                    "label": "AI Key Missing",
                    "tone": "tone-warning",
                    "tooltip": "OpenAI API key is not configured.",
                }
            elif importlib.util.find_spec("openai") is None:
                ai_status = {
                    "available": False,
                    "label": "AI SDK Missing",
                    "tone": "tone-warning",
                    "tooltip": "OpenAI SDK package is not installed.",
                }
            else:
                model_name = str(app.config.get("AI_OPENAI_TEXT_MODEL") or "gpt-4o-mini").strip()
                ai_status = {
                    "available": True,
                    "label": "AI Available",
                    "tone": "tone-positive",
                    "tooltip": f"OpenAI is configured and available (model: {model_name}).",
                }
        if current_user.is_authenticated:
            running_timer = (
                TimeTimer.query.filter_by(user_id=current_user.id, status="running")
                .order_by(TimeTimer.started_at.desc(), TimeTimer.id.desc())
                .first()
            )
            if running_timer is not None:
                seed_elapsed_seconds = max(0, int(running_timer.elapsed_seconds or 0))
                started_at = running_timer.started_at
                now_utc = utc_now()
                total_elapsed_seconds = seed_elapsed_seconds
                started_at_iso = ""
                if started_at:
                    total_elapsed_seconds += max(0, int((now_utc - started_at).total_seconds()))
                    started_at_iso = started_at.replace(microsecond=0).isoformat() + "Z"

                elapsed_hours, elapsed_remainder = divmod(total_elapsed_seconds, 3600)
                elapsed_minutes, elapsed_seconds = divmod(elapsed_remainder, 60)

                timer_matter = None
                timer_matter_id = int(running_timer.matter_id) if running_timer.matter_id else None
                if timer_matter_id and active_matter and int(active_matter.id) == timer_matter_id:
                    timer_matter = active_matter
                elif timer_matter_id:
                    timer_matter = db.session.get(Matter, timer_matter_id)

                active_timer_cue = {
                    "timer_id": int(running_timer.id),
                    "matter_id": timer_matter_id,
                    "matter_no": timer_matter.matter_no if timer_matter is not None else "",
                    "label": (running_timer.label or "").strip(),
                    "elapsed_seed_seconds": seed_elapsed_seconds,
                    "elapsed_display": f"{elapsed_hours:02d}:{elapsed_minutes:02d}:{elapsed_seconds:02d}",
                    "started_at_iso": started_at_iso,
                }

        return {
            "active_matter": active_matter,
            "active_timer_cue": active_timer_cue,
            "ai_status": ai_status,
            "current_user_role": role_value,
            "current_user_role_label": role_label,
            "current_user_role_slug": role_slug,
            "is_admin_role": is_admin_role,
            "is_director_role": is_director_role,
            "is_lawyer_role": is_lawyer_role,
            "is_case_role": is_case_role,
            "is_support_role": is_support_role,
            "can_access_finance": can_access_finance,
            "role_display_name": role_display_name,
            "canonical_role": canonical_role,
            "workspace_command_actions": workspace_command_actions,
            "workspace_command_mode": workspace_command_mode,
            "workspace_command_meta": workspace_command_meta,
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
