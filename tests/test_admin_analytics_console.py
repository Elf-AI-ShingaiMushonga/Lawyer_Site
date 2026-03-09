from __future__ import annotations

import datetime as dt

from flask import g

from intranet.extensions import db
from intranet.models import (
    Announcement,
    AuditLog,
    BurnoutSignal,
    ContractTemplate,
    DeadlineRule,
    DocumentTemplate,
    GovernanceIncident,
    Invoice,
    LegalHold,
    Matter,
    MatterTemplate,
    Office,
    PaymentAllocation,
    PortalUser,
    PracticeArea,
    RateCard,
    RetentionPolicy,
    Task,
    TaskAssignee,
    TaskTemplate,
    TimeEntry,
    TrustApprovalRequest,
    User,
    WorkloadForecast,
)
from intranet.timeutils import utc_now


def _set_internal_session(client, user_id: int, csrf_token: str = "test-csrf") -> str:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["_csrf_token"] = csrf_token
    if hasattr(g, "_login_user"):
        delattr(g, "_login_user")
    return csrf_token


def _seed_admin(email: str = "admin-console@example.com") -> User:
    user = User(
        email=email,
        full_name="Admin Console",
        role="admin",
        password_hash="x",
        is_active=True,
        mfa_enabled=True,
        mfa_secret="TESTMFATESTMFATESTMFATESTMFATEST12",
    )
    user.set_password("TestPassword123!")
    db.session.add(user)
    db.session.commit()
    return user


def test_admin_console_renders_command_center(app_ctx):
    admin = _seed_admin()
    db.session.add(
        User(
            email="ops-gap@example.com",
            full_name="Ops Gap",
            role="operations_staff",
            password_hash="x",
            is_active=True,
            mfa_enabled=False,
        )
    )
    portal_user = PortalUser(email="client@example.com", full_name="Client User", password_hash="x", is_active=True)
    portal_user.set_password("TestPassword123!")
    matter = Matter(
        matter_no="2026-ADMIN-0001",
        title="Admin Matter",
        client_name="Admin Client",
        status="Open",
        created_by=admin.id,
        opened_at=utc_now(),
        last_updated_at=utc_now(),
    )
    db.session.add_all(
        [
            portal_user,
            Office(name="Johannesburg", jurisdiction="ZA", is_active=True),
            PracticeArea(name="Litigation", is_active=True),
            MatterTemplate(name="Admin Archetype", legal_category="Litigation", created_by=admin.id),
            TaskTemplate(name="Admin Task Template", created_by=admin.id),
            DocumentTemplate(name="Admin Document Template", template_type="Letter", body="Template body", created_by=admin.id),
            ContractTemplate(name="Admin Contract Template", contract_type="Engagement", body="Contract body", created_by=admin.id),
            RateCard(name="Standard", user_id=admin.id, rate_per_hour=2500.0, currency="ZAR", is_active=True),
            DeadlineRule(name="Court Rule", trigger_type="filing", offset_days=5, created_by=admin.id, is_active=True),
            RetentionPolicy(name="General Retention", retain_days=365, is_active=True),
            Announcement(title="System Notice", body="Platform maintenance window.", created_by=admin.id),
            GovernanceIncident(title="Access review", incident_type="Access", severity="High", status="Open", summary="Review elevated access.", created_by=admin.id),
            TrustApprovalRequest(action_type="disbursement", payload_json='{"amount": 1000}', status="pending", requested_by=admin.id),
            matter,
        ]
    )
    db.session.flush()
    db.session.add(LegalHold(matter_id=matter.id, reason="Preserve file", created_by=admin.id, is_active=True))
    db.session.add(AuditLog(actor_user_id=admin.id, action="user_create", entity_type="User", entity_id=admin.id))
    db.session.commit()

    client = app_ctx.test_client()
    _set_internal_session(client, admin.id)

    response = client.get("/admin")

    assert response.status_code == 200
    assert b"Admin Command Center" in response.data
    assert b"Control Watchlist" in response.data
    assert b"Configuration Coverage" in response.data
    assert b"Recent Audit Activity" in response.data


def test_analytics_command_center_and_workload_pages_render(app_ctx):
    admin = _seed_admin(email="analytics-admin@example.com")
    matter = Matter(
        matter_no="2026-AN-0001",
        title="Analytics Matter",
        client_name="Analytics Client",
        status="Open",
        created_by=admin.id,
        opened_at=utc_now(),
        last_updated_at=utc_now(),
    )
    db.session.add(matter)
    db.session.flush()

    task = Task(
        matter_id=matter.id,
        title="Prepare analytics bundle",
        status="Todo",
        due_date=dt.date.today() + dt.timedelta(days=1),
        priority="High",
        created_by=admin.id,
        assigned_to=admin.id,
    )
    db.session.add(task)
    db.session.flush()
    db.session.add(TaskAssignee(task_id=task.id, user_id=admin.id, assigned_by=admin.id))
    db.session.add(
        TimeEntry(
            user_id=admin.id,
            matter_id=matter.id,
            task_id=task.id,
            start_at=utc_now() - dt.timedelta(hours=6),
            end_at=utc_now() - dt.timedelta(hours=2),
            hours=4.0,
            rounded_hours=4.0,
            narrative="Prepared litigation analytics for review.",
            is_billable=True,
            status="approved",
        )
    )
    invoice = Invoice(
        matter_id=matter.id,
        client_name=matter.client_name,
        period_start=dt.date(2026, 3, 1),
        period_end=dt.date(2026, 3, 31),
        status="approved",
        subtotal=1000.0,
        tax_total=150.0,
        total=1150.0,
        created_by=admin.id,
    )
    db.session.add(invoice)
    db.session.flush()
    db.session.add(
        PaymentAllocation(
            invoice_id=invoice.id,
            amount=850.0,
            status="settled",
            allocated_at=utc_now(),
            created_by=admin.id,
        )
    )
    db.session.add(WorkloadForecast(as_of_date=dt.date.today(), user_id=admin.id, predicted_hours=44.0, confidence=0.82))
    db.session.add(BurnoutSignal(user_id=admin.id, as_of_date=dt.date.today(), score=0.78, reason="Sustained trial prep load", status="open"))
    db.session.commit()

    client = app_ctx.test_client()
    _set_internal_session(client, admin.id)

    overview = client.get("/analytics")
    workload = client.get("/analytics/workload")

    assert overview.status_code == 200
    assert b"Analytics Command Center" in overview.data
    assert b"Revenue and Capacity Signal" in overview.data
    assert b"Intervention Queue" in overview.data

    assert workload.status_code == 200
    assert b"Capacity Pressure" in workload.data
    assert b"Who is carrying the work" in workload.data
