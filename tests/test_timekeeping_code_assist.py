from __future__ import annotations

import datetime as dt
from intranet.timeutils import utc_now
import html
import json
import re

from intranet.extensions import db
from intranet.models import Matter, MatterMember, TimeEntry, User


def _set_user_session(client, user_id: int, csrf_token: str = "test-csrf") -> None:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["_csrf_token"] = csrf_token


def _seed_user(email: str) -> User:
    user = User(
        email=email,
        full_name="Timekeeper",
        role="partner",
        password_hash="x",
        is_active=True,
    )
    user.set_password("TestPassword123!")
    db.session.add(user)
    db.session.flush()
    return user


def _seed_matter(owner: User, matter_no: str, title: str) -> Matter:
    matter = Matter(
        matter_no=matter_no,
        title=title,
        client_name="Code Assist Client",
        status="Open",
        created_by=owner.id,
        opened_at=utc_now(),
        last_updated_at=utc_now(),
    )
    db.session.add(matter)
    db.session.flush()
    return matter


def _seed_entry(
    *,
    user_id: int,
    matter_id: int,
    start_at: dt.datetime,
    task_code: str,
    activity_code: str,
) -> None:
    end_at = start_at + dt.timedelta(minutes=30)
    db.session.add(
        TimeEntry(
            user_id=user_id,
            matter_id=matter_id,
            start_at=start_at,
            end_at=end_at,
            hours=0.5,
            rounded_hours=0.5,
            narrative="Code assist seed entry",
            task_code=task_code,
            activity_code=activity_code,
            is_billable=True,
            status="draft",
        )
    )


def test_time_entries_exposes_recent_code_assist_payload(app_ctx):
    app = app_ctx
    user = _seed_user("time-code-assist@example.com")
    primary_matter = _seed_matter(user, "2026-TIME-CODE-001", "Primary Matter")
    secondary_matter = _seed_matter(user, "2026-TIME-CODE-002", "Secondary Matter")
    db.session.add(MatterMember(matter_id=primary_matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.add(MatterMember(matter_id=secondary_matter.id, user_id=user.id, role_in_matter="Lead"))

    _seed_entry(
        user_id=user.id,
        matter_id=primary_matter.id,
        start_at=dt.datetime(2026, 5, 1, 11, 0, 0),
        task_code="L120",
        activity_code="A101",
    )
    _seed_entry(
        user_id=user.id,
        matter_id=primary_matter.id,
        start_at=dt.datetime(2026, 5, 1, 9, 0, 0),
        task_code="L130",
        activity_code="A102",
    )
    _seed_entry(
        user_id=user.id,
        matter_id=secondary_matter.id,
        start_at=dt.datetime(2026, 5, 1, 8, 0, 0),
        task_code="M500",
        activity_code="B210",
    )
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.get("/time/entries")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Time Capture Desk" in body
    assert "Capture work cleanly the first time" in body
    assert 'id="entry-task-code-options"' in body
    assert 'id="entry-activity-code-options"' in body
    assert 'data-time-code-pair' in body

    payload_match = re.search(r'data-time-code-assist="([^"]+)"', body)
    assert payload_match is not None
    payload = json.loads(html.unescape(payload_match.group(1)))

    assert "global" in payload
    assert "by_matter" in payload

    primary_bucket = payload["by_matter"][str(primary_matter.id)]
    assert primary_bucket["task_codes"][:2] == ["L120", "L130"]
    assert primary_bucket["activity_codes"][:2] == ["A101", "A102"]
    assert primary_bucket["latest_pair"]["task_code"] == "L120"
    assert primary_bucket["latest_pair"]["activity_code"] == "A101"
    assert "M500" not in primary_bucket["task_codes"]

    assert "L120" in payload["global"]["task_codes"]
    assert "M500" in payload["global"]["task_codes"]
    assert any(
        pair["task_code"] == "L120" and pair["activity_code"] == "A101"
        for pair in primary_bucket["pairs"]
    )


def test_time_entries_handles_empty_state_without_matters(app_ctx):
    app = app_ctx
    user = _seed_user("time-empty-state@example.com")
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.get("/time/entries")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Time Capture Desk" in body
    assert "No accessible matters are available for time capture yet." in body


def test_time_entries_preserves_matter_context_after_save(app_ctx):
    app = app_ctx
    user = _seed_user("time-context@example.com")
    matter = _seed_matter(user, "2026-TIME-CONTEXT-001", "Context Matter")
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.post(
        "/time/entries",
        data={
            "csrf_token": "test-csrf",
            "matter_id": matter.id,
            "start_at": "2026-05-01T09:00",
            "end_at": "2026-05-01T09:30",
            "narrative": "Captured with context",
            "is_billable": "1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert f"/time/entries?matter_id={matter.id}" in response.headers["Location"]
