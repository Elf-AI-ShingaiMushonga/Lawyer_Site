from __future__ import annotations

import io
import json
import os

from flask import g

from intranet.extensions import db
from intranet.models import (
    Announcement,
    Contact,
    DocumentFile,
    DocumentRecord,
    DocumentVersion,
    KnowledgeBase,
    Matter,
    MatterMember,
    MatterTemplate,
    MatterTimelineEvent,
    Task,
    User,
)
from intranet.services.storage_paths import resolve_upload_path
from intranet.timeutils import utc_now


def _set_internal_session(client, user_id: int, csrf_token: str = "test-csrf") -> str:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["_csrf_token"] = csrf_token
    if hasattr(g, "_login_user"):
        delattr(g, "_login_user")
    return csrf_token


def _seed_user(email: str, role: str) -> User:
    row = User(
        email=email,
        full_name=email.split("@", 1)[0],
        role=role,
        password_hash="x",
        is_active=True,
    )
    row.set_password("TestPassword123!")
    db.session.add(row)
    db.session.commit()
    return row


def _seed_matter(owner: User, *, matter_no: str, title: str, archetype: MatterTemplate | None = None) -> Matter:
    now = utc_now()
    row = Matter(
        matter_no=matter_no,
        title=title,
        client_name="Production Gap Client",
        status="Open",
        created_by=owner.id,
        opened_at=now,
        last_updated_at=now,
        archetype_id=archetype.id if archetype else None,
    )
    db.session.add(row)
    db.session.flush()
    db.session.add(MatterMember(matter_id=row.id, user_id=owner.id, role_in_matter="Lead"))
    db.session.commit()
    return row


def test_admin_users_announcements_and_audit_routes(app_ctx):
    admin = _seed_user("gap-admin@example.com", "director")
    client = app_ctx.test_client()
    csrf_token = _set_internal_session(client, admin.id)

    response = client.get("/admin/users")
    assert response.status_code == 200

    create_response = client.post(
        "/admin/users",
        data={
            "csrf_token": csrf_token,
            "action": "create",
            "email": "gap-user@example.com",
            "full_name": "Gap User",
            "role": "junior_attorney",
            "password": "LongEnoughPass1",
            "confirm_password": "LongEnoughPass1",
            "is_active": "1",
        },
        follow_redirects=False,
    )
    assert create_response.status_code == 302

    created_user = User.query.filter_by(email="gap-user@example.com").first()
    assert created_user is not None
    assert created_user.role == "junior_attorney"

    role_response = client.post(
        "/admin/users",
        data={
            "csrf_token": csrf_token,
            "action": "set_role",
            "user_id": created_user.id,
            "role": "candidate_attorney",
        },
        follow_redirects=False,
    )
    assert role_response.status_code == 302

    active_response = client.post(
        "/admin/users",
        data={
            "csrf_token": csrf_token,
            "action": "set_active",
            "user_id": created_user.id,
            "is_active": "",
        },
        follow_redirects=False,
    )
    assert active_response.status_code == 302

    db.session.refresh(created_user)
    assert created_user.role == "candidate_attorney"
    assert created_user.is_active is False

    announcements_get = client.get("/admin/announcements")
    assert announcements_get.status_code == 200

    announcement_response = client.post(
        "/admin/announcements",
        data={
            "csrf_token": csrf_token,
            "title": "Production Readiness Notice",
            "body": "Final audit window is active.",
        },
        follow_redirects=False,
    )
    assert announcement_response.status_code == 302
    assert Announcement.query.filter_by(title="Production Readiness Notice").count() == 1

    audit_response = client.get("/admin/audit")
    audit_body = audit_response.get_data(as_text=True)
    assert audit_response.status_code == 200
    assert "user_create" in audit_body
    assert "announcement_create" in audit_body


def test_contacts_and_knowledge_routes_support_create_search_and_update(app_ctx):
    user = _seed_user("gap-content@example.com", "senior_attorney")
    client = app_ctx.test_client()
    csrf_token = _set_internal_session(client, user.id)

    contacts_get = client.get("/contacts")
    assert contacts_get.status_code == 200

    contact_response = client.post(
        "/contacts",
        data={
            "csrf_token": csrf_token,
            "name": "Production Contact",
            "organization": "DM Inc",
            "email": "contact@example.com",
            "phone": "+27 11 555 0100",
            "notes": "Primary rollout contact",
        },
        follow_redirects=False,
    )
    assert contact_response.status_code == 302
    assert Contact.query.filter_by(email="contact@example.com").count() == 1

    contacts_search = client.get("/contacts?q=DM Inc")
    assert contacts_search.status_code == 200
    assert "Production Contact" in contacts_search.get_data(as_text=True)

    kb_response = client.post(
        "/kb",
        data={
            "csrf_token": csrf_token,
            "title": "Production KB Note",
            "tags": "prod,launch",
            "body": "Readiness checklist and cutover notes.",
        },
        follow_redirects=False,
    )
    assert kb_response.status_code == 302

    article = KnowledgeBase.query.filter_by(title="Production KB Note").first()
    assert article is not None

    kb_view = client.get(f"/kb/{article.id}")
    assert kb_view.status_code == 200
    assert "Readiness checklist" in kb_view.get_data(as_text=True)

    kb_update = client.post(
        f"/kb/{article.id}",
        data={
            "csrf_token": csrf_token,
            "title": "Production KB Note Updated",
            "tags": "prod,launch,updated",
            "body": "Updated readiness checklist and cutover notes.",
        },
        follow_redirects=False,
    )
    assert kb_update.status_code == 302

    db.session.refresh(article)
    assert article.title == "Production KB Note Updated"
    assert "updated" in (article.tags or "")


def test_matter_update_actions_and_document_download_delete_flows(app_ctx):
    user = _seed_user("gap-matter@example.com", "senior_attorney")
    archetype = MatterTemplate(
        name="Production Gap Archetype",
        legal_category="Commercial Litigation",
        required_fields_json=json.dumps(
            [{"key": "incident_date", "label": "Incident Date", "help": ""}],
            ensure_ascii=True,
        ),
        boilerplate_template="Incident date: {{ incident_date }}",
        created_by=user.id,
    )
    db.session.add(archetype)
    db.session.commit()
    matter = _seed_matter(
        user,
        matter_no="2026-GAP-0001",
        title="Production Gap Matter",
        archetype=archetype,
    )
    task = Task(
        matter_id=matter.id,
        title="Initial filing review",
        status="Todo",
        created_by=user.id,
    )
    db.session.add(task)
    db.session.commit()

    client = app_ctx.test_client()
    csrf_token = _set_internal_session(client, user.id)

    archetype_response = client.post(
        f"/matters/{matter.id}/archetype-fields",
        data={"csrf_token": csrf_token, "field_incident_date": "2026-03-01"},
        follow_redirects=False,
    )
    assert archetype_response.status_code == 302

    db.session.refresh(matter)
    assert json.loads(matter.archetype_data_json or "{}") == {"incident_date": "2026-03-01"}

    timeline_response = client.post(
        f"/matters/{matter.id}/timeline",
        data={
            "csrf_token": csrf_token,
            "title": "First hearing",
            "event_type": "Hearing",
            "event_date": "2026-04-15",
            "description": "Initial hearing date confirmed.",
            "is_milestone": "1",
        },
        follow_redirects=False,
    )
    assert timeline_response.status_code == 302
    assert MatterTimelineEvent.query.filter_by(matter_id=matter.id, title="First hearing").count() == 1

    task_response = client.post(
        f"/tasks/{task.id}/status",
        data={"csrf_token": csrf_token, "status": "Done", "suggest_time_on_done": "1"},
        follow_redirects=False,
    )
    assert task_response.status_code == 302
    assert "/time/entries" in (task_response.headers.get("Location") or "")

    db.session.refresh(task)
    assert task.status == "Done"

    dms_upload = client.post(
        f"/matters/{matter.id}/dms",
        data={
            "csrf_token": csrf_token,
            "action": "upload_document",
            "title": "Gap DMS Document",
            "document_type": "General",
            "confidentiality": "Internal",
            "privilege_label": "",
            "retention_category": "",
            "version_notes": "Initial version",
            "file": (io.BytesIO(b"gap document content"), "gap-doc.txt"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert dms_upload.status_code == 302

    document = DocumentRecord.query.filter_by(matter_id=matter.id, title="Gap DMS Document").first()
    assert document is not None
    version = DocumentVersion.query.filter_by(document_id=document.id).first()
    assert version is not None
    file_row = db.session.get(DocumentFile, version.document_file_id)
    assert file_row is not None
    document_id = int(document.id)
    version_id = int(version.id)
    file_id = int(file_row.id)

    download_response = client.get(f"/documents/{file_row.id}/download")
    assert download_response.status_code == 200
    assert "attachment" in (download_response.headers.get("Content-Disposition") or "").lower()

    inline_response = client.get(f"/documents/{file_row.id}/download?inline=1")
    assert inline_response.status_code == 200
    assert inline_response.data == b"gap document content"

    _, stored_path = resolve_upload_path(app_ctx.config["UPLOAD_DIR"], version.stored_filename)
    assert os.path.isfile(stored_path)

    delete_response = client.post(
        f"/documents/{document_id}/delete",
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert delete_response.status_code == 302

    assert db.session.get(DocumentRecord, document_id) is None
    assert db.session.get(DocumentVersion, version_id) is None
    assert db.session.get(DocumentFile, file_id) is None
    assert not os.path.exists(stored_path)
