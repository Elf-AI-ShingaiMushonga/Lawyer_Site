from __future__ import annotations

import datetime as dt
from intranet.timeutils import utc_now
import io
import json

from intranet.extensions import db
from intranet.jobs.scheduler import DEFAULT_PERIODIC_JOBS
from intranet.jobs.worker import _handle_priority_inbox_digest
from intranet.models import (
    CRMFollowUp,
    CRMLead,
    DocumentRecord,
    FirmSetting,
    Matter,
    Notification,
    PortalMessage,
    PortalMessageThread,
    PortalUser,
    ScheduledJob,
    TimeEntry,
    User,
)
from intranet.services.priority_inbox import PRIORITY_INBOX_CONFIG_KEY, save_priority_inbox_config


def _set_user_session(client, user_id: int, csrf_token: str = "test-csrf") -> None:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["_csrf_token"] = csrf_token


def _seed_admin(email: str) -> User:
    user = User(
        email=email,
        full_name="Priority Admin",
        role="admin",
        password_hash="x",
        is_active=True,
        mfa_enabled=True,
    )
    user.set_password("TestPassword123!")
    db.session.add(user)
    db.session.flush()
    return user


def _seed_matter(owner: User, matter_no: str = "2026-PRI-0001") -> Matter:
    row = Matter(
        matter_no=matter_no,
        title="Priority Inbox Matter",
        client_name="Priority Client",
        status="Open",
        created_by=owner.id,
        opened_at=utc_now(),
        last_updated_at=utc_now(),
    )
    db.session.add(row)
    db.session.flush()
    return row


def test_admin_priority_inbox_settings_persist_and_sync_schedule(app_ctx):
    app = app_ctx
    admin = _seed_admin("priority-settings-admin@example.com")
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, admin.id)
    response = client.post(
        "/admin/settings/firm",
        data={
            "csrf_token": "test-csrf",
            "action": "priority_inbox",
            "portal_response_sla_hours": "5",
            "followup_horizon_hours": "26",
            "billing_capture_sla_hours": "54",
            "digest_enabled": "1",
            "digest_interval_minutes": "45",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers.get("Location", "").endswith("/admin/settings/firm")

    row = FirmSetting.query.filter_by(setting_key=PRIORITY_INBOX_CONFIG_KEY).first()
    assert row is not None
    payload = json.loads(row.setting_value_json or "{}")
    assert payload.get("portal_response_sla_hours") == 5
    assert payload.get("followup_horizon_hours") == 26
    assert payload.get("billing_capture_sla_hours") == 54
    assert payload.get("digest_enabled") is True
    assert payload.get("digest_interval_minutes") == 45

    digest_job = ScheduledJob.query.filter_by(job_type="priority_inbox_digest").first()
    assert digest_job is not None
    assert int(digest_job.interval_minutes or 0) == 45
    assert bool(digest_job.is_active) is True


def test_admin_dms_option_lists_persist_and_validate_upload_choices(app_ctx):
    app = app_ctx
    admin = _seed_admin("dms-option-admin@example.com")
    matter = _seed_matter(admin, "2026-DMS-OPT-0001")
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, admin.id)
    response = client.post(
        "/admin/settings/firm",
        data={
            "csrf_token": "test-csrf",
            "action": "dms_option_lists",
            "document_types": "Affidavit\nNotice",
            "confidentialities": "Internal\nConfidential",
            "privilege_labels": "Attorney-Client\nWithout Prejudice",
            "retention_categories": "Matter Lifecycle\nPermanent",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers.get("Location", "").endswith("/admin/settings/firm")

    row = FirmSetting.query.filter_by(setting_key="dms_option_lists").first()
    assert row is not None
    payload = json.loads(row.setting_value_json or "{}")
    assert payload.get("document_types") == ["Affidavit", "Notice"]
    assert payload.get("confidentialities") == ["Internal", "Confidential"]
    assert payload.get("privilege_labels") == ["Attorney-Client", "Without Prejudice"]
    assert payload.get("retention_categories") == ["Matter Lifecycle", "Permanent"]

    invalid_upload = client.post(
        f"/matters/{matter.id}/dms",
        data={
            "csrf_token": "test-csrf",
            "action": "upload_document",
            "title": "Invalid Metadata Upload",
            "document_type": "General",
            "confidentiality": "Internal",
            "file": (io.BytesIO(b"invalid"), "invalid.txt"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert invalid_upload.status_code == 302
    assert DocumentRecord.query.filter_by(matter_id=matter.id, title="Invalid Metadata Upload").first() is None

    valid_upload = client.post(
        f"/matters/{matter.id}/dms",
        data={
            "csrf_token": "test-csrf",
            "action": "upload_document",
            "title": "Valid Metadata Upload",
            "document_type": "Affidavit",
            "confidentiality": "Internal",
            "privilege_label": "Attorney-Client",
            "retention_category": "Matter Lifecycle",
            "file": (io.BytesIO(b"valid"), "valid.txt"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert valid_upload.status_code == 302
    assert DocumentRecord.query.filter_by(matter_id=matter.id, title="Valid Metadata Upload").first() is not None


def test_priority_inbox_digest_queues_and_dedupes(app_ctx):
    admin = _seed_admin("priority-digest-admin@example.com")
    matter = _seed_matter(admin, "2026-PRI-0002")

    lead = CRMLead(
        full_name="Digest Prospect",
        stage="new",
        created_by=admin.id,
        assigned_to=admin.id,
    )
    db.session.add(lead)
    db.session.flush()
    db.session.add(
        CRMFollowUp(
            lead_id=lead.id,
            due_at=utc_now() + dt.timedelta(hours=2),
            note="Call prospect for missing docs",
            status="open",
            created_by=admin.id,
        )
    )

    portal_user = PortalUser(
        email="portal-priority@example.com",
        full_name="Portal Priority",
        password_hash="x",
        is_active=True,
    )
    portal_user.set_password("PortalPassword123!")
    db.session.add(portal_user)
    db.session.flush()

    thread = PortalMessageThread(
        matter_id=matter.id,
        subject="Need urgent update",
        created_by_portal_user_id=portal_user.id,
    )
    db.session.add(thread)
    db.session.flush()
    db.session.add(
        PortalMessage(
            thread_id=thread.id,
            body="Please revert today.",
            from_portal_user_id=portal_user.id,
            created_at=utc_now() - dt.timedelta(hours=8),
        )
    )

    db.session.add(
        TimeEntry(
            user_id=admin.id,
            matter_id=matter.id,
            start_at=utc_now() - dt.timedelta(hours=53),
            end_at=utc_now() - dt.timedelta(hours=52),
            hours=1.0,
            rounded_hours=1.0,
            narrative="Aged approved work",
            is_billable=True,
            status="approved",
        )
    )
    db.session.commit()

    save_priority_inbox_config(
        {
            "portal_response_sla_hours": 4,
            "followup_horizon_hours": 24,
            "billing_capture_sla_hours": 48,
            "digest_enabled": True,
            "digest_interval_minutes": 60,
        },
        updated_by=admin.id,
    )

    first_result = _handle_priority_inbox_digest({})
    assert first_result == "queued digests: 1"

    queued_notifications = Notification.query.filter_by(
        event_type="priority_inbox_digest",
        actor_user_id=admin.id,
    ).all()
    assert len(queued_notifications) == 2

    second_result = _handle_priority_inbox_digest({})
    assert second_result == "queued digests: 0"
    assert (
        Notification.query.filter_by(
            event_type="priority_inbox_digest",
            actor_user_id=admin.id,
        ).count()
        == 2
    )


def test_priority_inbox_digest_honors_disabled_setting(app_ctx):
    admin = _seed_admin("priority-digest-disabled@example.com")
    db.session.commit()

    save_priority_inbox_config(
        {
            "portal_response_sla_hours": 4,
            "followup_horizon_hours": 24,
            "billing_capture_sla_hours": 48,
            "digest_enabled": False,
            "digest_interval_minutes": 60,
        },
        updated_by=admin.id,
    )

    result = _handle_priority_inbox_digest({})
    assert result == "priority inbox digest disabled"
    assert Notification.query.filter_by(event_type="priority_inbox_digest").count() == 0


def test_priority_inbox_digest_is_seeded_as_default_periodic_job():
    job_types = {job_type for job_type, _interval in DEFAULT_PERIODIC_JOBS}
    assert "priority_inbox_digest" in job_types
