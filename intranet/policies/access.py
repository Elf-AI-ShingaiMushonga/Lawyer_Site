from __future__ import annotations

from functools import wraps

from sqlalchemy import and_

from flask import abort, g
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
        "dms": {"read", "write", "manage", "export"},
    },
    "paralegal": {
        "matter": {"create"},
        "crm": {"read", "write", "conflict_check"},
        "dms": {"read", "write", "export"},
    },
    "staff": {
        "crm": {"read"},
        "dms": {"read"},
    },
}


def _request_cache() -> dict:
    cache = getattr(g, "_access_policy_cache", None)
    if cache is None:
        cache = {}
        g._access_policy_cache = cache
    return cache


def _clone_decision(decision: AccessDecision) -> AccessDecision:
    return AccessDecision(
        allow=bool(decision.allow),
        deny_reason=decision.deny_reason,
        ethical_wall_hit=bool(decision.ethical_wall_hit),
        scope_ids=[int(item) for item in (decision.scope_ids or [])],
    )


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
    cache = _request_cache()
    cache_key = ("visible_matter_ids", int(current_user.id))
    cached = cache.get(cache_key)
    if cached is not None:
        return [int(item) for item in cached]
    if is_admin():
        _, _, Matter, _, _ = _models()
        rows = [int(row[0]) for row in db.session.query(Matter.id).order_by(Matter.id.asc()).all()]
        cache[cache_key] = tuple(rows)
        return rows

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
    scoped_rows = [int(row[0]) for row in rows]
    cache[cache_key] = tuple(scoped_rows)
    return scoped_rows


def _ethical_wall_hit(matter_id: int) -> bool:
    if not current_user.is_authenticated or is_admin():
        return False
    cache = _request_cache()
    cache_key = ("ethical_wall_hit", int(current_user.id), int(matter_id))
    cached = cache.get(cache_key)
    if cached is not None:
        return bool(cached)

    EthicalWallMatter, EthicalWallRule, _, _, _ = _models()
    hit = (
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
    cache[cache_key] = bool(hit)
    return bool(hit)


def evaluate_matter_access(matter_id: int) -> AccessDecision:
    if not current_user.is_authenticated:
        return AccessDecision(allow=False, deny_reason="not_authenticated")
    if is_admin():
        return AccessDecision(allow=True, scope_ids=[matter_id])
    cache = _request_cache()
    cache_key = ("matter_access", int(current_user.id), int(matter_id))
    cached = cache.get(cache_key)
    if cached is not None:
        return _clone_decision(cached)

    _, _, _, MatterMember, _ = _models()
    membership = (
        db.session.query(MatterMember.id)
        .filter(MatterMember.matter_id == matter_id, MatterMember.user_id == current_user.id)
        .first()
    )
    if membership is None:
        decision = AccessDecision(allow=False, deny_reason="not_on_matter_team")
        cache[cache_key] = _clone_decision(decision)
        return decision

    if _ethical_wall_hit(matter_id):
        decision = AccessDecision(
            allow=False,
            deny_reason="ethical_wall_deny",
            ethical_wall_hit=True,
        )
        cache[cache_key] = _clone_decision(decision)
        return decision

    decision = AccessDecision(allow=True, scope_ids=[matter_id])
    cache[cache_key] = _clone_decision(decision)
    return decision


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
    cache = _request_cache()
    decision_key = ("permission_decision", int(current_user.id), raw_role, resource, action)
    cached_decision = cache.get(decision_key)
    if cached_decision is not None:
        return bool(cached_decision)

    _, _, _, _, PermissionGrant = _models()
    grants_key = ("permission_grants", tuple(sorted(roles)))
    grant_rows = cache.get(grants_key)
    if grant_rows is None:
        grant_rows = (
            PermissionGrant.query.filter(PermissionGrant.role.in_(roles))
            .order_by(PermissionGrant.id.desc())
            .all()
        )
        cache[grants_key] = grant_rows
    best_grant = None
    best_score = -1
    for row in grant_rows:
        if row.resource not in {resource, "*"} or row.action not in {action, "*"}:
            continue
        score = 0
        if row.resource == resource:
            score += 2
        if row.action == action:
            score += 1
        if score > best_score:
            best_score = score
            best_grant = row
    if best_grant is not None:
        decision = bool(best_grant.is_allowed)
        cache[decision_key] = decision
        return decision
    decision = _default_permission_allows(normalized_role, resource, action)
    cache[decision_key] = decision
    return decision


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
