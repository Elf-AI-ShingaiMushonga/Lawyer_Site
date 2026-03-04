from __future__ import annotations

import os
import re

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_DOC_EXT = {"pdf", "docx", "xlsx", "pptx", "txt", "png", "jpg", "jpeg", "eml", "msg"}
ALLOWED_AUDIO_EXT = {"m4a", "mp3", "wav", "ogg", "webm"}
MATTER_STATUSES = {"Open", "On Hold", "Closed"}
RISK_LEVELS = ("Low", "Medium", "High", "Critical")
BUDGET_STATUSES = ("On Track", "Watch", "Over Budget", "Needs Review")
ROLE_OPTIONS = (
    ("director", "Directors"),
    ("senior_attorney", "Senior Attorneys"),
    ("junior_attorney", "Junior Attorneys"),
    ("candidate_attorney", "Candidate Attorneys"),
    ("operations_staff", "Operations Staff"),
    ("finance_cost_admin", "Finance & Cost and Admin"),
)
LEGACY_COMPAT_ROLES = {
    "admin",
    "lawyer",
    "staff",
    "paralegal",
    "partner",
    "associate",
    "directors",
    "finance and cost and admin",
    "finance_and_cost_and_admin",
}
VALID_ROLES = {value for value, _label in ROLE_OPTIONS}.union(LEGACY_COMPAT_ROLES)
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
