from __future__ import annotations

import datetime as dt
import re

from intranet.extensions import db
from intranet.models import (
    DocumentFile,
    Matter,
    MatterActivity,
    MatterMember,
    MatterNote,
    MatterTimelineEvent,
    Task,
    TimeEntry,
    User,
)
from intranet.timeutils import utc_now


def _login(client, user_id: int, csrf_token: str = "test-csrf") -> str:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["_csrf_token"] = csrf_token
    return csrf_token


def _seed_user(*, email: str, role: str) -> User:
    user = User(
        email=email,
        full_name=email.split("@", 1)[0].replace(".", " ").title(),
        role=role,
        password_hash="x",
        is_active=True,
        mfa_enabled=True,
    )
    user.set_password("TestPassword123!")
    db.session.add(user)
    db.session.flush()
    return user


def _seed_matter(user: User, *, matter_no: str = "2026-AST-0001", title: str = "Assistant Matter") -> Matter:
    matter = Matter(
        matter_no=matter_no,
        title=title,
        client_name="Assistant Client",
        status="Open",
        risk_level="Medium",
        budget_status="On Track",
        created_by=user.id,
        opened_at=utc_now(),
        last_updated_at=utc_now(),
    )
    db.session.add(matter)
    db.session.flush()
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.commit()
    return matter


def _extract_confirm_token(body: str) -> str:
    match = re.search(r'name="confirm_token"\s+value="([^"]+)"', body)
    assert match, "confirm_token missing from assistant response"
    return match.group(1)


def test_assistant_page_renders(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.page@example.com", role="junior_attorney")
    client = app.test_client()
    _login(client, user.id)

    response = client.get("/assistant")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Supervised AI assistance for matter work" in body
    assert "Create a task to file the affidavit by tomorrow." in body


def test_assistant_summary_draft_uses_selected_matter(app_ctx):
    app = app_ctx
    app.config.update(AI_ENABLED=False)
    user = _seed_user(email="assistant.summary@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0002", title="Summary Matter")
    client = app.test_client()
    csrf_token = _login(client, user.id)

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "Summarize this matter for partner review.",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Executive Summary Draft" in body
    assert matter.matter_no in body
    assert "Objective" in body


def test_assistant_can_resolve_matter_from_prompt_title(app_ctx):
    app = app_ctx
    app.config.update(AI_ENABLED=False)
    user = _seed_user(email="assistant.resolve@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0016", title="Resolution Matter")
    client = app.test_client()
    csrf_token = _login(client, user.id)

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "prompt": "Summarize Resolution Matter for partner review.",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Executive Summary Draft" in body
    assert matter.matter_no in body
    assert "Resolved matter focus from the prompt" in body


def test_assistant_task_confirmation_creates_task(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.task@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0003", title="Task Matter")
    client = app.test_client()
    csrf_token = _login(client, user.id)

    preview_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "Create a task to file the affidavit by tomorrow.",
            "action_mode": "preview",
        },
    )
    preview_body = preview_response.get_data(as_text=True)
    token = _extract_confirm_token(preview_body)

    assert preview_response.status_code == 200
    assert "Task Ready for Confirmation" in preview_body

    confirm_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "Create a task to file the affidavit by tomorrow.",
            "confirm_token": token,
            "action_mode": "confirm",
        },
    )
    confirm_body = confirm_response.get_data(as_text=True)

    created = Task.query.filter_by(matter_id=matter.id).order_by(Task.id.desc()).first()
    assert confirm_response.status_code == 200
    assert "Task Created" in confirm_body
    assert created is not None
    assert "file the affidavit" in (created.title or "").lower()
    assert created.due_date == dt.date.today() + dt.timedelta(days=1)


def test_assistant_note_confirmation_creates_note(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.note@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0004", title="Note Matter")
    client = app.test_client()
    csrf_token = _login(client, user.id)
    prompt = "Add note that client approved the settlement range #settlement #client"

    preview_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "action_mode": "preview",
        },
    )
    token = _extract_confirm_token(preview_response.get_data(as_text=True))

    confirm_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "confirm_token": token,
            "action_mode": "confirm",
        },
    )
    body = confirm_response.get_data(as_text=True)

    note = MatterNote.query.filter_by(matter_id=matter.id).order_by(MatterNote.id.desc()).first()
    assert confirm_response.status_code == 200
    assert "Matter Note Added" in body
    assert note is not None
    assert "client approved the settlement range" in (note.body or "").lower()
    assert note.tags == "client, settlement"


def test_assistant_task_status_confirmation_updates_task(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.taskstatus@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0011", title="Task Status Matter")
    task = Task(
        matter_id=matter.id,
        title="Prepare witness bundle",
        status="Todo",
        created_by=user.id,
    )
    db.session.add(task)
    db.session.commit()

    client = app.test_client()
    csrf_token = _login(client, user.id)
    prompt = "Mark task prepare witness bundle done."

    preview_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "action_mode": "preview",
        },
    )
    preview_body = preview_response.get_data(as_text=True)
    token = _extract_confirm_token(preview_body)

    confirm_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "confirm_token": token,
            "action_mode": "confirm",
        },
    )
    db.session.refresh(task)
    body = confirm_response.get_data(as_text=True)

    assert preview_response.status_code == 200
    assert "Task Status Change Ready for Confirmation" in preview_body
    assert confirm_response.status_code == 200
    assert "Task Status Updated" in body
    assert task.status == "Done"


def test_assistant_task_status_noop_does_not_require_confirmation(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.taskstatus.noop@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0017", title="Task Status Noop Matter")
    task = Task(
        matter_id=matter.id,
        title="Prepare witness bundle",
        status="Done",
        created_by=user.id,
    )
    db.session.add(task)
    db.session.commit()

    client = app.test_client()
    csrf_token = _login(client, user.id)

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "Mark task prepare witness bundle done.",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Task Already In Requested Status" in body
    assert "confirm_token" not in body


def test_assistant_time_entry_confirmation_creates_draft_entry(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.time@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0012", title="Time Matter")
    task = Task(
        matter_id=matter.id,
        title="Draft affidavit",
        status="Doing",
        created_by=user.id,
    )
    db.session.add(task)
    db.session.commit()

    client = app.test_client()
    csrf_token = _login(client, user.id)
    prompt = "Log 1.5 hours drafting affidavit today."

    preview_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "action_mode": "preview",
        },
    )
    preview_body = preview_response.get_data(as_text=True)
    token = _extract_confirm_token(preview_body)

    confirm_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "confirm_token": token,
            "action_mode": "confirm",
        },
    )
    body = confirm_response.get_data(as_text=True)
    entry = TimeEntry.query.filter_by(matter_id=matter.id).order_by(TimeEntry.id.desc()).first()

    assert preview_response.status_code == 200
    assert "Time Entry Ready for Confirmation" in preview_body
    assert confirm_response.status_code == 200
    assert "Time Entry Added" in body
    assert entry is not None
    assert entry.status == "draft"
    assert abs(float(entry.rounded_hours or 0.0) - 1.5) < 0.001
    assert "drafting affidavit" in (entry.narrative or "").lower()


def test_assistant_blocks_time_entry_on_closed_matter(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.time.closed@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0018", title="Closed Time Matter")
    matter.status = "Closed"
    db.session.commit()

    client = app.test_client()
    csrf_token = _login(client, user.id)

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "Log 1.5 hours drafting affidavit today.",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Closed matters cannot accept new time entries" in body
    assert "confirm_token" not in body


def test_assistant_time_entry_duplicate_is_blocked(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.time.duplicate@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0014", title="Duplicate Time Matter")
    start_at = dt.datetime(2026, 4, 9, 9, 0)
    end_at = dt.datetime(2026, 4, 9, 10, 30)
    db.session.add(
        TimeEntry(
            user_id=user.id,
            matter_id=matter.id,
            start_at=start_at,
            end_at=end_at,
            hours=1.5,
            rounded_hours=1.5,
            narrative="drafting affidavit",
            status="draft",
        )
    )
    db.session.commit()

    client = app.test_client()
    csrf_token = _login(client, user.id)
    prompt = "Log time from 09:00 to 10:30 on 2026-04-09 drafting affidavit."

    preview_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "action_mode": "preview",
        },
    )
    body = preview_response.get_data(as_text=True)

    assert preview_response.status_code == 200
    assert "already exists" in body
    assert "confirm_token" not in body
    assert TimeEntry.query.filter_by(matter_id=matter.id).count() == 1


def test_assistant_time_entry_duplicate_is_blocked_on_confirmation(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.time.race@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0015", title="Duplicate Confirm Matter")
    client = app.test_client()
    csrf_token = _login(client, user.id)
    prompt = "Log time from 09:00 to 10:30 on 2026-04-09 drafting affidavit."

    preview_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "action_mode": "preview",
        },
    )
    token = _extract_confirm_token(preview_response.get_data(as_text=True))

    db.session.add(
        TimeEntry(
            user_id=user.id,
            matter_id=matter.id,
            start_at=dt.datetime(2026, 4, 9, 9, 0),
            end_at=dt.datetime(2026, 4, 9, 10, 30),
            hours=1.5,
            rounded_hours=1.5,
            narrative="drafting affidavit",
            status="draft",
        )
    )
    db.session.commit()

    confirm_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "confirm_token": token,
            "action_mode": "confirm",
        },
    )
    body = confirm_response.get_data(as_text=True)

    assert confirm_response.status_code == 200
    assert "already exists" in body
    assert TimeEntry.query.filter_by(matter_id=matter.id).count() == 1


def test_assistant_confirmation_token_cannot_be_replayed(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.replay@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0006", title="Replay Matter")
    client = app.test_client()
    csrf_token = _login(client, user.id)
    prompt = "Create a task to prepare the draft witness statement by tomorrow."

    preview_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "action_mode": "preview",
        },
    )
    token = _extract_confirm_token(preview_response.get_data(as_text=True))

    first_confirm = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "confirm_token": token,
            "action_mode": "confirm",
        },
    )
    second_confirm = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "confirm_token": token,
            "action_mode": "confirm",
        },
    )
    second_body = second_confirm.get_data(as_text=True)

    assert first_confirm.status_code == 200
    assert second_confirm.status_code == 200
    assert "already been used" in second_body
    assert Task.query.filter_by(matter_id=matter.id).count() == 1


def test_assistant_recent_history_renders_after_interaction(app_ctx):
    app = app_ctx
    app.config.update(AI_ENABLED=False)
    user = _seed_user(email="assistant.history@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0019", title="History Matter")
    client = app.test_client()
    csrf_token = _login(client, user.id)
    prompt = "Summarize this matter for partner review."

    client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "action_mode": "preview",
        },
    )
    response = client.get("/assistant")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Recent Assistant Activity" in body
    assert prompt in body
    assert "Executive Summary Draft" in body


def test_assistant_search_prompt_with_summary_keyword_stays_in_search_mode(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.search@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0007", title="Search Matter")
    db.session.add(
        DocumentFile(
            matter_id=matter.id,
            original_filename="summary-judgment-strategy.pdf",
            stored_filename="summary-judgment-strategy.pdf",
            sha256="abc123",
            content_type="application/pdf",
            category="Memo",
            doc_version="v1",
            lifecycle_stage="Draft",
            owner_name="Litigation Team",
            is_privileged=False,
            uploaded_by=user.id,
        )
    )
    db.session.commit()

    client = app.test_client()
    csrf_token = _login(client, user.id)

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "Find documents about summary judgment.",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Assistant Search Results" in body
    assert "Executive Summary Draft" not in body
    assert "summary-judgment-strategy.pdf" in body


def test_assistant_matter_briefing_surfaces_next_steps(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.briefing@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0008", title="Briefing Matter")
    db.session.add(
        Task(
            matter_id=matter.id,
            title="Prepare witness bundle",
            status="Todo",
            due_date=dt.date.today() + dt.timedelta(days=1),
            priority="High",
            created_by=user.id,
        )
    )
    db.session.add(
        MatterTimelineEvent(
            matter_id=matter.id,
            event_date=dt.date.today() + dt.timedelta(days=3),
            event_type="Hearing",
            title="Summary judgment hearing",
            is_milestone=True,
            created_by=user.id,
        )
    )
    db.session.commit()

    client = app.test_client()
    csrf_token = _login(client, user.id)

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "What are the next deadlines on this matter?",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Matter Briefing" in body
    assert "Summary judgment hearing" in body
    assert "Prepare witness bundle" in body
    assert "Upcoming Timeline" in body


def test_assistant_timeline_event_confirmation_creates_event(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.timeline@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0009", title="Timeline Matter")
    client = app.test_client()
    csrf_token = _login(client, user.id)
    prompt = "Schedule a hearing for summary judgment on 2026-05-14."

    preview_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "action_mode": "preview",
        },
    )
    preview_body = preview_response.get_data(as_text=True)
    token = _extract_confirm_token(preview_body)

    confirm_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "confirm_token": token,
            "action_mode": "confirm",
        },
    )
    body = confirm_response.get_data(as_text=True)
    event = MatterTimelineEvent.query.filter_by(matter_id=matter.id).order_by(MatterTimelineEvent.id.desc()).first()

    assert preview_response.status_code == 200
    assert "Timeline Event Ready for Confirmation" in preview_body
    assert confirm_response.status_code == 200
    assert "Timeline Event Added" in body
    assert event is not None
    assert event.event_type == "Hearing"
    assert event.event_date == dt.date(2026, 5, 14)
    assert "summary judgment" in (event.title or "").lower()


def test_assistant_search_surfaces_timeline_and_activity_results(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.timeline.search@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0010", title="Timeline Search Matter")
    db.session.add(
        MatterTimelineEvent(
            matter_id=matter.id,
            event_date=dt.date.today() + dt.timedelta(days=5),
            event_type="Hearing",
            title="Mediation strategy hearing",
            description="Court appearance on mediation posture",
            is_milestone=True,
            created_by=user.id,
        )
    )
    db.session.add(
        MatterActivity(
            matter_id=matter.id,
            action="Mediation pack updated",
            details="Negotiation strategy and counsel notes added.",
        )
    )
    db.session.commit()

    client = app.test_client()
    csrf_token = _login(client, user.id)

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "Find mediation",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Assistant Search Results" in body
    assert "Timeline &amp; Deadlines" in body or "Timeline & Deadlines" in body
    assert "Mediation strategy hearing" in body
    assert "Recent Activity" in body
    assert "Mediation pack updated" in body


def test_assistant_search_surfaces_my_time_entries(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.timesearch@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0013", title="Time Search Matter")
    db.session.add(
        TimeEntry(
            user_id=user.id,
            matter_id=matter.id,
            start_at=utc_now() - dt.timedelta(hours=2),
            end_at=utc_now() - dt.timedelta(hours=1),
            hours=1.0,
            rounded_hours=1.0,
            narrative="Reviewed mediation pack and prepared chronology",
            status="draft",
        )
    )
    db.session.commit()

    client = app.test_client()
    csrf_token = _login(client, user.id)

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "Find mediation",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Assistant Search Results" in body
    assert "My Time Entries" in body
    assert "Reviewed mediation pack and prepared chronology" in body


def test_assistant_blocks_task_creation_for_support_role(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.staff@example.com", role="staff")
    matter = _seed_matter(user, matter_no="2026-AST-0005", title="Support Matter")
    client = app.test_client()
    csrf_token = _login(client, user.id)

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "Create a task to prepare the hearing pack by tomorrow.",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Only legal case-team roles can create tasks from the assistant." in body
    assert Task.query.filter_by(matter_id=matter.id).count() == 0
