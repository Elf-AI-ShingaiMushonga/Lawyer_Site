from __future__ import annotations

import datetime as dt

from flask import g

from intranet.extensions import db
from intranet.models import DirectorTeamMember, Matter, MatterMember, User


def _set_user_session(client, user_id: int, csrf_token: str = "test-csrf") -> None:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["_csrf_token"] = csrf_token
    if hasattr(g, "_login_user"):
        delattr(g, "_login_user")


def _seed_user(email: str, *, role: str) -> User:
    user = User(
        email=email,
        full_name=email.split("@", 1)[0].replace(".", " ").title(),
        role=role,
        password_hash="x",
        is_active=True,
        mfa_enabled=True,
        mfa_secret="JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
    )
    user.set_password("TestPassword123!")
    db.session.add(user)
    db.session.flush()
    return user


def _seed_matter(owner: User, matter_no: str) -> Matter:
    row = Matter(
        matter_no=matter_no,
        title=f"Matter {matter_no}",
        client_name="Director Scope Client",
        status="Open",
        created_by=owner.id,
        opened_at=dt.datetime.utcnow(),
        last_updated_at=dt.datetime.utcnow(),
        legal_category="General",
    )
    db.session.add(row)
    db.session.flush()
    return row


def test_director_personnel_access_control(app_ctx):
    app = app_ctx
    director = _seed_user("director-one@example.com", role="director")
    senior = _seed_user("senior-one@example.com", role="senior_attorney")
    db.session.commit()

    director_client = app.test_client()
    _set_user_session(director_client, director.id)
    allowed = director_client.get("/director/personnel")
    assert allowed.status_code == 200

    non_director_client = app.test_client()
    _set_user_session(non_director_client, senior.id)
    blocked = non_director_client.get("/director/personnel")
    assert blocked.status_code == 403


def test_director_can_assign_and_remove_team_members(app_ctx):
    app = app_ctx
    director = _seed_user("director-assign@example.com", role="director")
    member = _seed_user("junior-assign@example.com", role="junior_attorney")
    ops = _seed_user("ops-assign@example.com", role="operations_staff")
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, director.id)

    assign = client.post(
        "/director/personnel",
        data={"csrf_token": "test-csrf", "action": "assign_member", "member_user_id": member.id},
    )
    assert assign.status_code == 302
    row = DirectorTeamMember.query.filter_by(director_id=director.id, member_user_id=member.id).first()
    assert row is not None

    assign_invalid_role = client.post(
        "/director/personnel",
        data={"csrf_token": "test-csrf", "action": "assign_member", "member_user_id": ops.id},
    )
    assert assign_invalid_role.status_code == 302
    assert DirectorTeamMember.query.filter_by(member_user_id=ops.id).first() is None

    remove = client.post(
        "/director/personnel",
        data={"csrf_token": "test-csrf", "action": "remove_member", "team_row_id": row.id},
    )
    assert remove.status_code == 302
    assert DirectorTeamMember.query.filter_by(id=row.id).first() is None


def test_director_team_membership_is_exclusive(app_ctx):
    app = app_ctx
    director_a = _seed_user("director-a@example.com", role="director")
    director_b = _seed_user("director-b@example.com", role="director")
    member = _seed_user("senior-exclusive@example.com", role="senior_attorney")
    db.session.commit()

    client_a = app.test_client()
    _set_user_session(client_a, director_a.id)
    response_a = client_a.post(
        "/director/personnel",
        data={"csrf_token": "test-csrf", "action": "assign_member", "member_user_id": member.id},
    )
    assert response_a.status_code == 302

    client_b = app.test_client()
    _set_user_session(client_b, director_b.id)
    response_b = client_b.post(
        "/director/personnel",
        data={"csrf_token": "test-csrf", "action": "assign_member", "member_user_id": member.id},
    )
    assert response_b.status_code == 302

    rows = DirectorTeamMember.query.filter_by(member_user_id=member.id).all()
    assert len(rows) == 1
    assert rows[0].director_id == director_a.id


def test_director_matter_assignment_is_scoped_to_team(app_ctx):
    app = app_ctx
    director = _seed_user("director-scope@example.com", role="director")
    in_team = _seed_user("junior-in-team@example.com", role="junior_attorney")
    out_team = _seed_user("junior-out-team@example.com", role="junior_attorney")
    db.session.add(
        DirectorTeamMember(
            director_id=director.id,
            member_user_id=in_team.id,
            assigned_by=director.id,
        )
    )
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, director.id)

    matter_create_get = client.get("/matters/new")
    body = matter_create_get.get_data(as_text=True)
    assert matter_create_get.status_code == 200
    assert in_team.full_name in body
    assert out_team.full_name not in body

    matter = _seed_matter(director, "2026-DIR-0001")
    db.session.add(MatterMember(matter_id=matter.id, user_id=director.id, role_in_matter="Responsible"))
    db.session.commit()

    blocked_add = client.post(
        f"/matters/{matter.id}/team",
        data={
            "csrf_token": "test-csrf",
            "email": out_team.email,
            "role_in_matter": "Team",
        },
    )
    assert blocked_add.status_code == 302
    assert MatterMember.query.filter_by(matter_id=matter.id, user_id=out_team.id).first() is None

    allowed_add = client.post(
        f"/matters/{matter.id}/team",
        data={
            "csrf_token": "test-csrf",
            "email": in_team.email,
            "role_in_matter": "Team",
        },
    )
    assert allowed_add.status_code == 302
    assert MatterMember.query.filter_by(matter_id=matter.id, user_id=in_team.id).first() is not None
