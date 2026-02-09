from __future__ import annotations

import os
import re

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_DOC_EXT = {"pdf", "docx", "xlsx", "pptx", "txt", "png", "jpg", "jpeg", "eml", "msg"}
MATTER_STATUSES = {"Open", "On Hold", "Closed"}
VALID_ROLES = {"admin", "lawyer", "staff", "paralegal"}
PRODUCTION_ENV_VALUES = {"prod", "production"}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def is_valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email))
