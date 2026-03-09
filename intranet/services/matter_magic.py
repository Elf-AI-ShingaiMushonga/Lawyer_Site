from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any, Sequence

from flask import url_for
from sqlalchemy import func

from ..extensions import db
from ..models import DocumentFile, Matter, MatterMember, MatterNote, MatterTimelineEvent, Task

URGENCY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}

_ACTION_TARGETS: dict[str, tuple[str, str | None, str]] = {
    "clear_overdue_tasks": ("matter_tasks", None, "Clear Tasks"),
    "prepare_due_tasks": ("matter_tasks", None, "Open Tasks"),
    "prepare_next_event": ("calendar_matter", None, "Open Calendar"),
    "schedule_next_milestone": ("calendar_matter", None, "Schedule"),
    "complete_summary": ("matter_detail", "#matter-summary-section", "Open Summary"),
    "send_client_update": ("matter_detail", "#matter-ai-section", "Draft Update"),
    "upload_first_document": ("matter_dms", None, "Open DMS"),
    "refresh_document_record": ("matter_dms", None, "Add Document"),
    "capture_strategy_note": ("matter_notes", None, "Add Note"),
    "confirm_team": ("matter_team", None, "Open Team"),
    "set_stage": ("matter_workspace", "#workspace-stage", "Set Stage"),
    "review_budget": ("billing_invoices", None, "Review Billing"),
    "complete_archetype_fields": ("matter_detail", "#matter-archetype-section", "Complete Fields"),
    "sync_archetype_checklist": ("matter_workspace", None, "Open Workspace"),
    "work_closing_checklist": ("matter_workspace", None, "Open Workspace"),
}

_BASE_DMS_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "label": "Client Advice",
        "summary": "Privileged advice note with matter metadata already mapped.",
        "title_template": "Client Advice - {matter_no} - {today}",
        "type_candidates": ("Advisory", "Advice", "Memo", "Letter", "General"),
        "confidentiality_candidates": ("Confidential", "Internal", "Privileged"),
        "privilege_candidates": ("Attorney-Client", "Legal Advice", "Privileged"),
        "retention_candidates": ("Client File", "Matter File"),
    },
    {
        "label": "Consultation Note",
        "summary": "Fast attendance note for calls, meetings, and strategy sessions.",
        "title_template": "Consultation Note - {matter_no} - {today}",
        "type_candidates": ("Memo", "Note", "Attendance Note", "General"),
        "confidentiality_candidates": ("Internal", "Confidential"),
        "privilege_candidates": ("Attorney-Client", "Privileged"),
        "retention_candidates": ("Client File", "Matter File"),
    },
    {
        "label": "Internal Strategy Memo",
        "summary": "Internal legal analysis without retyping the document heading.",
        "title_template": "Strategy Memo - {matter_no} - {today}",
        "type_candidates": ("Memo", "Advisory", "General"),
        "confidentiality_candidates": ("Internal", "Confidential"),
        "privilege_candidates": ("Attorney-Client", "Privileged"),
        "retention_candidates": ("Client File", "Matter File"),
    },
)

_PRACTICE_DMS_PRESETS: tuple[tuple[tuple[str, ...], tuple[dict[str, Any], ...]], ...] = (
    (
        ("litigation", "court", "dispute", "appeal", "raf", "injury"),
        (
            {
                "label": "Hearing Bundle",
                "summary": "Prepare a filing or hearing pack with the right naming convention.",
                "title_template": "Hearing Bundle - {matter_no} - {today}",
                "type_candidates": ("Court Filing", "Bundle", "Pleading", "Evidence", "General"),
                "confidentiality_candidates": ("Confidential", "Internal"),
                "privilege_candidates": ("Attorney-Client", "Privileged"),
                "retention_candidates": ("Court File", "Client File"),
            },
            {
                "label": "Pleading Draft",
                "summary": "Start the next pleading or notice without a blank title field.",
                "title_template": "Pleading Draft - {matter_no} - {today}",
                "type_candidates": ("Pleading", "Court Filing", "Notice", "General"),
                "confidentiality_candidates": ("Confidential", "Internal"),
                "privilege_candidates": ("Attorney-Client", "Privileged"),
                "retention_candidates": ("Court File", "Client File"),
            },
        ),
    ),
    (
        ("convey", "property", "transfer"),
        (
            {
                "label": "Transfer Pack",
                "summary": "Open a conveyancing pack with transfer-oriented metadata.",
                "title_template": "Transfer Pack - {matter_no} - {today}",
                "type_candidates": ("Checklist", "Pack", "General", "Correspondence"),
                "confidentiality_candidates": ("Confidential", "Internal"),
                "privilege_candidates": ("Attorney-Client", "Privileged"),
                "retention_candidates": ("Matter File", "Conveyancing"),
            },
            {
                "label": "Lodgement Note",
                "summary": "Capture lodgement or registration progress in one click.",
                "title_template": "Lodgement Note - {matter_no} - {today}",
                "type_candidates": ("Memo", "Note", "General"),
                "confidentiality_candidates": ("Internal", "Confidential"),
                "privilege_candidates": ("Attorney-Client", "Privileged"),
                "retention_candidates": ("Matter File", "Conveyancing"),
            },
        ),
    ),
    (
        ("estate", "trust", "deceased", "master"),
        (
            {
                "label": "Master Filing Pack",
                "summary": "Package estate or trust papers with the right matter framing.",
                "title_template": "Master Filing Pack - {matter_no} - {today}",
                "type_candidates": ("Pack", "Memo", "General", "Court Filing"),
                "confidentiality_candidates": ("Confidential", "Internal"),
                "privilege_candidates": ("Attorney-Client", "Privileged"),
                "retention_candidates": ("Estate File", "Matter File"),
            },
        ),
    ),
    (
        ("labour", "employment", "ccma", "dismissal"),
        (
            {
                "label": "CCMA Referral Pack",
                "summary": "Prepare a referral or arbitration pack without manual setup.",
                "title_template": "CCMA Referral Pack - {matter_no} - {today}",
                "type_candidates": ("Referral", "Bundle", "Pleading", "General"),
                "confidentiality_candidates": ("Confidential", "Internal"),
                "privilege_candidates": ("Attorney-Client", "Privileged"),
                "retention_candidates": ("Labour File", "Matter File"),
            },
        ),
    ),
)


def _as_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return None


def _as_datetime(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value
    return None


def _document_timestamp(doc: Any) -> dt.datetime | None:
    return _as_datetime(getattr(doc, "uploaded_at", None)) or _as_datetime(getattr(doc, "created_at", None))


def _document_label(doc: Any) -> str:
    label = str(getattr(doc, "title", None) or getattr(doc, "original_filename", None) or "Document").strip()
    return label or "Document"


def _document_kind(doc: Any) -> str:
    label = str(getattr(doc, "document_type", None) or getattr(doc, "category", None) or "General").strip()
    return label or "General"


def _coerce_snapshot_value(snapshot: Any, key: str, default: Any) -> Any:
    if isinstance(snapshot, dict):
        return snapshot.get(key, default)
    return getattr(snapshot, key, default)


def _format_when(target_date: dt.date, today: dt.date) -> str:
    days = (target_date - today).days
    if days < 0:
        return f"{abs(days)} day(s) ago"
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    return f"in {days} day(s)"


def _urgency_score(urgency: str) -> int:
    return URGENCY_RANK.get(str(urgency or "").lower(), 0)


def _action(code: str, title: str, summary: str, urgency: str, score: int, *, badge: str | None = None) -> dict[str, Any]:
    return {
        "code": code,
        "title": title,
        "summary": summary,
        "urgency": urgency,
        "score": score,
        "badge": badge or "",
    }


def build_matter_magic_snapshot(
    matter: Matter,
    *,
    today: dt.date | None = None,
    tasks: Sequence[Task] | None = None,
    docs: Sequence[Any] | None = None,
    timeline: Sequence[MatterTimelineEvent] | None = None,
    team_size: int = 0,
    notes_count: int = 0,
    checklist_remaining: int | None = None,
    archetype_compliance: Any | None = None,
    limit_actions: int = 5,
) -> dict[str, Any]:
    today = today or dt.date.today()
    tasks = list(tasks or [])
    docs = list(docs or [])
    timeline = list(timeline or [])

    open_tasks = [task for task in tasks if str(getattr(task, "status", "")) != "Done"]
    overdue_tasks = [task for task in open_tasks if _as_date(getattr(task, "due_date", None)) and task.due_date < today]
    due_today_tasks = [task for task in open_tasks if _as_date(getattr(task, "due_date", None)) == today]
    due_soon_tasks = [
        task
        for task in open_tasks
        if _as_date(getattr(task, "due_date", None)) and today < task.due_date <= (today + dt.timedelta(days=7))
    ]

    future_events = sorted(
        [event for event in timeline if _as_date(getattr(event, "event_date", None)) and event.event_date >= today],
        key=lambda event: (event.event_date, str(getattr(event, "title", ""))),
    )
    next_event = future_events[0] if future_events else None
    next_event_date = _as_date(getattr(next_event, "event_date", None))

    docs_sorted = sorted(
        docs,
        key=lambda doc: _document_timestamp(doc) or dt.datetime.min,
        reverse=True,
    )
    last_document_at = _document_timestamp(docs_sorted[0]) if docs_sorted else None
    recent_document_labels: list[str] = []
    document_type_counts: dict[str, int] = {}
    kind_counter: dict[str, int] = defaultdict(int)
    for doc in docs_sorted:
        kind = _document_kind(doc)
        kind_counter[kind] += 1
        label = _document_label(doc)
        if label not in recent_document_labels:
            recent_document_labels.append(label)
        if len(recent_document_labels) >= 3:
            break
    document_type_counts = dict(sorted(kind_counter.items(), key=lambda row: (-row[1], row[0]))[:3])

    reference_dt = _as_datetime(getattr(matter, "last_updated_at", None)) or _as_datetime(getattr(matter, "opened_at", None))
    stale_days = max(0, (today - reference_dt.date()).days) if reference_dt else 0

    missing_required = list(_coerce_snapshot_value(archetype_compliance, "required_missing_labels", []) or [])
    checklist_unsynced = int(_coerce_snapshot_value(archetype_compliance, "checklist_unsynced", 0) or 0)
    if checklist_remaining is None:
        checklist_remaining = int(_coerce_snapshot_value(archetype_compliance, "checklist_remaining", 0) or 0)
    checklist_remaining = int(checklist_remaining or 0)

    actions: list[dict[str, Any]] = []

    if overdue_tasks:
        actions.append(
            _action(
                "clear_overdue_tasks",
                "Clear overdue tasks",
                f"{len(overdue_tasks)} overdue task(s) are slowing this matter down.",
                "critical",
                520 + len(overdue_tasks),
                badge=f"{len(overdue_tasks)} overdue",
            )
        )
    elif due_today_tasks or due_soon_tasks:
        next_due = sorted(
            [_as_date(getattr(task, "due_date", None)) for task in (due_today_tasks or due_soon_tasks) if _as_date(getattr(task, "due_date", None))],
        )[0]
        due_count = len(due_today_tasks) or len(due_soon_tasks)
        actions.append(
            _action(
                "prepare_due_tasks",
                "Prepare due work",
                f"{due_count} open task(s) are due {_format_when(next_due, today)}.",
                "high" if due_today_tasks else "medium",
                465 if due_today_tasks else 430,
                badge=next_due.isoformat(),
            )
        )

    if next_event is not None and next_event_date is not None:
        days_until_event = (next_event_date - today).days
        actions.append(
            _action(
                "prepare_next_event",
                f"Prepare for {getattr(next_event, 'event_type', 'event')}",
                f"{getattr(next_event, 'title', 'Next event')} is {_format_when(next_event_date, today)}.",
                "high" if days_until_event <= 3 else "medium",
                450 if days_until_event <= 3 else 395,
                badge=next_event_date.isoformat(),
            )
        )
    elif str(getattr(matter, "status", "")).lower() != "closed":
        actions.append(
            _action(
                "schedule_next_milestone",
                "Schedule the next milestone",
                "This matter has no upcoming timeline event recorded.",
                "medium",
                315,
                badge="Timeline",
            )
        )

    if not str(getattr(matter, "objective", "") or "").strip() or not str(getattr(matter, "last_update_note", "") or "").strip():
        actions.append(
            _action(
                "complete_summary",
                "Complete the executive summary",
                "Objective and latest update should be client-ready on the matter itself.",
                "high",
                445,
                badge="Summary",
            )
        )
    elif stale_days >= 7 and str(getattr(matter, "status", "")).lower() != "closed":
        actions.append(
            _action(
                "send_client_update",
                "Send a client update",
                f"The matter summary has not moved for {stale_days} day(s).",
                "medium",
                375 + min(stale_days, 20),
                badge=f"{stale_days}d stale",
            )
        )

    if not docs_sorted:
        actions.append(
            _action(
                "upload_first_document",
                "Upload the first core document",
                "There is no opening pack, draft, or correspondence stored yet.",
                "high",
                405,
                badge="No docs",
            )
        )
    elif last_document_at is not None and (today - last_document_at.date()).days >= 14 and str(getattr(matter, "status", "")).lower() != "closed":
        doc_stale_days = (today - last_document_at.date()).days
        actions.append(
            _action(
                "refresh_document_record",
                "Refresh the document record",
                f"No document has been added for {doc_stale_days} day(s).",
                "medium",
                330 + min(doc_stale_days, 20),
                badge=f"{doc_stale_days}d",
            )
        )

    if not str(getattr(matter, "stage", "") or "").strip() and str(getattr(matter, "status", "")).lower() != "closed":
        actions.append(
            _action(
                "set_stage",
                "Set the matter stage",
                "Stage tracking is blank, which makes handovers and reporting harder.",
                "medium",
                392,
                badge="Stage",
            )
        )

    if str(getattr(matter, "budget_status", "") or "") in {"Watch", "Over Budget", "Needs Review"}:
        actions.append(
            _action(
                "review_budget",
                "Review billing and budget position",
                f"Budget status is {matter.budget_status}.",
                "high" if matter.budget_status == "Over Budget" else "medium",
                388,
                badge=str(getattr(matter, "budget_status", "") or ""),
            )
        )

    if missing_required:
        actions.append(
            _action(
                "complete_archetype_fields",
                "Complete required archetype fields",
                "Required legal workflow data is still missing on this matter.",
                "high",
                382 + len(missing_required),
                badge=f"{len(missing_required)} missing",
            )
        )
    elif checklist_unsynced > 0:
        actions.append(
            _action(
                "sync_archetype_checklist",
                "Sync the archetype checklist",
                "The template playbook changed and this matter is missing checklist items.",
                "medium",
                355 + checklist_unsynced,
                badge=f"{checklist_unsynced} unsynced",
            )
        )
    elif checklist_remaining > 0:
        actions.append(
            _action(
                "work_closing_checklist",
                "Work the closing checklist",
                f"{checklist_remaining} checklist item(s) remain open.",
                "medium",
                342 + checklist_remaining,
                badge=f"{checklist_remaining} open",
            )
        )

    if int(team_size or 0) <= 1 and str(getattr(matter, "status", "")).lower() != "closed":
        actions.append(
            _action(
                "confirm_team",
                "Confirm matter team coverage",
                "Only one team member is currently linked to this matter.",
                "medium",
                336,
                badge="Solo",
            )
        )

    if int(notes_count or 0) == 0 and str(getattr(matter, "status", "")).lower() != "closed":
        actions.append(
            _action(
                "capture_strategy_note",
                "Capture a strategy note",
                "There is no note history yet for calls, instructions, or legal reasoning.",
                "low",
                275,
                badge="No notes",
            )
        )

    deduped_actions: list[dict[str, Any]] = []
    seen_codes: set[str] = set()
    for action in sorted(actions, key=lambda item: (-int(item["score"]), item["title"])):
        code = str(action["code"])
        if code in seen_codes:
            continue
        seen_codes.add(code)
        deduped_actions.append(action)
        if len(deduped_actions) >= max(1, int(limit_actions)):
            break

    top_action = deduped_actions[0] if deduped_actions else None
    next_event_summary = ""
    if next_event is not None and next_event_date is not None:
        next_event_summary = f"{getattr(next_event, 'title', 'Next event')} ({_format_when(next_event_date, today)})"

    status_bits = [str(getattr(matter, "status", "") or "Open")]
    if str(getattr(matter, "stage", "") or "").strip():
        status_bits.append(f"Stage {matter.stage}")
    if str(getattr(matter, "risk_level", "") or "").strip():
        status_bits.append(f"{matter.risk_level} risk")
    if overdue_tasks:
        status_bits.append(f"{len(overdue_tasks)} overdue")
    elif due_today_tasks:
        status_bits.append(f"{len(due_today_tasks)} due today")
    if next_event_summary:
        status_bits.append(next_event_summary)
    status_line = " | ".join(status_bits[:4])

    health_tone = "steady"
    if top_action is not None:
        top_urgency = str(top_action["urgency"])
        if top_urgency == "critical":
            health_tone = "critical"
        elif top_urgency == "high":
            health_tone = "watch"
    elif str(getattr(matter, "risk_level", "") or "") in {"High", "Critical"}:
        health_tone = "watch"

    brief_lines = [
        f"Matter: {matter.matter_no} - {matter.title}",
        f"Client: {matter.client_name}",
        f"Status: {getattr(matter, 'status', 'Open') or 'Open'} | Stage: {getattr(matter, 'stage', None) or 'Not set'} | Risk: {getattr(matter, 'risk_level', None) or 'Medium'} | Budget: {getattr(matter, 'budget_status', None) or 'On Track'}",
    ]
    if str(getattr(matter, "objective", "") or "").strip():
        brief_lines.append(f"Objective: {str(matter.objective).strip()}")
    if str(getattr(matter, "last_update_note", "") or "").strip():
        brief_lines.append(f"Latest update: {str(matter.last_update_note).strip()}")
    if next_event_summary:
        brief_lines.append(f"Next event: {next_event_summary}")
    brief_lines.append(
        f"Open tasks: {len(open_tasks)} total, {len(overdue_tasks)} overdue, {len(due_today_tasks) + len(due_soon_tasks)} due within 7 days"
    )
    if recent_document_labels:
        brief_lines.append("Recent documents: " + ", ".join(recent_document_labels))
    else:
        brief_lines.append("Recent documents: None stored yet")
    if deduped_actions:
        brief_lines.append(
            "Recommended next steps: " + "; ".join(action["title"] for action in deduped_actions[:3])
        )

    risk_rank = {"Low": 1, "Medium": 2, "High": 3, "Critical": 4}
    priority_score = 50 + risk_rank.get(str(getattr(matter, "risk_level", "") or ""), 0) * 10 + stale_days
    if top_action is not None:
        priority_score += _urgency_score(str(top_action["urgency"])) * 100
        priority_score += int(top_action["score"])

    return {
        "actions": deduped_actions,
        "headline": top_action["summary"] if top_action else "Matter is currently on track.",
        "status_line": status_line,
        "brief_text": "\n".join(brief_lines),
        "health_tone": health_tone,
        "open_task_count": len(open_tasks),
        "overdue_task_count": len(overdue_tasks),
        "due_soon_task_count": len(due_today_tasks) + len(due_soon_tasks),
        "document_count": len(docs_sorted),
        "recent_document_labels": recent_document_labels,
        "document_type_counts": document_type_counts,
        "last_document_at": last_document_at,
        "next_event_summary": next_event_summary,
        "team_size": int(team_size or 0),
        "notes_count": int(notes_count or 0),
        "stale_days": stale_days,
        "priority_score": priority_score,
    }


def attach_matter_magic_links(actions: Sequence[dict[str, Any]], matter_id: int) -> list[dict[str, Any]]:
    linked: list[dict[str, Any]] = []
    for action in actions:
        endpoint, anchor, button_label = _ACTION_TARGETS.get(
            str(action.get("code") or ""),
            ("matter_detail", None, "Open Matter"),
        )
        href = url_for(endpoint, matter_id=matter_id)
        if anchor:
            href = f"{href}{anchor}"
        linked.append(
            {
                **action,
                "href": href,
                "button_label": button_label,
            }
        )
    return linked


def build_dashboard_focus_board(
    matters: Sequence[Matter],
    *,
    today: dt.date | None = None,
    limit: int = 6,
) -> list[dict[str, Any]]:
    today = today or dt.date.today()
    unique_matters: list[Matter] = []
    seen_ids: set[int] = set()
    for matter in matters:
        if matter is None or getattr(matter, "id", None) is None:
            continue
        matter_id = int(matter.id)
        if matter_id in seen_ids:
            continue
        seen_ids.add(matter_id)
        unique_matters.append(matter)
    if not unique_matters:
        return []

    matter_ids = [int(matter.id) for matter in unique_matters]

    tasks_by_matter: dict[int, list[Task]] = defaultdict(list)
    for task in Task.query.filter(Task.matter_id.in_(matter_ids), Task.status != "Done").all():
        tasks_by_matter[int(task.matter_id)].append(task)

    docs_by_matter: dict[int, list[DocumentFile]] = defaultdict(list)
    for doc in (
        DocumentFile.query.filter(DocumentFile.matter_id.in_(matter_ids))
        .order_by(DocumentFile.uploaded_at.desc(), DocumentFile.id.desc())
        .all()
    ):
        bucket = docs_by_matter[int(doc.matter_id)]
        if len(bucket) < 8:
            bucket.append(doc)

    timeline_by_matter: dict[int, list[MatterTimelineEvent]] = defaultdict(list)
    for event in (
        MatterTimelineEvent.query.filter(
            MatterTimelineEvent.matter_id.in_(matter_ids),
            MatterTimelineEvent.event_date >= today,
        )
        .order_by(MatterTimelineEvent.event_date.asc(), MatterTimelineEvent.id.asc())
        .all()
    ):
        bucket = timeline_by_matter[int(event.matter_id)]
        if len(bucket) < 8:
            bucket.append(event)

    team_counts = {
        int(matter_id): int(count)
        for matter_id, count in (
            db.session.query(MatterMember.matter_id, func.count(MatterMember.id))
            .filter(MatterMember.matter_id.in_(matter_ids))
            .group_by(MatterMember.matter_id)
            .all()
        )
    }
    note_counts = {
        int(matter_id): int(count)
        for matter_id, count in (
            db.session.query(MatterNote.matter_id, func.count(MatterNote.id))
            .filter(MatterNote.matter_id.in_(matter_ids))
            .group_by(MatterNote.matter_id)
            .all()
        )
    }

    cards: list[dict[str, Any]] = []
    for matter in unique_matters:
        snapshot = build_matter_magic_snapshot(
            matter,
            today=today,
            tasks=tasks_by_matter.get(int(matter.id), []),
            docs=docs_by_matter.get(int(matter.id), []),
            timeline=timeline_by_matter.get(int(matter.id), []),
            team_size=team_counts.get(int(matter.id), 0),
            notes_count=note_counts.get(int(matter.id), 0),
            limit_actions=4,
        )
        linked_actions = attach_matter_magic_links(snapshot["actions"], int(matter.id))
        snapshot["actions"] = linked_actions
        snapshot["top_action"] = linked_actions[0] if linked_actions else None
        snapshot["matter"] = matter
        cards.append(snapshot)

    cards.sort(
        key=lambda row: (
            -int(row.get("priority_score", 0)),
            -int(row.get("overdue_task_count", 0)),
            -int(row.get("due_soon_task_count", 0)),
            str(getattr(row["matter"], "matter_no", "")),
        )
    )
    return cards[: max(1, int(limit))]


def _pick_option(options: Sequence[str], candidates: Sequence[str], default: str = "") -> str:
    lookup = {
        str(option).strip().casefold(): str(option).strip()
        for option in options
        if str(option).strip()
    }
    for candidate in candidates:
        match = lookup.get(str(candidate).strip().casefold())
        if match:
            return match
    return str(default or "")


def build_dms_quick_starts(
    matter: Matter,
    document_type_options: Sequence[str],
    confidentiality_options: Sequence[str],
    privilege_label_options: Sequence[str],
    retention_category_options: Sequence[str],
    *,
    today: dt.date | None = None,
    limit: int = 4,
) -> list[dict[str, str]]:
    today = today or dt.date.today()
    matter_profile = " ".join(
        part
        for part in [
            str(getattr(matter, "practice_area", "") or ""),
            str(getattr(matter, "legal_category", "") or ""),
            str(getattr(matter, "case_type", "") or ""),
            str(getattr(matter, "title", "") or ""),
        ]
        if part
    ).lower()

    preset_library: list[dict[str, Any]] = list(_BASE_DMS_PRESETS)
    for keywords, presets in _PRACTICE_DMS_PRESETS:
        if any(keyword in matter_profile for keyword in keywords):
            preset_library.extend(presets)

    rendered: list[dict[str, str]] = []
    seen_labels: set[str] = set()
    for preset in preset_library:
        label = str(preset["label"])
        if label in seen_labels:
            continue
        seen_labels.add(label)
        title = str(preset["title_template"]).format(
            matter_no=str(getattr(matter, "matter_no", "") or "Matter"),
            client_name=str(getattr(matter, "client_name", "") or "Client"),
            today=today.isoformat(),
        )
        rendered.append(
            {
                "label": label,
                "summary": str(preset["summary"]),
                "title": title,
                "document_type": _pick_option(
                    document_type_options,
                    preset.get("type_candidates", ()),
                    default=str(document_type_options[0]) if document_type_options else "General",
                ),
                "confidentiality": _pick_option(
                    confidentiality_options,
                    preset.get("confidentiality_candidates", ()),
                    default=str(confidentiality_options[0]) if confidentiality_options else "Internal",
                ),
                "privilege_label": _pick_option(
                    privilege_label_options,
                    preset.get("privilege_candidates", ()),
                    default="",
                ),
                "retention_category": _pick_option(
                    retention_category_options,
                    preset.get("retention_candidates", ()),
                    default="",
                ),
            }
        )
        if len(rendered) >= max(1, int(limit)):
            break
    return rendered
