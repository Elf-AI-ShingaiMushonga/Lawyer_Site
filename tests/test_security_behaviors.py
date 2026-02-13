from __future__ import annotations

import datetime as dt

from flask_login import login_user, logout_user

from intranet import create_app
from intranet.extensions import db
from intranet.helpers import can_access_matter
from intranet.jobs.worker import _handle_suspicious_activity_scan
from intranet.models import (
    AuditLog,
    EthicalWall,
    EthicalWallMatter,
    EthicalWallRule,
    Matter,
    MatterMember,
    PortalUser,
    SuspiciousActivityAlert,
    User,
)
from intranet.policies import visible_matter_ids


def _csrf_token_for_login(client) -> str:
    client.get("/login")
    with client.session_transaction() as sess:
        return sess.get("_csrf_token") or ""


def test_visible_matter_ids_excludes_ethical_wall(app_ctx):
    app = app_ctx
    user = User(email="wall@example.com", full_name="Wall User", role="lawyer", password_hash="x")
    user.set_password("TestPassword123!")
    db.session.add(user)
    db.session.flush()

    denied_matter = Matter(
        matter_no="2026-WALL-0001",
        title="Denied Matter",
        client_name="Denied Client",
        status="Open",
        created_by=user.id,
        opened_at=dt.datetime.utcnow(),
        last_updated_at=dt.datetime.utcnow(),
    )
    allowed_matter = Matter(
        matter_no="2026-WALL-0002",
        title="Allowed Matter",
        client_name="Allowed Client",
        status="Open",
        created_by=user.id,
        opened_at=dt.datetime.utcnow(),
        last_updated_at=dt.datetime.utcnow(),
    )
    db.session.add_all([denied_matter, allowed_matter])
    db.session.flush()

    db.session.add(MatterMember(matter_id=denied_matter.id, user_id=user.id, role_in_matter="Team"))
    db.session.add(MatterMember(matter_id=allowed_matter.id, user_id=user.id, role_in_matter="Team"))
    db.session.flush()

    wall = EthicalWall(name="Conflict Wall", created_by=user.id, is_active=True)
    db.session.add(wall)
    db.session.flush()
    db.session.add(EthicalWallMatter(wall_id=wall.id, matter_id=denied_matter.id))
    db.session.add(EthicalWallRule(wall_id=wall.id, user_id=user.id, is_deny=True, is_active=True))
    db.session.commit()

    with app.test_request_context("/"):
        login_user(user)
        ids = visible_matter_ids()
        logout_user()

    assert ids == [allowed_matter.id]


def test_mfa_required_role_redirects_to_setup(app_ctx):
    app = app_ctx
    user = User(email="staff@example.com", full_name="Staff User", role="staff", password_hash="x", is_active=True)
    user.set_password("StrongPassword123!")
    db.session.add(user)
    db.session.commit()

    client = app.test_client()
    token = _csrf_token_for_login(client)
    resp = client.post(
        "/login",
        data={
            "csrf_token": token,
            "email": user.email,
            "password": "StrongPassword123!",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 302
    assert "/auth/mfa/setup" in (resp.headers.get("Location") or "")


def test_suspicious_scan_creates_alert_for_repeated_denied_access(seed_user_matter):
    user = seed_user_matter["user"]
    now = dt.datetime.utcnow()
    for _ in range(6):
        db.session.add(
            AuditLog(
                actor_user_id=user.id,
                action="matter_access_denied",
                entity_type="Matter",
                entity_id=seed_user_matter["matter"].id,
                at=now,
            )
        )
    db.session.commit()

    message = _handle_suspicious_activity_scan({})
    alerts = SuspiciousActivityAlert.query.filter_by(alert_type="repeated_denied_matter_access", status="open").all()

    assert "created alerts" in message
    assert alerts


def test_can_access_matter_fails_closed_when_policy_evaluator_errors(app_ctx, monkeypatch):
    app = app_ctx
    user = User(email="fail-closed@example.com", full_name="Fail Closed", role="lawyer", password_hash="x")
    user.set_password("StrongPassword123!")
    db.session.add(user)
    db.session.flush()

    matter = Matter(
        matter_no="2026-SEC-0001",
        title="Policy Error Matter",
        client_name="Security Client",
        status="Open",
        created_by=user.id,
        opened_at=dt.datetime.utcnow(),
        last_updated_at=dt.datetime.utcnow(),
    )
    db.session.add(matter)
    db.session.flush()
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Team"))
    db.session.commit()

    def _raise(_matter_id: int):
        raise RuntimeError("policy evaluation failed")

    monkeypatch.setattr("intranet.policies.evaluate_matter_access", _raise)

    with app.test_request_context("/matters"):
        login_user(user)
        allowed = can_access_matter(matter.id)
        logout_user()

    assert allowed is False


def test_portal_login_rate_limit_enforced(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'portal-rate-limit.db'}")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    monkeypatch.setenv("AUTH_LOGIN_RATE_LIMIT", "10000/minute")
    monkeypatch.setenv("AUTH_REGISTER_RATE_LIMIT", "10000/minute")
    monkeypatch.setenv("PORTAL_LOGIN_RATE_LIMIT", "2/minute")
    monkeypatch.setenv("AUTH_SSO_TOKEN_RATE_LIMIT", "10000/minute")
    app = create_app()
    app.config.update(TESTING=True)

    with app.app_context():
        db.create_all()
        user = PortalUser(
            email="portal-rate@example.com",
            full_name="Portal Rate User",
            password_hash="x",
            is_active=True,
        )
        user.set_password("PortalStrongPassword123!")
        db.session.add(user)
        db.session.commit()

    client = app.test_client()
    client.get("/portal/login")
    with client.session_transaction() as sess:
        csrf = sess.get("_csrf_token") or ""

    first = client.post(
        "/portal/login",
        data={"csrf_token": csrf, "email": "portal-rate@example.com", "password": "wrong-password"},
        follow_redirects=False,
    )
    second = client.post(
        "/portal/login",
        data={"csrf_token": csrf, "email": "portal-rate@example.com", "password": "wrong-password"},
        follow_redirects=False,
    )
    third = client.post(
        "/portal/login",
        data={"csrf_token": csrf, "email": "portal-rate@example.com", "password": "wrong-password"},
        follow_redirects=False,
    )

    assert first.status_code in {302, 401}
    assert second.status_code in {302, 401}
    assert third.status_code == 429
