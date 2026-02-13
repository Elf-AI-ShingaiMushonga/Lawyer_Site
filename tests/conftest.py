from __future__ import annotations

import datetime as dt

import pytest

from intranet import create_app
from intranet.extensions import db, login_manager
from intranet.models import Matter, TrustAccount, TrustClientLedger, User


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    monkeypatch.setenv("AUTH_LOGIN_RATE_LIMIT", "10000/minute")
    monkeypatch.setenv("AUTH_REGISTER_RATE_LIMIT", "10000/minute")
    monkeypatch.setenv("PORTAL_LOGIN_RATE_LIMIT", "10000/minute")
    monkeypatch.setenv("AUTH_SSO_TOKEN_RATE_LIMIT", "10000/minute")
    app = create_app()
    app.config.update(TESTING=True)
    login_manager.session_protection = None

    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()


@pytest.fixture()
def app_ctx(app):
    with app.app_context():
        yield app


@pytest.fixture()
def seed_user_matter(app_ctx):
    user = User(email="test@example.com", full_name="Test User", role="admin", password_hash="x")
    user.set_password("TestPassword123!")
    db.session.add(user)
    db.session.flush()

    matter = Matter(
        matter_no="2026-TEST-0001",
        title="Test Matter",
        client_name="Test Client",
        status="Open",
        created_by=user.id,
        opened_at=dt.datetime.utcnow(),
        last_updated_at=dt.datetime.utcnow(),
    )
    db.session.add(matter)
    db.session.flush()

    account = TrustAccount(name="Trust Main", currency="ZAR", is_active=True)
    db.session.add(account)
    db.session.flush()

    ledger = TrustClientLedger(trust_account_id=account.id, client_name="Test Client", matter_id=matter.id, current_balance=0.0)
    db.session.add(ledger)

    db.session.commit()
    return {"user": user, "matter": matter, "account": account, "ledger": ledger}
