from __future__ import annotations

import datetime as dt
from intranet.timeutils import utc_now
import json
import time

from intranet.extensions import db
from intranet.mfa import _totp, generate_totp_secret
from intranet.models import CRMFollowUp, CRMLead, FirmSetting, Matter, PortalMessage, PortalMessageThread, PortalUser, Task, User


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


def _seed_portal_thread_waiting_on_reply(user: User) -> tuple[Matter, PortalMessageThread, PortalUser, PortalMessage]:
    matter = Matter(
        matter_no="2026-PORTAL-0001",
        title="Portal Response Matter",
        client_name="Portal Client",
        status="Open",
        created_by=user.id,
        opened_at=utc_now() - dt.timedelta(days=2),
        last_updated_at=utc_now() - dt.timedelta(hours=6),
    )
    portal_user = PortalUser(
        email=f"portal-contact-{user.id}@example.com",
        full_name="Clement Client",
        password_hash="x",
        is_active=True,
    )
    portal_user.set_password("ClientPassword123!")
    db.session.add_all([matter, portal_user])
    db.session.flush()

    thread = PortalMessageThread(
        matter_id=matter.id,
        subject="Status update request",
        created_by_user_id=user.id,
        created_at=utc_now() - dt.timedelta(hours=8),
    )
    db.session.add(thread)
    db.session.flush()

    message = PortalMessage(
        thread_id=thread.id,
        body="Please confirm the next filing deadline and whether counsel has reverted.",
        from_portal_user_id=portal_user.id,
        created_at=utc_now() - dt.timedelta(hours=6),
    )
    db.session.add(message)
    db.session.commit()
    return matter, thread, portal_user, message


def test_dashboard_renders_priority_inbox(app_ctx):
    user, password, secret = _seed_admin_with_mfa()
    client = app_ctx.test_client()
    _login(client, user.email, password, secret)

    response = client.get("/dashboard")
    assert response.status_code == 200
    assert b"Priority Inbox" in response.data
    assert b"Client Response" in response.data


def test_dashboard_renders_legal_desk_with_next_best_move(app_ctx):
    user, password, secret = _seed_admin_with_mfa(email="legal-desk-admin@example.com")
    matter = Matter(
        matter_no="2026-DESK-0001",
        title="Urgent Motion",
        client_name="Desk Client",
        status="Open",
        risk_level="High",
        created_by=user.id,
        opened_at=utc_now(),
        last_updated_at=utc_now() - dt.timedelta(days=10),
    )
    db.session.add(matter)
    db.session.flush()
    db.session.add(
        Task(
            matter_id=matter.id,
            title="Prepare motion draft",
            status="Todo",
            due_date=dt.date.today() - dt.timedelta(days=1),
            created_by=user.id,
        )
    )
    db.session.commit()

    client = app_ctx.test_client()
    _login(client, user.email, password, secret)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"Legal Desk" in response.data
    assert b"Next Best Move" in response.data
    assert b"Clear overdue tasks" in response.data
    assert b"Personal Daily Briefing" in response.data
    assert b"Inbox and work queue pressure" in response.data


def test_dashboard_handles_missing_briefing_payloads(monkeypatch, app_ctx):
    from intranet.routes import auth as auth_routes

    user, password, secret = _seed_admin_with_mfa(email="dashboard-fallback-admin@example.com")
    client = app_ctx.test_client()
    _login(client, user.email, password, secret)

    monkeypatch.setattr(auth_routes, "build_priority_inbox", lambda *args, **kwargs: {})
    monkeypatch.setattr(auth_routes, "build_today_briefing", lambda *args, **kwargs: {})
    monkeypatch.setattr(auth_routes, "build_dashboard_focus_board", lambda *args, **kwargs: [])

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"Your workday is organized." in response.data
    assert b"No briefing actions are available yet." in response.data
    assert b"No matters are currently competing for your attention." in response.data


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


def test_dashboard_client_response_links_to_internal_message_center(app_ctx):
    user, password, secret = _seed_admin_with_mfa(email="priority-portal-admin@example.com")
    matter, thread, _portal_user, _message = _seed_portal_thread_waiting_on_reply(user)

    client = app_ctx.test_client()
    _login(client, user.email, password, secret)
    response = client.get("/dashboard")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert f"/portal/messages/workbench?matter_id={matter.id}&amp;thread_id={thread.id}" in body
    assert "Reply" in body
    assert "Please confirm the next filing deadline" in body


def test_internal_portal_message_center_allows_replying_to_client_thread(app_ctx):
    user, password, secret = _seed_admin_with_mfa(email="portal-workbench-admin@example.com")
    matter, thread, _portal_user, inbound = _seed_portal_thread_waiting_on_reply(user)

    client = app_ctx.test_client()
    csrf = _login(client, user.email, password, secret)

    response = client.get(f"/portal/messages/workbench?matter_id={matter.id}&thread_id={thread.id}")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Client Message Center" in body
    assert inbound.body in body

    post_response = client.post(
        "/portal/messages/workbench",
        data={
            "csrf_token": csrf,
            "matter_id": matter.id,
            "thread_id": thread.id,
            "body": "We have diarised the filing deadline and will revert with a fuller update today.",
        },
        follow_redirects=False,
    )
    assert post_response.status_code == 302
    assert f"/portal/messages/workbench?matter_id={matter.id}&thread_id={thread.id}" in (post_response.headers.get("Location") or "")

    replies = PortalMessage.query.filter_by(thread_id=thread.id, from_user_id=user.id).order_by(PortalMessage.id.desc()).all()
    assert replies
    assert replies[0].body == "We have diarised the filing deadline and will revert with a fuller update today."


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
        due_at=utc_now() + dt.timedelta(hours=2),
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


def test_dashboard_at_risk_defaults_to_criticality_sorting(app_ctx):
    user, password, secret = _seed_admin_with_mfa(email="risk-order-admin@example.com")
    now = utc_now()
    high = Matter(
        matter_no="2026-AR-HIGH",
        title="High Matter",
        client_name="Client High",
        status="Open",
        risk_level="High",
        created_by=user.id,
        opened_at=now - dt.timedelta(days=2),
        last_updated_at=now,
    )
    critical = Matter(
        matter_no="2026-AR-CRIT",
        title="Critical Matter",
        client_name="Client Critical",
        status="Open",
        risk_level="Critical",
        created_by=user.id,
        opened_at=now - dt.timedelta(days=5),
        last_updated_at=now - dt.timedelta(days=1),
    )
    db.session.add_all([high, critical])
    db.session.commit()

    client = app_ctx.test_client()
    _login(client, user.email, password, secret)
    response = client.get("/dashboard")
    assert response.status_code == 200
    body = response.get_data(as_text=True)

    critical_marker = "2026-AR-CRIT - Critical Matter"
    high_marker = "2026-AR-HIGH - High Matter"
    assert critical_marker in body
    assert high_marker in body
    assert body.index(critical_marker) < body.index(high_marker)


def test_dashboard_my_tasks_hides_done_tasks(app_ctx):
    user, password, secret = _seed_admin_with_mfa(email="task-visibility-admin@example.com")
    now = utc_now()
    matter = Matter(
        matter_no="2026-MY-TASKS-001",
        title="My Task Visibility Matter",
        client_name="Task Client",
        status="Open",
        created_by=user.id,
        opened_at=now - dt.timedelta(days=1),
        last_updated_at=now,
    )
    db.session.add(matter)
    db.session.flush()
    db.session.add_all(
        [
            Task(
                matter_id=matter.id,
                title="Active Dashboard Task",
                status="Todo",
                assigned_to=user.id,
                created_by=user.id,
            ),
            Task(
                matter_id=matter.id,
                title="Done Dashboard Task",
                status="Done",
                assigned_to=user.id,
                created_by=user.id,
            ),
        ]
    )
    db.session.commit()

    client = app_ctx.test_client()
    _login(client, user.email, password, secret)
    response = client.get("/dashboard")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Active Dashboard Task" in body
    assert "Done Dashboard Task" not in body


def test_dashboard_workspace_mode_persists_and_surfaces_command_actions(app_ctx):
    user, password, secret = _seed_admin_with_mfa(email="workspace-mode-admin@example.com")
    matter = Matter(
        matter_no="2026-WORKSPACE-001",
        title="Workspace Focus Matter",
        client_name="Workspace Client",
        status="Open",
        created_by=user.id,
        opened_at=utc_now(),
        last_updated_at=utc_now(),
        practice_area="General Litigation",
    )
    db.session.add(matter)
    db.session.commit()

    client = app_ctx.test_client()
    csrf = _login(client, user.email, password, secret)

    post_response = client.post(
        "/dashboard/workspace-mode",
        data={
            "csrf_token": csrf,
            "mode": "revenue",
        },
        follow_redirects=False,
    )
    assert post_response.status_code == 302
    row = FirmSetting.query.filter_by(setting_key=f"workspace_pref:user:{user.id}").first()
    assert row is not None
    assert '"mode": "revenue"' in (row.setting_value_json or "")

    response = client.get("/dashboard")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Workspace Command Center" in body
    assert "Revenue &amp; Risk" in body
    assert "Invoices" in body
    assert "Workspace Quick Actions" in body
    assert "Workspace mode and launch actions" in body
