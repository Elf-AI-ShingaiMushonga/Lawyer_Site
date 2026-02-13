from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import secrets

from flask import g, request, session
from flask_login import current_user

from .config import ALLOWED_AUDIO_EXT, ALLOWED_DOC_EXT
from .extensions import db
from .models import AuditLog, MatterActivity, MatterMember, TrustedDevice, UserSession


def is_admin() -> bool:
    return getattr(current_user, "role", None) == "admin"


def can_access_matter(matter_id: int) -> bool:
    """Admins access everything. Others must be in matter team."""
    try:
        from .policies import evaluate_matter_access

        decision = evaluate_matter_access(matter_id)
        if not decision.allow and current_user.is_authenticated:
            denied_seen = getattr(g, "_denied_matter_access_seen", set())
            signature = (int(matter_id), decision.deny_reason or "unknown")
            if signature not in denied_seen:
                denied_seen.add(signature)
                g._denied_matter_access_seen = denied_seen
                audit(
                    "matter_access_denied",
                    "Matter",
                    matter_id,
                    {
                        "reason": decision.deny_reason,
                        "ethical_wall_hit": bool(decision.ethical_wall_hit),
                    },
                )
        return decision.allow
    except Exception:
        # Fail closed on policy evaluation errors so ethical-wall denies
        # and scoped access rules cannot be bypassed.
        if is_admin():
            return True
        return False


def has_active_legal_hold(matter_id: int | None) -> bool:
    if not matter_id:
        return False
    from .models import LegalHold

    return (
        db.session.query(LegalHold.id)
        .filter(LegalHold.matter_id == int(matter_id), LegalHold.is_active.is_(True))
        .first()
        is not None
    )


def audit(action: str, entity_type: str | None = None, entity_id: int | None = None, details: dict | None = None):
    try:
        entry = AuditLog(
            actor_user_id=current_user.id if current_user.is_authenticated else None,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            ip=request.remote_addr,
            user_agent=(request.headers.get("User-Agent") or "")[:255],
            details_json=json.dumps(details or {}, ensure_ascii=False),
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        # Never break user flow for audit failure
        db.session.rollback()


def matter_activity(matter_id: int, action: str, details: str | None = None):
    try:
        entry = MatterActivity(
            matter_id=matter_id,
            actor_user_id=current_user.id if current_user.is_authenticated else None,
            action=action,
            details=details,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        # Never break user flow for activity feed failure
        db.session.rollback()


def normalize_query(q: str) -> str:
    return re.sub(r"\s+", " ", (q or "").strip())


def allowed_doc(filename: str) -> bool:
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_DOC_EXT


def allowed_audio(filename: str) -> bool:
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_AUDIO_EXT


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _session_token() -> str:
    token = session.get("_session_token")
    if token:
        return token
    token = secrets.token_urlsafe(24)
    session["_session_token"] = token
    return token


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def register_user_session(user_id: int, ttl_minutes: int = 8 * 60, *, rotate_token: bool = True) -> int | None:
    try:
        if rotate_token or not session.get("_session_token"):
            session["_session_token"] = secrets.token_urlsafe(24)
        token_hash = _token_hash(_session_token())
        now = dt.datetime.utcnow()
        row = UserSession.query.filter_by(session_token_hash=token_hash).first()
        if row is None:
            row = UserSession(
                user_id=user_id,
                session_token_hash=token_hash,
                ip=request.remote_addr,
                user_agent=(request.headers.get("User-Agent") or "")[:255],
                created_at=now,
                last_seen_at=now,
                expires_at=now + dt.timedelta(minutes=ttl_minutes),
            )
            db.session.add(row)
        else:
            row.user_id = user_id
            row.ip = request.remote_addr
            row.user_agent = (request.headers.get("User-Agent") or "")[:255]
            row.last_seen_at = now
            row.expires_at = now + dt.timedelta(minutes=ttl_minutes)
            row.revoked_at = None
        db.session.commit()
        return row.id
    except Exception:
        db.session.rollback()
        return None


def touch_user_session() -> None:
    if not current_user.is_authenticated:
        return
    try:
        token_hash = _token_hash(_session_token())
        row = UserSession.query.filter_by(session_token_hash=token_hash, user_id=current_user.id).first()
        if row is None:
            return
        row.last_seen_at = dt.datetime.utcnow()
        db.session.commit()
    except Exception:
        db.session.rollback()


def validate_user_session(
    ttl_minutes: int = 8 * 60,
    touch_interval_seconds: int = 60,
) -> tuple[bool, str | None]:
    """Validate current authenticated session token row, applying idle-time refresh."""
    return _validate_user_session(
        ttl_minutes=ttl_minutes,
        touch_interval_seconds=touch_interval_seconds,
    )


def _validate_user_session(
    *,
    ttl_minutes: int = 8 * 60,
    touch_interval_seconds: int = 60,
) -> tuple[bool, str | None]:
    """Validate current authenticated session row with throttled persistence writes."""
    if not current_user.is_authenticated:
        return True, None

    now = dt.datetime.utcnow()
    token = session.get("_session_token")
    if not token:
        sid = register_user_session(current_user.id, ttl_minutes=ttl_minutes, rotate_token=False)
        return (sid is not None), ("missing" if sid is None else None)

    try:
        token_hash = _token_hash(token)
        row = UserSession.query.filter_by(session_token_hash=token_hash, user_id=current_user.id).first()
        if row is None:
            sid = register_user_session(current_user.id, ttl_minutes=ttl_minutes, rotate_token=False)
            return (sid is not None), ("missing" if sid is None else None)
        if row.revoked_at is not None:
            return False, "revoked"
        if row.expires_at <= now:
            return False, "expired"

        ttl_seconds = max(60, int(ttl_minutes) * 60)
        touch_seconds = max(1, min(int(touch_interval_seconds or 60), max(1, ttl_seconds // 4)))
        last_seen_at = row.last_seen_at or now
        expires_at = row.expires_at or now
        needs_touch = (
            (now - last_seen_at).total_seconds() >= touch_seconds
            or (expires_at - now).total_seconds() <= touch_seconds
        )
        if needs_touch:
            row.last_seen_at = now
            row.expires_at = now + dt.timedelta(minutes=max(1, int(ttl_minutes)))
            db.session.commit()
        return True, None
    except Exception:
        db.session.rollback()
        return False, "error"


def revoke_user_session(session_id: int) -> bool:
    if not current_user.is_authenticated:
        return False
    row = UserSession.query.filter_by(id=session_id, user_id=current_user.id).first()
    if row is None:
        return False
    row.revoked_at = dt.datetime.utcnow()
    db.session.commit()
    return True


def revoke_current_session() -> bool:
    if not current_user.is_authenticated:
        return False
    token = session.get("_session_token")
    if not token:
        return False
    row = UserSession.query.filter_by(session_token_hash=_token_hash(token), user_id=current_user.id).first()
    if row is None:
        return False
    row.revoked_at = dt.datetime.utcnow()
    db.session.commit()
    return True


def register_trusted_device(user_id: int) -> int | None:
    """Upsert trusted device record for the current request fingerprint."""
    user_agent = (request.headers.get("User-Agent") or "").strip()
    remote_ip = (request.remote_addr or "").strip()
    if not user_agent and not remote_ip:
        return None

    fingerprint_raw = f"{user_agent}|{remote_ip}"
    fingerprint_hash = hashlib.sha256(fingerprint_raw.encode("utf-8")).hexdigest()
    device_name = user_agent[:200] or f"IP {remote_ip or 'unknown'}"
    now = dt.datetime.utcnow()

    try:
        row = TrustedDevice.query.filter_by(user_id=user_id, fingerprint_hash=fingerprint_hash).first()
        if row is None:
            row = TrustedDevice(
                user_id=user_id,
                device_name=device_name,
                fingerprint_hash=fingerprint_hash,
                created_at=now,
                last_seen_at=now,
                is_active=True,
            )
            db.session.add(row)
        else:
            row.device_name = device_name
            row.last_seen_at = now
            row.is_active = True
        db.session.commit()
        return row.id
    except Exception:
        db.session.rollback()
        return None


def revoke_trusted_device(device_id: int, user_id: int) -> bool:
    row = TrustedDevice.query.filter_by(id=device_id, user_id=user_id).first()
    if row is None:
        return False
    row.is_active = False
    db.session.commit()
    return True
