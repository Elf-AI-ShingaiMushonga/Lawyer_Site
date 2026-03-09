from __future__ import annotations

import datetime as dt
from intranet.timeutils import utc_now
import io

from flask import g

from intranet.extensions import db
from intranet.models import Matter, MatterMember, MatterTimelineEvent, Task, TimeEntry, User


def _set_internal_session(client, user_id: int, csrf_token: str = "test-csrf") -> str:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["_csrf_token"] = csrf_token
    if hasattr(g, "_login_user"):
        delattr(g, "_login_user")
    return csrf_token


def _seed_user() -> User:
    row = User(
        email="assist-ai-user@example.com",
        full_name="Assist AI User",
        role="admin",
        password_hash="x",
        mfa_enabled=True,
    )
    row.set_password("TestPassword123!")
    db.session.add(row)
    db.session.commit()
    return row


def _seed_matter(owner_user_id: int) -> Matter:
    now = utc_now()
    matter = Matter(
        matter_no="2026-AI-ASSIST-0001",
        title="AI Assist Matter",
        client_name="Assist Client",
        status="Open",
        risk_level="Medium",
        budget_status="On Track",
        created_by=owner_user_id,
        opened_at=now,
        last_updated_at=now,
    )
    db.session.add(matter)
    db.session.flush()
    db.session.add(MatterMember(matter_id=matter.id, user_id=owner_user_id, role_in_matter="Responsible"))
    db.session.add(
        Task(
            matter_id=matter.id,
            title="Prepare hearing pack",
            status="Todo",
            due_date=dt.date.today() + dt.timedelta(days=2),
            created_by=owner_user_id,
        )
    )
    db.session.add(
        MatterTimelineEvent(
            matter_id=matter.id,
            event_date=dt.date.today(),
            event_type="Milestone",
            title="Client strategy call completed",
            created_by=owner_user_id,
        )
    )
    db.session.commit()
    return matter


def test_matter_ai_summary_endpoint_returns_payload(app_ctx):
    app = app_ctx
    app.config.update(AI_ENABLED=False)
    user = _seed_user()
    matter = _seed_matter(user.id)
    client = app.test_client()
    csrf_token = _set_internal_session(client, user.id)

    response = client.post(
        f"/matters/{matter.id}/ai/summary",
        json={"objective": "Keep client advised and close near-term milestones."},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 200
    payload = response.get_json() or {}
    assert payload.get("ok") is True
    suggestion = payload.get("suggestion") or {}
    assert suggestion.get("objective")
    assert suggestion.get("last_update_note")
    assert suggestion.get("outcome_summary")
    assert suggestion.get("source") == "fallback"


def test_matter_ai_client_update_endpoint_returns_payload(app_ctx):
    app = app_ctx
    app.config.update(AI_ENABLED=False)
    user = _seed_user()
    matter = _seed_matter(user.id)
    client = app.test_client()
    csrf_token = _set_internal_session(client, user.id)

    response = client.post(
        f"/matters/{matter.id}/ai/client-update",
        json={"tone_hint": "Professional and concise"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 200
    payload = response.get_json() or {}
    assert payload.get("ok") is True
    suggestion = payload.get("suggestion") or {}
    assert suggestion.get("subject")
    assert suggestion.get("body")
    assert suggestion.get("source") == "fallback"


def test_time_ai_narrative_endpoint_returns_payload(app_ctx):
    app = app_ctx
    app.config.update(AI_ENABLED=False)
    user = _seed_user()
    matter = _seed_matter(user.id)
    client = app.test_client()
    csrf_token = _set_internal_session(client, user.id)

    start_at = (utc_now() - dt.timedelta(hours=1, minutes=15)).replace(second=0, microsecond=0)
    end_at = start_at + dt.timedelta(minutes=45)
    response = client.post(
        "/time/ai/narrative",
        json={
            "matter_id": matter.id,
            "start_at": start_at.isoformat(),
            "end_at": end_at.isoformat(),
            "task_code": "A101",
            "activity_code": "R210",
            "narrative": "",
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 200
    payload = response.get_json() or {}
    assert payload.get("ok") is True
    suggestion = payload.get("suggestion") or {}
    assert suggestion.get("narrative")
    assert suggestion.get("source") == "fallback"


def test_time_entries_import_photo_creates_entries_in_database(monkeypatch, app_ctx):
    app = app_ctx
    user = _seed_user()
    matter = _seed_matter(user.id)
    client = app.test_client()
    _set_internal_session(client, user.id)

    def _fake_parser(**_kwargs):
        return {
            "entries": [
                {
                    "matter_no": matter.matter_no,
                    "date": dt.date.today().isoformat(),
                    "start_time": "09:00",
                    "end_time": "10:30",
                    "hours": 1.5,
                    "narrative": "Reviewed correspondence and prepared advice memo.",
                    "task_code": "L110",
                    "activity_code": "A210",
                    "is_billable": True,
                }
            ],
            "source": "openai",
        }

    monkeypatch.setattr("intranet.routes.timekeeping.parse_timesheet_image_entries", _fake_parser)
    response = client.post(
        "/time/entries/import-photo",
        data={
            "csrf_token": "test-csrf",
            "default_matter_id": str(matter.id),
            "timesheet_photo": (io.BytesIO(b"fake-image-bytes"), "timesheet.jpg", "image/jpeg"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302

    created = (
        TimeEntry.query.filter_by(user_id=user.id, matter_id=matter.id)
        .order_by(TimeEntry.id.desc())
        .first()
    )
    assert created is not None
    assert created.hours > 0
    assert created.rounded_hours > 0
    assert (created.narrative or "").startswith("Reviewed correspondence")


def test_time_entries_import_photo_gracefully_handles_no_rows(monkeypatch, app_ctx):
    app = app_ctx
    user = _seed_user()
    matter = _seed_matter(user.id)
    client = app.test_client()
    _set_internal_session(client, user.id)

    def _fake_parser(**_kwargs):
        return {
            "entries": [],
            "source": "fallback",
            "fallback_reason": "openai_error",
            "fallback_detail": "test failure",
        }

    monkeypatch.setattr("intranet.routes.timekeeping.parse_timesheet_image_entries", _fake_parser)
    response = client.post(
        "/time/entries/import-photo",
        data={
            "csrf_token": "test-csrf",
            "default_matter_id": str(matter.id),
            "timesheet_photo": (io.BytesIO(b"fake-image-bytes"), "timesheet.jpg", "image/jpeg"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    assert TimeEntry.query.filter_by(user_id=user.id, matter_id=matter.id).count() == 0
