from __future__ import annotations

import datetime as dt
import json
import time

from intranet.extensions import db
from intranet.mfa import _totp, generate_totp_secret
from intranet.models import CRMFollowUp, CRMLead, FirmSetting, User


def _csrf_token_for(client, path: str = "/login") -> str:
    client.get(path)
    with client.session_transaction() as sess:
        return sess.get("_csrf_token") or ""


def _seed_admin_with_mfa(email: str = "priority-admin@example.com", password: str = "TestPassword123!") -> tuple[User, str, str]:
    secret = generate_totp_secret()
    user = User(
        email=email,
        full_name="Priority Admin",
        role="admin",
        password_hash="x",
        is_active=True,
        mfa_enabled=True,
        mfa_secret=secret,
    )
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user, password, secret


def _login(client, email: str, password: str, secret: str) -> str:
    csrf = _csrf_token_for(client, "/login")
    code = _totp(secret, int(time.time() // 30))
    response = client.post(
        "/login",
        data={
            "csrf_token": csrf,
            "email": email,
            "password": password,
            "mfa_code": code,
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "/dashboard" in (response.headers.get("Location") or "")
    return csrf


def test_dashboard_renders_priority_inbox(app_ctx):
    user, password, secret = _seed_admin_with_mfa()
    client = app_ctx.test_client()
    _login(client, user.email, password, secret)

    response = client.get("/dashboard")
    assert response.status_code == 200
    assert b"Priority Inbox" in response.data
    assert b"Client Response" in response.data


def test_dashboard_uses_configured_priority_inbox_sla_values(app_ctx):
    user, password, secret = _seed_admin_with_mfa(email="priority-config-admin@example.com")
    db.session.add(
        FirmSetting(
            setting_key="priority_inbox",
            setting_value_json=json.dumps(
                {
                    "portal_response_sla_hours": 6,
                    "followup_horizon_hours": 30,
                    "billing_capture_sla_hours": 72,
                    "digest_enabled": True,
                    "digest_interval_minutes": 90,
                }
            ),
            updated_by=user.id,
        )
    )
    db.session.commit()

    client = app_ctx.test_client()
    _login(client, user.email, password, secret)
    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"Client response SLA: 6h" in response.data
    assert b"Follow-up horizon: 30h" in response.data
    assert b"Billing capture SLA: 72h" in response.data


def test_crm_followup_status_route_updates_status_and_blocks_external_next(app_ctx):
    user, password, secret = _seed_admin_with_mfa(email="followup-admin@example.com")
    lead = CRMLead(
        full_name="Client Prospect",
        stage="new",
        created_by=user.id,
        assigned_to=user.id,
    )
    db.session.add(lead)
    db.session.flush()
    followup = CRMFollowUp(
        lead_id=lead.id,
        due_at=dt.datetime.utcnow() + dt.timedelta(hours=2),
        note="Call prospect to confirm documents.",
        status="open",
        created_by=user.id,
    )
    db.session.add(followup)
    db.session.commit()

    client = app_ctx.test_client()
    csrf = _login(client, user.email, password, secret)

    response = client.post(
        f"/crm/followups/{followup.id}/status",
        data={
            "csrf_token": csrf,
            "status": "done",
            "next": "https://evil.example/phish",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers.get("Location", "").endswith(f"/crm/leads/{lead.id}")
    db.session.refresh(followup)
    assert followup.status == "done"
