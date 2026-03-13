from __future__ import annotations

import datetime as dt
import time

from intranet.extensions import db
from intranet.mfa import _totp, generate_totp_secret
from intranet.models import (
    CRMLead,
    DocumentOCRText,
    DocumentRecord,
    DocumentTemplate,
    DocumentVersion,
    Matter,
    MatterWorkspaceDocument,
    MatterWorkspaceDocumentComment,
    Task,
    User,
)
from intranet.timeutils import utc_now


def _csrf_token_for(client, path: str = "/login") -> str:
    client.get(path)
    with client.session_transaction() as sess:
        return sess.get("_csrf_token") or ""


def _seed_admin(email: str = "collab-admin@example.com", password: str = "TestPassword123!") -> tuple[User, str, str]:
    secret = generate_totp_secret()
    user = User(
        email=email,
        full_name="Collaboration Admin",
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
    return csrf


def test_matters_task_queue_filter_surfaces_only_urgent_unassigned(app_ctx):
    user, password, secret = _seed_admin(email="queue-filter-admin@example.com")
    now = utc_now()
    urgent = Matter(
        matter_no="2026-QUEUE-URGENT",
        title="Urgent Unassigned Matter",
        client_name="Queue Client",
        status="Open",
        created_by=user.id,
        opened_at=now,
        last_updated_at=now,
    )
    assigned = Matter(
        matter_no="2026-QUEUE-ASSIGNED",
        title="Assigned Matter",
        client_name="Queue Client",
        status="Open",
        created_by=user.id,
        opened_at=now,
        last_updated_at=now,
    )
    future = Matter(
        matter_no="2026-QUEUE-FUTURE",
        title="Future Unassigned Matter",
        client_name="Queue Client",
        status="Open",
        created_by=user.id,
        opened_at=now,
        last_updated_at=now,
    )
    db.session.add_all([urgent, assigned, future])
    db.session.flush()
    db.session.add_all(
        [
            Task(
                matter_id=urgent.id,
                title="Urgent unassigned task",
                status="Todo",
                due_date=dt.date.today(),
                created_by=user.id,
            ),
            Task(
                matter_id=assigned.id,
                title="Urgent assigned task",
                status="Todo",
                due_date=dt.date.today(),
                assigned_to=user.id,
                created_by=user.id,
            ),
            Task(
                matter_id=future.id,
                title="Future unassigned task",
                status="Todo",
                due_date=dt.date.today() + dt.timedelta(days=10),
                created_by=user.id,
            ),
        ]
    )
    db.session.commit()

    client = app_ctx.test_client()
    _login(client, user.email, password, secret)
    response = client.get("/matters?task_queue=urgent-unassigned")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "2026-QUEUE-URGENT - Urgent Unassigned Matter" in body
    assert "2026-QUEUE-ASSIGNED - Assigned Matter" not in body
    assert "2026-QUEUE-FUTURE - Future Unassigned Matter" not in body
    assert "Urgent unassigned" in body


def test_dashboard_urgent_unassigned_points_to_filtered_directory(app_ctx):
    user, password, secret = _seed_admin(email="dashboard-queue-admin@example.com")
    matter = Matter(
        matter_no="2026-DASH-QUEUE",
        title="Dashboard Queue Matter",
        client_name="Dash Client",
        status="Open",
        created_by=user.id,
        opened_at=utc_now(),
        last_updated_at=utc_now(),
    )
    db.session.add(matter)
    db.session.flush()
    db.session.add(
        Task(
            matter_id=matter.id,
            title="Urgent unassigned task",
            status="Todo",
            due_date=dt.date.today(),
            created_by=user.id,
        )
    )
    db.session.commit()

    client = app_ctx.test_client()
    _login(client, user.email, password, secret)
    response = client.get("/dashboard")

    assert response.status_code == 200
    assert b"/matters?task_queue=urgent-unassigned" in response.data
    assert b"Calendar" in response.data


def test_crm_lead_detail_profile_update_persists_contact_and_owner(app_ctx):
    user, password, secret = _seed_admin(email="lead-profile-admin@example.com")
    owner = User(
        email="lead-owner@example.com",
        full_name="Lead Owner",
        role="admin",
        password_hash="x",
        is_active=True,
    )
    owner.set_password("TestPassword123!")
    db.session.add(owner)
    db.session.flush()
    lead = CRMLead(
        full_name="Prospect Client",
        stage="new",
        created_by=user.id,
    )
    db.session.add(lead)
    db.session.commit()

    client = app_ctx.test_client()
    csrf = _login(client, user.email, password, secret)
    response = client.post(
        f"/crm/leads/{lead.id}",
        data={
            "csrf_token": csrf,
            "action": "update",
            "full_name": "Prospect Client Updated",
            "organization": "Acme Holdings",
            "email": "prospect@example.com",
            "phone": "+27 11 555 0101",
            "source": "Referral",
            "assigned_to": owner.id,
            "stage": "qualified",
            "notes": "Ready for fee proposal.",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    db.session.refresh(lead)
    assert lead.full_name == "Prospect Client Updated"
    assert lead.organization == "Acme Holdings"
    assert lead.email == "prospect@example.com"
    assert lead.phone == "+27 11 555 0101"
    assert lead.source == "Referral"
    assert lead.assigned_to == owner.id
    assert lead.stage == "qualified"
    assert lead.notes == "Ready for fee proposal."


def test_matter_document_workbench_create_comment_and_publish_snapshot(app_ctx):
    user, password, secret = _seed_admin(email="workbench-admin@example.com")
    matter = Matter(
        matter_no="2026-WORKBENCH-001",
        title="Workbench Matter",
        client_name="Workbench Client",
        status="Open",
        created_by=user.id,
        opened_at=utc_now(),
        last_updated_at=utc_now(),
    )
    db.session.add(matter)
    db.session.flush()
    template = DocumentTemplate(
        name="Workbench Draft Template",
        template_type="Memo",
        body="Draft for {{matter_no}} / {{client_name}}",
        created_by=user.id,
    )
    db.session.add(template)
    db.session.commit()

    client = app_ctx.test_client()
    csrf = _login(client, user.email, password, secret)

    create_response = client.post(
        f"/matters/{matter.id}/documents/workbench",
        data={
            "csrf_token": csrf,
            "action": "create_document",
            "title": "Case Theory Outline",
            "template_id": template.id,
            "status": "draft",
            "document_type": "General",
            "confidentiality": "Internal",
        },
        follow_redirects=False,
    )
    assert create_response.status_code == 302

    workspace_document = MatterWorkspaceDocument.query.filter_by(
        matter_id=matter.id,
        title="Case Theory Outline",
    ).first()
    assert workspace_document is not None
    assert "2026-WORKBENCH-001" in workspace_document.body
    assert "Workbench Client" in workspace_document.body

    comment_response = client.post(
        f"/matters/{matter.id}/documents/workbench",
        data={
            "csrf_token": csrf,
            "action": "add_comment",
            "document_id": workspace_document.id,
            "anchor_label": "Facts",
            "comment_body": "Confirm the chronology against the witness statement.",
        },
        follow_redirects=False,
    )
    assert comment_response.status_code == 302
    assert MatterWorkspaceDocumentComment.query.filter_by(workspace_document_id=workspace_document.id).count() == 1

    publish_response = client.post(
        f"/matters/{matter.id}/documents/workbench",
        data={
            "csrf_token": csrf,
            "action": "publish_document",
            "document_id": workspace_document.id,
        },
        follow_redirects=False,
    )
    assert publish_response.status_code == 302

    db.session.refresh(workspace_document)
    assert workspace_document.published_document_id is not None
    assert workspace_document.published_version_id is not None

    record = db.session.get(DocumentRecord, workspace_document.published_document_id)
    version = db.session.get(DocumentVersion, workspace_document.published_version_id)
    ocr = DocumentOCRText.query.filter_by(document_version_id=version.id).first()

    assert record is not None
    assert record.title == "Case Theory Outline"
    assert version is not None
    assert version.version_no == 1
    assert ocr is not None
    assert "Workbench Client" in ocr.extracted_text
