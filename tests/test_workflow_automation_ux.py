from __future__ import annotations

import datetime as dt
from intranet.timeutils import utc_now
import io

from intranet.extensions import db
from intranet.models import (
    DocumentFile,
    EngagementLetter,
    Invoice,
    InvoiceLine,
    Matter,
    MatterMember,
    PaymentAllocation,
    PortalMatterAccess,
    PortalUpload,
    PortalUser,
    Task,
    TimeEntry,
    TimeTimer,
    User,
)


def _set_user_session(client, user_id: int, csrf_token: str = "test-csrf") -> None:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["_csrf_token"] = csrf_token


def _set_portal_session(client, portal_user_id: int, csrf_token: str = "test-csrf") -> None:
    with client.session_transaction() as sess:
        sess["portal_user_id"] = portal_user_id
        sess["_csrf_token"] = csrf_token


def _seed_user(email: str, *, role: str = "lawyer") -> User:
    row = User(
        email=email,
        full_name=email.split("@", 1)[0],
        role=role,
        password_hash="x",
        is_active=True,
        mfa_enabled=True,
    )
    row.set_password("TestPassword123!")
    db.session.add(row)
    db.session.flush()
    return row


def _seed_matter(owner: User, matter_no: str) -> Matter:
    row = Matter(
        matter_no=matter_no,
        title=f"Matter {matter_no}",
        client_name="Automation Client",
        status="Open",
        created_by=owner.id,
        opened_at=utc_now(),
        last_updated_at=utc_now(),
    )
    db.session.add(row)
    db.session.flush()
    db.session.add(MatterMember(matter_id=row.id, user_id=owner.id, role_in_matter="Lead"))
    db.session.flush()
    return row


def test_timer_pause_auto_creates_time_entry_and_draft_billing_item(app_ctx):
    app = app_ctx
    user = _seed_user("auto-timer@example.com", role="lawyer")
    matter = _seed_matter(user, "2026-AUTO-TIMER-001")
    timer = TimeTimer(
        user_id=user.id,
        matter_id=matter.id,
        task_id=None,
        label="Timer capture test",
        started_at=utc_now() - dt.timedelta(minutes=30),
        elapsed_seconds=0,
        status="running",
    )
    db.session.add(timer)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.post(
        "/time/timers/pause",
        data={
            "csrf_token": "test-csrf",
            "timer_id": timer.id,
            "auto_capture": "1",
            "auto_create_billing_item": "1",
        },
    )
    assert response.status_code == 302

    entry = TimeEntry.query.filter_by(user_id=user.id, matter_id=matter.id).order_by(TimeEntry.id.desc()).first()
    assert entry is not None
    assert "Timer capture test" in (entry.narrative or "")
    assert entry.status == "draft"

    marker = f"[time_entry:{entry.id}]"
    line = InvoiceLine.query.filter(InvoiceLine.description.ilike(f"%{marker}%")).first()
    assert line is not None
    invoice = db.session.get(Invoice, line.invoice_id)
    assert invoice is not None
    assert invoice.status == "draft"


def test_task_done_redirects_to_prefilled_time_entry_form(app_ctx):
    app = app_ctx
    user = _seed_user("auto-task@example.com", role="lawyer")
    matter = _seed_matter(user, "2026-AUTO-TASK-001")
    task = Task(
        matter_id=matter.id,
        title="Prepare hearing bundle",
        description="Bundle and index documents for hearing",
        status="Doing",
        due_date=dt.date.today(),
        assigned_to=user.id,
        created_by=user.id,
    )
    db.session.add(task)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.post(
        f"/tasks/{task.id}/status",
        data={
            "csrf_token": "test-csrf",
            "status": "Done",
            "suggest_time_on_done": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    location = response.headers.get("Location") or ""
    assert "/time/entries" in location
    assert f"matter_id={matter.id}" in location
    assert f"task_id={task.id}" in location
    assert "narrative=Completed+task:+Prepare+hearing+bundle" in location


def test_portal_upload_auto_files_to_dms_and_creates_review_task(app_ctx, tmp_path):
    app = app_ctx
    app.config["UPLOAD_DIR"] = str(tmp_path)

    admin = _seed_user("portal-auto-admin@example.com", role="admin")
    matter = _seed_matter(admin, "2026-AUTO-PORTAL-001")
    portal_user = PortalUser(
        email="portal-auto@example.com",
        full_name="Portal Auto",
        password_hash="x",
        is_active=True,
    )
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
    db.session.commit()

    client = app.test_client()
    _set_portal_session(client, portal_user.id)
    response = client.post(
        "/portal/uploads",
        data={
            "csrf_token": "test-csrf",
            "matter_id": matter.id,
            "file": (io.BytesIO(b"client upload"), "client_upload.txt"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert response.status_code == 302

    upload = PortalUpload.query.filter_by(matter_id=matter.id, portal_user_id=portal_user.id).order_by(PortalUpload.id.desc()).first()
    assert upload is not None

    dms_row = DocumentFile.query.filter_by(owner_name=f"portal_upload:{upload.id}").first()
    assert dms_row is not None
    assert dms_row.matter_id == matter.id

    review_task = (
        Task.query.filter(
            Task.matter_id == matter.id,
            Task.description.isnot(None),
            Task.description.ilike(f"%[portal_upload:{upload.id}]%"),
        )
        .order_by(Task.id.desc())
        .first()
    )
    assert review_task is not None


def test_engagement_sign_auto_creates_kickoff_tasks(app_ctx):
    app = app_ctx
    lawyer = _seed_user("engage-auto@example.com", role="lawyer")
    matter = _seed_matter(lawyer, "2026-AUTO-ENGAGE-001")
    letter = EngagementLetter(
        matter_id=matter.id,
        template_name="default",
        content="Engagement terms",
        status="pending_signature",
        created_by=lawyer.id,
    )
    db.session.add(letter)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, lawyer.id)
    response = client.post(
        f"/crm/engagements/{letter.id}/sign",
        data={"csrf_token": "test-csrf", "signer_name": "Client Auto"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "/workspace" in (response.headers.get("Location") or "")

    created = (
        Task.query.filter(
            Task.matter_id == matter.id,
            Task.description.isnot(None),
            Task.description.ilike(f"%[engagement:{letter.id}]%"),
        )
        .order_by(Task.id.asc())
        .all()
    )
    assert len(created) == 2


def test_settled_payment_auto_reconciles_invoice_status(app_ctx):
    app = app_ctx
    lawyer = _seed_user("invoice-auto@example.com", role="lawyer")
    matter = _seed_matter(lawyer, "2026-AUTO-PAY-001")
    invoice = Invoice(
        matter_id=matter.id,
        client_name=matter.client_name,
        period_start=dt.date.today().replace(day=1),
        period_end=dt.date.today(),
        status="approved",
        subtotal=100.0,
        tax_total=0.0,
        total=100.0,
        created_by=lawyer.id,
        approved_by=lawyer.id,
        approved_at=utc_now(),
    )
    db.session.add(invoice)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, lawyer.id)
    response = client.post(
        f"/billing/invoices/{invoice.id}/payments",
        data={
            "csrf_token": "test-csrf",
            "amount": "100.00",
            "status": "settled",
            "method": "eft",
            "reference": "AUTO-PAY-100",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    payment = PaymentAllocation.query.filter_by(invoice_id=invoice.id).order_by(PaymentAllocation.id.desc()).first()
    assert payment is not None
    db.session.refresh(invoice)
    assert invoice.status == "paid"
