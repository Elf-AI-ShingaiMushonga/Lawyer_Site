from __future__ import annotations

from flask_login import current_user
from sqlalchemy import text

from .extensions import db


def _is_postgres() -> bool:
    try:
        bind = db.session.get_bind()
    except Exception:
        return False
    return bool(bind is not None and bind.dialect.name == "postgresql")


def set_db_access_context(
    *,
    user_id: int | None = None,
    role: str | None = None,
    is_admin: bool = False,
    service_account: bool = False,
) -> None:
    if not _is_postgres():
        return
    db.session.execute(
        text("SELECT set_config('app.current_user_id', :value, false)"),
        {"value": str(user_id or "")},
    )
    db.session.execute(
        text("SELECT set_config('app.user_role', :value, false)"),
        {"value": str(role or "")},
    )
    db.session.execute(
        text("SELECT set_config('app.is_admin', :value, false)"),
        {"value": "true" if is_admin else "false"},
    )
    db.session.execute(
        text("SELECT set_config('app.service_account', :value, false)"),
        {"value": "true" if service_account else "false"},
    )


def apply_request_db_context() -> None:
    try:
        authenticated = bool(current_user.is_authenticated)
    except Exception:
        authenticated = False

    if not authenticated:
        set_db_access_context(user_id=None, role=None, is_admin=False, service_account=False)
        return

    role = str(getattr(current_user, "role", "") or "")
    set_db_access_context(
        user_id=int(current_user.id),
        role=role,
        is_admin=(role == "admin"),
        service_account=False,
    )
