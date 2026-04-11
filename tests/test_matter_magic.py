from __future__ import annotations

import datetime as dt

from intranet.extensions import db
from intranet.models import Matter, MatterTimelineEvent, User
from intranet.services.matter_magic import build_matter_magic_snapshot
from intranet.timeutils import utc_now


def _seed_user(email: str) -> User:
    user = User(
        email=email,
        full_name=email.split("@", 1)[0].replace(".", " ").title(),
        role="admin",
        password_hash="x",
        is_active=True,
    )
    user.set_password("TestPassword123!")
    db.session.add(user)
    db.session.flush()
    return user


def _seed_matter(user: User, *, matter_no: str, title: str, status: str = "Open") -> Matter:
    matter = Matter(
        matter_no=matter_no,
        title=title,
        client_name="Matter Magic Client",
        status=status,
        risk_level="Medium",
        budget_status="On Track",
        created_by=user.id,
        opened_at=utc_now(),
        last_updated_at=utc_now(),
    )
    db.session.add(matter)
    db.session.flush()
    return matter


def test_matter_magic_uses_archetype_inputs_for_guidance(app_ctx):
    user = _seed_user("matter.magic.archetype@example.com")
    matter = _seed_matter(user, matter_no="2026-MAG-1001", title="Archetype Guidance Matter")
    db.session.commit()

    snapshot = build_matter_magic_snapshot(
        matter,
        today=dt.date.today(),
        tasks=[],
        docs=[],
        timeline=[],
        team_size=2,
        notes_count=1,
        checklist_remaining=2,
        archetype_compliance={
            "required_missing_labels": ["Incident Date", "Forum"],
            "checklist_remaining": 2,
            "checklist_unsynced": 1,
        },
    )

    action_codes = {action["code"] for action in snapshot["actions"]}

    assert "complete_archetype_fields" in action_codes
    assert "sync_archetype_checklist" in action_codes


def test_matter_magic_includes_event_and_dms_guidance(app_ctx):
    user = _seed_user("matter.magic.event@example.com")
    matter = _seed_matter(user, matter_no="2026-MAG-1002", title="Event Guidance Matter")
    upcoming = MatterTimelineEvent(
        matter_id=matter.id,
        title="Urgent hearing",
        event_date=dt.date.today() + dt.timedelta(days=1),
        event_type="Hearing",
        created_by=user.id,
    )
    db.session.add(upcoming)
    db.session.commit()

    snapshot = build_matter_magic_snapshot(
        matter,
        today=dt.date.today(),
        tasks=[],
        docs=[],
        timeline=[upcoming],
        team_size=2,
        notes_count=1,
    )

    action_codes = {action["code"] for action in snapshot["actions"]}

    assert "prepare_next_event" in action_codes
    assert "upload_first_document" in action_codes


def test_matter_magic_closed_matters_surface_closing_checklist(app_ctx):
    user = _seed_user("matter.magic.closed@example.com")
    matter = _seed_matter(
        user,
        matter_no="2026-MAG-1003",
        title="Closed Guidance Matter",
        status="Closed",
    )
    db.session.commit()

    snapshot = build_matter_magic_snapshot(
        matter,
        today=dt.date.today(),
        tasks=[],
        docs=[],
        timeline=[],
        team_size=2,
        notes_count=1,
        checklist_remaining=1,
        archetype_compliance={
            "required_missing_labels": [],
            "checklist_remaining": 1,
            "checklist_unsynced": 0,
        },
    )

    action_codes = {action["code"] for action in snapshot["actions"]}

    assert "work_closing_checklist" in action_codes
