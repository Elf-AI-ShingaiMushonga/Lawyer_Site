from __future__ import annotations

from functools import wraps

from sqlalchemy import and_

from flask import abort
from flask_login import current_user

from ..extensions import db
from ..helpers import is_admin
from ..roles import canonical_role
from ..types import AccessDecision

ROLE_ALIASES = {
    "director": "admin",
    "finance_cost_admin": "admin",
    "senior_attorney": "lawyer",
    "junior_attorney": "lawyer",
    "candidate_attorney": "paralegal",
    "operations_staff": "staff",
    "partner": "lawyer",
    "associate": "lawyer",
    "admin": "admin",
    "lawyer": "lawyer",
    "paralegal": "paralegal",
    "staff": "staff",
}

# Default route-level permissions. PermissionGrant rows can override these.
DEFAULT_ROLE_PERMISSIONS: dict[str, dict[str, set[str]]] = {
    "lawyer": {
        "matter": {"create"},
        "matter_team": {"manage"},
        "time_entry": {"review", "lock"},
        "crm": {"read", "write", "conflict_check", "override", "export", "sign_engagement"},
        "billing": {"generate", "approve", "adjust", "capture_payment", "settle_payment", "report", "audit"},
    },
    "paralegal": {
        "matter": {"create"},
        "crm": {"read", "write", "conflict_check"},
    },
    "staff": {
        "crm": {"read"},
    },
}


def _models():
    from ..models import EthicalWallMatter, EthicalWallRule, Matter, MatterMember, PermissionGrant

    return EthicalWallMatter, EthicalWallRule, Matter, MatterMember, PermissionGrant


def _normalized_role() -> str:
    canonical = canonical_role(getattr(current_user, "role", ""))
    return ROLE_ALIASES.get(canonical, canonical)


def _default_permission_allows(role: str, resource: str, action: str) -> bool:
    role_matrix = DEFAULT_ROLE_PERMISSIONS.get(role, {})
    direct_actions = role_matrix.get(resource, set())
    if "*" in direct_actions or action in direct_actions:
        return True
    wildcard_actions = role_matrix.get("*", set())
    return "*" in wildcard_actions or action in wildcard_actions


def visible_matter_ids() -> list[int]:
    if not current_user.is_authenticated:
        return []
    if is_admin():
        _, _, Matter, _, _ = _models()
        return [row[0] for row in db.session.query(Matter.id).order_by(Matter.id.asc()).all()]

    EthicalWallMatter, EthicalWallRule, _, MatterMember, _ = _models()
    rows = (
        db.session.query(MatterMember.matter_id)
        .outerjoin(EthicalWallMatter, EthicalWallMatter.matter_id == MatterMember.matter_id)
        .outerjoin(
            EthicalWallRule,
            and_(
                EthicalWallRule.wall_id == EthicalWallMatter.wall_id,
                EthicalWallRule.user_id == current_user.id,
                EthicalWallRule.is_deny.is_(True),
                EthicalWallRule.is_active.is_(True),
            ),
        )
        .filter(
            MatterMember.user_id == current_user.id,
            EthicalWallRule.id.is_(None),
        )
        .distinct()
        .order_by(MatterMember.matter_id.asc())
        .all()
    )
    return [int(row[0]) for row in rows]


def _ethical_wall_hit(matter_id: int) -> bool:
    if not current_user.is_authenticated or is_admin():
        return False

    EthicalWallMatter, EthicalWallRule, _, _, _ = _models()
    return (
        db.session.query(EthicalWallRule.id)
        .join(EthicalWallMatter, EthicalWallMatter.wall_id == EthicalWallRule.wall_id)
        .filter(
            EthicalWallMatter.matter_id == matter_id,
            EthicalWallRule.user_id == current_user.id,
            EthicalWallRule.is_deny.is_(True),
            EthicalWallRule.is_active.is_(True),
        )
        .first()
        is not None
    )


def evaluate_matter_access(matter_id: int) -> AccessDecision:
    if not current_user.is_authenticated:
        return AccessDecision(allow=False, deny_reason="not_authenticated")
    if is_admin():
        return AccessDecision(allow=True, scope_ids=[matter_id])

    _, _, _, MatterMember, _ = _models()
    membership = (
        db.session.query(MatterMember.id)
        .filter(MatterMember.matter_id == matter_id, MatterMember.user_id == current_user.id)
        .first()
    )
    if membership is None:
        return AccessDecision(allow=False, deny_reason="not_on_matter_team")

    if _ethical_wall_hit(matter_id):
        return AccessDecision(
            allow=False,
            deny_reason="ethical_wall_deny",
            ethical_wall_hit=True,
        )

    return AccessDecision(allow=True, scope_ids=[matter_id])


def has_permission(resource: str, action: str) -> bool:
    if not current_user.is_authenticated:
        return False
    if is_admin():
        return True

    normalized_role = _normalized_role()
    raw_role = str(getattr(current_user, "role", "") or "").strip().lower()
    roles = {normalized_role}
    if raw_role:
        roles.add(raw_role)

    _, _, _, _, PermissionGrant = _models()
    grant_rows = (
        PermissionGrant.query.filter(
            PermissionGrant.role.in_(roles),
            PermissionGrant.resource.in_([resource, "*"]),
            PermissionGrant.action.in_([action, "*"]),
        )
        .order_by(PermissionGrant.id.desc())
        .all()
    )
    best_grant = None
    best_score = -1
    for row in grant_rows:
        score = 0
        if row.resource == resource:
            score += 2
        if row.action == action:
            score += 1
        if score > best_score:
            best_score = score
            best_grant = row
    if best_grant is not None:
        return bool(best_grant.is_allowed)
    return _default_permission_allows(normalized_role, resource, action)


def enforce_matter_access(matter_id: int) -> None:
    decision = evaluate_matter_access(matter_id)
    if not decision.allow:
        abort(403)


def enforce_permission(resource: str, action: str) -> None:
    if not has_permission(resource, action):
        abort(403)


def permission_required(resource: str, action: str):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            enforce_permission(resource, action)
            return view(*args, **kwargs)

        return wrapped

    return decorator
