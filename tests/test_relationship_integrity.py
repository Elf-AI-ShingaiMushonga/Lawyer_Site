from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import os
import time

import pytest

from intranet.extensions import db
from intranet.jobs.worker import _handle_retention_archive_sweep
from intranet.mfa import _totp, generate_totp_secret
from intranet.models import (
    AuditLog,
    ConflictCheck,
    DataResidencyPolicy,
    Deadline,
    DocumentRecord,
    DocumentOCRText,
    DocumentVersion,
    EmailCapture,
    EthicalWall,
    EthicalWallMatter,
    EthicalWallRule,
    ExpenseEntry,
    IntakeForm,
    InvoiceAdjustment,
    Invoice,
    LegalHold,
    PortalLinkToken,
    RetentionPolicy,
    SavedSearch,
    Matter,
    MatterMember,
    MatterNote,
    MatterNoteACL,
    MatterTimelineEvent,
    Notification,
    PortalMatterAccess,
    PortalMessage,
    PortalUser,
    TimeEntry,
    TrustAccount,
    TrustApprovalRequest,
    TrustClientLedger,
    TrustLedgerEntry,
    TrustReconciliationRun,
    TrustedDevice,
    UserSession,
    User,
)
from intranet.services.billing_engine import BillingEngine
from intranet.services.trust_engine import TrustEngine


def _set_user_session(client, user_id: int, csrf_token: str = "test-csrf") -> None:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["_csrf_token"] = csrf_token


def _set_portal_session(client, portal_user_id: int, csrf_token: str = "test-csrf") -> None:
    with client.session_transaction() as sess:
        sess["portal_user_id"] = portal_user_id
        sess["_csrf_token"] = csrf_token


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
        opened_at=dt.datetime.utcnow(),
        last_updated_at=dt.datetime.utcnow(),
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
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.get(f"/crm/conflicts/{conflict.id}/export")

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    payload = response.get_data(as_text=True)
    assert "check_id,status,intake_id,matches" in payload
    assert "entity:Acme" in payload


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
            expires_at=dt.datetime.utcnow() - dt.timedelta(minutes=1),
        )
    )
    db.session.commit()

    client = app.test_client()
    response = client.get(f"/portal/link/{raw_token}")
    assert response.status_code == 410


def test_revoked_user_session_forces_relogin(app_ctx):
    app = app_ctx
    user = _seed_user("revoked-session@example.com")
    now = dt.datetime.utcnow()
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
    now = dt.datetime.utcnow()
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
    now = dt.datetime.utcnow()

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
    now = dt.datetime.utcnow()
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
        opened_at=dt.datetime.utcnow(),
        last_updated_at=dt.datetime.utcnow(),
    )
    db.session.add(matter)
    db.session.flush()
    db.session.add(LegalHold(matter_id=matter.id, reason="Delete restricted", is_active=True, created_by=user.id))
    db.session.commit()

    db.session.delete(matter)
    with pytest.raises(Exception):
        db.session.commit()
    db.session.rollback()
