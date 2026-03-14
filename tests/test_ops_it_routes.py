from __future__ import annotations

from flask import g

from intranet.extensions import db
from intranet.models import HelpdeskTicket, HelpdeskTicketComment, ITAsset, User


def _set_internal_session(client, user_id: int, csrf_token: str = "test-csrf") -> str:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["_csrf_token"] = csrf_token
    if hasattr(g, "_login_user"):
        delattr(g, "_login_user")
    return csrf_token


def _seed_user(email: str, full_name: str, role: str) -> User:
    user = User(
        email=email,
        full_name=full_name,
        role=role,
        password_hash="x",
        is_active=True,
        mfa_enabled=True,
        mfa_secret="TESTMFATESTMFATESTMFATESTMFATEST12",
    )
    user.set_password("TestPassword123!")
    db.session.add(user)
    db.session.flush()
    return user


def test_ops_assets_requires_support_or_admin(app_ctx):
    admin = _seed_user("ops-admin@example.com", "Ops Admin", "admin")
    support = _seed_user("ops-support@example.com", "Ops Support", "operations_staff")
    lawyer = _seed_user("ops-lawyer@example.com", "Ops Lawyer", "senior_attorney")
    db.session.commit()

    client = app_ctx.test_client()

    _set_internal_session(client, lawyer.id)
    forbidden = client.get("/ops/assets")
    assert forbidden.status_code == 403

    _set_internal_session(client, support.id)
    support_view = client.get("/ops/assets")
    assert support_view.status_code == 200
    assert b"IT Asset Registry" in support_view.data

    _set_internal_session(client, admin.id)
    admin_view = client.get("/ops/assets")
    assert admin_view.status_code == 200


def test_support_can_create_and_update_assets(app_ctx):
    support = _seed_user("asset-support@example.com", "Asset Support", "operations_staff")
    assignee = _seed_user("asset-user@example.com", "Asset User", "senior_attorney")
    db.session.commit()

    client = app_ctx.test_client()
    csrf = _set_internal_session(client, support.id)

    create_response = client.post(
        "/ops/assets",
        data={
            "csrf_token": csrf,
            "asset_tag": "DM-LAP-014",
            "name": "Partner Laptop",
            "asset_type": "laptop",
            "status": "assigned",
            "assigned_user_id": str(assignee.id),
            "vendor": "Dell",
            "location": "Johannesburg HQ",
        },
        follow_redirects=True,
    )

    assert create_response.status_code == 200
    assert b"DM-LAP-014" in create_response.data

    asset = ITAsset.query.filter_by(asset_tag="DM-LAP-014").first()
    assert asset is not None
    assert asset.assigned_user_id == assignee.id
    assert asset.status == "assigned"

    update_response = client.post(
        "/ops/assets",
        data={
            "csrf_token": csrf,
            "asset_id": str(asset.id),
            "asset_tag": asset.asset_tag,
            "name": asset.name,
            "asset_type": asset.asset_type,
            "status": "repair",
            "assigned_user_id": "",
            "vendor": asset.vendor or "",
            "location": asset.location or "",
        },
        follow_redirects=True,
    )

    assert update_response.status_code == 200
    refreshed = db.session.get(ITAsset, asset.id)
    assert refreshed is not None
    assert refreshed.status == "repair"
    assert refreshed.assigned_user_id is None


def test_helpdesk_ticket_submission_and_triage_flow(app_ctx):
    support = _seed_user("desk-support@example.com", "Desk Support", "operations_staff")
    reporter = _seed_user("desk-user@example.com", "Desk User", "senior_attorney")
    outsider = _seed_user("desk-outsider@example.com", "Desk Outsider", "senior_attorney")
    db.session.flush()

    asset = ITAsset(
        asset_tag="DM-DOCK-003",
        name="USB-C Dock",
        asset_type="peripheral",
        status="assigned",
        assigned_user_id=reporter.id,
        created_by=support.id,
    )
    db.session.add(asset)
    db.session.commit()

    client = app_ctx.test_client()
    csrf = _set_internal_session(client, reporter.id)

    create_response = client.post(
        "/ops/helpdesk",
        data={
            "csrf_token": csrf,
            "subject": "Dock not connecting external monitors",
            "category": "hardware",
            "priority": "high",
            "asset_id": str(asset.id),
            "description": "Dual monitor setup stopped working after reconnecting the dock.",
        },
        follow_redirects=False,
    )

    assert create_response.status_code == 302

    ticket = HelpdeskTicket.query.filter_by(reporter_user_id=reporter.id).first()
    assert ticket is not None
    assert ticket.asset_id == asset.id
    assert ticket.ticket_no.startswith("HD-")
    assert ticket.status == "new"

    reporter_queue = client.get("/ops/helpdesk")
    assert reporter_queue.status_code == 200
    assert b"Dock not connecting external monitors" in reporter_queue.data

    reporter_detail = client.get(f"/ops/helpdesk/{ticket.id}")
    assert reporter_detail.status_code == 200
    assert ticket.ticket_no.encode("utf-8") in reporter_detail.data

    _set_internal_session(client, outsider.id)
    forbidden_detail = client.get(f"/ops/helpdesk/{ticket.id}")
    assert forbidden_detail.status_code == 403

    support_csrf = _set_internal_session(client, support.id)
    update_response = client.post(
        f"/ops/helpdesk/{ticket.id}",
        data={
            "csrf_token": support_csrf,
            "action": "update",
            "status": "in_progress",
            "priority": "critical",
            "category": "hardware",
            "assigned_to": str(support.id),
            "asset_id": str(asset.id),
            "resolution_summary": "Testing replacement dock and cable set.",
        },
        follow_redirects=True,
    )

    assert update_response.status_code == 200
    triaged_ticket = db.session.get(HelpdeskTicket, ticket.id)
    assert triaged_ticket is not None
    assert triaged_ticket.status == "in_progress"
    assert triaged_ticket.priority == "critical"
    assert triaged_ticket.assigned_to == support.id
    assert triaged_ticket.first_response_at is not None

    comment_response = client.post(
        f"/ops/helpdesk/{ticket.id}",
        data={
            "csrf_token": support_csrf,
            "action": "comment",
            "body": "Replacement dock issued and user asked to retest.",
        },
        follow_redirects=True,
    )

    assert comment_response.status_code == 200
    comments = HelpdeskTicketComment.query.filter_by(ticket_id=ticket.id).all()
    assert len(comments) == 1
    assert comments[0].author_user_id == support.id
