from __future__ import annotations

from ..extensions import db
from ..models import DirectorTeamMember, User
from ..roles import canonical_role, role_is_director

TEAM_MEMBER_CANONICAL_ROLES = {"senior_attorney", "junior_attorney", "candidate_attorney"}
TEAM_MEMBER_COMPAT_ROLES = {"lawyer", "partner", "associate", "paralegal"}


def user_can_be_team_member(user: User | None) -> bool:
    if user is None:
        return False
    role_value = canonical_role(getattr(user, "role", None))
    if role_value in TEAM_MEMBER_CANONICAL_ROLES:
        return True
    return str(getattr(user, "role", "") or "").strip().lower() in TEAM_MEMBER_COMPAT_ROLES


def team_candidate_users_query():
    compatible_roles = sorted(TEAM_MEMBER_CANONICAL_ROLES.union(TEAM_MEMBER_COMPAT_ROLES))
    return (
        User.query.filter(
            User.is_active.is_(True),
            db.func.lower(User.role).in_(compatible_roles),
        )
        .order_by(User.full_name.asc(), User.email.asc())
    )


def director_team_member_ids(director_user_id: int) -> set[int]:
    rows = (
        db.session.query(DirectorTeamMember.member_user_id)
        .filter(DirectorTeamMember.director_id == int(director_user_id))
        .all()
    )
    return {int(member_user_id) for (member_user_id,) in rows if member_user_id is not None}


def user_in_director_scope(director_user_id: int, user_id: int) -> bool:
    if int(director_user_id) == int(user_id):
        return True
    return int(user_id) in director_team_member_ids(director_user_id)


def require_director_role(role_value: str | None) -> bool:
    return role_is_director(role_value)
