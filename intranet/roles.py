from __future__ import annotations

import re

from .config import ROLE_OPTIONS

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

ROLE_LABELS = {value: label for value, label in ROLE_OPTIONS}

_ROLE_ALIASES = {
    # legacy/compat
    "admin": "finance_cost_admin",
    "lawyer": "senior_attorney",
    "partner": "senior_attorney",
    "associate": "junior_attorney",
    "paralegal": "candidate_attorney",
    "staff": "operations_staff",
    # new taxonomy + normalization variants
    "director": "director",
    "directors": "director",
    "senior_attorney": "senior_attorney",
    "senior_attorneys": "senior_attorney",
    "junior_attorney": "junior_attorney",
    "junior_attorneys": "junior_attorney",
    "candidate_attorney": "candidate_attorney",
    "candidate_attorneys": "candidate_attorney",
    "operations_staff": "operations_staff",
    "finance_cost_admin": "finance_cost_admin",
    "finance_and_cost_and_admin": "finance_cost_admin",
    "finance_cost_and_admin": "finance_cost_admin",
}

_ROLE_GROUPS = {
    "admin": {"director", "finance_cost_admin"},
    "lawyer": {"director", "senior_attorney", "junior_attorney"},
    "paralegal": {"candidate_attorney"},
    "staff": {"operations_staff", "finance_cost_admin"},
}


def _slugify_role(value: str | None) -> str:
    raw = str(value or "").strip().lower().replace("&", " and ")
    slug = _NON_ALNUM_RE.sub("_", raw).strip("_")
    return slug


def canonical_role(value: str | None) -> str:
    slug = _slugify_role(value)
    if not slug:
        return ""
    return _ROLE_ALIASES.get(slug, slug)


def role_display_name(value: str | None) -> str:
    canonical = canonical_role(value)
    if canonical in ROLE_LABELS:
        return ROLE_LABELS[canonical]
    if canonical:
        return canonical.replace("_", " ").title()
    return ""


def _in_group(value: str | None, group_name: str) -> bool:
    canonical = canonical_role(value)
    return canonical in _ROLE_GROUPS.get(group_name, set())


def role_is_admin(value: str | None) -> bool:
    return _in_group(value, "admin")


def role_is_director(value: str | None) -> bool:
    return canonical_role(value) == "director"


def role_is_lawyer(value: str | None) -> bool:
    return role_is_admin(value) or _in_group(value, "lawyer")


def role_is_case(value: str | None) -> bool:
    return role_is_lawyer(value) or _in_group(value, "paralegal")


def role_is_support(value: str | None) -> bool:
    return _in_group(value, "paralegal") or _in_group(value, "staff")


def role_requires_mfa(value: str | None) -> bool:
    canonical = canonical_role(value)
    return role_is_admin(canonical) or canonical == "operations_staff"


def role_can_access_finance(value: str | None) -> bool:
    return role_is_lawyer(value) or canonical_role(value) == "finance_cost_admin"


def role_query_values_for_legal_team() -> set[str]:
    # Values currently persisted in DB + compatibility variants.
    return {
        "director",
        "directors",
        "senior_attorney",
        "junior_attorney",
        "candidate_attorney",
        "lawyer",
        "partner",
        "associate",
        "paralegal",
    }
