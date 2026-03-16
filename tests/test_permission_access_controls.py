from __future__ import annotations

import datetime as dt
from intranet.timeutils import utc_now
import io

from flask import g

from intranet.extensions import db
from intranet.models import (
    CRMLead,
    Deadline,
    DocumentFile,
    DocumentOCRText,
    DocumentRecord,
    DocumentVersion,
    Invoice,
    Matter,
    MatterMember,
    MatterParty,
    MatterTimelineEvent,
    MatterWorkspaceDocument,
    PortalUser,
    Task,
    TimeEntry,
    User,
)


def _set_user_session(client, user_id: int, csrf_token: str = "test-csrf") -> None:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["_csrf_token"] = csrf_token
    if hasattr(g, "_login_user"):
        delattr(g, "_login_user")


def _set_portal_session(client, portal_user_id: int, csrf_token: str = "test-csrf") -> None:
    with client.session_transaction() as sess:
        sess["portal_user_id"] = portal_user_id
        sess["_csrf_token"] = csrf_token
    if hasattr(g, "_login_user"):
        delattr(g, "_login_user")


def _seed_user(
    email: str,
    *,
    role: str,
    is_active: bool = True,
    mfa_enabled: bool = True,
) -> User:
    user = User(
        email=email,
        full_name=email.split("@", 1)[0],
        role=role,
        password_hash="x",
        is_active=is_active,
        mfa_enabled=mfa_enabled,
    )
    user.set_password("TestPassword123!")
    db.session.add(user)
    db.session.flush()
    return user


def _seed_matter(owner: User, matter_no: str) -> Matter:
    row = Matter(
        matter_no=matter_no,
        title=f"Matter {matter_no}",
        client_name="Permission Client",
        status="Open",
        created_by=owner.id,
        opened_at=utc_now(),
        last_updated_at=utc_now(),
    )
    db.session.add(row)
    db.session.flush()
    return row


def test_crm_permissions_staff_and_paralegal(app_ctx):
    app = app_ctx
    staff = _seed_user("crm-staff@example.com", role="staff")
    paralegal = _seed_user("crm-paralegal@example.com", role="paralegal")
    db.session.commit()

    staff_client = app.test_client()
    _set_user_session(staff_client, staff.id)

    staff_get = staff_client.get("/crm/leads")
    assert staff_get.status_code == 200
    staff_post = staff_client.post(
        "/crm/leads",
        data={"csrf_token": "test-csrf", "full_name": "Blocked Staff Lead"},
    )
    assert staff_post.status_code == 403

    paralegal_client = app.test_client()
    _set_user_session(paralegal_client, paralegal.id)
    paralegal_post = paralegal_client.post(
        "/crm/leads",
        data={"csrf_token": "test-csrf", "full_name": "Allowed Paralegal Lead"},
    )
    assert paralegal_post.status_code == 302
    assert CRMLead.query.filter_by(full_name="Allowed Paralegal Lead").first() is not None


def test_staff_cannot_review_or_lock_time_entries(app_ctx):
    app = app_ctx
    lawyer = _seed_user("time-lawyer@example.com", role="lawyer")
    staff = _seed_user("time-staff@example.com", role="staff")
    matter = _seed_matter(lawyer, "2026-PERM-TIME-0001")
    db.session.add(MatterMember(matter_id=matter.id, user_id=lawyer.id, role_in_matter="Lead"))
    db.session.add(MatterMember(matter_id=matter.id, user_id=staff.id, role_in_matter="Team"))
    entry = TimeEntry(
        user_id=lawyer.id,
        matter_id=matter.id,
        start_at=utc_now() - dt.timedelta(hours=2),
        end_at=utc_now() - dt.timedelta(hours=1),
        hours=1.0,
        rounded_hours=1.0,
        narrative="Permission test entry",
        status="approved",
    )
    db.session.add(entry)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, staff.id)

    review_response = client.post(
        "/time/review",
        data={"csrf_token": "test-csrf", "entry_id": entry.id, "state": "approved"},
    )
    assert review_response.status_code == 403

    lock_response = client.post(
        f"/time/entries/{entry.id}/lock",
        data={"csrf_token": "test-csrf"},
    )
    assert lock_response.status_code == 403


def test_only_matter_team_managers_can_add_members(app_ctx):
    app = app_ctx
    lawyer = _seed_user("team-lawyer@example.com", role="lawyer")
    staff = _seed_user("team-staff@example.com", role="staff")
    invitee = _seed_user("team-invitee@example.com", role="staff")
    matter = _seed_matter(lawyer, "2026-PERM-TEAM-0001")
    db.session.add(MatterMember(matter_id=matter.id, user_id=lawyer.id, role_in_matter="Lead"))
    db.session.add(MatterMember(matter_id=matter.id, user_id=staff.id, role_in_matter="Team"))
    db.session.commit()

    staff_client = app.test_client()
    _set_user_session(staff_client, staff.id)
    blocked = staff_client.post(
        f"/matters/{matter.id}/team",
        data={"csrf_token": "test-csrf", "email": invitee.email, "role_in_matter": "Team"},
    )
    assert blocked.status_code == 403
    assert MatterMember.query.filter_by(matter_id=matter.id, user_id=invitee.id).first() is None

    lawyer_client = app.test_client()
    _set_user_session(lawyer_client, lawyer.id)
    allowed = lawyer_client.post(
        f"/matters/{matter.id}/team",
        data={"csrf_token": "test-csrf", "email": invitee.email, "role_in_matter": "Team"},
    )
    assert allowed.status_code == 302
    assert MatterMember.query.filter_by(matter_id=matter.id, user_id=invitee.id).first() is not None


def test_inactive_internal_user_session_is_blocked(app_ctx):
    app = app_ctx
    user = _seed_user("inactive-internal@example.com", role="lawyer", is_active=False)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.get("/dashboard")
    assert response.status_code == 302
    assert "/login" in (response.headers.get("Location") or "")
    with client.session_transaction() as sess:
        assert "_user_id" not in sess


def test_inactive_portal_user_session_is_blocked(app_ctx):
    app = app_ctx
    portal_user = PortalUser(
        email="inactive-portal@example.com",
        full_name="Inactive Portal",
        password_hash="x",
        is_active=False,
    )
    portal_user.set_password("PortalPassword123!")
    db.session.add(portal_user)
    db.session.commit()

    client = app.test_client()
    _set_portal_session(client, portal_user.id)
    response = client.get("/portal/matters")
    assert response.status_code == 302
    assert "/portal/login" in (response.headers.get("Location") or "")
    with client.session_transaction() as sess:
        assert "portal_user_id" not in sess


def test_staff_cannot_generate_invoices(app_ctx):
    app = app_ctx
    lawyer = _seed_user("billing-lawyer@example.com", role="lawyer")
    staff = _seed_user("billing-staff@example.com", role="staff")
    matter = _seed_matter(lawyer, "2026-PERM-BILL-0001")
    db.session.add(MatterMember(matter_id=matter.id, user_id=lawyer.id, role_in_matter="Lead"))
    db.session.add(MatterMember(matter_id=matter.id, user_id=staff.id, role_in_matter="Team"))
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, staff.id)
    response = client.post(
        "/billing/invoices",
        data={
            "csrf_token": "test-csrf",
            "matter_id": matter.id,
            "period_start": dt.date.today().isoformat(),
            "period_end": dt.date.today().isoformat(),
        },
    )
    assert response.status_code == 403


def test_candidate_attorney_can_upload_dms_documents(app_ctx):
    app = app_ctx
    lawyer = _seed_user("dms-lawyer@example.com", role="lawyer")
    candidate = _seed_user("dms-candidate@example.com", role="candidate_attorney")
    matter = _seed_matter(lawyer, "2026-PERM-DMS-0001")
    db.session.add(MatterMember(matter_id=matter.id, user_id=lawyer.id, role_in_matter="Lead"))
    db.session.add(MatterMember(matter_id=matter.id, user_id=candidate.id, role_in_matter="Team"))
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, candidate.id)
    response = client.post(
        f"/matters/{matter.id}/dms",
        data={
            "csrf_token": "test-csrf",
            "action": "upload_document",
            "title": "Allowed Upload",
            "document_type": "General",
            "confidentiality": "Internal",
            "file": (io.BytesIO(b"allowed"), "allowed.txt"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    doc = DocumentRecord.query.filter_by(matter_id=matter.id, title="Allowed Upload").first()
    assert doc is not None
    version = DocumentVersion.query.filter_by(document_id=doc.id, version_no=1).first()
    assert version is not None
    assert version.document_file_id is not None

    download_response = client.get(f"/documents/{version.document_file_id}/download")
    assert download_response.status_code == 200
    assert download_response.data == b"allowed"


def test_matter_member_without_dms_grants_cannot_upload_dms_documents(app_ctx):
    app = app_ctx
    lawyer = _seed_user("dms-owner-any-role@example.com", role="lawyer")
    analyst = _seed_user("dms-analyst-any-role@example.com", role="analyst")
    matter = _seed_matter(lawyer, "2026-PERM-DMS-ANY-0001")
    db.session.add(MatterMember(matter_id=matter.id, user_id=lawyer.id, role_in_matter="Lead"))
    db.session.add(MatterMember(matter_id=matter.id, user_id=analyst.id, role_in_matter="Team"))
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, analyst.id)
    response = client.post(
        f"/matters/{matter.id}/dms",
        data={
            "csrf_token": "test-csrf",
            "action": "upload_document",
            "title": "Any Role Upload",
            "document_type": "General",
            "confidentiality": "Internal",
            "file": (io.BytesIO(b"any-role-allowed"), "any-role.txt"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 403
    doc = DocumentRecord.query.filter_by(matter_id=matter.id, title="Any Role Upload").first()
    assert doc is None


def test_non_matter_member_cannot_upload_dms_documents(app_ctx):
    app = app_ctx
    owner = _seed_user("dms-owner-open-upload@example.com", role="senior_attorney")
    outsider = _seed_user("dms-outsider-open-upload@example.com", role="operations_staff")
    matter = _seed_matter(owner, "2026-PERM-DMS-OPEN-0001")
    db.session.add(MatterMember(matter_id=matter.id, user_id=owner.id, role_in_matter="Lead"))
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, outsider.id)
    response = client.post(
        f"/matters/{matter.id}/dms",
        data={
            "csrf_token": "test-csrf",
            "action": "upload_document",
            "title": "Open Upload",
            "document_type": "General",
            "confidentiality": "Internal",
            "file": (io.BytesIO(b"outsider-upload"), "outsider-upload.txt"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 403
    doc = DocumentRecord.query.filter_by(matter_id=matter.id, title="Open Upload").first()
    assert doc is None


def test_non_matter_member_receives_forbidden_on_dms_upload(app_ctx):
    app = app_ctx
    owner = _seed_user("dms-owner-open-upload-redirect@example.com", role="senior_attorney")
    outsider = _seed_user("dms-outsider-open-upload-redirect@example.com", role="operations_staff")
    matter = _seed_matter(owner, "2026-PERM-DMS-OPEN-0002")
    db.session.add(MatterMember(matter_id=matter.id, user_id=owner.id, role_in_matter="Lead"))
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, outsider.id)
    response = client.post(
        f"/matters/{matter.id}/dms",
        data={
            "csrf_token": "test-csrf",
            "action": "upload_document",
            "title": "Open Upload Redirect",
            "document_type": "General",
            "confidentiality": "Internal",
            "file": (io.BytesIO(b"outsider-upload-redirect"), "outsider-upload-redirect.txt"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 403
    doc = DocumentRecord.query.filter_by(matter_id=matter.id, title="Open Upload Redirect").first()
    assert doc is None


def test_unprivileged_matter_member_receives_forbidden_on_dms_upload(app_ctx):
    app = app_ctx
    owner = _seed_user("dms-owner-any-role-redirect@example.com", role="lawyer")
    analyst = _seed_user("dms-analyst-any-role-redirect@example.com", role="analyst")
    matter = _seed_matter(owner, "2026-PERM-DMS-ANY-0002")
    db.session.add(MatterMember(matter_id=matter.id, user_id=owner.id, role_in_matter="Lead"))
    db.session.add(MatterMember(matter_id=matter.id, user_id=analyst.id, role_in_matter="Team"))
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, analyst.id)
    response = client.post(
        f"/matters/{matter.id}/dms",
        data={
            "csrf_token": "test-csrf",
            "action": "upload_document",
            "title": "Any Role Redirect",
            "document_type": "General",
            "confidentiality": "Internal",
            "file": (io.BytesIO(b"any-role-redirect"), "any-role-redirect.txt"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 403
    doc = DocumentRecord.query.filter_by(matter_id=matter.id, title="Any Role Redirect").first()
    assert doc is None


def test_staff_cannot_upload_new_dms_versions_without_write_permission(app_ctx):
    app = app_ctx
    lawyer = _seed_user("dms-version-owner@example.com", role="lawyer")
    staff = _seed_user("dms-version-staff@example.com", role="staff")
    matter = _seed_matter(lawyer, "2026-PERM-DMS-VERS-0001")
    db.session.add(MatterMember(matter_id=matter.id, user_id=lawyer.id, role_in_matter="Lead"))
    db.session.add(MatterMember(matter_id=matter.id, user_id=staff.id, role_in_matter="Team"))
    db.session.commit()

    owner_client = app.test_client()
    _set_user_session(owner_client, lawyer.id)
    create_response = owner_client.post(
        f"/matters/{matter.id}/dms",
        data={
            "csrf_token": "test-csrf",
            "action": "upload_document",
            "title": "Version Guard",
            "document_type": "General",
            "confidentiality": "Internal",
            "file": (io.BytesIO(b"version-1"), "version-1.txt"),
        },
        content_type="multipart/form-data",
    )
    assert create_response.status_code == 302
    document = DocumentRecord.query.filter_by(matter_id=matter.id, title="Version Guard").first()
    assert document is not None

    staff_client = app.test_client()
    _set_user_session(staff_client, staff.id)
    blocked = staff_client.post(
        f"/documents/{document.id}/versions",
        data={
            "csrf_token": "test-csrf",
            "state": "draft",
            "file": (io.BytesIO(b"version-2"), "version-2.txt"),
        },
        content_type="multipart/form-data",
    )
    assert blocked.status_code == 403
    version_count = DocumentVersion.query.filter_by(document_id=document.id).count()
    assert version_count == 1


def test_staff_cannot_upload_legacy_matter_documents_without_write_permission(app_ctx):
    app = app_ctx
    lawyer = _seed_user("legacy-doc-owner@example.com", role="lawyer")
    staff = _seed_user("legacy-doc-staff@example.com", role="staff")
    matter = _seed_matter(lawyer, "2026-PERM-DMS-LEGACY-0001")
    db.session.add(MatterMember(matter_id=matter.id, user_id=lawyer.id, role_in_matter="Lead"))
    db.session.add(MatterMember(matter_id=matter.id, user_id=staff.id, role_in_matter="Team"))
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, staff.id)
    response = client.post(
        f"/matters/{matter.id}/documents",
        data={
            "csrf_token": "test-csrf",
            "category": "General",
            "lifecycle_stage": "Draft",
            "file": (io.BytesIO(b"legacy-bypass"), "legacy-bypass.txt"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 403
    assert DocumentFile.query.filter_by(matter_id=matter.id, uploaded_by=staff.id).first() is None


def test_staff_cannot_create_or_publish_workbench_documents_without_write_permission(app_ctx):
    app = app_ctx
    lawyer = _seed_user("workbench-owner@example.com", role="lawyer")
    staff = _seed_user("workbench-staff@example.com", role="staff")
    matter = _seed_matter(lawyer, "2026-PERM-DMS-WORKBENCH-0001")
    db.session.add(MatterMember(matter_id=matter.id, user_id=lawyer.id, role_in_matter="Lead"))
    db.session.add(MatterMember(matter_id=matter.id, user_id=staff.id, role_in_matter="Team"))
    db.session.commit()

    staff_client = app.test_client()
    _set_user_session(staff_client, staff.id)
    create_response = staff_client.post(
        f"/matters/{matter.id}/documents/workbench",
        data={
            "csrf_token": "test-csrf",
            "action": "create_document",
            "title": "Staff Workbench Draft",
        },
    )
    assert create_response.status_code == 403
    assert MatterWorkspaceDocument.query.filter_by(matter_id=matter.id, created_by=staff.id).first() is None

    owner_client = app.test_client()
    _set_user_session(owner_client, lawyer.id)
    owner_create = owner_client.post(
        f"/matters/{matter.id}/documents/workbench",
        data={
            "csrf_token": "test-csrf",
            "action": "create_document",
            "title": "Owner Workbench Draft",
        },
    )
    assert owner_create.status_code == 302
    draft = MatterWorkspaceDocument.query.filter_by(matter_id=matter.id, title="Owner Workbench Draft").first()
    assert draft is not None

    _set_user_session(staff_client, staff.id)
    publish_response = staff_client.post(
        f"/matters/{matter.id}/documents/workbench",
        data={
            "csrf_token": "test-csrf",
            "action": "publish_document",
            "document_id": draft.id,
        },
    )
    assert publish_response.status_code == 403
    assert DocumentRecord.query.filter_by(matter_id=matter.id, created_by=staff.id).first() is None


def test_dms_upload_sanitizes_nul_bytes_in_ocr_text(app_ctx):
    app = app_ctx
    owner = _seed_user("dms-ocr-owner@example.com", role="senior_attorney")
    matter = _seed_matter(owner, "2026-PERM-DMS-OCR-0001")
    db.session.add(MatterMember(matter_id=matter.id, user_id=owner.id, role_in_matter="Lead"))
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, owner.id)
    response = client.post(
        f"/matters/{matter.id}/dms",
        data={
            "csrf_token": "test-csrf",
            "action": "upload_document",
            "title": "NUL OCR Upload",
            "document_type": "General",
            "confidentiality": "Internal",
            "file": (io.BytesIO(b"PK\x03\x04hello\x00world\x00docx"), "nul-ocr.docx"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    doc = DocumentRecord.query.filter_by(matter_id=matter.id, title="NUL OCR Upload").first()
    assert doc is not None
    version = DocumentVersion.query.filter_by(document_id=doc.id, version_no=1).first()
    assert version is not None
    ocr = DocumentOCRText.query.filter_by(document_version_id=version.id).first()
    assert ocr is not None
    assert "\x00" not in (ocr.extracted_text or "")


def test_only_senior_attorney_can_delete_dms_documents(app_ctx):
    app = app_ctx
    director = _seed_user("dms-director@example.com", role="director")
    senior = _seed_user("dms-senior@example.com", role="senior_attorney")
    staff = _seed_user("dms-staff-delete@example.com", role="staff")
    admin = _seed_user("dms-admin@example.com", role="finance_cost_admin")
    junior = _seed_user("dms-junior-delete@example.com", role="junior_attorney")
    matter = _seed_matter(director, "2026-PERM-DMS-0002")
    db.session.add(MatterMember(matter_id=matter.id, user_id=director.id, role_in_matter="Lead"))
    db.session.add(MatterMember(matter_id=matter.id, user_id=senior.id, role_in_matter="Team"))
    db.session.add(MatterMember(matter_id=matter.id, user_id=staff.id, role_in_matter="Team"))
    db.session.add(MatterMember(matter_id=matter.id, user_id=admin.id, role_in_matter="Team"))
    db.session.add(MatterMember(matter_id=matter.id, user_id=junior.id, role_in_matter="Team"))
    db.session.commit()

    director_client = app.test_client()
    _set_user_session(director_client, director.id)
    upload_response = director_client.post(
        f"/matters/{matter.id}/dms",
        data={
            "csrf_token": "test-csrf",
            "action": "upload_document",
            "title": "Delete Candidate",
            "document_type": "General",
            "confidentiality": "Internal",
            "file": (io.BytesIO(b"delete me"), "delete-me.txt"),
        },
        content_type="multipart/form-data",
    )
    assert upload_response.status_code == 302
    delete_candidate = DocumentRecord.query.filter_by(matter_id=matter.id, title="Delete Candidate").first()
    assert delete_candidate is not None

    staff_client = app.test_client()
    _set_user_session(staff_client, staff.id)
    staff_delete = staff_client.post(
        f"/documents/{delete_candidate.id}/delete",
        data={"csrf_token": "test-csrf"},
    )
    assert staff_delete.status_code == 403
    assert db.session.get(DocumentRecord, delete_candidate.id) is not None

    director_delete = director_client.post(
        f"/documents/{delete_candidate.id}/delete",
        data={"csrf_token": "test-csrf"},
    )
    assert director_delete.status_code == 403
    assert db.session.get(DocumentRecord, delete_candidate.id) is not None

    junior_client = app.test_client()
    _set_user_session(junior_client, junior.id)
    junior_delete = junior_client.post(
        f"/documents/{delete_candidate.id}/delete",
        data={"csrf_token": "test-csrf"},
    )
    assert junior_delete.status_code == 403
    assert db.session.get(DocumentRecord, delete_candidate.id) is not None

    admin_client = app.test_client()
    _set_user_session(admin_client, admin.id)
    admin_delete = admin_client.post(
        f"/documents/{delete_candidate.id}/delete",
        data={"csrf_token": "test-csrf"},
    )
    assert admin_delete.status_code == 403
    assert db.session.get(DocumentRecord, delete_candidate.id) is not None

    senior_client = app.test_client()
    _set_user_session(senior_client, senior.id)
    senior_delete = senior_client.post(
        f"/documents/{delete_candidate.id}/delete",
        data={"csrf_token": "test-csrf"},
    )
    assert senior_delete.status_code == 302
    assert db.session.get(DocumentRecord, delete_candidate.id) is None

    # Finance/admin is intentionally blocked from delete; only senior attorneys can delete.
    admin_delete_after = admin_client.post(
        f"/documents/{delete_candidate.id}/delete",
        data={"csrf_token": "test-csrf"},
    )
    assert admin_delete_after.status_code in {403, 404}


def test_partner_role_alias_inherits_lawyer_permissions(app_ctx):
    app = app_ctx
    partner = _seed_user("crm-partner@example.com", role="partner", mfa_enabled=False)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, partner.id)
    response = client.post(
        "/crm/leads",
        data={"csrf_token": "test-csrf", "full_name": "Partner Allowed Lead"},
    )
    assert response.status_code == 302
    assert CRMLead.query.filter_by(full_name="Partner Allowed Lead").first() is not None


def test_staff_cannot_close_matter_via_summary_update(app_ctx):
    app = app_ctx
    lawyer = _seed_user("matter-close-owner@example.com", role="lawyer")
    staff = _seed_user("matter-close-staff@example.com", role="staff")
    matter = _seed_matter(lawyer, "2026-PERM-MATTER-CLOSE-0001")
    db.session.add(MatterMember(matter_id=matter.id, user_id=lawyer.id, role_in_matter="Lead"))
    db.session.add(MatterMember(matter_id=matter.id, user_id=staff.id, role_in_matter="Team"))
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, staff.id)
    response = client.post(
        f"/matters/{matter.id}/summary",
        data={
            "csrf_token": "test-csrf",
            "objective": "",
            "risk_level": "Medium",
            "budget_status": "On Track",
            "status": "Closed",
            "last_update_note": "",
            "outcome_summary": "Closed by staff",
        },
    )
    assert response.status_code == 403
    db.session.refresh(matter)
    assert matter.status == "Open"
    assert matter.closed_at is None


def test_operations_staff_cannot_mutate_casework_routes(app_ctx):
    app = app_ctx
    lawyer = _seed_user("casework-owner@example.com", role="lawyer")
    staff = _seed_user("casework-ops@example.com", role="operations_staff")
    matter = _seed_matter(lawyer, "2026-PERM-CASEWORK-0001")
    deadline = Deadline(
        matter_id=matter.id,
        title="Serve papers",
        due_at=dt.date.today(),
        is_critical=False,
        status="open",
        created_by=lawyer.id,
    )
    db.session.add(MatterMember(matter_id=matter.id, user_id=lawyer.id, role_in_matter="Lead"))
    db.session.add(MatterMember(matter_id=matter.id, user_id=staff.id, role_in_matter="Team"))
    db.session.add(deadline)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, staff.id)

    task_response = client.post(
        f"/matters/{matter.id}/tasks/new",
        data={"csrf_token": "test-csrf", "title": "Ops-created task"},
    )
    assert task_response.status_code == 403
    assert Task.query.filter_by(matter_id=matter.id, title="Ops-created task").first() is None

    timeline_response = client.post(
        f"/matters/{matter.id}/timeline",
        data={
            "csrf_token": "test-csrf",
            "title": "Ops hearing",
            "event_type": "Hearing",
            "event_date": dt.date.today().isoformat(),
        },
    )
    assert timeline_response.status_code == 403
    assert MatterTimelineEvent.query.filter_by(matter_id=matter.id, title="Ops hearing").first() is None

    party_response = client.post(
        f"/matters/{matter.id}/parties",
        data={"csrf_token": "test-csrf", "entity_name": "Ops Witness", "party_role": "Witness"},
    )
    assert party_response.status_code == 403
    assert MatterParty.query.filter_by(matter_id=matter.id).count() == 0

    ack_response = client.post(
        f"/deadlines/{deadline.id}/ack",
        data={"csrf_token": "test-csrf"},
    )
    assert ack_response.status_code == 403
    db.session.refresh(deadline)
    assert deadline.status == "open"
    assert deadline.acknowledged_by is None


def test_candidate_attorney_retains_casework_collaboration_access(app_ctx):
    app = app_ctx
    lawyer = _seed_user("candidate-owner@example.com", role="lawyer")
    candidate = _seed_user("candidate-casework@example.com", role="candidate_attorney")
    matter = _seed_matter(lawyer, "2026-PERM-CASEWORK-0002")
    db.session.add(MatterMember(matter_id=matter.id, user_id=lawyer.id, role_in_matter="Lead"))
    db.session.add(MatterMember(matter_id=matter.id, user_id=candidate.id, role_in_matter="Team"))
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, candidate.id)

    task_response = client.post(
        f"/matters/{matter.id}/tasks/new",
        data={"csrf_token": "test-csrf", "title": "Candidate task"},
    )
    assert task_response.status_code == 302
    assert Task.query.filter_by(matter_id=matter.id, title="Candidate task").first() is not None

    timeline_response = client.post(
        f"/matters/{matter.id}/timeline",
        data={
            "csrf_token": "test-csrf",
            "title": "Candidate milestone",
            "event_type": "Milestone",
            "event_date": dt.date.today().isoformat(),
        },
    )
    assert timeline_response.status_code == 302
    assert MatterTimelineEvent.query.filter_by(matter_id=matter.id, title="Candidate milestone").first() is not None


def test_candidate_attorney_cannot_access_billing_surfaces_without_billing_permission(app_ctx):
    app = app_ctx
    lawyer = _seed_user("billing-owner@example.com", role="lawyer")
    candidate = _seed_user("billing-candidate@example.com", role="candidate_attorney")
    matter = _seed_matter(lawyer, "2026-PERM-BILLING-READ-0001")
    invoice = Invoice(
        matter_id=matter.id,
        client_name=matter.client_name,
        period_start=dt.date.today().replace(day=1),
        period_end=dt.date.today(),
        status="approved",
        subtotal=1000.0,
        tax_total=150.0,
        total=1150.0,
        created_by=lawyer.id,
    )
    db.session.add(MatterMember(matter_id=matter.id, user_id=lawyer.id, role_in_matter="Lead"))
    db.session.add(MatterMember(matter_id=matter.id, user_id=candidate.id, role_in_matter="Team"))
    db.session.add(invoice)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, candidate.id)

    assert client.get("/billing/rates").status_code == 403
    assert client.get("/billing/invoices").status_code == 403
    assert client.get(f"/billing/invoices/{invoice.id}").status_code == 403
    assert client.get(f"/billing/invoices/{invoice.id}/pdf").status_code == 403
    assert client.get(f"/billing/invoices/{invoice.id}/tax-invoice").status_code == 403
    assert client.get(f"/billing/accounts/{matter.id}/statement").status_code == 403
