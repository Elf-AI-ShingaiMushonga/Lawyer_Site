from __future__ import annotations

import datetime as dt
from intranet.timeutils import utc_now
import io
import json
import os
import time

from intranet.extensions import db
from intranet.mfa import _totp, generate_totp_secret
from intranet.models import (
    CRMLead,
    ConflictCheck,
    EngagementLetter,
    IntakeForm,
    Invoice,
    LeadQuote,
    Matter,
    MatterTemplate,
    MatterMember,
    PaymentAllocation,
    PortalLinkToken,
    PortalMatterAccess,
    PortalMessage,
    PortalPaymentReceipt,
    PortalUpload,
    PortalUser,
    RateCard,
    Task,
    TimeEntry,
    TrustAccount,
    TrustClientLedger,
    TrustReconciliationRun,
    User,
)


def _seed_admin_user(email: str = "smoke-admin@example.com", password: str = "TestPassword123!") -> tuple[User, str, str]:
    secret = generate_totp_secret()
    user = User(
        email=email,
        full_name="Smoke Admin",
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


def _set_csrf(client, token: str = "test-csrf") -> str:
    with client.session_transaction() as sess:
        sess["_csrf_token"] = token
    return token


def _login(client, email: str, password: str, mfa_secret: str, csrf_token: str) -> None:
    code = _totp(mfa_secret, int(time.time() // 30))
    response = client.post(
        "/login",
        data={
            "csrf_token": csrf_token,
            "email": email,
            "password": password,
            "mfa_code": code,
        },
    )
    assert response.status_code == 302
    assert "/dashboard" in (response.headers.get("Location") or "")


def _create_matter_via_route(client, csrf_token: str, *, matter_no: str, title: str, client_name: str) -> Matter:
    archetype = MatterTemplate.query.filter_by(name="Smoke Negligence Clause").first()
    if archetype is None:
        owner = User.query.order_by(User.id.asc()).first()
        assert owner is not None
        archetype = MatterTemplate(
            name="Smoke Negligence Clause",
            legal_category="Labour Law",
            default_risk_level="Medium",
            required_fields_json=json.dumps(
                [{"key": "incident_date", "label": "Incident Date", "help": ""}],
                ensure_ascii=True,
            ),
            boilerplate_template=(
                "Matter {{ matter_no }} for {{ client_name }} in {{ legal_category }}. "
                "Incident date: {{ incident_date }}."
            ),
            created_by=owner.id,
        )
        db.session.add(archetype)
        db.session.commit()

    response = client.post(
        "/matters/new",
        data={
            "csrf_token": csrf_token,
            "matter_no": matter_no,
            "title": title,
            "client_name": client_name,
            "legal_category": "Labour Law",
            "archetype_id": archetype.id,
            "field_incident_date": dt.date.today().isoformat(),
            "status": "Open",
            "risk_level": "Medium",
            "budget_status": "On Track",
        },
    )
    assert response.status_code == 302
    row = Matter.query.filter_by(matter_no=matter_no).first()
    assert row is not None
    return row


def test_smoke_core_matter_time_billing_flow(app_ctx):
    app = app_ctx
    user, password, secret = _seed_admin_user()
    client = app.test_client()
    csrf_token = _set_csrf(client)
    _login(client, user.email, password, secret, csrf_token)

    matter = _create_matter_via_route(
        client,
        csrf_token,
        matter_no="2026-SMOKE-CORE-0001",
        title="Core Workflow Matter",
        client_name="Smoke Client Core",
    )

    response = client.post(
        f"/matters/{matter.id}/tasks/new",
        data={
            "csrf_token": csrf_token,
            "title": "Draft strategy memo",
            "description": "Draft and circulate first strategy memo.",
            "due_date": dt.date.today().isoformat(),
            "assignee_user_ids": str(user.id),
        },
    )
    assert response.status_code == 302
    task = Task.query.filter_by(matter_id=matter.id).order_by(Task.id.desc()).first()
    assert task is not None

    start_at = (utc_now() - dt.timedelta(hours=2)).replace(second=0, microsecond=0)
    end_at = start_at + dt.timedelta(hours=1, minutes=15)
    response = client.post(
        "/time/entries",
        data={
            "csrf_token": csrf_token,
            "matter_id": matter.id,
            "task_id": task.id,
            "start_at": start_at.isoformat(),
            "end_at": end_at.isoformat(),
            "narrative": "Prepared detailed strategy memo and coordinated task assignments.",
            "is_billable": "1",
        },
    )
    assert response.status_code == 302
    entry = TimeEntry.query.filter_by(matter_id=matter.id, user_id=user.id).order_by(TimeEntry.id.desc()).first()
    assert entry is not None

    response = client.post(
        "/time/review",
        data={"csrf_token": csrf_token, "entry_id": entry.id, "state": "approved"},
    )
    assert response.status_code == 302
    db.session.refresh(entry)
    assert entry.status == "approved"

    response = client.post(
        "/billing/rates",
        data={
            "csrf_token": csrf_token,
            "name": "Smoke Rate",
            "matter_id": matter.id,
            "user_id": user.id,
            "rate_per_hour": "350",
            "currency": "USD",
        },
    )
    assert response.status_code == 302
    assert RateCard.query.filter_by(matter_id=matter.id, user_id=user.id).count() >= 1

    period_date = start_at.date().isoformat()
    response = client.post(
        "/billing/invoices",
        data={
            "csrf_token": csrf_token,
            "matter_id": matter.id,
            "period_start": period_date,
            "period_end": period_date,
        },
    )
    assert response.status_code == 302
    invoice = Invoice.query.filter_by(matter_id=matter.id).order_by(Invoice.id.desc()).first()
    assert invoice is not None

    response = client.post(f"/billing/invoices/{invoice.id}/approve", data={"csrf_token": csrf_token})
    assert response.status_code == 302
    db.session.refresh(invoice)
    assert invoice.status == "approved"

    lock_start_at = end_at + dt.timedelta(hours=1)
    lock_end_at = lock_start_at + dt.timedelta(minutes=45)
    response = client.post(
        "/time/entries",
        data={
            "csrf_token": csrf_token,
            "matter_id": matter.id,
            "start_at": lock_start_at.isoformat(),
            "end_at": lock_end_at.isoformat(),
            "narrative": "Prepared lock-cycle admin follow-up notes.",
            "is_billable": "1",
        },
    )
    assert response.status_code == 302
    lock_entry = TimeEntry.query.filter_by(matter_id=matter.id, user_id=user.id).order_by(TimeEntry.id.desc()).first()
    assert lock_entry is not None
    assert lock_entry.id != entry.id

    response = client.post(
        "/time/review",
        data={"csrf_token": csrf_token, "entry_id": lock_entry.id, "state": "approved"},
    )
    assert response.status_code == 302
    response = client.post(f"/time/entries/{lock_entry.id}/lock", data={"csrf_token": csrf_token})
    assert response.status_code == 302
    db.session.refresh(lock_entry)
    assert lock_entry.locked_at is not None

    payment_amount = max(10.0, round(float(invoice.total or 0.0) / 2.0, 2))
    response = client.post(
        f"/billing/invoices/{invoice.id}/payments",
        data={
            "csrf_token": csrf_token,
            "amount": str(payment_amount),
            "status": "settled",
            "method": "eft",
            "reference": "SMOKE-PAY-CORE-1",
        },
    )
    assert response.status_code == 302
    payment = PaymentAllocation.query.filter_by(invoice_id=invoice.id).order_by(PaymentAllocation.id.desc()).first()
    assert payment is not None
    assert (payment.status or "").lower() == "settled"

    for route in [
        f"/billing/invoices/{invoice.id}",
        f"/billing/invoices/{invoice.id}/pdf",
        f"/billing/invoices/{invoice.id}/tax-invoice",
        f"/billing/invoices/{invoice.id}/ledes",
        f"/billing/accounts/{matter.id}/statement",
        "/billing/ar-aging",
        "/billing/transactions",
        "/billing/audit-log",
    ]:
        response = client.get(route)
        assert response.status_code == 200, route


def test_smoke_trust_crm_portal_flow(app_ctx):
    app = app_ctx
    user, password, secret = _seed_admin_user(email="smoke-admin-2@example.com")
    client = app.test_client()
    csrf_token = _set_csrf(client)
    _login(client, user.email, password, secret, csrf_token)

    matter = _create_matter_via_route(
        client,
        csrf_token,
        matter_no="2026-SMOKE-CROSS-0001",
        title="Cross Module Workflow Matter",
        client_name="Smoke Client Cross",
    )
    if MatterMember.query.filter_by(matter_id=matter.id, user_id=user.id).first() is None:
        db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Responsible"))
        db.session.commit()

    account = TrustAccount(name="Smoke Trust Main", currency="ZAR", is_active=True)
    db.session.add(account)
    db.session.flush()
    source_ledger = TrustClientLedger(
        trust_account_id=account.id,
        client_name=matter.client_name,
        matter_id=matter.id,
        current_balance=0.0,
    )
    target_ledger = TrustClientLedger(
        trust_account_id=account.id,
        client_name=f"{matter.client_name} Reserve",
        matter_id=matter.id,
        current_balance=0.0,
    )
    db.session.add_all([source_ledger, target_ledger])
    db.session.commit()

    response = client.post(
        "/trust/deposits",
        data={
            "csrf_token": csrf_token,
            "trust_account_id": account.id,
            "client_ledger_id": source_ledger.id,
            "amount": "1500",
            "currency": "ZAR",
            "description": "Initial deposit",
        },
    )
    assert response.status_code == 302

    response = client.post(
        "/trust/disbursements",
        data={
            "csrf_token": csrf_token,
            "trust_account_id": account.id,
            "client_ledger_id": source_ledger.id,
            "amount": "250",
            "currency": "ZAR",
            "description": "Counsel fees",
        },
    )
    assert response.status_code == 302

    response = client.post(
        "/trust/transfers",
        data={
            "csrf_token": csrf_token,
            "trust_account_id": account.id,
            "source_ledger_id": source_ledger.id,
            "target_ledger_id": target_ledger.id,
            "amount": "100",
            "currency": "ZAR",
        },
    )
    assert response.status_code == 302

    period_start = (utc_now() - dt.timedelta(days=1)).replace(microsecond=0, second=0)
    period_end = utc_now().replace(microsecond=0, second=0)
    response = client.post(
        "/trust/reconciliations",
        data={
            "csrf_token": csrf_token,
            "trust_account_id": account.id,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "bank_closing_balance": "1250",
        },
    )
    assert response.status_code == 302
    assert TrustReconciliationRun.query.filter_by(trust_account_id=account.id).count() >= 1

    for route in [
        "/trust/ledger",
        "/trust/reconciliations",
        "/trust/cashbook",
        "/trust/section86",
        "/trust/reports/monthly",
        "/trust/reports/trial-balance",
        "/trust/reports/auditor",
    ]:
        response = client.get(route)
        assert response.status_code == 200, route

    response = client.post(
        "/crm/leads",
        data={
            "csrf_token": csrf_token,
            "full_name": "Smoke Lead",
            "organization": "Smoke Prospect Ltd",
            "email": "lead-smoke@example.com",
            "phone": "+1-555-0100",
            "source": "website",
            "notes": "Interested in litigation support.",
            "assigned_to": user.id,
        },
    )
    assert response.status_code == 302
    lead = CRMLead.query.filter_by(email="lead-smoke@example.com").first()
    assert lead is not None

    response = client.post(
        f"/crm/leads/{lead.id}",
        data={
            "csrf_token": csrf_token,
            "action": "follow_up",
            "due_at": (utc_now() + dt.timedelta(days=1)).replace(second=0, microsecond=0).isoformat(timespec="minutes"),
            "note": "Send onboarding checklist.",
        },
    )
    assert response.status_code == 302

    response = client.post(
        f"/crm/leads/{lead.id}",
        data={
            "csrf_token": csrf_token,
            "action": "quote_create",
            "quote_title": "Smoke Proposal",
            "fee_model": "fixed",
            "currency": "USD",
            "estimated_amount": "4200",
            "disbursement_estimate": "250",
            "tax_rate": "15",
            "scope_summary": "Initial engagement scope",
            "assumptions": "Client provides documents within 3 days",
        },
    )
    assert response.status_code == 302
    quote = LeadQuote.query.filter_by(lead_id=lead.id).order_by(LeadQuote.id.desc()).first()
    assert quote is not None

    response = client.post(
        f"/crm/quotes/{quote.id}/status",
        data={"csrf_token": csrf_token, "status": "sent", "status_note": "Issued to client."},
    )
    assert response.status_code == 302

    response = client.post(
        f"/crm/leads/{lead.id}",
        data={
            "csrf_token": csrf_token,
            "action": "intake",
            "matter_id_select": matter.id,
        },
    )
    assert response.status_code == 302
    intake = IntakeForm.query.filter_by(lead_id=lead.id).order_by(IntakeForm.id.desc()).first()
    assert intake is not None

    response = client.post("/crm/conflicts/check", data={"csrf_token": csrf_token, "intake_id": intake.id})
    assert response.status_code == 302
    conflict = ConflictCheck.query.filter_by(intake_form_id=intake.id).order_by(ConflictCheck.id.desc()).first()
    assert conflict is not None
    response = client.get(f"/crm/conflicts/{conflict.id}/export")
    assert response.status_code == 200

    response = client.post(
        f"/crm/leads/{lead.id}",
        data={
            "csrf_token": csrf_token,
            "action": "engagement",
            "matter_id_select": matter.id,
            "template_name": "default",
            "content": "Engagement terms for smoke test.",
        },
    )
    assert response.status_code == 302
    letter = EngagementLetter.query.filter_by(matter_id=matter.id).order_by(EngagementLetter.id.desc()).first()
    assert letter is not None

    response = client.post(
        f"/crm/engagements/{letter.id}/sign",
        data={"csrf_token": csrf_token, "signer_name": "Client Signatory"},
    )
    assert response.status_code == 302

    response = client.post(
        "/admin/portal/users",
        data={
            "csrf_token": csrf_token,
            "action": "create_user",
            "email": "portal-smoke@example.com",
            "full_name": "Portal Smoke User",
            "password": "PortalPassword123!",
        },
    )
    assert response.status_code == 302
    portal_user = PortalUser.query.filter_by(email="portal-smoke@example.com").first()
    assert portal_user is not None

    response = client.post(
        "/admin/portal/users",
        data={
            "csrf_token": csrf_token,
            "action": "grant_access",
            "portal_user_id": portal_user.id,
            "matter_id": matter.id,
            "visibility_level": "full_curated",
        },
    )
    assert response.status_code == 302
    assert PortalMatterAccess.query.filter_by(portal_user_id=portal_user.id, matter_id=matter.id, revoked_at=None).count() == 1

    invoice = Invoice(
        matter_id=matter.id,
        client_name=matter.client_name,
        period_start=dt.date.today() - dt.timedelta(days=7),
        period_end=dt.date.today(),
        status="approved",
        subtotal=500.0,
        tax_total=75.0,
        total=575.0,
        created_by=user.id,
    )
    db.session.add(invoice)
    db.session.commit()

    response = client.post(
        "/portal/login",
        data={"csrf_token": csrf_token, "email": portal_user.email, "password": "PortalPassword123!"},
    )
    assert response.status_code == 302
    assert "/portal/matters" in (response.headers.get("Location") or "")

    for route in [
        "/portal/matters",
        f"/portal/matters/{matter.id}",
        "/portal/messages",
        "/portal/uploads",
        "/portal/invoices",
        f"/portal/payments/{invoice.id}",
        "/portal/links",
    ]:
        response = client.get(route)
        assert response.status_code == 200, route

    response = client.post(
        "/portal/messages",
        data={
            "csrf_token": csrf_token,
            "matter_id": matter.id,
            "subject": "Smoke Update",
            "body": "Please confirm receipt of latest bundle.",
        },
    )
    assert response.status_code == 302
    assert PortalMessage.query.count() >= 1

    response = client.post(
        "/portal/uploads",
        data={
            "csrf_token": csrf_token,
            "matter_id": matter.id,
            "file": (io.BytesIO(b"portal smoke upload"), "portal_smoke.txt"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    upload = PortalUpload.query.order_by(PortalUpload.id.desc()).first()
    assert upload is not None
    upload_path = os.path.join(app.config["UPLOAD_DIR"], upload.stored_filename)
    assert os.path.isfile(upload_path)

    response = client.post(
        f"/portal/payments/{invoice.id}",
        data={
            "csrf_token": csrf_token,
            "amount": "125",
            "currency": "USD",
            "reference": "PORTAL-SMOKE-PAY-1",
        },
    )
    assert response.status_code == 302
    assert PortalPaymentReceipt.query.filter_by(invoice_id=invoice.id, portal_user_id=portal_user.id).count() >= 1

    response = client.post(
        "/portal/links",
        data={
            "csrf_token": csrf_token,
            "matter_id": matter.id,
            "expires_minutes": "30",
        },
    )
    assert response.status_code == 302
    assert PortalLinkToken.query.filter_by(portal_user_id=portal_user.id, matter_id=matter.id).count() >= 1

    with client.session_transaction() as sess:
        link_path = sess.get("portal_last_link_url")
    assert link_path
    response = client.get(link_path)
    assert response.status_code == 302
    assert f"/portal/matters/{matter.id}" in (response.headers.get("Location") or "")

    response = client.post("/portal/logout", data={"csrf_token": csrf_token})
    assert response.status_code == 302

    if os.path.isfile(upload_path):
        os.remove(upload_path)
