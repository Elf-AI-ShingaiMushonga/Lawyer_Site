from __future__ import annotations

import datetime as dt

from intranet.extensions import db
from intranet.models import MatterPin, MatterRecentView, Task


def _set_user_session(client, user_id: int, csrf_token: str = "test-csrf") -> None:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["_csrf_token"] = csrf_token


def _enable_mfa(user) -> None:
    user.mfa_enabled = True
    user.mfa_secret = "TEST-SECRET"
    db.session.commit()


def test_matter_detail_records_recent_view(seed_user_matter, app):
    user = seed_user_matter["user"]
    matter = seed_user_matter["matter"]
    _enable_mfa(user)
    client = app.test_client()
    _set_user_session(client, user.id)

    response = client.get(f"/matters/{matter.id}")
    assert response.status_code == 200

    row = MatterRecentView.query.filter_by(user_id=user.id, matter_id=matter.id).first()
    assert row is not None
    assert int(row.view_count or 0) == 1

    response = client.get(f"/matters/{matter.id}")
    assert response.status_code == 200

    row = MatterRecentView.query.filter_by(user_id=user.id, matter_id=matter.id).first()
    assert row is not None
    assert int(row.view_count or 0) == 1


def test_matter_pin_toggle_adds_and_removes_pin(seed_user_matter, app):
    user = seed_user_matter["user"]
    matter = seed_user_matter["matter"]
    _enable_mfa(user)
    client = app.test_client()
    _set_user_session(client, user.id)

    response = client.post(
        f"/matters/{matter.id}/pin",
        data={"csrf_token": "test-csrf", "next": "/matters"},
    )
    assert response.status_code == 302
    assert MatterPin.query.filter_by(user_id=user.id, matter_id=matter.id).count() == 1

    response = client.post(
        f"/matters/{matter.id}/pin",
        data={"csrf_token": "test-csrf", "next": "/matters"},
    )
    assert response.status_code == 302
    assert MatterPin.query.filter_by(user_id=user.id, matter_id=matter.id).count() == 0


def test_recent_history_clear_removes_rows(seed_user_matter, app):
    user = seed_user_matter["user"]
    matter = seed_user_matter["matter"]
    _enable_mfa(user)
    client = app.test_client()
    _set_user_session(client, user.id)

    response = client.get(f"/matters/{matter.id}")
    assert response.status_code == 200
    assert MatterRecentView.query.filter_by(user_id=user.id, matter_id=matter.id).count() == 1

    response = client.post("/matters/recent/clear", data={"csrf_token": "test-csrf", "next": "/dashboard"})
    assert response.status_code == 302
    assert MatterRecentView.query.filter_by(user_id=user.id).count() == 0


def test_dashboard_hides_pinned_and_recent_sections(seed_user_matter, app):
    user = seed_user_matter["user"]
    matter = seed_user_matter["matter"]
    _enable_mfa(user)
    client = app.test_client()
    _set_user_session(client, user.id)

    client.post(
        f"/matters/{matter.id}/pin",
        data={"csrf_token": "test-csrf", "next": "/matters"},
    )
    client.get(f"/matters/{matter.id}")

    response = client.get("/dashboard")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Pinned Matters" not in body
    assert "Recently Viewed Matters" not in body


def test_matter_detail_renders_matter_magic_brief(seed_user_matter, app):
    user = seed_user_matter["user"]
    matter = seed_user_matter["matter"]
    _enable_mfa(user)
    db.session.add(
        Task(
            matter_id=matter.id,
            title="Overdue filing",
            status="Todo",
            due_date=dt.date.today() - dt.timedelta(days=1),
            created_by=user.id,
        )
    )
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)

    response = client.get(f"/matters/{matter.id}")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Matter Magic" in body
    assert "Quick Brief" in body
    assert "Clear overdue tasks" in body


def test_workspace_renders_next_best_moves(seed_user_matter, app):
    user = seed_user_matter["user"]
    matter = seed_user_matter["matter"]
    _enable_mfa(user)
    db.session.add(
        Task(
            matter_id=matter.id,
            title="Draft update",
            status="Todo",
            due_date=dt.date.today(),
            created_by=user.id,
        )
    )
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)

    response = client.get(f"/matters/{matter.id}/workspace")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Next Best Moves" in body
    assert "Prepare due work" in body
