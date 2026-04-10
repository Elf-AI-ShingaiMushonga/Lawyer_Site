from __future__ import annotations

import csv
import datetime as dt
from intranet.timeutils import utc_now
import hashlib
import io
import json
import os
import time

import pytest
from flask import g
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql

from intranet.extensions import db
from intranet.jobs.worker import _handle_retention_archive_sweep
from intranet.mfa import _totp, generate_totp_secret, verify_totp
from intranet.models import (
    AuditLog,
    CRMLead,
    ConflictCheck,
    ConflictSemanticHit,
    ContractTemplate,
    DataResidencyPolicy,
    Deadline,
    DocumentFile,
    DocumentRecord,
    DocumentOCRText,
    DocumentTemplate,
    DocumentVersion,
    EmailCapture,
    EthicalWall,
    EthicalWallMatter,
    EthicalWallRule,
    ExpenseEntry,
    FirmSetting,
    IntakeForm,
    InvoiceAdjustment,
    Invoice,
    InvoiceLine,
    JobQueue,
    LegalHold,
    LeadQuote,
    PortalLinkToken,
    RetentionPolicy,
    SavedSearch,
    Matter,
    MatterClosingChecklistItem,
    MatterMember,
    MatterNote,
    MatterNoteACL,
    MatterPin,
    MatterRecentView,
    MatterTemplate,
    MatterTimelineEvent,
    Notification,
    PaymentAllocation,
    ProductionItem,
    ProductionSet,
    BatesRange,
    PortalMatterAccess,
    PortalMessage,
    PortalMessageThread,
    PortalUser,
    PracticeArea,
    RateCard,
    Section86Accrual,
    Section86Investment,
    Task,
    TaskAssignee,
    TaskTemplate,
    TimeEntry,
    TimeRoundingPolicy,
    TrustAccount,
    TrustApprovalRequest,
    TrustBankStatementImport,
    TrustClientLedger,
    TrustLedgerEntry,
    TrustReconciliationRun,
    TrustedDevice,
    UserSession,
    User,
)
from intranet.routes.billing import _payment_status_group_expr
from intranet.services.billing_engine import BillingEngine
from intranet.services.sa_practice import DEFAULT_SA_PRACTICE_AREAS
from intranet.services.trust_engine import TrustEngine


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


def _csrf_token_for_path(client, path: str) -> str:
    client.get(path)
    with client.session_transaction() as sess:
        return sess.get("_csrf_token") or "test-csrf"


def _seed_user(email: str, role: str = "partner", *, mfa_enabled: bool = False) -> User:
    user = User(
        email=email,
        full_name=email.split("@", 1)[0],
        role=role,
        password_hash="x",
        is_active=True,
        mfa_enabled=mfa_enabled,
    )
    user.set_password("TestPassword123!")
    db.session.add(user)
    db.session.flush()
    return user


def _seed_matter(owner: User, matter_no: str, title: str, client: str) -> Matter:
    matter = Matter(
        matter_no=matter_no,
        title=title,
        client_name=client,
        status="Open",
        created_by=owner.id,
        opened_at=utc_now(),
        last_updated_at=utc_now(),
    )
    db.session.add(matter)
    db.session.flush()
    return matter


def _apply_ethics_denial(user: User, matter: Matter) -> None:
    wall = EthicalWall(name=f"Wall-{matter.id}", created_by=user.id, is_active=True)
    db.session.add(wall)
    db.session.flush()
    db.session.add(EthicalWallMatter(wall_id=wall.id, matter_id=matter.id))
    db.session.add(EthicalWallRule(wall_id=wall.id, user_id=user.id, is_deny=True, is_active=True))


def test_matter_list_honors_ethical_walls(app_ctx):
    app = app_ctx
    user = _seed_user("wall-list@example.com")
    denied = _seed_matter(user, "2026-WALL-LIST-001", "Denied Matter", "Denied Client")
    allowed = _seed_matter(user, "2026-WALL-LIST-002", "Allowed Matter", "Allowed Client")
    db.session.add(MatterMember(matter_id=denied.id, user_id=user.id, role_in_matter="Team"))
    db.session.add(MatterMember(matter_id=allowed.id, user_id=user.id, role_in_matter="Team"))
    _apply_ethics_denial(user, denied)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.get("/matters")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Allowed Matter" in body
    assert "Denied Matter" not in body


def test_matter_note_acl_filters_note_visibility(app_ctx):
    app = app_ctx
    owner = _seed_user("note-owner@example.com")
    viewer = _seed_user("note-viewer@example.com")
    matter = _seed_matter(owner, "2026-NOTE-0001", "Notes Matter", "Client A")
    db.session.add(MatterMember(matter_id=matter.id, user_id=owner.id, role_in_matter="Lead"))
    db.session.add(MatterMember(matter_id=matter.id, user_id=viewer.id, role_in_matter="Team"))
    db.session.flush()

    public_note = MatterNote(matter_id=matter.id, body="Visible note", created_by=owner.id)
    private_note = MatterNote(matter_id=matter.id, body="Hidden note", created_by=owner.id)
    db.session.add_all([public_note, private_note])
    db.session.flush()
    db.session.add(MatterNoteACL(note_id=private_note.id, user_id=owner.id, can_read=True, can_edit=True))
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, viewer.id)
    response = client.get(f"/matters/{matter.id}/notes")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Visible note" in body
    assert "Hidden note" not in body


def test_restricted_note_voice_attachment_is_not_searchable_or_downloadable(app_ctx, tmp_path):
    app = app_ctx
    owner = _seed_user("note-voice-owner@example.com")
    viewer = _seed_user("note-voice-viewer@example.com")
    matter = _seed_matter(owner, "2026-NOTE-VOICE-0001", "Voice ACL Matter", "Client A")
    db.session.add(MatterMember(matter_id=matter.id, user_id=owner.id, role_in_matter="Lead"))
    db.session.add(MatterMember(matter_id=matter.id, user_id=viewer.id, role_in_matter="Team"))
    db.session.commit()

    app.config["UPLOAD_DIR"] = str(tmp_path)
    owner_client = app.test_client()
    _set_user_session(owner_client, owner.id)
    create_response = owner_client.post(
        f"/matters/{matter.id}/notes",
        data={
            "csrf_token": "test-csrf",
            "body": "Hidden voice note",
            "acl_emails": owner.email,
            "voice_note": (io.BytesIO(b"voice-note"), "private-voice-note.mp3"),
        },
        content_type="multipart/form-data",
    )
    assert create_response.status_code == 302
    voice_doc = DocumentFile.query.filter_by(matter_id=matter.id, category="Voice Note").first()
    assert voice_doc is not None

    viewer_client = app.test_client()
    _set_user_session(viewer_client, viewer.id)
    search_response = viewer_client.get("/search?q=voice")
    assert search_response.status_code == 200
    assert "private-voice-note.mp3" not in search_response.get_data(as_text=True)

    download_response = viewer_client.get(f"/documents/{voice_doc.id}/download")
    assert download_response.status_code == 403


def test_matter_note_voice_upload_creates_document_file(app_ctx, tmp_path):
    app = app_ctx
    owner = _seed_user("voice-owner@example.com")
    matter = _seed_matter(owner, "2026-VOICE-0001", "Voice Notes Matter", "Voice Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=owner.id, role_in_matter="Lead"))
    db.session.commit()

    app.config["UPLOAD_DIR"] = str(tmp_path)
    client = app.test_client()
    _set_user_session(client, owner.id)

    response = client.post(
        f"/matters/{matter.id}/notes",
        data={
            "csrf_token": "test-csrf",
            "body": "",
            "voice_note": (io.BytesIO(b"voice-note-bytes"), "matter_note.webm"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert response.status_code == 302

    note = MatterNote.query.filter_by(matter_id=matter.id).order_by(MatterNote.id.desc()).first()
    assert note is not None
    assert "Voice note" in note.body

    voice_doc = DocumentFile.query.filter_by(matter_id=matter.id, category="Voice Note").order_by(DocumentFile.id.desc()).first()
    assert voice_doc is not None
    assert voice_doc.owner_name == f"note:{note.id}"
    assert os.path.isfile(os.path.join(app.config["UPLOAD_DIR"], voice_doc.stored_filename))


def test_crm_quote_creation_and_status_flow(app_ctx):
    app = app_ctx
    user = _seed_user("quote-owner@example.com", role="lawyer", mfa_enabled=True)
    lead = CRMLead(
        full_name="Quote Prospect",
        organization="Prospect Co",
        email="prospect@example.com",
        stage="qualified",
        created_by=user.id,
        assigned_to=user.id,
    )
    db.session.add(lead)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)

    create_response = client.post(
        f"/crm/leads/{lead.id}",
        data={
            "csrf_token": "test-csrf",
            "action": "quote_create",
            "quote_title": "Initial Litigation Quote",
            "fee_model": "fixed",
            "currency": "ZAR",
            "estimated_amount": "25000",
            "disbursement_estimate": "1200",
            "tax_rate": "15",
            "scope_summary": "Urgent pleadings and case strategy memo.",
            "assumptions": "Court filing fees billed at cost.",
            "valid_until": (dt.date.today() + dt.timedelta(days=14)).isoformat(),
        },
        follow_redirects=False,
    )
    assert create_response.status_code == 302

    db.session.refresh(lead)
    assert lead.stage == "proposal"
    quote = LeadQuote.query.filter_by(lead_id=lead.id).first()
    assert quote is not None
    assert quote.status == "draft"

    sent_response = client.post(
        f"/crm/quotes/{quote.id}/status",
        data={"csrf_token": "test-csrf", "status": "sent"},
        follow_redirects=False,
    )
    assert sent_response.status_code == 302
    db.session.refresh(quote)
    assert quote.status == "sent"
    assert quote.sent_at is not None

    accepted_response = client.post(
        f"/crm/quotes/{quote.id}/status",
        data={"csrf_token": "test-csrf", "status": "accepted", "status_note": "Client approved quote terms."},
        follow_redirects=False,
    )
    assert accepted_response.status_code == 302
    db.session.refresh(quote)
    db.session.refresh(lead)
    assert quote.status == "accepted"
    assert quote.decided_by == user.id
    assert lead.stage == "retained"


def test_billing_invoice_list_respects_visible_matter_scope(app_ctx):
    app = app_ctx
    user = _seed_user("billing-scope@example.com")
    allowed = _seed_matter(user, "2026-BILL-0001", "Allowed Billing Matter", "Client 1")
    denied = _seed_matter(user, "2026-BILL-0002", "Denied Billing Matter", "Client 2")
    db.session.add(MatterMember(matter_id=allowed.id, user_id=user.id, role_in_matter="Team"))
    db.session.add(MatterMember(matter_id=denied.id, user_id=user.id, role_in_matter="Team"))
    _apply_ethics_denial(user, denied)
    db.session.flush()

    db.session.add(
        Invoice(
            matter_id=allowed.id,
            client_name=allowed.client_name,
            period_start=dt.date(2026, 1, 1),
            period_end=dt.date(2026, 1, 31),
            status="approved",
            subtotal=100.0,
            tax_total=15.0,
            total=115.0,
            created_by=user.id,
        )
    )
    db.session.add(
        Invoice(
            matter_id=denied.id,
            client_name=denied.client_name,
            period_start=dt.date(2026, 1, 1),
            period_end=dt.date(2026, 1, 31),
            status="approved",
            subtotal=200.0,
            tax_total=30.0,
            total=230.0,
            created_by=user.id,
        )
    )
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.get("/billing/invoices")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Allowed Billing Matter" in body
    assert "Denied Billing Matter" not in body


def test_portal_visibility_levels_are_enforced(app_ctx):
    app = app_ctx
    admin = _seed_user("portal-admin@example.com", role="admin", mfa_enabled=True)
    matter = _seed_matter(admin, "2026-PORTAL-0001", "Portal Matter", "Portal Client")

    portal_user = PortalUser(email="client@example.com", full_name="Client User", password_hash="x", is_active=True)
    portal_user.set_password("ClientPassword123!")
    db.session.add(portal_user)
    db.session.flush()

    db.session.add(
        PortalMatterAccess(
            portal_user_id=portal_user.id,
            matter_id=matter.id,
            visibility_level="summary_only",
            granted_by=admin.id,
        )
    )
    db.session.add(
        Invoice(
            matter_id=matter.id,
            client_name=matter.client_name,
            period_start=dt.date(2026, 1, 1),
            period_end=dt.date(2026, 1, 31),
            status="approved",
            subtotal=300.0,
            tax_total=45.0,
            total=345.0,
            created_by=admin.id,
        )
    )
    db.session.commit()

    client = app.test_client()
    _set_portal_session(client, portal_user.id)

    detail = client.get(f"/portal/matters/{matter.id}")
    assert detail.status_code == 200

    blocked_messages = client.post(
        "/portal/messages",
        data={
            "csrf_token": "test-csrf",
            "matter_id": matter.id,
            "subject": "Blocked",
            "body": "Should fail",
        },
        follow_redirects=False,
    )
    assert blocked_messages.status_code == 403

    blocked_upload = client.post(
        "/portal/uploads",
        data={"csrf_token": "test-csrf", "matter_id": matter.id},
        follow_redirects=False,
    )
    assert blocked_upload.status_code == 403

    db.session.query(PortalMatterAccess).filter_by(portal_user_id=portal_user.id, matter_id=matter.id).update(
        {"visibility_level": "full_curated"}
    )
    db.session.commit()

    allowed_messages = client.post(
        "/portal/messages",
        data={
            "csrf_token": "test-csrf",
            "matter_id": matter.id,
            "subject": "Allowed",
            "body": "Now allowed",
        },
        follow_redirects=False,
    )
    assert allowed_messages.status_code == 302
    assert PortalMessage.query.count() == 1


def test_internal_active_matter_prefills_cross_module_forms(app_ctx):
    app = app_ctx
    user = _seed_user("active-matter-user@example.com", role="partner")
    matter = _seed_matter(user, "2026-ACTIVE-0001", "Active Context Matter", "Context Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    lead = CRMLead(
        full_name="Context Lead",
        organization="Context Org",
        email="context-lead@example.com",
        stage="new",
        created_by=user.id,
        assigned_to=user.id,
    )
    db.session.add(lead)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)

    detail = client.get(f"/time/timers?matter_id={matter.id}")
    assert detail.status_code == 200
    with client.session_transaction() as sess:
        assert sess.get("active_matter_id") == matter.id

    for route in ["/time/timers", "/time/entries", "/billing/invoices", f"/crm/leads/{lead.id}"]:
        response = client.get(route)
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert f'value="{matter.id}" selected' in body

    crm_body = client.get(f"/crm/leads/{lead.id}").get_data(as_text=True)
    assert "lead-intake-matter-id" not in crm_body
    assert "engage-matter-id" not in crm_body


def test_portal_message_thread_reply_uses_thread_matter_context(app_ctx):
    app = app_ctx
    admin = _seed_user("portal-thread-admin@example.com", role="admin")
    matter = _seed_matter(admin, "2026-PORTAL-THREAD-1", "Thread Context Matter", "Portal Thread Client")
    other = _seed_matter(admin, "2026-PORTAL-THREAD-2", "Thread Context Other", "Portal Thread Client")
    portal_user = PortalUser(email="portal-thread-user@example.com", full_name="Thread Portal User", password_hash="x", is_active=True)
    portal_user.set_password("PortalPassword123!")
    db.session.add(portal_user)
    db.session.flush()
    db.session.add(
        PortalMatterAccess(
            portal_user_id=portal_user.id,
            matter_id=matter.id,
            visibility_level="full_curated",
            granted_by=admin.id,
        )
    )
    db.session.add(
        PortalMatterAccess(
            portal_user_id=portal_user.id,
            matter_id=other.id,
            visibility_level="full_curated",
            granted_by=admin.id,
        )
    )
    thread = PortalMessageThread(matter_id=matter.id, subject="Initial Thread", created_by_portal_user_id=portal_user.id)
    db.session.add(thread)
    db.session.commit()

    client = app.test_client()
    _set_portal_session(client, portal_user.id)

    reply = client.post(
        "/portal/messages",
        data={
            "csrf_token": "test-csrf",
            "thread_id": thread.id,
            "body": "Reply without matter id.",
        },
        follow_redirects=False,
    )
    assert reply.status_code == 302
    created = PortalMessage.query.order_by(PortalMessage.id.desc()).first()
    assert created is not None
    assert created.thread_id == thread.id
    with client.session_transaction() as sess:
        assert sess.get("portal_active_matter_id") == matter.id

    mismatch = client.post(
        "/portal/messages",
        data={
            "csrf_token": "test-csrf",
            "matter_id": other.id,
            "thread_id": thread.id,
            "body": "This should not be accepted.",
        },
        follow_redirects=False,
    )
    assert mismatch.status_code == 302
    assert PortalMessage.query.count() == 1


def test_portal_active_matter_prefills_message_upload_and_link_forms(app_ctx):
    app = app_ctx
    admin = _seed_user("portal-prefill-admin@example.com", role="admin")
    matter = _seed_matter(admin, "2026-PORTAL-PREFILL-1", "Portal Prefill Matter", "Portal Prefill Client")
    portal_user = PortalUser(email="portal-prefill-user@example.com", full_name="Portal Prefill User", password_hash="x", is_active=True)
    portal_user.set_password("PortalPassword123!")
    db.session.add(portal_user)
    db.session.flush()
    db.session.add(
        PortalMatterAccess(
            portal_user_id=portal_user.id,
            matter_id=matter.id,
            visibility_level="full_curated",
            granted_by=admin.id,
        )
    )
    db.session.commit()

    client = app.test_client()
    _set_portal_session(client, portal_user.id)
    detail = client.get(f"/portal/matters/{matter.id}")
    assert detail.status_code == 200

    for route in ["/portal/messages", "/portal/uploads", "/portal/links"]:
        response = client.get(route)
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert f'value="{matter.id}" selected' in body


def test_portal_payment_prefills_outstanding_amount(app_ctx):
    app = app_ctx
    admin = _seed_user("portal-pay-admin@example.com", role="admin")
    matter = _seed_matter(admin, "2026-PORTAL-PAY-1", "Portal Payment Matter", "Portal Payment Client")
    portal_user = PortalUser(email="portal-pay-user@example.com", full_name="Portal Payment User", password_hash="x", is_active=True)
    portal_user.set_password("PortalPassword123!")
    db.session.add(portal_user)
    db.session.flush()
    db.session.add(
        PortalMatterAccess(
            portal_user_id=portal_user.id,
            matter_id=matter.id,
            visibility_level="full_curated",
            granted_by=admin.id,
        )
    )
    invoice = Invoice(
        matter_id=matter.id,
        client_name=matter.client_name,
        period_start=dt.date(2026, 1, 1),
        period_end=dt.date(2026, 1, 31),
        status="approved",
        subtotal=500.0,
        tax_total=75.0,
        total=575.0,
        created_by=admin.id,
    )
    db.session.add(invoice)
    db.session.flush()
    db.session.add(PaymentAllocation(invoice_id=invoice.id, amount=175.0, status="settled", created_by=admin.id))
    db.session.add(PaymentAllocation(invoice_id=invoice.id, amount=50.0, status="pending", created_by=admin.id))
    db.session.commit()

    client = app.test_client()
    _set_portal_session(client, portal_user.id)
    response = client.get(f"/portal/payments/{invoice.id}")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Outstanding 400.00" in body
    assert 'id="portal-payment-amount" name="amount" type="number" step="0.01" value="400.00"' in body


def test_portal_login_enforces_optional_mfa_when_enabled(app_ctx):
    app = app_ctx
    portal_user = PortalUser(
        email="portal-mfa@example.com",
        full_name="Portal MFA User",
        password_hash="x",
        is_active=True,
        mfa_enabled=True,
        mfa_secret=generate_totp_secret(),
    )
    portal_user.set_password("PortalPassword123!")
    db.session.add(portal_user)
    db.session.commit()

    client = app.test_client()
    csrf_token = _csrf_token_for_path(client, "/portal/login")
    no_mfa = client.post(
        "/portal/login",
        data={
            "csrf_token": csrf_token,
            "email": portal_user.email,
            "password": "PortalPassword123!",
        },
        follow_redirects=False,
    )
    assert no_mfa.status_code == 302
    assert "/portal/login" in (no_mfa.headers.get("Location") or "")

    valid_code = _totp(portal_user.mfa_secret, int(time.time() // 30))
    csrf_token = _csrf_token_for_path(client, "/portal/login")
    with_mfa = client.post(
        "/portal/login",
        data={
            "csrf_token": csrf_token,
            "email": portal_user.email,
            "password": "PortalPassword123!",
            "mfa_code": valid_code,
        },
        follow_redirects=False,
    )
    assert with_mfa.status_code == 302
    assert "/portal/matters" in (with_mfa.headers.get("Location") or "")


def test_verify_totp_handles_legacy_secret_format_and_clock_skew(monkeypatch):
    # Freeze counter so we can assert deterministic skew-window behavior.
    fixed_counter = 100_000
    monkeypatch.setattr("intranet.mfa.time.time", lambda: fixed_counter * 30)

    legacy_secret = "NB2W45DFOIZA====NB2W45DFOIZA===="
    normalized_secret = "NB2W45DFOIZANB2W45DFOIZA"
    code = _totp(normalized_secret, fixed_counter - 2)

    assert verify_totp(legacy_secret, code)


def test_verify_totp_rejects_malformed_secret_without_crashing():
    assert verify_totp("not-a-base32-secret", "123456") is False


def test_time_offline_sync_creates_entries_and_skips_duplicates(app_ctx):
    app = app_ctx
    user = _seed_user("offline-time@example.com")
    matter = _seed_matter(user, "2026-OFFLINE-0001", "Offline Time Matter", "Offline Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    start = "2026-02-10T09:00:00"
    end = "2026-02-10T10:30:00"
    payload = {
        "entries": [
            {
                "matter_id": matter.id,
                "start_at": start,
                "end_at": end,
                "narrative": "Offline capture block",
                "task_code": "TASK1",
                "activity_code": "A100",
                "is_billable": True,
            },
            {
                "matter_id": matter.id,
                "start_at": start,
                "end_at": end,
                "narrative": "Offline capture block",
                "task_code": "TASK1",
                "activity_code": "A100",
                "is_billable": True,
            },
        ]
    }
    resp = client.post(
        "/time/offline-sync",
        json=payload,
        headers={"X-CSRF-Token": "test-csrf"},
        follow_redirects=False,
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert body["ok"] is True
    assert int(body["created"]) == 1
    assert int(body["skipped"]) == 1
    assert TimeEntry.query.filter_by(user_id=user.id, matter_id=matter.id).count() == 1


def test_trust_maker_checker_for_high_value_disbursement(app_ctx):
    app = app_ctx
    maker = _seed_user("trust-maker@example.com", role="lawyer", mfa_enabled=True)
    checker = _seed_user("trust-checker@example.com", role="lawyer", mfa_enabled=True)
    account = TrustAccount(name="Maker Checker Trust", currency="ZAR", is_active=True)
    db.session.add(account)
    db.session.flush()
    ledger = TrustClientLedger(
        trust_account_id=account.id,
        client_name="Maker Checker Client",
        matter_id=None,
        current_balance=0.0,
    )
    db.session.add(ledger)
    db.session.commit()

    seeded = TrustEngine.post_transaction(
        {
            "trust_account_id": account.id,
            "client_ledger_id": ledger.id,
            "entry_type": "deposit",
            "amount": 50000.0,
            "currency": "ZAR",
            "created_by": maker.id,
        }
    )
    assert seeded.posted is True

    client = app.test_client()
    _set_user_session(client, maker.id)
    queue_resp = client.post(
        "/trust/disbursements",
        data={
            "csrf_token": "test-csrf",
            "trust_account_id": account.id,
            "client_ledger_id": ledger.id,
            "amount": 20000.0,
            "currency": "ZAR",
            "description": "High value payout",
        },
        follow_redirects=False,
    )
    assert queue_resp.status_code == 302
    approval = TrustApprovalRequest.query.order_by(TrustApprovalRequest.id.desc()).first()
    assert approval is not None
    assert approval.status == "pending"
    assert approval.requested_by == maker.id
    assert TrustLedgerEntry.query.filter_by(client_ledger_id=ledger.id, entry_type="disbursement").count() == 0

    checker_client = app.test_client()
    _set_user_session(checker_client, checker.id)
    approve_resp = checker_client.post(
        f"/trust/approvals/{approval.id}/decision",
        data={"csrf_token": "test-csrf", "decision": "approve"},
        follow_redirects=False,
    )
    assert approve_resp.status_code == 302
    db.session.refresh(approval)
    assert approval.status == "executed"
    assert approval.approved_by == checker.id
    assert TrustLedgerEntry.query.filter_by(client_ledger_id=ledger.id, entry_type="disbursement").count() == 1


def test_trust_reconciliation_uses_signed_ledger_balances(app_ctx):
    app = app_ctx
    reviewer = _seed_user("trust-reviewer@example.com", role="lawyer", mfa_enabled=True)
    account = TrustAccount(name="Main Trust", currency="ZAR", is_active=True)
    db.session.add(account)
    db.session.flush()
    ledger = TrustClientLedger(
        trust_account_id=account.id,
        client_name="Trust Client",
        matter_id=None,
        current_balance=0.0,
    )
    db.session.add(ledger)
    db.session.commit()

    TrustEngine.post_transaction(
        {
            "trust_account_id": account.id,
            "client_ledger_id": ledger.id,
            "entry_type": "deposit",
            "amount": 1000.0,
            "currency": "ZAR",
            "created_by": reviewer.id,
        }
    )
    TrustEngine.post_transaction(
        {
            "trust_account_id": account.id,
            "client_ledger_id": ledger.id,
            "entry_type": "disbursement",
            "amount": 200.0,
            "currency": "ZAR",
            "created_by": reviewer.id,
        }
    )

    client = app.test_client()
    _set_user_session(client, reviewer.id)
    response = client.post(
        "/trust/reconciliations",
        data={
            "csrf_token": "test-csrf",
            "trust_account_id": account.id,
            "period_start": "2026-01-01T00:00:00",
            "period_end": "2026-12-31T23:59:59",
            "bank_closing_balance": 800.0,
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    run = TrustReconciliationRun.query.order_by(TrustReconciliationRun.id.desc()).first()
    assert run is not None
    assert run.status == "balanced"
    assert float(run.ledger_closing_balance or 0.0) == 800.0


def test_trust_statement_import_links_to_reconciliation_and_section86_automation(app_ctx):
    app = app_ctx
    reviewer = _seed_user("trust-statement-import@example.com", role="lawyer", mfa_enabled=True)
    account = TrustAccount(name="Import Trust", currency="ZAR", is_active=True)
    db.session.add(account)
    db.session.flush()
    ledger = TrustClientLedger(
        trust_account_id=account.id,
        client_name="Import Client",
        matter_id=None,
        current_balance=0.0,
    )
    db.session.add(ledger)
    db.session.commit()

    TrustEngine.post_transaction(
        {
            "trust_account_id": account.id,
            "client_ledger_id": ledger.id,
            "entry_type": "deposit",
            "amount": 1000.0,
            "currency": "ZAR",
            "created_by": reviewer.id,
        }
    )
    TrustEngine.post_transaction(
        {
            "trust_account_id": account.id,
            "client_ledger_id": ledger.id,
            "entry_type": "disbursement",
            "amount": 200.0,
            "currency": "ZAR",
            "created_by": reviewer.id,
        }
    )

    client = app.test_client()
    _set_user_session(client, reviewer.id)
    csv_payload = "\n".join(
        [
            "date,description,debit,credit,balance",
            "2026-01-01,Opening,0,1000,1000",
            "2026-01-02,Payout,200,0,800",
        ]
    )
    import_resp = client.post(
        "/trust/statements/import",
        data={
            "csrf_token": "test-csrf",
            "trust_account_id": account.id,
            "statement_label": "Jan 2026",
            "statement_file": (io.BytesIO(csv_payload.encode("utf-8")), "statement.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert import_resp.status_code == 302
    statement = TrustBankStatementImport.query.order_by(TrustBankStatementImport.id.desc()).first()
    assert statement is not None
    assert statement.row_count == 2

    recon_resp = client.post(
        "/trust/reconciliations",
        data={
            "csrf_token": "test-csrf",
            "trust_account_id": account.id,
            "bank_statement_import_id": statement.id,
            "period_start": "2026-01-01T00:00:00",
            "period_end": "2026-12-31T23:59:59",
        },
        follow_redirects=False,
    )
    assert recon_resp.status_code == 302
    run = TrustReconciliationRun.query.order_by(TrustReconciliationRun.id.desc()).first()
    assert run is not None
    assert run.bank_statement_import_id == statement.id
    assert run.status == "balanced"

    create_investment = client.post(
        "/trust/section86",
        data={
            "csrf_token": "test-csrf",
            "action": "create",
            "trust_account_id": account.id,
            "client_ledger_id": ledger.id,
            "investment_ref": "S86-TEST-001",
            "principal_amount": 36500.0,
            "annual_rate_percent": 10.0,
            "opened_on": "2026-01-01",
            "status": "active",
        },
        follow_redirects=False,
    )
    assert create_investment.status_code == 302
    investment = Section86Investment.query.filter_by(investment_ref="S86-TEST-001").first()
    assert investment is not None

    automate_resp = client.post(
        "/trust/section86",
        data={
            "csrf_token": "test-csrf",
            "action": "automate",
            "as_of_date": "2026-01-10",
            "withholding_percent": "15",
            "post_to_ledger": "1",
        },
        follow_redirects=False,
    )
    assert automate_resp.status_code == 302
    accrual = Section86Accrual.query.filter_by(investment_id=investment.id, accrual_date=dt.date(2026, 1, 10)).first()
    assert accrual is not None
    assert float(accrual.net_interest_amount or 0.0) > 0


def test_trust_cashbook_and_report_exports(app_ctx):
    app = app_ctx
    reviewer = _seed_user("trust-exports@example.com", role="lawyer", mfa_enabled=True)
    account = TrustAccount(name="Export Trust", currency="ZAR", is_active=True)
    db.session.add(account)
    db.session.flush()
    ledger = TrustClientLedger(
        trust_account_id=account.id,
        client_name="Export Client",
        matter_id=None,
        current_balance=0.0,
    )
    db.session.add(ledger)
    db.session.commit()
    TrustEngine.post_transaction(
        {
            "trust_account_id": account.id,
            "client_ledger_id": ledger.id,
            "entry_type": "deposit",
            "amount": 600.0,
            "currency": "ZAR",
            "created_by": reviewer.id,
        }
    )

    client = app.test_client()
    _set_user_session(client, reviewer.id)
    cashbook = client.get("/trust/cashbook?format=csv")
    assert cashbook.status_code == 200
    assert cashbook.mimetype == "text/csv"
    assert "trust_account_id,trust_account_name,currency,deposit_total" in cashbook.get_data(as_text=True)

    trial_balance = client.get("/trust/reports/trial-balance?format=csv")
    assert trial_balance.status_code == 200
    assert trial_balance.mimetype == "text/csv"
    assert "trust_account_id,trust_account_name,currency,cashbook_total" in trial_balance.get_data(as_text=True)

    auditor = client.get("/trust/reports/auditor?format=json")
    assert auditor.status_code == 200
    assert auditor.mimetype == "application/json"
    payload = auditor.get_json()
    assert float(payload["cashbook_total"]) >= 600.0

    section86 = client.get("/trust/section86/report?format=csv")
    assert section86.status_code == 200
    assert section86.mimetype == "text/csv"
    assert "investment_id,investment_ref,trust_account_id" in section86.get_data(as_text=True)


def test_billing_engine_uses_matter_client_on_expense_only_invoice(seed_user_matter):
    user = seed_user_matter["user"]
    matter = seed_user_matter["matter"]
    db.session.add(
        ExpenseEntry(
            matter_id=matter.id,
            user_id=user.id,
            amount=125.0,
            currency="ZAR",
            category="Travel",
            incurred_on=dt.date(2026, 2, 5),
            status="approved",
        )
    )
    db.session.commit()

    result = BillingEngine.generate_invoice(
        matter.id,
        (dt.date(2026, 2, 1), dt.date(2026, 2, 28)),
        created_by=user.id,
    )
    invoice = db.session.get(Invoice, result.invoice_id)

    assert result.invoice_id is not None
    assert invoice is not None
    assert invoice.client_name == matter.client_name
    assert invoice.created_by == user.id


def test_intake_creation_queues_semantic_conflict_scan(app_ctx):
    app = app_ctx
    user = _seed_user("conflict-queue@example.com")
    matter = _seed_matter(user, "2026-CONFLICT-QUEUE-01", "Queued Conflict Matter", "Queue Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    lead = CRMLead(
        full_name="Queue Lead",
        organization="Acme Queue Holdings",
        email="queue.lead@example.com",
        stage="qualified",
        created_by=user.id,
    )
    db.session.add(lead)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    csrf = _csrf_token_for_path(client, f"/crm/leads/{lead.id}")
    response = client.post(
        f"/crm/leads/{lead.id}",
        data={
            "csrf_token": csrf,
            "action": "intake",
            "matter_id": matter.id,
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    intake = IntakeForm.query.filter_by(lead_id=lead.id).order_by(IntakeForm.created_at.desc()).first()
    assert intake is not None

    conflict = ConflictCheck.query.filter_by(intake_form_id=intake.id).order_by(ConflictCheck.created_at.desc()).first()
    assert conflict is not None
    assert conflict.status == "pending"
    payload = json.loads(conflict.result_json or "{}")
    assert payload.get("semantic_status") == "queued"

    queued_job = (
        JobQueue.query.filter(
            JobQueue.job_type == "conflict_semantic_scan",
            JobQueue.payload_json.like(f'%"conflict_check_id": {conflict.id}%'),
        )
        .order_by(JobQueue.created_at.desc())
        .first()
    )
    assert queued_job is not None


def test_conflict_report_export_route_returns_csv(app_ctx):
    app = app_ctx
    user = _seed_user("conflict-export@example.com")
    matter = _seed_matter(user, "2026-CONF-0001", "Conflict Export Matter", "Conflict Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    intake = IntakeForm(lead_id=None, matter_id=matter.id, data_json="{}", created_by=user.id)
    db.session.add(intake)
    db.session.flush()
    conflict = ConflictCheck(
        intake_form_id=intake.id,
        status="potential_conflict",
        result_json=json.dumps({"matches": ["entity:Acme"]}),
        override_required=True,
    )
    db.session.add(conflict)
    db.session.flush()
    record = DocumentRecord(
        matter_id=matter.id,
        title="Conflict Evidence Memo",
        document_type="Memo",
        confidentiality="Internal",
        created_by=user.id,
    )
    db.session.add(record)
    db.session.flush()
    version = DocumentVersion(
        document_id=record.id,
        version_no=1,
        original_filename="conflict-evidence.txt",
        stored_filename="conflict-evidence.txt",
        sha256="conflict-evidence-sha",
        state="final",
        uploaded_by=user.id,
    )
    db.session.add(version)
    db.session.flush()
    ocr = DocumentOCRText(document_version_id=version.id, extracted_text="Acme Holdings appears in historical memo.")
    db.session.add(ocr)
    db.session.flush()
    db.session.add(
        ConflictSemanticHit(
            conflict_check_id=conflict.id,
            document_ocr_text_id=ocr.id,
            document_version_id=version.id,
            matter_id=matter.id,
            candidate_entity="Acme Holdings",
            matched_phrase="acme holdings",
            match_reason="semantic_vector",
            similarity_score=0.78,
            lexical_score=0.41,
            vector_score=0.86,
            excerpt="Acme Holdings appears in historical memo.",
            semantic_rank=1,
        )
    )
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.get(f"/crm/conflicts/{conflict.id}/export")

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    payload = response.get_data(as_text=True)
    assert "check_id,status,intake_id,matches" in payload
    assert "entity:Acme" in payload
    assert "semantic_hits" in payload
    assert "Acme Holdings -> matter" in payload


def test_billing_adjustments_and_ar_aging_routes(app_ctx):
    app = app_ctx
    lawyer = _seed_user("billing-adjust@example.com", role="lawyer", mfa_enabled=True)
    matter = _seed_matter(lawyer, "2026-AR-0001", "AR Matter", "AR Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=lawyer.id, role_in_matter="Lead"))
    db.session.flush()
    invoice = Invoice(
        matter_id=matter.id,
        client_name=matter.client_name,
        period_start=dt.date(2026, 1, 1),
        period_end=dt.date(2026, 1, 31),
        status="approved",
        subtotal=1000.0,
        tax_total=150.0,
        total=1150.0,
        created_by=lawyer.id,
    )
    db.session.add(invoice)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, lawyer.id)
    adjust_resp = client.post(
        f"/billing/invoices/{invoice.id}/adjust",
        data={
            "csrf_token": "test-csrf",
            "adjustment_type": "write_down",
            "amount": 100.0,
            "reason": "Client courtesy discount",
        },
        follow_redirects=False,
    )
    assert adjust_resp.status_code == 302

    db.session.refresh(invoice)
    assert float(invoice.total or 0.0) == 1050.0
    assert InvoiceAdjustment.query.filter_by(invoice_id=invoice.id).count() == 1

    aging_resp = client.get("/billing/ar-aging")
    assert aging_resp.status_code == 200
    body = aging_resp.get_data(as_text=True)
    assert f"#{invoice.id}" in body
    assert "1050.0" in body


def test_billing_tax_invoice_account_statement_and_reports(app_ctx):
    app = app_ctx
    lawyer = _seed_user("billing-reports@example.com", role="lawyer", mfa_enabled=True)
    matter = _seed_matter(lawyer, "2026-BILL-REPORTS-1", "Billing Reports Matter", "Billing Reports Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=lawyer.id, role_in_matter="Lead"))
    db.session.flush()
    invoice = Invoice(
        matter_id=matter.id,
        client_name=matter.client_name,
        period_start=dt.date(2026, 1, 1),
        period_end=dt.date(2026, 1, 31),
        status="approved",
        subtotal=800.0,
        tax_total=120.0,
        total=920.0,
        created_by=lawyer.id,
    )
    db.session.add(invoice)
    db.session.flush()
    db.session.add(PaymentAllocation(invoice_id=invoice.id, amount=200.0, method="eft", created_by=lawyer.id))
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, lawyer.id)

    tax_pdf = client.get(f"/billing/invoices/{invoice.id}/tax-invoice")
    assert tax_pdf.status_code == 200
    assert tax_pdf.mimetype == "application/pdf"
    assert tax_pdf.data.startswith(b"%PDF-1.4")

    statement_html = client.get(f"/billing/accounts/{matter.id}/statement")
    assert statement_html.status_code == 200
    statement_body = statement_html.get_data(as_text=True)
    assert f"#{invoice.id}" in statement_body

    statement_csv = client.get(f"/billing/accounts/{matter.id}/statement?format=csv")
    assert statement_csv.status_code == 200
    assert statement_csv.mimetype == "text/csv"
    assert "invoice_id,status,period_start,period_end" in statement_csv.get_data(as_text=True)

    trial_balance_csv = client.get("/billing/reports/trial-balance?format=csv")
    assert trial_balance_csv.status_code == 200
    assert trial_balance_csv.mimetype == "text/csv"
    assert "matter_id,matter_no,matter_title,invoice_count" in trial_balance_csv.get_data(as_text=True)

    auditor_json = client.get("/billing/reports/auditor?format=json")
    assert auditor_json.status_code == 200
    assert auditor_json.mimetype == "application/json"
    payload = auditor_json.get_json()
    assert int(payload["invoice_count"]) >= 1
    assert float(payload["billed_total"]) >= 920.0


def test_capture_settled_payments_and_pending_settlement_flow(app_ctx):
    app = app_ctx
    lawyer = _seed_user("billing-payment-capture@example.com", role="lawyer", mfa_enabled=True)
    matter = _seed_matter(lawyer, "2026-BILL-PAY-1", "Payment Capture Matter", "Payment Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=lawyer.id, role_in_matter="Lead"))
    db.session.flush()
    invoice = Invoice(
        matter_id=matter.id,
        client_name=matter.client_name,
        period_start=dt.date(2026, 3, 1),
        period_end=dt.date(2026, 3, 31),
        status="approved",
        subtotal=1000.0,
        tax_total=0.0,
        total=1000.0,
        created_by=lawyer.id,
    )
    db.session.add(invoice)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, lawyer.id)

    pending_resp = client.post(
        f"/billing/invoices/{invoice.id}/payments",
        data={
            "csrf_token": "test-csrf",
            "amount": "300.00",
            "method": "EFT",
            "reference": "PENDING-REF-1",
            "status": "pending",
            "processor_note": "Awaiting bank settlement",
        },
        follow_redirects=False,
    )
    assert pending_resp.status_code == 302
    payment = PaymentAllocation.query.order_by(PaymentAllocation.id.desc()).first()
    assert payment is not None
    assert payment.status == "pending"

    statement_before = client.get(f"/billing/accounts/{matter.id}/statement?format=csv")
    assert statement_before.status_code == 200
    rows_before = list(csv.DictReader(io.StringIO(statement_before.get_data(as_text=True))))
    invoice_row_before = next(row for row in rows_before if row["invoice_id"] == str(invoice.id))
    assert float(invoice_row_before["outstanding"]) == pytest.approx(1000.0)

    settle_resp = client.post(
        f"/billing/payments/{payment.id}/settle",
        data={"csrf_token": "test-csrf", "external_txn_id": "BANK-TXN-0001"},
        follow_redirects=False,
    )
    assert settle_resp.status_code == 302
    db.session.refresh(payment)
    assert payment.status == "settled"
    assert payment.settled_by == lawyer.id
    assert payment.settled_at is not None
    assert payment.external_txn_id == "BANK-TXN-0001"

    statement_after = client.get(f"/billing/accounts/{matter.id}/statement?format=csv")
    rows_after = list(csv.DictReader(io.StringIO(statement_after.get_data(as_text=True))))
    invoice_row_after = next(row for row in rows_after if row["invoice_id"] == str(invoice.id))
    assert float(invoice_row_after["paid"]) == pytest.approx(300.0)
    assert float(invoice_row_after["outstanding"]) == pytest.approx(700.0)

    assert AuditLog.query.filter_by(action="payment_capture", entity_type="PaymentAllocation", entity_id=payment.id).count() >= 1
    assert AuditLog.query.filter_by(action="payment_settle", entity_type="PaymentAllocation", entity_id=payment.id).count() >= 1


def test_per_transaction_billing_and_billing_audit_log_exports(app_ctx):
    app = app_ctx
    lawyer = _seed_user("billing-transaction-view@example.com", role="lawyer", mfa_enabled=True)
    matter = _seed_matter(lawyer, "2026-BILL-TXN-1", "Transaction View Matter", "Transaction Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=lawyer.id, role_in_matter="Lead"))
    db.session.flush()
    invoice = Invoice(
        matter_id=matter.id,
        client_name=matter.client_name,
        period_start=dt.date(2026, 4, 1),
        period_end=dt.date(2026, 4, 30),
        status="approved",
        subtotal=0.0,
        tax_total=0.0,
        total=0.0,
        created_by=lawyer.id,
    )
    db.session.add(invoice)
    db.session.flush()
    line = InvoiceLine(
        invoice_id=invoice.id,
        description="Per-transaction line",
        hours=2.0,
        rate=500.0,
        amount=1000.0,
        tax_amount=150.0,
    )
    adjustment = InvoiceAdjustment(
        invoice_id=invoice.id,
        adjustment_type="write_down",
        reason="Courtesy reduction",
        amount=-100.0,
        created_by=lawyer.id,
    )
    payment = PaymentAllocation(
        invoice_id=invoice.id,
        amount=400.0,
        method="EFT",
        reference="TXN-REF-001",
        status="settled",
        settled_at=utc_now(),
        settled_by=lawyer.id,
        created_by=lawyer.id,
    )
    db.session.add_all([line, adjustment, payment])
    invoice.subtotal = 1000.0
    invoice.tax_total = 150.0
    invoice.total = 1150.0
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, lawyer.id)

    transactions_csv = client.get("/billing/transactions?format=csv")
    assert transactions_csv.status_code == 200
    assert transactions_csv.mimetype == "text/csv"
    payload = transactions_csv.get_data(as_text=True)
    assert "transaction_type,transaction_id,invoice_id" in payload
    assert "bill_line" in payload
    assert "adjustment" in payload
    assert "payment" in payload

    audit_csv = client.get("/billing/audit-log?format=csv")
    assert audit_csv.status_code == 200
    assert audit_csv.mimetype == "text/csv"
    audit_payload = audit_csv.get_data(as_text=True)
    assert "action,entity_type,entity_id" in audit_payload
    assert "billing_transactions_export" in audit_payload


def test_per_transaction_billing_filters_and_pending_queue(app_ctx):
    app = app_ctx
    lawyer = _seed_user("billing-transaction-filter@example.com", role="lawyer", mfa_enabled=True)
    matter = _seed_matter(lawyer, "2026-BILL-TXN-2", "Transaction Filter Matter", "Filter Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=lawyer.id, role_in_matter="Lead"))
    db.session.flush()

    invoice = Invoice(
        matter_id=matter.id,
        client_name=matter.client_name,
        period_start=dt.date(2026, 5, 1),
        period_end=dt.date(2026, 5, 31),
        status="approved",
        subtotal=1500.0,
        tax_total=225.0,
        total=1725.0,
        created_by=lawyer.id,
    )
    db.session.add(invoice)
    db.session.flush()

    db.session.add_all(
        [
            PaymentAllocation(
                invoice_id=invoice.id,
                amount=300.0,
                method="Card",
                reference="PAY-PENDING-001",
                status="pending",
                created_by=lawyer.id,
            ),
            PaymentAllocation(
                invoice_id=invoice.id,
                amount=200.0,
                method="EFT",
                reference="PAY-SETTLED-001",
                status="settled",
                settled_at=utc_now(),
                settled_by=lawyer.id,
                created_by=lawyer.id,
            ),
        ]
    )
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, lawyer.id)

    filtered = client.get("/billing/transactions?txn_type=payment&status=pending")
    assert filtered.status_code == 200
    body = filtered.get_data(as_text=True)
    assert "Pending Payment Queue" in body
    assert "PAY-PENDING-001" in body
    assert "PAY-SETTLED-001" not in body

    filtered_csv = client.get("/billing/transactions?txn_type=payment&status=pending&format=csv")
    assert filtered_csv.status_code == 200
    rows = list(csv.DictReader(io.StringIO(filtered_csv.get_data(as_text=True))))
    assert rows
    assert {row["status"] for row in rows} == {"pending"}
    assert {row["reference"] for row in rows} == {"PAY-PENDING-001"}


def test_payment_status_group_query_reuses_single_postgres_bind():
    status_expr = _payment_status_group_expr()
    statement = select(status_expr, func.count(PaymentAllocation.id)).group_by(status_expr)
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)

    assert "GROUP BY coalesce(payment_allocation.status, %(param_1)s)" in sql
    assert "%(param_2)s" not in sql
    assert compiled.params == {"param_1": "settled"}


def test_billing_audit_log_scopes_records_to_visible_matters(app_ctx):
    app = app_ctx
    viewer = _seed_user("billing-audit-viewer@example.com", role="lawyer", mfa_enabled=True)
    peer = _seed_user("billing-audit-peer@example.com", role="lawyer", mfa_enabled=True)

    visible_matter = _seed_matter(viewer, "2026-BILL-AUDIT-01", "Visible Billing Matter", "Visible Client")
    hidden_matter = _seed_matter(peer, "2026-BILL-AUDIT-02", "Hidden Billing Matter", "Hidden Client")
    db.session.add_all(
        [
            MatterMember(matter_id=visible_matter.id, user_id=viewer.id, role_in_matter="Lead"),
            MatterMember(matter_id=hidden_matter.id, user_id=peer.id, role_in_matter="Lead"),
        ]
    )
    db.session.flush()

    visible_invoice = Invoice(
        matter_id=visible_matter.id,
        client_name=visible_matter.client_name,
        period_start=dt.date(2026, 5, 1),
        period_end=dt.date(2026, 5, 31),
        status="approved",
        subtotal=1000.0,
        tax_total=150.0,
        total=1150.0,
        created_by=viewer.id,
    )
    hidden_invoice = Invoice(
        matter_id=hidden_matter.id,
        client_name=hidden_matter.client_name,
        period_start=dt.date(2026, 5, 1),
        period_end=dt.date(2026, 5, 31),
        status="approved",
        subtotal=2000.0,
        tax_total=300.0,
        total=2300.0,
        created_by=peer.id,
    )
    db.session.add_all([visible_invoice, hidden_invoice])
    db.session.flush()

    now = utc_now()
    db.session.add_all(
        [
            AuditLog(
                actor_user_id=viewer.id,
                action="billing_transactions_view",
                entity_type="Invoice",
                entity_id=None,
                at=now,
            ),
            AuditLog(
                actor_user_id=viewer.id,
                action="invoice_approve",
                entity_type="Invoice",
                entity_id=visible_invoice.id,
                at=now - dt.timedelta(minutes=1),
            ),
            AuditLog(
                actor_user_id=peer.id,
                action="invoice_approve",
                entity_type="Invoice",
                entity_id=hidden_invoice.id,
                at=now - dt.timedelta(minutes=2),
            ),
            AuditLog(
                actor_user_id=peer.id,
                action="billing_rate_create",
                entity_type="RateCard",
                entity_id=9999,
                at=now - dt.timedelta(minutes=3),
            ),
        ]
    )
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, viewer.id)

    response = client.get("/billing/audit-log?format=csv")
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))

    assert any(
        row["action"] == "invoice_approve"
        and row["entity_type"] == "Invoice"
        and row["entity_id"] == str(visible_invoice.id)
        for row in rows
    )
    assert not any(
        row["action"] == "invoice_approve"
        and row["entity_type"] == "Invoice"
        and row["entity_id"] == str(hidden_invoice.id)
        for row in rows
    )
    assert any(row["action"] == "billing_transactions_view" and row["actor_user_id"] == str(viewer.id) for row in rows)
    assert not any(row["action"] == "billing_rate_create" and row["actor_user_id"] == str(peer.id) for row in rows)

    html_response = client.get("/billing/audit-log")
    assert html_response.status_code == 200
    html_body = html_response.get_data(as_text=True)
    assert "Billing Audit Log" in html_body
    assert f"Invoice #{visible_invoice.id}" in html_body
    assert f"Invoice #{hidden_invoice.id}" not in html_body


def test_expense_receipt_pdf_route_returns_pdf(app_ctx):
    app = app_ctx
    user = _seed_user("expense-receipt-pdf@example.com")
    matter = _seed_matter(user, "2026-EXP-PDF-1", "Expense PDF Matter", "Expense Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.flush()
    expense = ExpenseEntry(
        matter_id=matter.id,
        user_id=user.id,
        amount=250.0,
        currency="ZAR",
        category="Travel",
        description="Court travel",
        incurred_on=dt.date(2026, 2, 2),
        status="approved",
    )
    db.session.add(expense)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.get(f"/expenses/{expense.id}/receipt/pdf")
    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    assert response.data.startswith(b"%PDF-1.4")


def test_expense_receipt_download_returns_uploaded_file(app_ctx, tmp_path):
    app = app_ctx
    app.config["UPLOAD_DIR"] = str(tmp_path)
    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)

    user = _seed_user("expense-receipt-download@example.com")
    matter = _seed_matter(user, "2026-EXP-DL-1", "Expense Download Matter", "Expense Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.flush()

    stored_filename = "expense_1_receipt.txt"
    receipt_path = os.path.join(app.config["UPLOAD_DIR"], stored_filename)
    with open(receipt_path, "wb") as handle:
        handle.write(b"expense receipt body")

    expense = ExpenseEntry(
        matter_id=matter.id,
        user_id=user.id,
        amount=150.0,
        currency="ZAR",
        category="Travel",
        description="Filing run",
        incurred_on=dt.date(2026, 2, 3),
        status="submitted",
        receipt_filename=stored_filename,
        receipt_sha256=hashlib.sha256(b"expense receipt body").hexdigest(),
    )
    db.session.add(expense)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.get(f"/expenses/{expense.id}/receipt")

    assert response.status_code == 200
    assert response.data == b"expense receipt body"
    assert "attachment" in (response.headers.get("Content-Disposition") or "").lower()


def test_matter_documents_upload_and_download_roundtrip(app_ctx, tmp_path):
    app = app_ctx
    app.config["UPLOAD_DIR"] = str(tmp_path)
    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)

    user = _seed_user("matter-doc-roundtrip@example.com")
    matter = _seed_matter(user, "2026-MDOC-RT-1", "Matter Doc Roundtrip", "Roundtrip Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    upload_response = client.post(
        f"/matters/{matter.id}/documents",
        data={
            "csrf_token": "test-csrf",
            "category": "General",
            "lifecycle_stage": "Draft",
            "owner_name": "Lead Attorney",
            "file": (io.BytesIO(b"matter doc body"), "matter-doc.txt"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )

    assert upload_response.status_code == 302
    doc = DocumentFile.query.filter_by(matter_id=matter.id, original_filename="matter-doc.txt").first()
    assert doc is not None

    download_response = client.get(f"/documents/{doc.id}/download")
    assert download_response.status_code == 200
    assert download_response.data == b"matter doc body"
    assert "attachment" in (download_response.headers.get("Content-Disposition") or "").lower()

    preview_response = client.get(f"/documents/{doc.id}/download?inline=1")
    assert preview_response.status_code == 200
    assert preview_response.data == b"matter doc body"


def test_production_export_returns_document_version_manifest(app_ctx):
    app = app_ctx
    user = _seed_user("production-export@example.com")
    matter = _seed_matter(user, "2026-PROD-EXP-1", "Production Export Matter", "Production Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.flush()

    document = DocumentRecord(
        matter_id=matter.id,
        title="Production Bundle",
        document_type="General",
        confidentiality="Internal",
        created_by=user.id,
    )
    db.session.add(document)
    db.session.flush()

    version = DocumentVersion(
        document_id=document.id,
        version_no=1,
        original_filename="production-bundle.txt",
        stored_filename="dms/production-bundle.txt",
        sha256=hashlib.sha256(b"production-bundle").hexdigest(),
        state="final",
        uploaded_by=user.id,
    )
    db.session.add(version)
    db.session.flush()

    production = ProductionSet(
        matter_id=matter.id,
        name="First Production",
        confidentiality_designation="Confidential",
        watermark_text="Produced for review",
        created_by=user.id,
    )
    db.session.add(production)
    db.session.flush()

    item = ProductionItem(
        production_set_id=production.id,
        document_version_id=version.id,
        bates_number="DM000001",
    )
    db.session.add(item)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.get(f"/productions/{production.id}/export")

    assert response.status_code == 200
    assert response.mimetype == "application/json"
    payload = json.loads(response.get_data(as_text=True))
    assert payload["production_set"]["id"] == production.id
    assert payload["production_set"]["name"] == "First Production"
    assert payload["items"] == [
        {
            "production_item_id": item.id,
            "bates_number": "DM000001",
            "document_version_id": version.id,
            "filename": "production-bundle.txt",
            "sha256": version.sha256,
            "state": "final",
        }
    ]


def test_data_residency_policy_blocks_export_when_region_mismatch(app_ctx):
    app = app_ctx
    admin = _seed_user("residency-admin@example.com", role="admin", mfa_enabled=True)
    matter = _seed_matter(admin, "2026-RES-0001", "Residency Matter", "Residency Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=admin.id, role_in_matter="Lead"))
    db.session.flush()
    invoice = Invoice(
        matter_id=matter.id,
        client_name=matter.client_name,
        period_start=dt.date(2026, 1, 1),
        period_end=dt.date(2026, 1, 31),
        status="approved",
        subtotal=500.0,
        tax_total=75.0,
        total=575.0,
        created_by=admin.id,
    )
    db.session.add(invoice)
    db.session.add(
        DataResidencyPolicy(
            name="Exports in EU only",
            data_class="exports",
            region_code="EU",
            is_active=True,
        )
    )
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, admin.id)
    response = client.get(f"/billing/invoices/{invoice.id}/pdf")
    assert response.status_code == 403


def test_dms_saved_search_and_email_capture_dedup(app_ctx):
    app = app_ctx
    user = _seed_user("dms-workflow@example.com")
    matter = _seed_matter(user, "2026-DMS-0001", "DMS Matter", "DMS Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)

    saved_resp = client.post(
        "/dms/saved-searches",
        data={
            "csrf_token": "test-csrf",
            "name": "Important docs",
            "query": "privileged draft",
            "matter_id": matter.id,
        },
        follow_redirects=False,
    )
    assert saved_resp.status_code == 302
    assert SavedSearch.query.count() == 1

    first_capture = client.post(
        f"/matters/{matter.id}/email-capture",
        data={
            "csrf_token": "test-csrf",
            "message_id": "<msg-1@example.com>",
            "dedup_key": "msg-1",
            "subject": "Initial capture",
        },
        follow_redirects=False,
    )
    assert first_capture.status_code == 302

    second_capture = client.post(
        f"/matters/{matter.id}/email-capture",
        data={
            "csrf_token": "test-csrf",
            "message_id": "<msg-1@example.com>",
            "dedup_key": "msg-1",
            "subject": "Duplicate capture",
        },
        follow_redirects=False,
    )
    assert second_capture.status_code == 302
    assert EmailCapture.query.filter_by(matter_id=matter.id).count() == 1


def test_dms_saved_search_scope_filter_applies_before_limit(app_ctx):
    app = app_ctx
    user = _seed_user("dms-saved-search-scope@example.com")
    hidden_owner = _seed_user("dms-saved-search-hidden-owner@example.com")
    visible_matter = _seed_matter(user, "2026-DMS-SEARCH-SCOPE-1", "Visible Matter", "Visible Client")
    hidden_matter = _seed_matter(hidden_owner, "2026-DMS-SEARCH-SCOPE-2", "Hidden Matter", "Hidden Client")
    db.session.add(MatterMember(matter_id=visible_matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.flush()

    base_time = utc_now() - dt.timedelta(minutes=20)
    db.session.add(
        SavedSearch(
            user_id=user.id,
            name="Visible search should remain",
            query_json=json.dumps({"q": "visible"}),
            matter_id=visible_matter.id,
            created_at=base_time,
        )
    )
    for idx in range(205):
        db.session.add(
            SavedSearch(
                user_id=user.id,
                name=f"Hidden search {idx}",
                query_json=json.dumps({"q": "hidden"}),
                matter_id=hidden_matter.id,
                created_at=base_time + dt.timedelta(seconds=idx + 1),
            )
        )
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)

    response = client.get("/dms/saved-searches")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Visible search should remain" in body
    assert "Hidden search 0" not in body
    assert "Hidden search 204" not in body


def test_bates_range_assign_updates_existing_items_and_creates_missing(app_ctx):
    app = app_ctx
    user = _seed_user("dms-bates-user@example.com")
    matter = _seed_matter(user, "2026-DMS-BATES-0001", "Bates Matter", "Bates Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.flush()

    doc_one = DocumentRecord(
        matter_id=matter.id,
        title="Bates Doc One",
        document_type="Memo",
        confidentiality="Internal",
        created_by=user.id,
    )
    doc_two = DocumentRecord(
        matter_id=matter.id,
        title="Bates Doc Two",
        document_type="Memo",
        confidentiality="Internal",
        created_by=user.id,
    )
    db.session.add_all([doc_one, doc_two])
    db.session.flush()

    file_one = DocumentFile(
        matter_id=matter.id,
        original_filename="bates-one.txt",
        stored_filename="matter_1_dms_bates-one.txt",
        sha256="a" * 64,
        content_type="text/plain",
        category="Memo",
        doc_version="1",
        lifecycle_stage="Draft",
        owner_name=user.full_name,
        is_privileged=False,
        uploaded_by=user.id,
    )
    file_two = DocumentFile(
        matter_id=matter.id,
        original_filename="bates-two.txt",
        stored_filename="matter_1_dms_bates-two.txt",
        sha256="b" * 64,
        content_type="text/plain",
        category="Memo",
        doc_version="1",
        lifecycle_stage="Draft",
        owner_name=user.full_name,
        is_privileged=False,
        uploaded_by=user.id,
    )
    db.session.add_all([file_one, file_two])
    db.session.flush()

    ver_one = DocumentVersion(
        document_id=doc_one.id,
        document_file_id=file_one.id,
        version_no=1,
        original_filename="bates-one.txt",
        stored_filename=file_one.stored_filename,
        sha256=file_one.sha256,
        hash_chain_prev=None,
        hash_chain_current="c" * 64,
        state="draft",
        uploaded_by=user.id,
    )
    ver_two = DocumentVersion(
        document_id=doc_two.id,
        document_file_id=file_two.id,
        version_no=1,
        original_filename="bates-two.txt",
        stored_filename=file_two.stored_filename,
        sha256=file_two.sha256,
        hash_chain_prev=None,
        hash_chain_current="d" * 64,
        state="draft",
        uploaded_by=user.id,
    )
    db.session.add_all([ver_one, ver_two])
    db.session.flush()

    production = ProductionSet(matter_id=matter.id, name="Bates Set", created_by=user.id)
    db.session.add(production)
    db.session.flush()
    existing_item = ProductionItem(
        production_set_id=production.id,
        document_version_id=ver_one.id,
        bates_number="OLD000001",
    )
    db.session.add(existing_item)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.post(
        "/bates/ranges",
        data={
            "csrf_token": "test-csrf",
            "production_set_id": production.id,
            "prefix": "BAT",
            "start_no": 1,
            "end_no": 2,
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    ranges = BatesRange.query.filter_by(production_set_id=production.id).all()
    assert len(ranges) == 1

    items = (
        ProductionItem.query.filter_by(production_set_id=production.id)
        .order_by(ProductionItem.document_version_id.asc())
        .all()
    )
    assert len(items) == 2
    assert items[0].document_version_id == ver_one.id
    assert items[0].bates_number == "BAT000001"
    assert items[1].document_version_id == ver_two.id
    assert items[1].bates_number == "BAT000002"


def test_matter_dms_ranked_search_filters_to_matching_documents(app_ctx):
    app = app_ctx
    user = _seed_user("dms-search@example.com")
    matter = _seed_matter(user, "2026-DMS-SEARCH-1", "Search Matter", "Search Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.flush()

    doc_alpha = DocumentRecord(
        matter_id=matter.id,
        title="Alpha memo",
        document_type="Memo",
        confidentiality="Internal",
        created_by=user.id,
    )
    doc_budget = DocumentRecord(
        matter_id=matter.id,
        title="Budget sheet",
        document_type="Spreadsheet",
        confidentiality="Internal",
        created_by=user.id,
    )
    db.session.add_all([doc_alpha, doc_budget])
    db.session.flush()
    ver_alpha = DocumentVersion(
        document_id=doc_alpha.id,
        version_no=1,
        original_filename="alpha.txt",
        stored_filename="alpha.txt",
        sha256="sha-alpha",
        state="final",
        uploaded_by=user.id,
    )
    ver_budget = DocumentVersion(
        document_id=doc_budget.id,
        version_no=1,
        original_filename="budget.txt",
        stored_filename="budget.txt",
        sha256="sha-budget",
        state="final",
        uploaded_by=user.id,
    )
    db.session.add_all([ver_alpha, ver_budget])
    db.session.flush()
    db.session.add(DocumentOCRText(document_version_id=ver_budget.id, extracted_text="quarterly forecast data"))
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.get(f"/matters/{matter.id}/dms?q=alpha")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Alpha memo" in body
    assert "Budget sheet" not in body


def test_dms_template_generation_creates_document_version(app_ctx):
    app = app_ctx
    user = _seed_user("dms-template-gen@example.com")
    matter = _seed_matter(user, "2026-DMS-GEN-0001", "Generated Document Matter", "Template Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    template = DocumentTemplate(
        name="Notice Template",
        template_type="Notice",
        body="Matter {{matter_no}} for {{client_name}} against {{opponent_name}}.",
        created_by=user.id,
    )
    db.session.add(template)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.post(
        f"/matters/{matter.id}/dms",
        data={
            "csrf_token": "test-csrf",
            "action": "generate_from_template",
            "template_id": template.id,
            "generated_title": "Generated Notice",
            "custom_fields": "opponent_name=Acme Holdings",
            "generated_document_type": "Notice",
            "generated_confidentiality": "Internal",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    doc = DocumentRecord.query.filter_by(matter_id=matter.id, title="Generated Notice").first()
    assert doc is not None
    version = DocumentVersion.query.filter_by(document_id=doc.id, version_no=1).first()
    assert version is not None
    ocr = DocumentOCRText.query.filter_by(document_version_id=version.id).first()
    assert ocr is not None
    assert "2026-DMS-GEN-0001" in ocr.extracted_text
    assert "Acme Holdings" in ocr.extracted_text
    assert os.path.isfile(os.path.join(app.config["UPLOAD_DIR"], version.stored_filename))


def test_matter_dms_template_variable_requirements_payload(app_ctx):
    app = app_ctx
    user = _seed_user("dms-template-vars@example.com")
    matter = _seed_matter(user, "2026-DMS-VARS-0001", "Template Variables Matter", "Variables Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    template = DocumentTemplate(
        name="Variable Prompt Template",
        template_type="Notice",
        body="For {{matter_no}} and {{client_name}} include {{counterparty_name}} by {{hearing_date}}.",
        created_by=user.id,
    )
    db.session.add(template)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.get(f"/matters/{matter.id}/dms")
    body = response.get_data(as_text=True)
    compact_body = body.replace(" ", "").replace("\n", "")

    assert response.status_code == 200
    assert "Template variable requirements" in body
    assert '"built_in_tokens":["client_name","matter_no"]' in compact_body
    assert '"custom_tokens":["counterparty_name","hearing_date"]' in compact_body


def test_matter_dms_renders_quick_starts_and_matter_brief(app_ctx):
    app = app_ctx
    user = _seed_user("dms-quick-starts@example.com")
    matter = _seed_matter(user, "2026-DMS-QS-0001", "Litigation Matter", "Quick Start Client")
    matter.practice_area = "Commercial Litigation"
    matter.legal_category = "Commercial Litigation"
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.get(f"/matters/{matter.id}/dms")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Document Operations Hub" in body
    assert "DMS Quick Starts" in body
    assert "Client Advice" in body
    assert "Matter Brief" in body
    assert f'/matters/{matter.id}/dms?prefill_title=' in body
    assert "#new-dms-document" in body


def test_matter_dms_prefill_query_populates_upload_form(app_ctx):
    app = app_ctx
    user = _seed_user("dms-prefill-query@example.com")
    matter = _seed_matter(user, "2026-DMS-PREFILL-1", "Prefill Matter", "Prefill Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.get(
        f"/matters/{matter.id}/dms",
        query_string={
            "prefill_title": "Prefilled Advice Memo",
            "prefill_document_type": "General",
            "prefill_confidentiality": "Confidential",
            "prefill_privilege_label": "Attorney-Client",
            "prefill_retention_category": "Matter Lifecycle",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'value="Prefilled Advice Memo"' in body
    assert '<option value="General" selected' in body
    assert '<option value="Confidential" selected' in body
    assert '<option value="Attorney-Client" selected' in body
    assert '<option value="Matter Lifecycle" selected' in body
    assert "This document form was prefilled from a workflow shortcut." in body


def test_matter_dms_handles_missing_snapshot_payloads(monkeypatch, app_ctx):
    from intranet.routes import dms as dms_routes

    app = app_ctx
    user = _seed_user("dms-fallbacks@example.com")
    matter = _seed_matter(user, "2026-DMS-FALLBACK-1", "Fallback DMS Matter", "Fallback Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.commit()

    monkeypatch.setattr(dms_routes, "build_matter_magic_snapshot", lambda *args, **kwargs: {})
    monkeypatch.setattr(dms_routes, "attach_matter_magic_links", lambda actions, matter_id: actions or [])
    monkeypatch.setattr(dms_routes, "build_dms_quick_starts", lambda *args, **kwargs: [])

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.get(f"/matters/{matter.id}/dms")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Document Operations Hub" in body
    assert "No quick-start presets are available for this matter yet." in body
    assert "No matter brief is available yet." in body


def test_matter_create_auto_generates_linked_archetype_document_templates(app_ctx):
    app = app_ctx
    user = _seed_user("archetype-doc-autogen@example.com")
    archetype = MatterTemplate(
        name="Archetype Linked Document",
        legal_category="Commercial Litigation",
        required_fields_json=json.dumps([]),
        boilerplate_template="Baseline archetype boilerplate for {{ matter_no }}.",
        created_by=user.id,
    )
    db.session.add(archetype)
    db.session.flush()
    linked_template = DocumentTemplate(
        name="Linked Pleading Draft",
        archetype_id=archetype.id,
        template_type="Pleading",
        body="Draft for {{matter_no}} and {{client_name}}.",
        requires_signature=False,
        created_by=user.id,
    )
    db.session.add(linked_template)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.post(
        "/matters/new",
        data={
            "csrf_token": "test-csrf",
            "matter_no": "2026-ARCH-DOC-0001",
            "title": "Archetype Linked Matter",
            "client_name": "Linked Client",
            "status": "Open",
            "risk_level": "Medium",
            "budget_status": "On Track",
            "legal_category": "Commercial Litigation",
            "archetype_id": str(archetype.id),
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    matter = Matter.query.filter_by(matter_no="2026-ARCH-DOC-0001").first()
    assert matter is not None

    doc = DocumentRecord.query.filter_by(
        matter_id=matter.id,
        title="Linked Pleading Draft - 2026-ARCH-DOC-0001",
    ).first()
    assert doc is not None
    version = DocumentVersion.query.filter_by(document_id=doc.id, version_no=1).first()
    assert version is not None
    ocr = DocumentOCRText.query.filter_by(document_version_id=version.id).first()
    assert ocr is not None
    assert "2026-ARCH-DOC-0001" in ocr.extracted_text
    assert os.path.isfile(os.path.join(app.config["UPLOAD_DIR"], version.stored_filename))


def test_matter_intake_seeds_archetype_playbook_and_linked_documents(app_ctx):
    app = app_ctx
    user = _seed_user("archetype-intake-standard@example.com")
    archetype = MatterTemplate(
        name="Archetype Intake Standard",
        legal_category="Labour Law",
        required_fields_json=json.dumps(
            [
                {"key": "claim_value", "label": "Claim Value"},
            ]
        ),
        checklist_json=json.dumps(["Collect signed mandate", "Confirm key witness list"]),
        created_by=user.id,
    )
    db.session.add(archetype)
    db.session.flush()
    linked_template = DocumentTemplate(
        name="Labour Intake Memo",
        archetype_id=archetype.id,
        template_type="Memo",
        body="Memo for {{matter_no}}. Claim value: {{claim_value}}.",
        requires_signature=False,
        created_by=user.id,
    )
    db.session.add(linked_template)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.post(
        "/matters/intake",
        data={
            "csrf_token": "test-csrf",
            "matter_no": "2026-ARCH-INTAKE-0001",
            "title": "Intake Standard Matter",
            "client_name": "Standard Client",
            "legal_category": "Labour Law",
            "template_id": str(archetype.id),
            "field_claim_value": "R500000",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    matter = Matter.query.filter_by(matter_no="2026-ARCH-INTAKE-0001").first()
    assert matter is not None

    checklist_rows = (
        MatterClosingChecklistItem.query.filter_by(matter_id=matter.id)
        .order_by(MatterClosingChecklistItem.id.asc())
        .all()
    )
    assert [row.item_text for row in checklist_rows] == [
        "Collect signed mandate",
        "Confirm key witness list",
    ]

    doc = DocumentRecord.query.filter_by(
        matter_id=matter.id,
        title="Labour Intake Memo - 2026-ARCH-INTAKE-0001",
    ).first()
    assert doc is not None
    version = DocumentVersion.query.filter_by(document_id=doc.id, version_no=1).first()
    assert version is not None
    ocr = DocumentOCRText.query.filter_by(document_version_id=version.id).first()
    assert ocr is not None
    assert "R500000" in ocr.extracted_text


def test_matter_archetype_sync_checklist_backfills_existing_matter(app_ctx):
    app = app_ctx
    user = _seed_user("archetype-sync-checklist@example.com")
    archetype = MatterTemplate(
        name="Archetype Sync Checklist",
        legal_category="Commercial Litigation",
        required_fields_json=json.dumps([]),
        checklist_json=json.dumps(["Open case file", "Verify engagement letter"]),
        created_by=user.id,
    )
    db.session.add(archetype)
    db.session.flush()

    matter = _seed_matter(user, "2026-ARCH-SYNC-0001", "Checklist Sync Matter", "Sync Client")
    matter.archetype_id = archetype.id
    matter.legal_category = archetype.legal_category
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.commit()

    assert MatterClosingChecklistItem.query.filter_by(matter_id=matter.id).count() == 0

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.post(
        f"/matters/{matter.id}/archetype/sync-checklist",
        data={"csrf_token": "test-csrf"},
        follow_redirects=False,
    )
    assert response.status_code == 302

    checklist_rows = (
        MatterClosingChecklistItem.query.filter_by(matter_id=matter.id)
        .order_by(MatterClosingChecklistItem.id.asc())
        .all()
    )
    assert [row.item_text for row in checklist_rows] == [
        "Open case file",
        "Verify engagement letter",
    ]


def test_matter_close_blocks_when_archetype_required_fields_missing(app_ctx):
    app = app_ctx
    user = _seed_user("archetype-close-block@example.com", role="admin", mfa_enabled=True)
    archetype = MatterTemplate(
        name="Close Guard Archetype",
        legal_category="Corporate",
        required_fields_json=json.dumps([{"key": "counterparty_name", "label": "Counterparty Name"}]),
        checklist_json=json.dumps([]),
        created_by=user.id,
    )
    db.session.add(archetype)
    db.session.flush()

    matter = _seed_matter(user, "2026-ARCH-CLOSE-0001", "Close Guard Matter", "Close Guard Client")
    matter.archetype_id = archetype.id
    matter.legal_category = archetype.legal_category
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.post(
        f"/matters/{matter.id}/close",
        data={"csrf_token": "test-csrf"},
        follow_redirects=False,
    )
    assert response.status_code == 302

    db.session.refresh(matter)
    assert matter.status == "Open"
    assert matter.closed_at is None
    assert AuditLog.query.filter_by(action="matter_close", entity_type="Matter", entity_id=matter.id).count() == 0


def test_email_capture_attachment_download_is_audited(app_ctx):
    app = app_ctx
    user = _seed_user("email-audit@example.com")
    matter = _seed_matter(user, "2026-EMAIL-AUDIT-1", "Email Audit Matter", "Audit Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)

    create_resp = client.post(
        f"/matters/{matter.id}/email-capture",
        data={
            "csrf_token": "test-csrf",
            "message_id": "<audit-msg@example.com>",
            "dedup_key": "audit-msg-1",
            "subject": "Audit capture",
            "attachment": (io.BytesIO(b"audit attachment"), "audit.txt"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert create_resp.status_code == 302

    row = EmailCapture.query.filter_by(matter_id=matter.id, dedup_key="audit-msg-1").first()
    assert row is not None
    assert row.stored_filename is not None

    download_resp = client.get(f"/email-capture/{row.id}/attachment")
    assert download_resp.status_code == 200
    assert "attachment" in (download_resp.headers.get("Content-Disposition") or "").lower()
    assert (
        AuditLog.query.filter_by(action="email_capture_attachment_access", entity_type="EmailCapture", entity_id=row.id).count()
        >= 1
    )


def test_portal_time_limited_document_link_download(app_ctx):
    app = app_ctx
    admin = _seed_user("portal-link-admin@example.com", role="admin", mfa_enabled=True)
    matter = _seed_matter(admin, "2026-PORTAL-LINK-1", "Portal Link Matter", "Client Link")

    portal_user = PortalUser(email="portal-link-user@example.com", full_name="Portal Link User", password_hash="x", is_active=True)
    portal_user.set_password("PortalPassword123!")
    db.session.add(portal_user)
    db.session.flush()
    db.session.add(
        PortalMatterAccess(
            portal_user_id=portal_user.id,
            matter_id=matter.id,
            visibility_level="shared_docs",
            granted_by=admin.id,
        )
    )

    document = DocumentRecord(
        matter_id=matter.id,
        title="Shared Document",
        document_type="General",
        confidentiality="Internal",
        created_by=admin.id,
    )
    db.session.add(document)
    db.session.flush()

    stored_filename = f"test_portal_link_{document.id}.txt"
    upload_dir = app.config["UPLOAD_DIR"]
    os.makedirs(upload_dir, exist_ok=True)
    path = os.path.join(upload_dir, stored_filename)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("portal link content")

    version = DocumentVersion(
        document_id=document.id,
        version_no=1,
        original_filename="shared.txt",
        stored_filename=stored_filename,
        sha256="abc123",
        state="final",
        uploaded_by=admin.id,
    )
    db.session.add(version)
    db.session.commit()

    client = app.test_client()
    _set_portal_session(client, portal_user.id)
    create_resp = client.post(
        "/portal/links",
        data={
            "csrf_token": "test-csrf",
            "matter_id": matter.id,
            "document_version_id": version.id,
            "expires_minutes": 30,
        },
        follow_redirects=False,
    )
    assert create_resp.status_code == 302
    token_row = PortalLinkToken.query.order_by(PortalLinkToken.id.desc()).first()
    assert token_row is not None

    with client.session_transaction() as sess:
        link_url = sess.get("portal_last_link_url")
    assert link_url and "/portal/link/" in link_url
    raw_token = link_url.rsplit("/", 1)[-1]

    access_resp = client.get(f"/portal/link/{raw_token}")
    assert access_resp.status_code == 200
    assert "attachment" in (access_resp.headers.get("Content-Disposition") or "").lower()
    db.session.refresh(token_row)
    assert token_row.used_at is not None

    reuse_resp = client.get(f"/portal/link/{raw_token}")
    assert reuse_resp.status_code == 410


def test_calendar_matter_post_creates_deadline_and_hearing(app_ctx):
    app = app_ctx
    user = _seed_user("calendar-owner@example.com")
    matter = _seed_matter(user, "2026-CAL-0001", "Calendar Matter", "Calendar Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    create_deadline = client.post(
        f"/calendar/matter/{matter.id}",
        data={
            "csrf_token": "test-csrf",
            "action": "create_deadline",
            "title": "File answering affidavit",
            "due_at": "2026-03-05",
            "is_critical": "1",
        },
        follow_redirects=False,
    )
    assert create_deadline.status_code == 302

    deadline = Deadline.query.filter_by(matter_id=matter.id, title="File answering affidavit").first()
    assert deadline is not None
    assert deadline.is_critical is True

    schedule_hearing = client.post(
        f"/calendar/matter/{matter.id}",
        data={
            "csrf_token": "test-csrf",
            "action": "schedule_hearing",
            "title": "Case management hearing",
            "event_date": "2026-03-07",
            "description": "Courtroom 5",
        },
        follow_redirects=False,
    )
    assert schedule_hearing.status_code == 302

    hearing = MatterTimelineEvent.query.filter_by(matter_id=matter.id, title="Case management hearing").first()
    assert hearing is not None
    assert hearing.event_type == "Hearing"
    assert hearing.is_milestone is True


def test_calendar_milestone_report_includes_scoped_summary(app_ctx):
    app = app_ctx
    admin = _seed_user("calendar-report-admin@example.com", role="admin", mfa_enabled=True)
    matter = _seed_matter(admin, "2026-CAL-REPORT-1", "Calendar Report Matter", "Calendar Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=admin.id, role_in_matter="Lead"))
    today = dt.date.today()
    db.session.add(
        MatterTimelineEvent(
            matter_id=matter.id,
            event_date=today + dt.timedelta(days=5),
            event_type="Milestone",
            title="Evidence exchange",
            is_milestone=True,
            created_by=admin.id,
        )
    )
    db.session.add(
        Deadline(
            matter_id=matter.id,
            title="Serve heads of argument",
            due_at=today - dt.timedelta(days=1),
            status="open",
            is_critical=True,
            created_by=admin.id,
        )
    )
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, admin.id)
    response = client.get(
        f"/calendar/milestones/report?scope=team&start={today.isoformat()}&end={(today + dt.timedelta(days=30)).isoformat()}",
        follow_redirects=False,
    )
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Milestone Summary Report" in body
    assert "2026-CAL-REPORT-1" in body
    assert "Calendar Report Matter" in body


def test_time_prompts_return_policy_and_fee_guidance(app_ctx):
    app = app_ctx
    user = _seed_user("time-prompts@example.com")
    matter = _seed_matter(user, "2026-TIME-PROMPT-1", "Prompt Matter", "Prompt Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.add(
        TimeRoundingPolicy(
            matter_id=matter.id,
            increment_hours=0.25,
            min_narrative_length=30,
            require_activity_code=True,
            daily_hour_cap=4.0,
            is_active=True,
        )
    )
    db.session.add(
        RateCard(
            matter_id=matter.id,
            user_id=user.id,
            currency="ZAR",
            rate_per_hour=1500.0,
            is_active=True,
        )
    )
    db.session.add(
        TimeEntry(
            user_id=user.id,
            matter_id=matter.id,
            start_at=dt.datetime(2026, 4, 1, 8, 0, 0),
            end_at=dt.datetime(2026, 4, 1, 10, 30, 0),
            hours=2.5,
            rounded_hours=2.5,
            narrative="Existing entry",
            status="approved",
        )
    )
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.get(
        "/time/prompts",
        query_string={
            "matter_id": matter.id,
            "start_at": "2026-04-01T11:00:00",
            "end_at": "2026-04-01T13:00:00",
            "narrative": "Short note",
            "activity_code": "",
            "task_code": "",
            "is_billable": "1",
        },
    )
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["ok"] is True
    codes = {row["code"] for row in payload["prompts"]}
    assert "missing_activity_code" in codes
    assert "narrative_too_short" in codes
    assert "daily_cap_risk" in codes
    assert "fee_preview" in codes
    assert payload["fee_preview"]["currency"] == "ZAR"
    assert float(payload["fee_preview"]["estimated_fee"]) == pytest.approx(3000.0)


def test_portal_link_expiry_returns_gone(app_ctx):
    app = app_ctx
    admin = _seed_user("portal-expiry-admin@example.com", role="admin", mfa_enabled=True)
    matter = _seed_matter(admin, "2026-PORTAL-LINK-EXP-1", "Portal Link Expiry Matter", "Expiry Client")
    portal_user = PortalUser(email="portal-expiry-user@example.com", full_name="Expiry User", password_hash="x", is_active=True)
    portal_user.set_password("PortalPassword123!")
    db.session.add(portal_user)
    db.session.flush()
    db.session.add(
        PortalMatterAccess(
            portal_user_id=portal_user.id,
            matter_id=matter.id,
            visibility_level="summary_only",
            granted_by=admin.id,
        )
    )

    raw_token = "expired-link-token"
    db.session.add(
        PortalLinkToken(
            portal_user_id=portal_user.id,
            matter_id=matter.id,
            document_version_id=None,
            token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            expires_at=utc_now() - dt.timedelta(minutes=1),
        )
    )
    db.session.commit()

    client = app.test_client()
    response = client.get(f"/portal/link/{raw_token}")
    assert response.status_code == 410


def test_revoked_user_session_forces_relogin(app_ctx):
    app = app_ctx
    user = _seed_user("revoked-session@example.com")
    now = utc_now()
    token_raw = "revoked-session-token"
    token_hash = hashlib.sha256(token_raw.encode("utf-8")).hexdigest()

    db.session.add(
        UserSession(
            user_id=user.id,
            session_token_hash=token_hash,
            ip="127.0.0.1",
            user_agent="pytest",
            created_at=now - dt.timedelta(minutes=5),
            last_seen_at=now - dt.timedelta(minutes=2),
            expires_at=now + dt.timedelta(minutes=30),
            revoked_at=now - dt.timedelta(seconds=1),
        )
    )
    db.session.commit()

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
        sess["_csrf_token"] = "test-csrf"
        sess["_session_token"] = token_raw

    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in (response.headers.get("Location") or "")


def test_expired_user_session_forces_relogin(app_ctx):
    app = app_ctx
    user = _seed_user("expired-session@example.com")
    now = utc_now()
    token_raw = "expired-session-token"
    token_hash = hashlib.sha256(token_raw.encode("utf-8")).hexdigest()

    db.session.add(
        UserSession(
            user_id=user.id,
            session_token_hash=token_hash,
            ip="127.0.0.1",
            user_agent="pytest",
            created_at=now - dt.timedelta(hours=3),
            last_seen_at=now - dt.timedelta(hours=2),
            expires_at=now - dt.timedelta(minutes=1),
            revoked_at=None,
        )
    )
    db.session.commit()

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True
        sess["_csrf_token"] = "test-csrf"
        sess["_session_token"] = token_raw

    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in (response.headers.get("Location") or "")


def test_login_registers_trusted_device_and_supports_revoke(app_ctx):
    app = app_ctx
    user = _seed_user("trusted-device@example.com", role="partner", mfa_enabled=False)
    db.session.commit()

    client = app.test_client()
    csrf = _csrf_token_for_path(client, "/login")
    response = client.post(
        "/login",
        data={
            "csrf_token": csrf,
            "email": user.email,
            "password": "TestPassword123!",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "/dashboard" in (response.headers.get("Location") or "")

    device = TrustedDevice.query.filter_by(user_id=user.id, is_active=True).first()
    assert device is not None

    csrf = _csrf_token_for_path(client, "/auth/sessions")
    revoke = client.post(
        f"/auth/devices/{device.id}/revoke",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert revoke.status_code == 302
    db.session.refresh(device)
    assert device.is_active is False


def test_matter_stage_change_triggers_notification(app_ctx):
    app = app_ctx
    user = _seed_user("stage-trigger@example.com")
    matter = _seed_matter(user, "2026-STAGE-TRIGGER-1", "Stage Trigger Matter", "Trigger Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.post(
        f"/matters/{matter.id}/stage",
        data={"csrf_token": "test-csrf", "stage": "Discovery", "reason": "Ready for discovery"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert (
        Notification.query.filter_by(
            event_type="matter_stage_changed",
            actor_user_id=user.id,
            subject_ref=f"matter:{matter.id}:stage:Discovery",
        ).count()
        >= 1
    )


def test_document_upload_triggers_notification(app_ctx):
    app = app_ctx
    user = _seed_user("document-trigger@example.com")
    matter = _seed_matter(user, "2026-DOC-TRIGGER-1", "Document Trigger Matter", "Trigger Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.flush()
    doc = DocumentRecord(
        matter_id=matter.id,
        title="Trigger Upload",
        document_type="General",
        confidentiality="Internal",
        created_by=user.id,
    )
    db.session.add(doc)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.post(
        f"/documents/{doc.id}/versions",
        data={
            "csrf_token": "test-csrf",
            "state": "final",
            "notes": "Uploaded from trigger test",
            "file": (io.BytesIO(b"trigger content"), "trigger.txt"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert response.status_code == 302
    created_version = DocumentVersion.query.filter_by(document_id=doc.id).order_by(DocumentVersion.id.desc()).first()
    assert created_version is not None
    assert (
        Notification.query.filter_by(
            event_type="document_uploaded",
            actor_user_id=user.id,
            subject_ref=f"document_version:{created_version.id}",
        ).count()
        >= 1
    )


def test_document_version_upload_rejects_invalid_state(app_ctx):
    app = app_ctx
    user = _seed_user("document-invalid-state@example.com")
    matter = _seed_matter(user, "2026-DOC-INVALID-STATE-1", "Document Invalid State Matter", "State Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.flush()
    doc = DocumentRecord(
        matter_id=matter.id,
        title="Invalid State Upload",
        document_type="General",
        confidentiality="Internal",
        created_by=user.id,
    )
    db.session.add(doc)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.post(
        f"/documents/{doc.id}/versions",
        data={
            "csrf_token": "test-csrf",
            "state": "this-state-should-not-be-accepted",
            "notes": "state should fail validation",
            "file": (io.BytesIO(b"invalid state content"), "invalid-state.txt"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert DocumentVersion.query.filter_by(document_id=doc.id).count() == 0


def test_document_version_upload_rejects_invalid_filename(app_ctx):
    app = app_ctx
    user = _seed_user("document-invalid-name@example.com")
    matter = _seed_matter(user, "2026-DOC-INVALID-NAME-1", "Document Invalid Filename Matter", "Filename Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.flush()
    doc = DocumentRecord(
        matter_id=matter.id,
        title="Invalid Filename Upload",
        document_type="General",
        confidentiality="Internal",
        created_by=user.id,
    )
    db.session.add(doc)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.post(
        f"/documents/{doc.id}/versions",
        data={
            "csrf_token": "test-csrf",
            "state": "draft",
            "notes": "filename should fail validation",
            "file": (io.BytesIO(b"invalid filename content"), "../../../"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert DocumentVersion.query.filter_by(document_id=doc.id).count() == 0


def test_matter_close_with_active_legal_hold_blocks_archival(app_ctx):
    app = app_ctx
    admin = _seed_user("legal-hold-close@example.com", role="admin", mfa_enabled=True)
    matter = _seed_matter(admin, "2026-LH-CLOSE-0001", "Legal Hold Close Matter", "Hold Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=admin.id, role_in_matter="Lead"))
    db.session.add(LegalHold(matter_id=matter.id, reason="Regulatory preservation", is_active=True, created_by=admin.id))
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, admin.id)
    response = client.post(
        f"/matters/{matter.id}/close",
        data={"csrf_token": "test-csrf"},
        follow_redirects=False,
    )
    assert response.status_code == 302

    db.session.refresh(matter)
    assert matter.status == "Closed"
    assert matter.archival_status == "legal_hold_blocked"
    assert matter.archival_due_at is None
    assert (
        AuditLog.query.filter_by(action="matter_close_legal_hold_blocked", entity_type="Matter", entity_id=matter.id).count() >= 1
    )


def test_retention_archive_sweep_blocks_held_matters_and_archives_clear_matters(seed_user_matter):
    user = seed_user_matter["user"]
    now = utc_now()

    clear_matter = Matter(
        matter_no="2026-RET-0001",
        title="Clear Retention Matter",
        client_name="Retention Client A",
        status="Closed",
        created_by=user.id,
        opened_at=now - dt.timedelta(days=60),
        closed_at=now - dt.timedelta(days=30),
        archival_status="archive_pending",
        archival_due_at=now - dt.timedelta(days=1),
        last_updated_at=now,
    )
    held_matter = Matter(
        matter_no="2026-RET-0002",
        title="Held Retention Matter",
        client_name="Retention Client B",
        status="Closed",
        created_by=user.id,
        opened_at=now - dt.timedelta(days=60),
        closed_at=now - dt.timedelta(days=30),
        archival_status="archive_pending",
        archival_due_at=now - dt.timedelta(days=1),
        last_updated_at=now,
    )
    db.session.add_all([clear_matter, held_matter])
    db.session.flush()
    db.session.add(LegalHold(matter_id=held_matter.id, reason="Investigation", is_active=True, created_by=user.id))
    db.session.commit()

    message = _handle_retention_archive_sweep({"batch_size": 50})
    db.session.refresh(clear_matter)
    db.session.refresh(held_matter)

    assert "archived=1" in message
    assert "blocked=1" in message
    assert clear_matter.archival_status == "archived"
    assert clear_matter.archival_due_at is None
    assert held_matter.archival_status == "legal_hold_blocked"
    assert held_matter.archival_due_at is None


def test_admin_legal_hold_create_release_updates_closed_matter_archival(app_ctx):
    app = app_ctx
    admin = _seed_user("legal-hold-admin@example.com", role="admin", mfa_enabled=True)
    now = utc_now()
    matter = Matter(
        matter_no="2026-LH-ADMIN-0001",
        title="Admin Hold Matter",
        client_name="Admin Hold Client",
        status="Closed",
        created_by=admin.id,
        opened_at=now - dt.timedelta(days=90),
        closed_at=now - dt.timedelta(days=20),
        archival_status="archive_pending",
        archival_due_at=now - dt.timedelta(days=5),
        last_updated_at=now,
    )
    db.session.add(matter)
    db.session.add(
        RetentionPolicy(
            name="Default retention",
            retain_days=365,
            archive_after_days=45,
            is_active=True,
        )
    )
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, admin.id)

    create_resp = client.post(
        "/admin/rules/legal-holds",
        data={
            "csrf_token": "test-csrf",
            "action": "create",
            "matter_id": matter.id,
            "reason": "Preserve evidence",
        },
        follow_redirects=False,
    )
    assert create_resp.status_code == 302

    hold = LegalHold.query.filter_by(matter_id=matter.id, is_active=True).first()
    assert hold is not None
    db.session.refresh(matter)
    assert matter.archival_status == "legal_hold_blocked"
    assert matter.archival_due_at is None

    release_resp = client.post(
        "/admin/rules/legal-holds",
        data={
            "csrf_token": "test-csrf",
            "action": "release",
            "hold_id": hold.id,
            "release_note": "Matter released for archival scheduling",
        },
        follow_redirects=False,
    )
    assert release_resp.status_code == 302
    db.session.refresh(hold)
    db.session.refresh(matter)
    assert hold.is_active is False
    assert hold.released_at is not None
    assert "Release note" in (hold.reason or "")
    assert matter.archival_status == "archive_pending"
    assert matter.archival_due_at is not None
    expected_due = (matter.closed_at + dt.timedelta(days=45)).date()
    assert matter.archival_due_at.date() == expected_due


def test_sqlite_legal_hold_guard_blocks_matter_delete(seed_user_matter):
    user = seed_user_matter["user"]
    matter = Matter(
        matter_no="2026-LH-DEL-0001",
        title="Delete Guard Matter",
        client_name="Delete Guard Client",
        status="Open",
        created_by=user.id,
        opened_at=utc_now(),
        last_updated_at=utc_now(),
    )
    db.session.add(matter)
    db.session.flush()
    db.session.add(LegalHold(matter_id=matter.id, reason="Delete restricted", is_active=True, created_by=user.id))
    db.session.commit()

    db.session.delete(matter)
    with pytest.raises(Exception):
        db.session.commit()
    db.session.rollback()


def test_collapsed_demo_suite_surfaces_return_404(app_ctx):
    app = app_ctx
    user = _seed_user("surface-reduction@example.com", role="admin", mfa_enabled=True)
    client = app.test_client()
    _set_user_session(client, user.id)

    for path in (
        "/integrations/office365",
        "/integrations/office365/outlook.ics",
        "/integrations/south-africa",
        "/integrations/third-party",
        "/mobile/hub",
        "/trust/policy",
        "/trust/security",
        "/trust/incidents",
        "/director/personnel",
    ):
        assert client.get(path).status_code == 404


def test_search_results_render_operational_actions(app_ctx):
    app = app_ctx
    user = _seed_user("search-actions@example.com")
    matter = _seed_matter(user, "2026-SEARCH-ACT-1", "Action Search Matter", "Search Action Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.flush()
    task = Task(
        matter_id=matter.id,
        title="Alpha review task",
        status="Todo",
        created_by=user.id,
    )
    db.session.add(task)
    db.session.flush()
    doc = DocumentFile(
        matter_id=matter.id,
        original_filename="alpha-brief.pdf",
        stored_filename="alpha-brief.pdf",
        sha256="a" * 64,
        content_type="application/pdf",
        category="Memo",
        lifecycle_stage="Draft",
        uploaded_by=user.id,
    )
    db.session.add(doc)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.get("/search?q=alpha")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Search Command Center" in body
    assert "Smart Launch Pack" in body
    assert "Search Brief" in body
    assert "Workspace" in body
    assert "Start Timer" in body
    assert "Download" in body


def test_search_handles_partial_launch_pack_payloads(monkeypatch, app_ctx):
    from intranet.routes import content as content_routes

    app = app_ctx
    user = _seed_user("search-launch-fallback@example.com")
    matter = _seed_matter(user, "2026-SEARCH-FALLBACK-1", "Fallback Search Matter", "Fallback Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.commit()

    monkeypatch.setattr(content_routes, "build_matter_magic_snapshot", lambda *args, **kwargs: {})
    monkeypatch.setattr(content_routes, "build_matter_launch_pack", lambda *args, **kwargs: {"headline": "Ready"})

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.get("/search?q=fallback")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Smart Launch Pack" in body
    assert "Jump straight into the likely matter workflow." in body
    assert "Search found a likely matter match." in body
    assert "Search identified a likely matter, but no direct launch actions are available yet." in body


def test_matter_tasks_renders_task_radar_and_handoff(app_ctx):
    app = app_ctx
    user = _seed_user("task-radar@example.com")
    matter = _seed_matter(user, "2026-TASK-RADAR-1", "Task Radar Matter", "Radar Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.flush()
    db.session.add(
        Task(
            matter_id=matter.id,
            title="Overdue brief review",
            status="Todo",
            due_date=dt.date.today() - dt.timedelta(days=1),
            priority="High",
            created_by=user.id,
        )
    )
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.get(f"/matters/{matter.id}/tasks")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Task Radar" in body
    assert "Handoff Brief" in body
    assert "Overdue brief review" in body


def test_matter_tasks_handles_missing_tracker_payloads(monkeypatch, app_ctx):
    from intranet.routes import matters as matters_routes

    app = app_ctx
    user = _seed_user("task-tracker-fallback@example.com")
    matter = _seed_matter(user, "2026-TASK-FALLBACK-1", "Fallback Tasks Matter", "Fallback Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.commit()

    monkeypatch.setattr(matters_routes, "build_task_tracker_snapshot", lambda *args, **kwargs: {})

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.get(f"/matters/{matter.id}/tasks")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Task Radar" in body
    assert "No task radar signal is available yet." in body
    assert "No handoff brief is available yet." in body


def test_matter_workspace_renders_war_room_launch_pack(app_ctx):
    app = app_ctx
    user = _seed_user("war-room@example.com")
    matter = _seed_matter(user, "2026-WAR-ROOM-1", "War Room Matter", "War Room Client")
    matter.practice_area = "General Litigation"
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.add(
        Task(
            matter_id=matter.id,
            title="Prepare argument outline",
            status="Todo",
            due_date=dt.date.today() + dt.timedelta(days=1),
            created_by=user.id,
            priority="High",
        )
    )
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.get(f"/matters/{matter.id}/workspace")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Workspace Flight Deck" in body
    assert "Matter War Room" in body
    assert "Start Timer" in body
    assert "Draft Document" in body
    assert "War Room Brief" in body


def test_matter_workspace_handles_missing_snapshot_payloads(monkeypatch, app_ctx):
    from intranet.routes import matters_plus as matters_plus_routes

    app = app_ctx
    user = _seed_user("workspace-fallbacks@example.com")
    archetype = MatterTemplate(
        name="Fallback Archetype",
        legal_category="General Litigation",
        required_fields_json=json.dumps([]),
        created_by=user.id,
    )
    db.session.add(archetype)
    db.session.flush()
    matter = _seed_matter(user, "2026-WORKSPACE-FALLBACK-1", "Fallback Workspace Matter", "Fallback Client")
    matter.archetype_id = archetype.id
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.commit()

    monkeypatch.setattr(matters_plus_routes, "build_archetype_compliance_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(matters_plus_routes, "build_matter_magic_snapshot", lambda *args, **kwargs: {})
    monkeypatch.setattr(matters_plus_routes, "attach_matter_magic_links", lambda actions, matter_id: actions or [])
    monkeypatch.setattr(matters_plus_routes, "build_matter_launch_pack", lambda *args, **kwargs: {})

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.get(f"/matters/{matter.id}/workspace")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "No matter guidance is available yet." in body
    assert "Launch common matter actions from one place." in body


def test_matter_detail_handles_missing_snapshot_payloads(monkeypatch, app_ctx):
    from intranet.routes import matters as matters_routes

    app = app_ctx
    user = _seed_user("detail-fallbacks@example.com")
    archetype = MatterTemplate(
        name="Detail Fallback Archetype",
        legal_category="General Litigation",
        required_fields_json=json.dumps([]),
        created_by=user.id,
    )
    db.session.add(archetype)
    db.session.flush()
    matter = _seed_matter(user, "2026-DETAIL-FALLBACK-1", "Fallback Detail Matter", "Fallback Client")
    matter.archetype_id = archetype.id
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.commit()

    monkeypatch.setattr(matters_routes, "build_archetype_compliance_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(matters_routes, "build_matter_magic_snapshot", lambda *args, **kwargs: {})
    monkeypatch.setattr(matters_routes, "attach_matter_magic_links", lambda actions, matter_id: actions or [])

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.get(f"/matters/{matter.id}")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Matter Command Deck" in body
    assert "No matter guidance is available yet." in body
    assert "Compliance metrics are temporarily unavailable for this matter." in body


def test_sa_workflow_shortcuts_prefill_task_calendar_and_dms_forms(app_ctx):
    app = app_ctx
    user = _seed_user("sa-prefill@example.com")
    matter = _seed_matter(user, "2026-SA-PREFILL-1", "Workflow Matter", "Workflow Client")
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)

    task_response = client.get(
        f"/matters/{matter.id}/tasks/new"
        "?prefill_title=Prepare%20CCMA%20referral"
        "&prefill_due_date=2026-03-20"
        "&prefill_description=Confirm%20deadline%20and%20supporting%20papers"
    )
    task_body = task_response.get_data(as_text=True)
    assert task_response.status_code == 200
    assert 'value="Prepare CCMA referral"' in task_body
    assert 'value="2026-03-20"' in task_body
    assert "Confirm deadline and supporting papers" in task_body
    assert 'data-task-new-form' in task_body
    assert 'data-assignee-picker' in task_body
    assert "Ctrl/Cmd + click" not in task_body

    calendar_response = client.get(
        f"/calendar/matter/{matter.id}"
        "?prefill_deadline_title=File%20notice%20of%20set%20down"
        "&prefill_due_at=2026-03-21"
        "&prefill_event_title=High%20Court%20hearing"
        "&prefill_event_date=2026-03-28"
        "&prefill_event_description=Prepare%20bundle%20and%20counsel%20brief"
    )
    calendar_body = calendar_response.get_data(as_text=True)
    assert calendar_response.status_code == 200
    assert 'value="File notice of set down"' in calendar_body
    assert 'value="2026-03-21"' in calendar_body
    assert 'value="High Court hearing"' in calendar_body
    assert 'value="2026-03-28"' in calendar_body
    assert "Prepare bundle and counsel brief" in calendar_body

    dms_response = client.get(
        f"/matters/{matter.id}/dms"
        "?prefill_title=Client%20Advice%20-%202026-SA-PREFILL-1"
        "&prefill_document_type=Opinion"
        "&prefill_confidentiality=Confidential"
        "&prefill_privilege_label=Attorney-Client"
    )
    dms_body = dms_response.get_data(as_text=True)
    assert dms_response.status_code == 200
    assert 'value="Client Advice - 2026-SA-PREFILL-1"' in dms_body
    assert 'option value="Opinion" selected' in dms_body
    assert 'option value="Confidential" selected' in dms_body


def test_admin_can_seed_south_africa_practice_area_defaults(app_ctx):
    app = app_ctx
    admin = _seed_user("practice-area-admin@example.com", role="admin", mfa_enabled=True)
    client = app.test_client()
    _set_user_session(client, admin.id)

    response = client.post(
        "/admin/settings/practice-areas",
        data={
            "csrf_token": "test-csrf",
            "action": "seed_south_africa",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    names = {row.name for row in PracticeArea.query.order_by(PracticeArea.name.asc()).all()}
    assert set(DEFAULT_SA_PRACTICE_AREAS).issubset(names)


def test_admin_can_seed_south_africa_playbooks(app_ctx):
    app = app_ctx
    admin = _seed_user("playbook-admin@example.com", role="admin", mfa_enabled=True)
    client = app.test_client()
    _set_user_session(client, admin.id)

    response = client.post(
        "/admin/templates/matters",
        data={
            "csrf_token": "test-csrf",
            "action": "seed_south_africa",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    template_names = {row.name for row in MatterTemplate.query.order_by(MatterTemplate.name.asc()).all()}
    document_names = {row.name for row in DocumentTemplate.query.order_by(DocumentTemplate.name.asc()).all()}
    contract_names = {row.name for row in ContractTemplate.query.order_by(ContractTemplate.name.asc()).all()}
    task_template_names = {row.name for row in TaskTemplate.query.order_by(TaskTemplate.name.asc()).all()}

    assert "SA Conveyancing - Property Transfer" in template_names
    assert "SA Deceased Estate Administration" in template_names
    assert "SA CCMA Unfair Dismissal" in template_names
    assert "SA High Court Civil Litigation" in template_names
    assert "SA Conveyancing Opening Pack" in document_names
    assert "SA Litigation Engagement Letter" in contract_names
    assert "SA High Court Litigation Checklist" in task_template_names
