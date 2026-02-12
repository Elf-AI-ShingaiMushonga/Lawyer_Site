from __future__ import annotations

from flask import abort
from flask_login import current_user

from ..extensions import db
from ..helpers import is_admin
from ..types import AccessDecision


def _models():
    from ..models import EthicalWallMatter, EthicalWallRule, Matter, MatterMember, PermissionGrant

    return EthicalWallMatter, EthicalWallRule, Matter, MatterMember, PermissionGrant


def visible_matter_ids() -> list[int]:
    if not current_user.is_authenticated:
        return []
    if is_admin():
        _, _, Matter, _, _ = _models()
        return [row[0] for row in db.session.query(Matter.id).order_by(Matter.id.asc()).all()]

    EthicalWallMatter, EthicalWallRule, _, MatterMember, _ = _models()
    member_ids = [
        row[0]
        for row in db.session.query(MatterMember.matter_id)
        .filter(MatterMember.user_id == current_user.id)
        .all()
    ]
    if not member_ids:
        return []

    denied_ids = {
        row[0]
        for row in db.session.query(EthicalWallMatter.matter_id)
        .join(EthicalWallRule, EthicalWallRule.wall_id == EthicalWallMatter.wall_id)
        .filter(
            EthicalWallRule.user_id == current_user.id,
            EthicalWallRule.is_deny.is_(True),
            EthicalWallRule.is_active.is_(True),
            EthicalWallMatter.matter_id.in_(member_ids),
        )
        .all()
    }
    return sorted(matter_id for matter_id in set(member_ids) if matter_id not in denied_ids)


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

    _, _, _, _, PermissionGrant = _models()
    grant = (
        PermissionGrant.query.filter_by(
            role=current_user.role,
            resource=resource,
            action=action,
            is_allowed=True,
        )
        .order_by(PermissionGrant.id.desc())
        .first()
    )
    return grant is not None


def enforce_matter_access(matter_id: int) -> None:
    decision = evaluate_matter_access(matter_id)
    if not decision.allow:
        abort(403)


def enforce_permission(resource: str, action: str) -> None:
    if not has_permission(resource, action):
        abort(403)
