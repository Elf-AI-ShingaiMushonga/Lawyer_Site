from __future__ import annotations

import hashlib
import json
import re

from flask import request
from flask_login import current_user

from .config import ALLOWED_DOC_EXT
from .extensions import db
from .models import AuditLog, MatterActivity, MatterMember


def is_admin() -> bool:
    return getattr(current_user, "role", None) == "admin"


def can_access_matter(matter_id: int) -> bool:
    """Admins access everything. Others must be in matter team."""
    if is_admin():
        return True
    if not current_user.is_authenticated:
        return False
    return (
        db.session.query(MatterMember)
        .filter(MatterMember.matter_id == matter_id, MatterMember.user_id == current_user.id)
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


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
