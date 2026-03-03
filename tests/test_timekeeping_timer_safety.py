from __future__ import annotations

import datetime as dt

from intranet.extensions import db
from intranet.models import Matter, MatterMember, TimeTimer, User


def _set_user_session(client, user_id: int, csrf_token: str = "test-csrf") -> None:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["_csrf_token"] = csrf_token


def _seed_user(email: str, role: str = "partner") -> User:
    user = User(email=email, full_name=email.split("@", 1)[0], role=role, password_hash="x", is_active=True)
    user.set_password("TestPassword123!")
    db.session.add(user)
    db.session.flush()
    return user


def _seed_matter(owner: User, matter_no: str) -> Matter:
    matter = Matter(
        matter_no=matter_no,
        title=f"Matter {matter_no}",
        client_name="Timer Safety Client",
        status="Open",
        created_by=owner.id,
        opened_at=dt.datetime.utcnow(),
        last_updated_at=dt.datetime.utcnow(),
    )
    db.session.add(matter)
    db.session.flush()
    db.session.add(MatterMember(matter_id=matter.id, user_id=owner.id, role_in_matter="Lead"))
    return matter


def test_timer_is_auto_paused_when_single_run_cap_hit(app_ctx):
    app = app_ctx
    app.config["TIMER_SINGLE_CAP_MINUTES"] = 5
    user = _seed_user("timer-cap@example.com")
    matter = _seed_matter(user, "2026-TIMER-CAP-0001")
    timer = TimeTimer(
        user_id=user.id,
        matter_id=matter.id,
        status="running",
        elapsed_seconds=120,
        started_at=dt.datetime.utcnow() - dt.timedelta(minutes=8),
    )
    db.session.add(timer)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.get("/time/timers")
    assert response.status_code == 200

    db.session.refresh(timer)
    assert timer.status == "paused"
    assert timer.elapsed_seconds == 5 * 60


def test_timer_start_caps_existing_running_timer_before_switching_focus(app_ctx):
    app = app_ctx
    app.config["TIMER_SINGLE_CAP_MINUTES"] = 5
    user = _seed_user("timer-switch-cap@example.com")
    matter = _seed_matter(user, "2026-TIMER-CAP-0002")
    existing = TimeTimer(
        user_id=user.id,
        matter_id=matter.id,
        status="running",
        elapsed_seconds=60,
        started_at=dt.datetime.utcnow() - dt.timedelta(minutes=9),
        label="Old timer",
    )
    db.session.add(existing)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.post(
        "/time/timers/start",
        data={
            "csrf_token": "test-csrf",
            "matter_id": matter.id,
            "label": "New timer",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    db.session.refresh(existing)
    assert existing.status == "paused"
    assert existing.elapsed_seconds == 5 * 60

    new_timer = (
        TimeTimer.query.filter(TimeTimer.user_id == user.id, TimeTimer.id != existing.id)
        .order_by(TimeTimer.id.desc())
        .first()
    )
    assert new_timer is not None
    assert new_timer.status == "running"
    assert new_timer.label == "New timer"


def test_idle_pause_reason_shows_inactivity_message(app_ctx):
    app = app_ctx
    user = _seed_user("timer-idle@example.com")
    matter = _seed_matter(user, "2026-TIMER-IDLE-0001")
    timer = TimeTimer(
        user_id=user.id,
        matter_id=matter.id,
        status="running",
        started_at=dt.datetime.utcnow() - dt.timedelta(minutes=2),
        elapsed_seconds=0,
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
            "pause_reason": "idle_timeout",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Timer auto-paused after inactivity." in body

    db.session.refresh(timer)
    assert timer.status == "paused"
    assert timer.elapsed_seconds >= 100


def test_timers_page_renders_idle_presence_guard_attributes(app_ctx):
    app = app_ctx
    app.config["TIMER_IDLE_PROMPT_SECONDS"] = 1234
    app.config["TIMER_IDLE_GRACE_SECONDS"] = 45
    app.config["TIMER_SINGLE_CAP_MINUTES"] = 180
    user = _seed_user("timer-presence@example.com")
    matter = _seed_matter(user, "2026-TIMER-IDLE-0002")
    timer = TimeTimer(
        user_id=user.id,
        matter_id=matter.id,
        status="running",
        started_at=dt.datetime.utcnow() - dt.timedelta(minutes=1),
        elapsed_seconds=0,
    )
    db.session.add(timer)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.get("/time/timers")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "data-timer-presence-root" in body
    assert 'data-idle-prompt-seconds="1234"' in body
    assert 'data-idle-grace-seconds="45"' in body
    assert 'data-cap-minutes="180"' in body


def test_global_live_billing_cue_renders_when_timer_running(app_ctx):
    app = app_ctx
    user = _seed_user("timer-live-cue@example.com")
    matter = _seed_matter(user, "2026-TIMER-CUE-0001")
    timer = TimeTimer(
        user_id=user.id,
        matter_id=matter.id,
        status="running",
        label="Drafting heads of argument",
        elapsed_seconds=75,
        started_at=dt.datetime.utcnow() - dt.timedelta(minutes=2),
    )
    db.session.add(timer)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.get("/dashboard")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Billing Timer Running" in body
    assert "data-live-timer-root" in body
    assert f'data-elapsed-seed-seconds="{timer.elapsed_seconds}"' in body
    assert matter.matter_no in body


def test_global_live_billing_cue_hidden_without_running_timer(app_ctx):
    app = app_ctx
    user = _seed_user("timer-live-cue-paused@example.com")
    matter = _seed_matter(user, "2026-TIMER-CUE-0002")
    timer = TimeTimer(
        user_id=user.id,
        matter_id=matter.id,
        status="paused",
        label="Paused timer",
        elapsed_seconds=180,
        started_at=dt.datetime.utcnow() - dt.timedelta(minutes=5),
    )
    db.session.add(timer)
    db.session.commit()

    client = app.test_client()
    _set_user_session(client, user.id)
    response = client.get("/dashboard")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "data-live-timer-root" not in body
    assert "Billing Timer Running" not in body
