from __future__ import annotations

import datetime as dt
import hashlib
import re
from typing import Any

from flask import current_app, session, url_for
from flask_login import current_user
from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy import or_

from ..extensions import db
from ..helpers import (
    audit,
    can_access_matter,
    filter_accessible_document_files,
    filter_accessible_matter_notes,
    is_admin,
    matter_activity,
    normalize_query,
    resolve_active_matter,
)
from ..models import (
    DocumentFile,
    Matter,
    MatterActivity,
    MatterNote,
    MatterTimelineEvent,
    Task,
    TaskAssignee,
    TimeEntry,
    TimeRoundingPolicy,
    TimeValidationEvent,
    User,
)
from ..policies import visible_matter_ids
from ..roles import role_is_case
from ..timeutils import utc_now
from .assist_ai import suggest_matter_client_update, suggest_matter_executive_summary
from .semantic_search import SemanticSearchService

_MATTER_NO_RE = re.compile(r"\b\d{4}-[A-Z]{2,8}-\d{2,8}\b", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_TAG_RE = re.compile(r"#([a-z0-9][a-z0-9_-]{1,39})", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_SLASH_DATE_RE = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")
_TASK_ID_RE = re.compile(r"\btask\s*#?\s*(\d+)\b", re.IGNORECASE)
_TIME_RANGE_RE = re.compile(
    r"\b(?:from\s+)?(\d{1,2}:\d{2})(?:\s*(?:to|\-|–)\s*|\s+to\s+)(\d{1,2}:\d{2})\b",
    re.IGNORECASE,
)
_HOURS_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|hr|h)\b", re.IGNORECASE)
_MINUTES_RE = re.compile(r"\b(\d+)\s*(?:minutes?|mins?)\b", re.IGNORECASE)

_TASK_PREFIX_RE = re.compile(
    r"^(?:please\s+)?(?:create|add|open|make)\s+(?:a\s+)?task(?:\s+for|\s+to)?\s*",
    re.IGNORECASE,
)
_NOTE_PREFIX_RE = re.compile(
    r"^(?:please\s+)?(?:add|save|record|create)\s+(?:a\s+)?note(?:\s+that)?\s*",
    re.IGNORECASE,
)
_TIMELINE_PREFIX_RE = re.compile(
    r"^(?:please\s+)?(?:add|create|record|schedule|log)\s+(?:(?:a|an)\s+)?(?:(?:timeline\s+)?(?:event|entry)|hearing|filing|milestone|deadline|client update|internal review|delivery)\b(?:\s+for|\s+to|\s+on)?\s*",
    re.IGNORECASE,
)
_TIME_ENTRY_PREFIX_RE = re.compile(
    r"^(?:please\s+)?(?:log|record|capture|add)\s+(?:(?:a|an)\s+)?(?:billable\s+|non[- ]billable\s+)?(?:time\s+entry|time)\b(?:\s+for|\s+on|\s+to)?\s*",
    re.IGNORECASE,
)
_TASK_STATUS_PREFIX_RE = re.compile(
    r"^(?:please\s+)?(?:mark|set|update|move|change|complete|finish|start)\s+(?:the\s+)?(?:task\s+)?",
    re.IGNORECASE,
)
_SEARCH_PREFIX_RE = re.compile(
    r"^(?:please\s+)?(?:find|search(?:\s+for)?|look\s+up|show\s+me)\s+",
    re.IGNORECASE,
)

_BLOCKED_PHRASES: dict[str, str] = {
    "approve invoice": "Invoice approvals stay in the native billing workflow.",
    "settle payment": "Payment settlement stays in the native billing workflow.",
    "capture payment": "Payment capture stays in the native billing workflow.",
    "trust disbursement": "Trust movements stay in the native trust workflow.",
    "trust transfer": "Trust movements stay in the native trust workflow.",
    "override conflict": "Conflict overrides stay in the native CRM workflow.",
    "release legal hold": "Legal-hold releases stay in the admin workflow.",
    "delete document": "Document deletion is blocked from the assistant.",
    "delete matter": "Matter deletion is blocked from the assistant.",
    "close matter": "Matter closing stays in the native matter workflow.",
    "archive matter": "Matter archival stays in the native matter workflow.",
}

_SUMMARY_INTENT_RE = re.compile(
    r"\b(?:summari[sz]e|draft(?:\s+an?)?\s+(?:executive\s+)?summary|prepare(?:\s+an?)?\s+(?:executive\s+)?summary|executive\s+briefing|partner\s+briefing)\b|what should i focus on|focus me on",
    re.IGNORECASE,
)
_CLIENT_UPDATE_INTENT_RE = re.compile(
    r"\b(?:draft|prepare|write|create)?\s*(?:a\s+)?client update\b|\bupdate the client\b|\bclient email\b|\bstatus email\b",
    re.IGNORECASE,
)
_BRIEFING_INTENT_RE = re.compile(
    r"\b(?:what(?:'s| is)\s+next|next steps|next deadlines?|upcoming deadlines?|upcoming dates|upcoming timeline|deadline snapshot|matter briefing|brief me on (?:this|the) matter|where do things stand)\b",
    re.IGNORECASE,
)
_TASK_INTENT_RE = re.compile(
    r"\b(?:create|add|open|make)\s+(?:a\s+)?task\b|\bremind me to\b|\btodo\b",
    re.IGNORECASE,
)
_NOTE_INTENT_RE = re.compile(
    r"\b(?:add|save|record|create)\s+(?:a\s+)?note\b|\bnote that\b",
    re.IGNORECASE,
)
_TIMELINE_INTENT_RE = re.compile(
    r"\b(?:add|create|record|schedule|log)\s+(?:(?:a|an)\s+)?(?:(?:timeline\s+)?(?:event|entry)|hearing|filing|milestone|deadline|client update|internal review|delivery)\b",
    re.IGNORECASE,
)
_TIME_ENTRY_INTENT_RE = re.compile(
    r"\b(?:log|record|capture|add)\b.*\b(?:time\s+entry|time|\d+(?:\.\d+)?\s*(?:hours?|hrs?|hr|h|minutes?|mins?))\b|\bworked\s+\d+(?:\.\d+)?\s*(?:hours?|hrs?|hr|h)\b",
    re.IGNORECASE,
)
_TASK_STATUS_INTENT_RE = re.compile(
    r"\b(?:mark|set|update|move|change|complete|finish|start)\b.*\btask\b.*\b(?:done|complete(?:d)?|doing|in progress|todo|to do|start(?:ed)?)\b|\bmark\b.*\bdone\b",
    re.IGNORECASE,
)

_TIMELINE_EVENT_TYPES = {"Milestone", "Filing", "Hearing", "Client Update", "Internal Review", "Delivery"}
_TASK_STATUSES = {"Todo", "Doing", "Done"}

_EXAMPLES = [
    "What are the next deadlines on this matter?",
    "Summarize this matter for partner review.",
    "Draft a client update for this matter in plain English.",
    "Create a task to file the affidavit by tomorrow.",
    "Mark task prepare witness bundle done.",
    "Schedule a hearing for summary judgment on 2026-05-14.",
    "Log 1.5 hours drafting the affidavit today.",
    "Add note that client approved the settlement range discussed today.",
    "Find documents about arbitration strategy.",
]


def assistant_examples() -> list[str]:
    return list(_EXAMPLES)


_CONSUMED_CONFIRMATION_SESSION_KEY = "assistant_consumed_confirmations"
_MAX_CONSUMED_CONFIRMATIONS = 24
_ASSISTANT_HISTORY_SESSION_KEY = "assistant_recent_history"
_MAX_ASSISTANT_HISTORY = 8
_SEARCH_STOPWORDS = {
    "about",
    "case",
    "client",
    "document",
    "documents",
    "docs",
    "find",
    "look",
    "matter",
    "matters",
    "note",
    "notes",
    "search",
    "show",
    "task",
    "tasks",
}
_MATTER_MATCH_STOPWORDS = {
    "about",
    "add",
    "brief",
    "briefing",
    "client",
    "create",
    "deadline",
    "deadlines",
    "doing",
    "done",
    "draft",
    "entry",
    "file",
    "find",
    "for",
    "hearing",
    "log",
    "mark",
    "matter",
    "matters",
    "next",
    "note",
    "notes",
    "partner",
    "prepare",
    "record",
    "review",
    "search",
    "status",
    "summarize",
    "summary",
    "task",
    "tasks",
    "time",
    "timeline",
    "todo",
    "update",
    "what",
}


def assistant_recent_history() -> list[dict[str, Any]]:
    rows = session.get(_ASSISTANT_HISTORY_SESSION_KEY) or []
    payload: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        prompt = normalize_query(str(row.get("prompt") or ""))[:240]
        headline = normalize_query(str(row.get("headline") or ""))[:120]
        if not prompt or not headline:
            continue
        payload.append(
            {
                "prompt": prompt,
                "headline": headline,
                "summary": normalize_query(str(row.get("summary") or ""))[:220],
                "status": normalize_query(str(row.get("status") or ""))[:20] or "ok",
                "matter_label": normalize_query(str(row.get("matter_label") or ""))[:160],
                "matter_id": int(row.get("matter_id") or 0) if str(row.get("matter_id") or "").isdigit() else None,
                "created_at": normalize_query(str(row.get("created_at") or ""))[:32],
            }
        )
    return payload


def record_assistant_result(prompt: str, result: dict[str, Any] | None) -> None:
    if not result:
        return
    prompt_value = normalize_query(prompt or "")[:240]
    headline = normalize_query(str(result.get("headline") or ""))[:120]
    if not prompt_value or not headline:
        return
    history = assistant_recent_history()
    entry = {
        "prompt": prompt_value,
        "headline": headline,
        "summary": normalize_query(str(result.get("summary") or ""))[:220],
        "status": normalize_query(str(result.get("status") or ""))[:20] or "ok",
        "matter_label": normalize_query(str(result.get("matter_label") or ""))[:160],
        "matter_id": int(result.get("matter_id") or 0) if result.get("matter_id") else None,
        "created_at": utc_now().strftime("%Y-%m-%d %H:%M"),
    }
    history = [row for row in history if not (row.get("prompt") == entry["prompt"] and row.get("headline") == entry["headline"])]
    history.insert(0, entry)
    session[_ASSISTANT_HISTORY_SESSION_KEY] = history[:_MAX_ASSISTANT_HISTORY]
    session.modified = True


def assistant_matter_options(limit: int = 160) -> list[Matter]:
    query = Matter.query
    if not is_admin():
        scoped_ids = visible_matter_ids()
        if not scoped_ids:
            return []
        query = query.filter(Matter.id.in_(scoped_ids))
    return (
        query.order_by(Matter.last_updated_at.desc().nullslast(), Matter.opened_at.desc().nullslast(), Matter.id.desc())
        .limit(max(1, int(limit)))
        .all()
    )


def assistant_matter_label(matter: Matter | None) -> str:
    if matter is None:
        return ""
    return f"{matter.matter_no} - {matter.title}"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(str(current_app.config.get("SECRET_KEY") or "assistant-secret"), salt="assistant-actions")


def _sign_confirmation_payload(payload: dict[str, Any]) -> str:
    return _serializer().dumps(payload)


def _load_confirmation_payload(token: str) -> dict[str, Any]:
    return _serializer().loads(token, max_age=60 * 60)


def _confirmation_fingerprint(confirm_token: str) -> str:
    return hashlib.sha256((confirm_token or "").encode("utf-8")).hexdigest()


def _confirmation_already_consumed(confirm_token: str) -> bool:
    fingerprint = _confirmation_fingerprint(confirm_token)
    consumed = session.get(_CONSUMED_CONFIRMATION_SESSION_KEY) or []
    return fingerprint in {str(item) for item in consumed if str(item)}


def _mark_confirmation_consumed(confirm_token: str) -> None:
    fingerprint = _confirmation_fingerprint(confirm_token)
    consumed = [str(item) for item in (session.get(_CONSUMED_CONFIRMATION_SESSION_KEY) or []) if str(item)]
    if fingerprint in consumed:
        return
    consumed.append(fingerprint)
    session[_CONSUMED_CONFIRMATION_SESSION_KEY] = consumed[-_MAX_CONSUMED_CONFIRMATIONS:]
    session.modified = True


def _result(
    *,
    status: str,
    kind: str,
    headline: str,
    summary: str,
    prompt: str,
    matter: Matter | None = None,
    warnings: list[str] | None = None,
    fields: list[dict[str, str]] | None = None,
    text_blocks: list[dict[str, str]] | None = None,
    sections: list[dict[str, Any]] | None = None,
    links: list[dict[str, str]] | None = None,
    requires_confirmation: bool = False,
    confirm_token: str = "",
) -> dict[str, Any]:
    return {
        "status": status,
        "kind": kind,
        "headline": headline,
        "summary": summary,
        "prompt": prompt,
        "matter_id": int(matter.id) if matter is not None else None,
        "matter_label": assistant_matter_label(matter),
        "warnings": warnings or [],
        "fields": fields or [],
        "text_blocks": text_blocks or [],
        "sections": sections or [],
        "links": links or [],
        "requires_confirmation": bool(requires_confirmation),
        "confirm_token": confirm_token,
    }


def _error_result(prompt: str, message: str, *, matter: Matter | None = None) -> dict[str, Any]:
    return _result(
        status="error",
        kind="error",
        headline="Assistant Request Needs Attention",
        summary=message,
        prompt=prompt,
        matter=matter,
    )


def _blocked_result(prompt: str, message: str, *, matter: Matter | None = None) -> dict[str, Any]:
    return _result(
        status="blocked",
        kind="blocked",
        headline="Action Kept in Native Workflow",
        summary=message,
        prompt=prompt,
        matter=matter,
        warnings=[
            "High-risk financial, trust, conflict, deletion, and irreversible actions stay outside the assistant.",
        ],
    )


def _clean_prompt(prompt: str) -> str:
    return normalize_query(prompt or "")


def _requested_block_reason(prompt: str) -> str:
    lowered = f" {str(prompt or '').lower()} "
    for phrase, reason in _BLOCKED_PHRASES.items():
        if phrase in lowered:
            return reason
    return ""


def _classify_intent(prompt: str) -> str:
    lowered = str(prompt or "").lower()
    if _CLIENT_UPDATE_INTENT_RE.search(prompt or ""):
        return "draft_client_update"
    if _SUMMARY_INTENT_RE.search(prompt or ""):
        return "draft_summary"
    if _TIME_ENTRY_INTENT_RE.search(prompt or ""):
        return "create_time_entry"
    if _TIMELINE_INTENT_RE.search(prompt or ""):
        return "create_timeline_event"
    if _TASK_STATUS_INTENT_RE.search(prompt or ""):
        return "update_task_status"
    if _NOTE_INTENT_RE.search(prompt or ""):
        return "add_note"
    if _TASK_INTENT_RE.search(prompt or ""):
        return "create_task"
    if _BRIEFING_INTENT_RE.search(prompt or ""):
        return "matter_briefing"
    if lowered.startswith("send ") and "client" in lowered and ("update" in lowered or "email" in lowered):
        return "draft_client_update"
    return "search"


def _resolve_matter(selected_matter_id: int | None, prompt: str) -> tuple[Matter | None, list[str]]:
    warnings: list[str] = []
    if selected_matter_id:
        if can_access_matter(int(selected_matter_id)):
            selected = db.session.get(Matter, int(selected_matter_id))
            if selected is not None:
                return selected, warnings
        warnings.append("Selected matter is not available in your current access scope.")

    matter_no_match = _MATTER_NO_RE.search(prompt or "")
    if matter_no_match:
        matter_no = str(matter_no_match.group(0) or "").upper()
        row = Matter.query.filter_by(matter_no=matter_no).first()
        if row is not None and can_access_matter(int(row.id)):
            return row, warnings
        warnings.append(f"Matter {matter_no} was referenced but is not available in your current access scope.")

    inferred = _infer_matter_from_prompt(prompt)
    if inferred is not None:
        warnings.append(f"Resolved matter focus from the prompt to {assistant_matter_label(inferred)}.")
        return inferred, warnings

    active_matter = resolve_active_matter()
    if active_matter is not None and can_access_matter(int(active_matter.id)):
        return active_matter, warnings
    return None, warnings


def _matter_context(matter: Matter) -> dict[str, object]:
    tasks = (
        Task.query.filter_by(matter_id=matter.id)
        .order_by(Task.status.asc(), Task.due_date.asc().nullslast(), Task.id.desc())
        .limit(180)
        .all()
    )
    timeline = (
        MatterTimelineEvent.query.filter_by(matter_id=matter.id)
        .order_by(MatterTimelineEvent.event_date.desc(), MatterTimelineEvent.created_at.desc())
        .limit(40)
        .all()
    )
    docs = (
        DocumentFile.query.filter_by(matter_id=matter.id)
        .order_by(DocumentFile.uploaded_at.desc())
        .limit(30)
        .all()
    )
    docs = filter_accessible_document_files(docs)
    notes = filter_accessible_matter_notes(
        MatterNote.query.filter_by(matter_id=matter.id)
        .order_by(MatterNote.updated_at.desc(), MatterNote.id.desc())
        .limit(20)
        .all()
    )
    activity_rows = (
        MatterActivity.query.filter_by(matter_id=matter.id)
        .order_by(MatterActivity.created_at.desc())
        .limit(20)
        .all()
    )
    recent_time_entries = (
        TimeEntry.query.filter_by(user_id=current_user.id, matter_id=matter.id)
        .order_by(TimeEntry.start_at.desc())
        .limit(12)
        .all()
    )

    today = dt.date.today()
    open_tasks = [task for task in tasks if (task.status or "").strip().lower() != "done"]
    overdue_tasks = [task for task in open_tasks if task.due_date and task.due_date < today]
    next_due_task_row = (
        sorted([task for task in open_tasks if task.due_date], key=lambda item: (item.due_date, item.id))[0]
        if open_tasks
        else None
    )
    next_due_task = ""
    if next_due_task_row is not None:
        next_due_task = f"{next_due_task_row.title or 'Task'} ({next_due_task_row.due_date.isoformat()})"

    return {
        "matter_id": int(matter.id),
        "matter_no": matter.matter_no or "",
        "title": matter.title or "",
        "client_name": matter.client_name or "",
        "status": matter.status or "",
        "risk_level": matter.risk_level or "Medium",
        "budget_status": matter.budget_status or "On Track",
        "legal_category": matter.legal_category or "",
        "objective": matter.objective or "",
        "last_update_note": matter.last_update_note or "",
        "outcome_summary": matter.outcome_summary or "",
        "open_task_count": len(open_tasks),
        "overdue_task_count": len(overdue_tasks),
        "next_due_task": next_due_task,
        "latest_timeline_title": timeline[0].title if timeline else "",
        "recent_timeline": [
            {
                "date": row.event_date.isoformat() if row.event_date else "",
                "type": row.event_type or "",
                "title": (row.title or "")[:180],
            }
            for row in timeline[:8]
        ],
        "recent_notes": [
            (row.body or "").strip().replace("\n", " ")[:260]
            for row in notes[:6]
            if (row.body or "").strip()
        ],
        "recent_documents": [
            {
                "filename": (row.original_filename or "")[:180],
                "category": row.category or "",
                "uploaded_at": row.uploaded_at.isoformat() if row.uploaded_at else "",
            }
            for row in docs[:8]
        ],
        "recent_activity": [
            {
                "action": (row.action or "")[:140],
                "details": (row.details or "")[:220],
                "created_at": row.created_at.isoformat() if row.created_at else "",
            }
            for row in activity_rows[:8]
        ],
        "recent_time_entries": [
            {
                "start_at": row.start_at.isoformat() if row.start_at else "",
                "rounded_hours": float(row.rounded_hours or 0.0),
                "status": row.status or "",
                "narrative": (row.narrative or "")[:220],
            }
            for row in recent_time_entries[:6]
        ],
    }


def _tone_hint_from_prompt(prompt: str) -> str:
    lowered = str(prompt or "").lower()
    if "plain english" in lowered:
        return "Plain English, professional and concise"
    if "formal" in lowered:
        return "Formal and precise"
    if "warm" in lowered:
        return "Professional, warm, and concise"
    return "Professional and concise"


def _strip_task_prompt(prompt: str) -> str:
    stripped = _TASK_PREFIX_RE.sub("", prompt or "").strip()
    if stripped:
        return stripped
    lowered = str(prompt or "").strip()
    if lowered.lower().startswith("remind me to "):
        return lowered[13:].strip()
    return lowered


def _strip_note_prompt(prompt: str) -> str:
    stripped = _NOTE_PREFIX_RE.sub("", prompt or "").strip()
    return stripped or str(prompt or "").strip()


def _strip_timeline_prompt(prompt: str) -> str:
    stripped = _TIMELINE_PREFIX_RE.sub("", prompt or "").strip()
    return stripped or str(prompt or "").strip()


def _strip_time_entry_prompt(prompt: str) -> str:
    stripped = _TIME_ENTRY_PREFIX_RE.sub("", prompt or "").strip()
    return stripped or str(prompt or "").strip()


def _strip_task_status_prompt(prompt: str) -> str:
    stripped = _TASK_STATUS_PREFIX_RE.sub("", prompt or "").strip()
    return stripped or str(prompt or "").strip()


def _strip_search_prompt(prompt: str) -> str:
    stripped = _SEARCH_PREFIX_RE.sub("", prompt or "").strip()
    if not stripped:
        stripped = str(prompt or "").strip()
    stripped = re.sub(r"^(?:documents?|docs?|notes?|tasks?|matters?)\s+(?:about|for|on)\s+", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"^(?:documents?|docs?|notes?|tasks?|matters?)\s+", "", stripped, flags=re.IGNORECASE)
    return normalize_query(stripped).strip(" .,:;-")


def _search_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[a-z0-9]+", normalize_query(value or "").lower()):
        if len(token) < 3 or token in _SEARCH_STOPWORDS or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def _matter_match_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[a-z0-9]+", normalize_query(value or "").lower()):
        if len(token) < 3 or token in _MATTER_MATCH_STOPWORDS or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def _matter_search_text(row: Matter) -> str:
    return normalize_query(
        " ".join(
            part
            for part in [row.matter_no or "", row.title or "", row.client_name or ""]
            if part
        )
    ).lower()


def _infer_matter_from_prompt(prompt: str) -> Matter | None:
    prompt_norm = normalize_query(prompt or "").lower()
    if len(prompt_norm) < 6:
        return None
    matters = assistant_matter_options(limit=200)
    if not matters:
        return None

    exact_matches: list[Matter] = []
    for row in matters:
        for candidate in (normalize_query(row.title or "").lower(), normalize_query(row.client_name or "").lower()):
            if candidate and len(candidate) >= 8 and candidate in prompt_norm:
                exact_matches.append(row)
                break
    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        return None

    tokens = _matter_match_tokens(prompt_norm)
    if not tokens:
        return None

    scored: list[tuple[int, Matter]] = []
    for row in matters:
        haystack = _matter_search_text(row)
        score = sum(1 for token in tokens if token in haystack)
        if score <= 0:
            continue
        scored.append((score, row))
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], int(item[1].id or 0)), reverse=True)
    top_score, top_row = scored[0]
    next_score = scored[1][0] if len(scored) > 1 else 0
    if top_score >= 2 and top_score > next_score:
        return top_row
    return None


def _document_search_text(row: DocumentFile) -> str:
    return normalize_query(
        " ".join(
            str(part or "").replace("-", " ").replace("_", " ")
            for part in (row.original_filename, row.category, row.owner_name, row.doc_version)
        )
    ).lower()


def _extract_due_date(prompt: str) -> dt.date | None:
    lowered = str(prompt or "").lower()
    today = dt.date.today()
    if "tomorrow" in lowered:
        return today + dt.timedelta(days=1)
    if "today" in lowered:
        return today
    if "next week" in lowered:
        return today + dt.timedelta(days=7)

    match = _ISO_DATE_RE.search(prompt or "")
    if match:
        try:
            return dt.date.fromisoformat(match.group(0))
        except ValueError:
            return None

    match = _SLASH_DATE_RE.search(prompt or "")
    if match:
        token = match.group(0).replace("-", "/")
        parts = token.split("/")
        if len(parts) != 3:
            return None
        try:
            day = int(parts[0])
            month = int(parts[1])
            year = int(parts[2])
        except ValueError:
            return None
        if year < 100:
            year += 2000
        try:
            return dt.date(year, month, day)
        except ValueError:
            return None
    return None


def _extract_entry_date(prompt: str) -> dt.date | None:
    lowered = str(prompt or "").lower()
    today = dt.date.today()
    if "yesterday" in lowered:
        return today - dt.timedelta(days=1)
    if "tomorrow" in lowered:
        return today + dt.timedelta(days=1)
    if "today" in lowered:
        return today
    return _extract_due_date(prompt)


def _parse_hhmm(raw: str) -> dt.time | None:
    try:
        return dt.time.fromisoformat(str(raw).strip())
    except ValueError:
        return None


def _extract_time_range(prompt: str, *, default_date: dt.date) -> tuple[dt.datetime, dt.datetime] | None:
    match = _TIME_RANGE_RE.search(prompt or "")
    if not match:
        return None
    start_time = _parse_hhmm(match.group(1))
    end_time = _parse_hhmm(match.group(2))
    if start_time is None or end_time is None:
        return None
    start_at = dt.datetime.combine(default_date, start_time)
    end_at = dt.datetime.combine(default_date, end_time)
    if end_at <= start_at:
        return None
    return start_at, end_at


def _extract_hours(prompt: str) -> float | None:
    hour_match = _HOURS_RE.search(prompt or "")
    if hour_match:
        try:
            return max(0.0, float(hour_match.group(1)))
        except ValueError:
            return None
    minute_match = _MINUTES_RE.search(prompt or "")
    if minute_match:
        try:
            minutes = int(minute_match.group(1))
        except ValueError:
            return None
        return max(0.0, minutes / 60.0)
    return None


def _extract_priority(prompt: str) -> str:
    lowered = str(prompt or "").lower()
    if any(token in lowered for token in ("critical", "urgent", "high priority", "priority high")):
        return "High"
    if any(token in lowered for token in ("low priority", "priority low")):
        return "Low"
    return "Medium"


def _extract_assignee(prompt: str) -> User | None:
    email_match = _EMAIL_RE.search(prompt or "")
    if not email_match:
        return None
    return User.query.filter_by(email=str(email_match.group(0)).strip().lower()).first()


def _task_title_and_description(prompt: str) -> tuple[str, str]:
    body = _strip_task_prompt(prompt)
    working = body
    working = re.sub(r"\b(?:by|on)\s+\d{4}-\d{2}-\d{2}\b", "", working, flags=re.IGNORECASE)
    working = re.sub(r"\bby\s+tomorrow\b", "", working, flags=re.IGNORECASE)
    working = re.sub(r"\bby\s+today\b", "", working, flags=re.IGNORECASE)
    working = re.sub(r"\bnext week\b", "", working, flags=re.IGNORECASE)
    working = re.sub(r"\bassign(?:ed)?\s+to\s+" + _EMAIL_RE.pattern, "", working, flags=re.IGNORECASE)
    working = normalize_query(working).strip(" .,:;-")
    if not working:
        working = body
    title = working[:255]
    description = ""
    cleaned_body = normalize_query(body).strip()
    if cleaned_body and cleaned_body.casefold() != title.casefold():
        description = cleaned_body[:2000]
    return title, description


def _task_search_text(task: Task) -> str:
    return normalize_query(" ".join(part for part in [task.title or "", task.description or ""] if part)).lower()


def _task_match_candidates(query: str, *, matter: Matter) -> list[Task]:
    task_id_match = _TASK_ID_RE.search(query or "")
    if task_id_match:
        row = db.session.get(Task, int(task_id_match.group(1)))
        if row is not None and int(row.matter_id) == int(matter.id):
            return [row]
        return []

    cleaned = _strip_task_status_prompt(query)
    cleaned = re.sub(r"\b(?:done|complete(?:d)?|doing|in progress|todo|to do|start(?:ed)?)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = normalize_query(cleaned).strip(" .,:;-")
    tokens = _search_tokens(cleaned)
    if not tokens:
        return []

    tasks = (
        Task.query.filter_by(matter_id=matter.id)
        .order_by(Task.status.asc(), Task.due_date.asc().nullslast(), Task.id.desc())
        .limit(80)
        .all()
    )
    scored: list[tuple[int, Task]] = []
    for row in tasks:
        haystack = _task_search_text(row)
        score = sum(1 for token in tokens if token in haystack)
        if score <= 0:
            continue
        scored.append((score, row))
    scored.sort(key=lambda item: (item[0], -(item[1].id or 0)), reverse=True)
    return [row for _, row in scored[:8]]


def _extract_task_status(prompt: str) -> str | None:
    lowered = str(prompt or "").lower()
    if any(token in lowered for token in (" done", "mark done", "completed", "complete ", "finish", "finished")):
        return "Done"
    if any(token in lowered for token in ("doing", "in progress", "start ", "started")):
        return "Doing"
    if any(token in lowered for token in ("todo", "to do", "backlog")):
        return "Todo"
    return None


def _note_body_tags_and_privilege(prompt: str) -> tuple[str, str, str | None]:
    body = _strip_note_prompt(prompt)
    tags = sorted({match.group(1).lower() for match in _TAG_RE.finditer(body)})
    if tags:
        body = _TAG_RE.sub("", body)
    privilege_label = None
    lowered = body.lower()
    if "privileged" in lowered or "attorney-client" in lowered:
        privilege_label = "Attorney-Client Privileged"
    cleaned = normalize_query(body).strip(" .")
    return cleaned[:4000], ", ".join(tags)[:255], privilege_label


def _time_entry_defaults_for_date(entry_date: dt.date, *, hours: float) -> tuple[dt.datetime, dt.datetime]:
    now = utc_now().replace(second=0, microsecond=0)
    if entry_date == now.date():
        end_at = now
        start_at = end_at - dt.timedelta(hours=max(hours, 0.0))
        return start_at, end_at
    start_at = dt.datetime.combine(entry_date, dt.time(hour=9, minute=0))
    end_at = start_at + dt.timedelta(hours=max(hours, 0.0))
    return start_at, end_at


def _assistant_round_hours(hours: float, increment: float) -> float:
    if increment <= 0:
        return round(hours, 4)
    steps = round(hours / increment)
    return round(steps * increment, 4)


def _assistant_policy_for_matter(matter_id: int) -> TimeRoundingPolicy | None:
    matter = db.session.get(Matter, matter_id)
    if matter is None:
        return None
    policy = (
        TimeRoundingPolicy.query.filter_by(matter_id=matter_id, is_active=True)
        .order_by(TimeRoundingPolicy.id.desc())
        .first()
    )
    if policy is not None:
        return policy
    return (
        TimeRoundingPolicy.query.filter_by(client_name=matter.client_name, is_active=True)
        .order_by(TimeRoundingPolicy.id.desc())
        .first()
    )


def _assistant_validate_time_entry(entry: TimeEntry, policy: TimeRoundingPolicy | None) -> list[str]:
    issues: list[str] = []
    if policy and policy.min_narrative_length and len((entry.narrative or "").strip()) < int(policy.min_narrative_length):
        issues.append(f"Narrative must be at least {policy.min_narrative_length} characters")
    if policy and policy.require_activity_code and not (entry.activity_code or "").strip():
        issues.append("Activity code required")

    overlap = (
        TimeEntry.query.filter(
            TimeEntry.user_id == entry.user_id,
            TimeEntry.id != entry.id,
            TimeEntry.start_at < (entry.end_at or entry.start_at),
            or_(TimeEntry.end_at.is_(None), TimeEntry.end_at > entry.start_at),
        )
        .limit(1)
        .first()
    )
    if overlap is not None:
        issues.append(f"Overlaps with entry #{overlap.id}")

    if policy and policy.daily_hour_cap:
        day_start = dt.datetime.combine(entry.start_at.date(), dt.time.min)
        day_end = dt.datetime.combine(entry.start_at.date(), dt.time.max)
        day_total = (
            db.session.query(db.func.coalesce(db.func.sum(TimeEntry.rounded_hours), 0.0))
            .filter(TimeEntry.user_id == entry.user_id, TimeEntry.start_at >= day_start, TimeEntry.start_at <= day_end)
            .scalar()
            or 0.0
        )
        if float(day_total) > float(policy.daily_hour_cap):
            issues.append(f"Daily cap exceeded ({policy.daily_hour_cap}h)")
    return issues


def _existing_time_entry_duplicate(
    *,
    user_id: int,
    matter_id: int,
    start_at: dt.datetime,
    end_at: dt.datetime,
    narrative: str | None,
    exclude_entry_id: int | None = None,
) -> TimeEntry | None:
    query = TimeEntry.query.filter(
        TimeEntry.user_id == user_id,
        TimeEntry.matter_id == matter_id,
        TimeEntry.start_at == start_at,
        TimeEntry.end_at == end_at,
        TimeEntry.narrative == (narrative or None),
    )
    if exclude_entry_id:
        query = query.filter(TimeEntry.id != int(exclude_entry_id))
    return query.order_by(TimeEntry.id.desc()).limit(1).first()


def _extract_billable(prompt: str) -> bool:
    lowered = str(prompt or "").lower()
    if "non-billable" in lowered or "non billable" in lowered or "not billable" in lowered:
        return False
    return True


def _time_entry_narrative(prompt: str) -> str:
    body = _strip_time_entry_prompt(prompt)
    working = body
    working = _TIME_RANGE_RE.sub("", working)
    working = _HOURS_RE.sub("", working)
    working = _MINUTES_RE.sub("", working)
    working = re.sub(r"\b(?:today|yesterday|tomorrow|billable|non[- ]billable|not billable)\b", "", working, flags=re.IGNORECASE)
    working = re.sub(r"\b(?:on|for)\s+\d{4}-\d{2}-\d{2}\b", "", working, flags=re.IGNORECASE)
    working = re.sub(r"\b(?:on|for)\s+\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", "", working, flags=re.IGNORECASE)
    cleaned = normalize_query(working).strip(" .,:;-")
    if cleaned:
        return cleaned[:2000]
    return normalize_query(body).strip()[:2000]


def _extract_timeline_event_type(prompt: str) -> str:
    lowered = str(prompt or "").lower()
    if "hearing" in lowered:
        return "Hearing"
    if "filing" in lowered or re.search(r"\bfile\b|\bserve\b", lowered):
        return "Filing"
    if "client update" in lowered:
        return "Client Update"
    if "internal review" in lowered or "partner review" in lowered:
        return "Internal Review"
    if "delivery" in lowered or "deliver " in lowered:
        return "Delivery"
    return "Milestone"


def _extract_timeline_milestone_flag(prompt: str, event_type: str) -> bool:
    lowered = str(prompt or "").lower()
    if event_type in {"Hearing", "Filing", "Delivery"}:
        return True
    if "deadline" in lowered or "milestone" in lowered:
        return True
    return False


def _timeline_title_and_description(prompt: str, *, event_type: str) -> tuple[str, str]:
    body = _strip_timeline_prompt(prompt)
    working = body
    working = re.sub(r"\b(?:on|by)\s+\d{4}-\d{2}-\d{2}\b", "", working, flags=re.IGNORECASE)
    working = re.sub(r"\b(?:on|by)\s+\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", "", working, flags=re.IGNORECASE)
    working = re.sub(r"\b(?:on|by)\s+tomorrow\b", "", working, flags=re.IGNORECASE)
    working = re.sub(r"\b(?:on|by)\s+today\b", "", working, flags=re.IGNORECASE)
    working = re.sub(r"\bnext week\b", "", working, flags=re.IGNORECASE)
    working = re.sub(r"^\d{4}-\d{2}-\d{2}\b", "", working, flags=re.IGNORECASE)
    working = re.sub(r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", "", working, flags=re.IGNORECASE)
    working = re.sub(r"^(?:for|to|about)\s+", "", working, flags=re.IGNORECASE)
    cleaned_title = normalize_query(working).strip(" .,:;-")
    if not cleaned_title:
        cleaned_title = f"{event_type} event"
    title = cleaned_title[:180]
    cleaned_body = normalize_query(body).strip()
    description = ""
    if cleaned_body and cleaned_body.casefold() != title.casefold():
        description = cleaned_body[:2000]
    return title, description


def _matter_briefing_sections(matter: Matter) -> tuple[list[dict[str, Any]], list[str]]:
    today = dt.date.today()
    timeline = (
        MatterTimelineEvent.query.filter(
            MatterTimelineEvent.matter_id == matter.id,
            MatterTimelineEvent.event_date >= today,
        )
        .order_by(MatterTimelineEvent.event_date.asc(), MatterTimelineEvent.id.asc())
        .limit(8)
        .all()
    )
    tasks = (
        Task.query.filter_by(matter_id=matter.id)
        .order_by(Task.due_date.asc().nullslast(), Task.id.desc())
        .limit(20)
        .all()
    )
    open_tasks = [row for row in tasks if (row.status or "").strip().lower() != "done"]
    notes = filter_accessible_matter_notes(
        MatterNote.query.filter_by(matter_id=matter.id)
        .order_by(MatterNote.updated_at.desc().nullslast(), MatterNote.id.desc())
        .limit(6)
        .all()
    )
    docs = filter_accessible_document_files(
        DocumentFile.query.filter_by(matter_id=matter.id)
        .order_by(DocumentFile.uploaded_at.desc())
        .limit(6)
        .all()
    )
    time_entries = (
        TimeEntry.query.filter_by(user_id=current_user.id, matter_id=matter.id)
        .order_by(TimeEntry.start_at.desc())
        .limit(6)
        .all()
    )

    sections: list[dict[str, Any]] = []
    warnings: list[str] = []
    overdue_tasks = [row for row in open_tasks if row.due_date and row.due_date < today]
    if overdue_tasks:
        warnings.append(f"{len(overdue_tasks)} open task(s) are overdue on this matter.")
    if not timeline:
        warnings.append("No future timeline event is recorded on this matter yet.")

    if timeline:
        sections.append(
            {
                "title": "Upcoming Timeline",
                "items": [
                    {
                        "title": row.title or f"{row.event_type} event",
                        "meta": " • ".join(
                            item
                            for item in [
                                row.event_date.isoformat() if row.event_date else "",
                                row.event_type or "",
                                "Milestone" if row.is_milestone else "",
                            ]
                            if item
                        ),
                        "href": url_for("matter_detail", matter_id=matter.id),
                    }
                    for row in timeline[:6]
                ],
            }
        )
    if open_tasks:
        sections.append(
            {
                "title": "Open Tasks",
                "items": [
                    {
                        "title": row.title or f"Task {row.id}",
                        "meta": " • ".join(
                            item
                            for item in [
                                row.status or "",
                                f"Due {row.due_date.isoformat()}" if row.due_date else "No due date",
                                f"Priority {row.priority}" if row.priority else "",
                            ]
                            if item
                        ),
                        "href": url_for("matter_tasks", matter_id=matter.id),
                    }
                    for row in open_tasks[:6]
                ],
            }
        )
    if notes:
        sections.append(
            {
                "title": "Recent Notes",
                "items": [
                    {
                        "title": ((row.body or "").strip().replace("\n", " ")[:90] or "Matter note"),
                        "meta": " • ".join(
                            item
                            for item in [
                                f"Tags {row.tags}" if row.tags else "",
                                row.privilege_label or "",
                            ]
                            if item
                        ),
                        "href": url_for("matter_notes", matter_id=matter.id),
                    }
                    for row in notes[:4]
                ],
            }
        )
    if docs:
        sections.append(
            {
                "title": "Recent Documents",
                "items": [
                    {
                        "title": row.original_filename or f"Document {row.id}",
                        "meta": " • ".join(
                            item
                            for item in [
                                row.category or "",
                                row.doc_version or "",
                                row.uploaded_at.date().isoformat() if row.uploaded_at else "",
                            ]
                            if item
                        ),
                        "href": url_for("matter_dms", matter_id=matter.id),
                    }
                    for row in docs[:4]
                ],
            }
        )
    if time_entries:
        sections.append(
            {
                "title": "Recent Time Capture",
                "items": [
                    {
                        "title": row.narrative or "Time entry",
                        "meta": " • ".join(
                            item
                            for item in [
                                f"{float(row.rounded_hours or 0.0):.2f}h",
                                row.status or "",
                                row.start_at.date().isoformat() if row.start_at else "",
                            ]
                            if item
                        ),
                        "href": url_for("time_entries", matter_id=matter.id),
                    }
                    for row in time_entries[:4]
                ],
            }
        )
    return sections, warnings


def _search_sections(query: str, matter: Matter | None) -> list[dict[str, Any]]:
    q = _strip_search_prompt(query)
    if len(q) < 3:
        return []

    like = f"%{q}%"
    search_tokens = _search_tokens(q)
    matter_scope_ids: set[int] | None = None
    if matter is not None:
        matter_scope_ids = {int(matter.id)}
    elif not is_admin():
        matter_scope_ids = set(visible_matter_ids())
        if not matter_scope_ids:
            return []

    matter_query = Matter.query
    task_query = Task.query
    doc_query = DocumentFile.query
    timeline_query = MatterTimelineEvent.query
    activity_query = MatterActivity.query
    time_entry_query = TimeEntry.query.filter(TimeEntry.user_id == current_user.id)
    if matter_scope_ids is not None:
        matter_query = matter_query.filter(Matter.id.in_(sorted(matter_scope_ids)))
        task_query = task_query.filter(Task.matter_id.in_(sorted(matter_scope_ids)))
        doc_query = doc_query.filter(DocumentFile.matter_id.in_(sorted(matter_scope_ids)))
        timeline_query = timeline_query.filter(MatterTimelineEvent.matter_id.in_(sorted(matter_scope_ids)))
        activity_query = activity_query.filter(MatterActivity.matter_id.in_(sorted(matter_scope_ids)))
        time_entry_query = time_entry_query.filter(TimeEntry.matter_id.in_(sorted(matter_scope_ids)))

    matters = (
        matter_query.filter(
            or_(
                Matter.matter_no.ilike(like),
                Matter.title.ilike(like),
                Matter.client_name.ilike(like),
                Matter.objective.ilike(like),
                Matter.last_update_note.ilike(like),
                Matter.outcome_summary.ilike(like),
            )
        )
        .order_by(Matter.last_updated_at.desc().nullslast(), Matter.id.desc())
        .limit(6)
        .all()
    )
    tasks = (
        task_query.filter(or_(Task.title.ilike(like), Task.description.ilike(like)))
        .order_by(Task.due_date.asc().nullslast(), Task.id.desc())
        .limit(8)
        .all()
    )
    docs = filter_accessible_document_files(
        doc_query.filter(
            or_(
                DocumentFile.original_filename.ilike(like),
                DocumentFile.category.ilike(like),
                DocumentFile.owner_name.ilike(like),
                DocumentFile.doc_version.ilike(like),
            )
        )
        .order_by(DocumentFile.uploaded_at.desc())
        .limit(10)
        .all()
    )[:8]
    if not docs and search_tokens:
        candidate_docs = filter_accessible_document_files(
            doc_query.order_by(DocumentFile.uploaded_at.desc()).limit(40).all()
        )
        min_token_hits = len(search_tokens) if len(search_tokens) <= 2 else max(2, (len(search_tokens) + 1) // 2)
        scored_docs: list[tuple[int, DocumentFile]] = []
        for row in candidate_docs:
            haystack = _document_search_text(row)
            score = sum(1 for token in search_tokens if token in haystack)
            if score >= min_token_hits:
                scored_docs.append((score, row))
        scored_docs.sort(key=lambda item: item[0], reverse=True)
        docs = [row for _, row in scored_docs[:8]]

    note_query = MatterNote.query
    if matter_scope_ids is not None:
        note_query = note_query.filter(MatterNote.matter_id.in_(sorted(matter_scope_ids)))
    notes = filter_accessible_matter_notes(
        note_query.filter(
            or_(
                MatterNote.body.ilike(like),
                MatterNote.tags.ilike(like),
                MatterNote.privilege_label.ilike(like),
            )
        )
        .order_by(MatterNote.updated_at.desc().nullslast(), MatterNote.id.desc())
        .limit(10)
        .all()
    )
    timeline = (
        timeline_query.filter(
            or_(
                MatterTimelineEvent.title.ilike(like),
                MatterTimelineEvent.description.ilike(like),
                MatterTimelineEvent.event_type.ilike(like),
            )
        )
        .order_by(MatterTimelineEvent.event_date.asc(), MatterTimelineEvent.id.desc())
        .limit(10)
        .all()
    )
    activity = (
        activity_query.filter(
            or_(
                MatterActivity.action.ilike(like),
                MatterActivity.details.ilike(like),
            )
        )
        .order_by(MatterActivity.created_at.desc())
        .limit(10)
        .all()
    )
    time_entries = (
        time_entry_query.filter(
            or_(
                TimeEntry.narrative.ilike(like),
                TimeEntry.task_code.ilike(like),
                TimeEntry.activity_code.ilike(like),
            )
        )
        .order_by(TimeEntry.start_at.desc())
        .limit(10)
        .all()
    )

    matter_by_id = {
        int(row.id): row
        for row in Matter.query.filter(
            Matter.id.in_(
                sorted(
                    {
                        int(row.matter_id)
                        for row in [*tasks, *docs, *notes, *timeline, *activity, *time_entries]
                        if getattr(row, "matter_id", None)
                    }
                )
            )
        ).all()
    }

    sections: list[dict[str, Any]] = []
    if matters:
        sections.append(
            {
                "title": "Matters",
                "items": [
                    {
                        "title": assistant_matter_label(row),
                        "meta": " • ".join(
                            item
                            for item in [row.client_name or "", row.status or "", f"Risk {row.risk_level or 'Medium'}"]
                            if item
                        ),
                        "href": url_for("matter_detail", matter_id=row.id),
                    }
                    for row in matters
                ],
            }
        )
    if tasks:
        sections.append(
            {
                "title": "Tasks",
                "items": [
                    {
                        "title": row.title or f"Task {row.id}",
                        "meta": " • ".join(
                            item
                            for item in [
                                assistant_matter_label(matter_by_id.get(int(row.matter_id))),
                                row.status or "",
                                f"Due {row.due_date.isoformat()}" if row.due_date else "",
                            ]
                            if item
                        ),
                        "href": url_for("matter_tasks", matter_id=row.matter_id),
                    }
                    for row in tasks
                ],
            }
        )
    if docs:
        sections.append(
            {
                "title": "Documents",
                "items": [
                    {
                        "title": row.original_filename or f"Document {row.id}",
                        "meta": " • ".join(
                            item
                            for item in [
                                assistant_matter_label(matter_by_id.get(int(row.matter_id))),
                                row.category or "",
                                row.doc_version or "",
                            ]
                            if item
                        ),
                        "href": url_for("matter_dms", matter_id=row.matter_id),
                    }
                    for row in docs
                ],
            }
        )
    if notes:
        sections.append(
            {
                "title": "Matter Notes",
                "items": [
                    {
                        "title": assistant_matter_label(matter_by_id.get(int(row.matter_id))) or f"Matter {row.matter_id}",
                        "meta": " • ".join(
                            item
                            for item in [
                                ((row.body or "").strip().replace("\n", " ")[:140] or "Note"),
                                f"Tags {row.tags}" if row.tags else "",
                                row.privilege_label or "",
                            ]
                            if item
                        ),
                        "href": url_for("matter_notes", matter_id=row.matter_id),
                    }
                    for row in notes[:6]
                ],
            }
        )
    if timeline:
        sections.append(
            {
                "title": "Timeline & Deadlines",
                "items": [
                    {
                        "title": row.title or f"{row.event_type or 'Timeline'} event",
                        "meta": " • ".join(
                            item
                            for item in [
                                assistant_matter_label(matter_by_id.get(int(row.matter_id))),
                                row.event_date.isoformat() if row.event_date else "",
                                row.event_type or "",
                            ]
                            if item
                        ),
                        "href": url_for("matter_detail", matter_id=row.matter_id),
                    }
                    for row in timeline[:6]
                ],
            }
        )
    if activity:
        sections.append(
            {
                "title": "Recent Activity",
                "items": [
                    {
                        "title": row.action or f"Activity {row.id}",
                        "meta": " • ".join(
                            item
                            for item in [
                                assistant_matter_label(matter_by_id.get(int(row.matter_id))),
                                (row.details or "").strip()[:140],
                                row.created_at.date().isoformat() if row.created_at else "",
                            ]
                            if item
                        ),
                        "href": url_for("matter_detail", matter_id=row.matter_id),
                    }
                    for row in activity[:6]
                ],
            }
        )
    if time_entries:
        sections.append(
            {
                "title": "My Time Entries",
                "items": [
                    {
                        "title": row.narrative or "Time entry",
                        "meta": " • ".join(
                            item
                            for item in [
                                assistant_matter_label(matter_by_id.get(int(row.matter_id))),
                                f"{float(row.rounded_hours or 0.0):.2f}h",
                                row.status or "",
                                row.start_at.date().isoformat() if row.start_at else "",
                            ]
                            if item
                        ),
                        "href": url_for("time_entries", matter_id=row.matter_id),
                    }
                    for row in time_entries[:6]
                ],
            }
        )
    if bool(current_app.config.get("AI_SEMANTIC_SEARCH_ENABLED", False)):
        semantic_hits = SemanticSearchService.search(q, matter_scope_ids=matter_scope_ids, limit=5)
        if semantic_hits:
            sections.append(
                {
                    "title": "Semantic Results",
                    "items": [
                        {
                            "title": row.get("title") or "Semantic document match",
                            "meta": " • ".join(
                                item
                                for item in [
                                    f"Score {row.get('score')}",
                                    f"Matter {row.get('matter_id')}" if row.get("matter_id") else "",
                                ]
                                if item
                            ),
                            "href": row.get("link") or url_for("search", q=q),
                        }
                        for row in semantic_hits
                    ],
                }
            )
    return sections


def process_assistant_prompt(prompt: str, *, selected_matter_id: int | None = None) -> dict[str, Any]:
    cleaned_prompt = _clean_prompt(prompt)
    if len(cleaned_prompt) < 3:
        return _error_result(cleaned_prompt, "Enter a fuller instruction for the assistant to work from.")

    matter, warnings = _resolve_matter(selected_matter_id, cleaned_prompt)
    blocked_reason = _requested_block_reason(cleaned_prompt)
    if blocked_reason:
        audit(
            "assistant_request_blocked",
            "Matter",
            int(matter.id) if matter is not None else None,
            {"prompt": cleaned_prompt[:255], "reason": blocked_reason},
        )
        return _blocked_result(cleaned_prompt, blocked_reason, matter=matter)

    intent = _classify_intent(cleaned_prompt)
    if intent == "draft_summary":
        if matter is None:
            return _error_result(cleaned_prompt, "Pick a matter or reference its matter number to draft a summary.")
        context = _matter_context(matter)
        suggestion = suggest_matter_executive_summary(
            matter_context=context,
            current_values={
                "objective": matter.objective or "",
                "last_update_note": matter.last_update_note or "",
                "outcome_summary": matter.outcome_summary or "",
                "risk_level": matter.risk_level or "",
                "budget_status": matter.budget_status or "",
            },
        )
        audit(
            "assistant_summary_draft",
            "Matter",
            matter.id,
            {"source": suggestion.get("source"), "fallback_reason": suggestion.get("fallback_reason")},
        )
        return _result(
            status="ok",
            kind="draft_summary",
            headline="Executive Summary Draft",
            summary=f"Prepared a matter summary draft for {assistant_matter_label(matter)}.",
            prompt=cleaned_prompt,
            matter=matter,
            warnings=warnings,
            fields=[
                {"label": "Risk", "value": suggestion.get("risk_level") or matter.risk_level or "Medium"},
                {"label": "Budget", "value": suggestion.get("budget_status") or matter.budget_status or "On Track"},
                {"label": "Source", "value": str(suggestion.get("source") or "fallback").title()},
            ],
            text_blocks=[
                {"title": "Objective", "body": suggestion.get("objective") or ""},
                {"title": "Latest Update", "body": suggestion.get("last_update_note") or ""},
                {"title": "Outcome Summary", "body": suggestion.get("outcome_summary") or ""},
            ],
            links=[
                {"label": "Open Matter", "href": url_for("matter_detail", matter_id=matter.id)},
                {"label": "Open Notes", "href": url_for("matter_notes", matter_id=matter.id)},
            ],
        )

    if intent == "draft_client_update":
        if matter is None:
            return _error_result(cleaned_prompt, "Pick a matter or reference its matter number to draft a client update.")
        context = _matter_context(matter)
        tone_hint = _tone_hint_from_prompt(cleaned_prompt)
        suggestion = suggest_matter_client_update(matter_context=context, tone_hint=tone_hint)
        warning_list = list(warnings)
        if "send " in cleaned_prompt.lower():
            warning_list.append("The assistant drafts the update here. Sending still happens through the native workflow.")
        audit(
            "assistant_client_update_draft",
            "Matter",
            matter.id,
            {"source": suggestion.get("source"), "fallback_reason": suggestion.get("fallback_reason")},
        )
        return _result(
            status="ok",
            kind="draft_client_update",
            headline="Client Update Draft",
            summary=f"Prepared a client-facing update draft for {assistant_matter_label(matter)}.",
            prompt=cleaned_prompt,
            matter=matter,
            warnings=warning_list,
            fields=[
                {"label": "Tone", "value": tone_hint},
                {"label": "Source", "value": str(suggestion.get("source") or "fallback").title()},
            ],
            text_blocks=[
                {"title": "Subject", "body": suggestion.get("subject") or ""},
                {"title": "Body", "body": suggestion.get("body") or ""},
            ],
            links=[
                {"label": "Open Matter", "href": url_for("matter_detail", matter_id=matter.id)},
                {"label": "Portal Messages", "href": url_for("portal_message_center")},
            ],
        )

    if intent == "create_task":
        if matter is None:
            return _error_result(cleaned_prompt, "Pick a matter or reference its matter number before creating a task.")
        if not role_is_case(getattr(current_user, "role", None)):
            return _blocked_result(
                cleaned_prompt,
                "Only legal case-team roles can create tasks from the assistant.",
                matter=matter,
            )
        title, description = _task_title_and_description(cleaned_prompt)
        if not title:
            return _error_result(cleaned_prompt, "The assistant could not determine a task title from that prompt.", matter=matter)
        due_date = _extract_due_date(cleaned_prompt)
        assignee = _extract_assignee(cleaned_prompt)
        priority = _extract_priority(cleaned_prompt)
        preview_payload = {
            "action": "create_task",
            "user_id": int(current_user.id),
            "matter_id": int(matter.id),
            "title": title[:255],
            "description": description[:2000],
            "due_date": due_date.isoformat() if due_date else "",
            "priority": priority,
            "assigned_to_user_id": int(assignee.id) if assignee is not None else None,
        }
        audit(
            "assistant_task_preview",
            "Matter",
            matter.id,
            {"prompt": cleaned_prompt[:255], "has_due_date": bool(due_date), "priority": priority},
        )
        return _result(
            status="ok",
            kind="create_task_preview",
            headline="Task Ready for Confirmation",
            summary=f"Prepared a task draft for {assistant_matter_label(matter)}. Confirm before it is written.",
            prompt=cleaned_prompt,
            matter=matter,
            warnings=warnings,
            fields=[
                {"label": "Title", "value": title[:255]},
                {"label": "Due Date", "value": due_date.isoformat() if due_date else "No due date parsed"},
                {"label": "Priority", "value": priority},
                {"label": "Assignee", "value": assignee.full_name if assignee is not None else "Unassigned"},
            ],
            text_blocks=[{"title": "Description", "body": description or "No extra description was parsed."}],
            links=[{"label": "Open Matter Tasks", "href": url_for("matter_tasks", matter_id=matter.id)}],
            requires_confirmation=True,
            confirm_token=_sign_confirmation_payload(preview_payload),
        )

    if intent == "update_task_status":
        if matter is None:
            return _error_result(cleaned_prompt, "Pick a matter or reference its matter number before updating task status.")
        if not role_is_case(getattr(current_user, "role", None)):
            return _blocked_result(
                cleaned_prompt,
                "Only legal case-team roles can update task status from the assistant.",
                matter=matter,
            )
        status = _extract_task_status(cleaned_prompt)
        if status is None:
            return _error_result(
                cleaned_prompt,
                "Specify the target task status in the prompt, for example Done, Doing, or Todo.",
                matter=matter,
            )
        matches = _task_match_candidates(cleaned_prompt, matter=matter)
        if not matches:
            return _error_result(
                cleaned_prompt,
                "The assistant could not identify the target task on this matter. Reference the task title or task number.",
                matter=matter,
            )
        if len(matches) > 1:
            return _result(
                status="error",
                kind="task_status_ambiguous",
                headline="Task Match Needs Clarification",
                summary="More than one task matches that request. Reference the exact task title or task number.",
                prompt=cleaned_prompt,
                matter=matter,
                warnings=warnings,
                sections=[
                    {
                        "title": "Matching Tasks",
                        "items": [
                            {
                                "title": row.title or f"Task {row.id}",
                                "meta": " • ".join(
                                    item
                                    for item in [
                                        f"Task #{row.id}",
                                        row.status or "",
                                        f"Due {row.due_date.isoformat()}" if row.due_date else "",
                                    ]
                                    if item
                                ),
                                "href": url_for("matter_tasks", matter_id=matter.id),
                            }
                            for row in matches[:6]
                        ],
                    }
                ],
                links=[{"label": "Open Matter Tasks", "href": url_for("matter_tasks", matter_id=matter.id)}],
            )
        task = matches[0]
        if (task.status or "Todo") == status:
            return _result(
                status="ok",
                kind="task_status_noop",
                headline="Task Already In Requested Status",
                summary=f"{task.title or f'Task #{task.id}'} is already {status} on {assistant_matter_label(matter)}.",
                prompt=cleaned_prompt,
                matter=matter,
                warnings=warnings,
                fields=[
                    {"label": "Task", "value": task.title or f"Task #{task.id}"},
                    {"label": "Current Status", "value": task.status or "Todo"},
                ],
                links=[{"label": "Open Matter Tasks", "href": url_for("matter_tasks", matter_id=matter.id)}],
            )
        preview_payload = {
            "action": "update_task_status",
            "user_id": int(current_user.id),
            "matter_id": int(matter.id),
            "task_id": int(task.id),
            "status": status,
        }
        audit(
            "assistant_task_status_preview",
            "Task",
            task.id,
            {"matter_id": matter.id, "status": status, "prompt": cleaned_prompt[:255]},
        )
        return _result(
            status="ok",
            kind="update_task_status_preview",
            headline="Task Status Change Ready for Confirmation",
            summary=f"Prepared a task status update for {assistant_matter_label(matter)}. Confirm before it is written.",
            prompt=cleaned_prompt,
            matter=matter,
            warnings=warnings,
            fields=[
                {"label": "Task", "value": task.title or f"Task #{task.id}"},
                {"label": "Current Status", "value": task.status or "Todo"},
                {"label": "New Status", "value": status},
            ],
            links=[{"label": "Open Matter Tasks", "href": url_for("matter_tasks", matter_id=matter.id)}],
            requires_confirmation=True,
            confirm_token=_sign_confirmation_payload(preview_payload),
        )

    if intent == "create_time_entry":
        if matter is None:
            return _error_result(cleaned_prompt, "Pick a matter or reference its matter number before logging time.")
        if not role_is_case(getattr(current_user, "role", None)):
            return _blocked_result(
                cleaned_prompt,
                "Only legal case-team roles can log time from the assistant.",
                matter=matter,
            )
        if (matter.status or "").strip().lower() == "closed":
            return _blocked_result(
                cleaned_prompt,
                "Closed matters cannot accept new time entries. Reopen the matter first.",
                matter=matter,
            )
        entry_date = _extract_entry_date(cleaned_prompt) or dt.date.today()
        time_range = _extract_time_range(cleaned_prompt, default_date=entry_date)
        parsed_hours = _extract_hours(cleaned_prompt)
        if time_range is not None:
            start_at, end_at = time_range
            hours = max(0.0, (end_at - start_at).total_seconds() / 3600.0)
        elif parsed_hours and parsed_hours > 0:
            start_at, end_at = _time_entry_defaults_for_date(entry_date, hours=parsed_hours)
            hours = parsed_hours
        else:
            return _error_result(
                cleaned_prompt,
                "Include either a duration like 1.5 hours or a time range like 09:00 to 10:30 when logging time.",
                matter=matter,
            )
        if hours <= 0:
            return _error_result(cleaned_prompt, "Time entry duration must be greater than zero.", matter=matter)
        narrative = _time_entry_narrative(cleaned_prompt)
        if not narrative:
            return _error_result(cleaned_prompt, "The assistant could not determine a time-entry narrative.", matter=matter)
        task_matches = _task_match_candidates(cleaned_prompt, matter=matter)
        task = task_matches[0] if len(task_matches) == 1 else None
        policy = _assistant_policy_for_matter(matter.id)
        rounded_hours = _assistant_round_hours(hours, float(policy.increment_hours if policy else 0.1))
        duplicate = _existing_time_entry_duplicate(
            user_id=int(current_user.id),
            matter_id=int(matter.id),
            start_at=start_at,
            end_at=end_at,
            narrative=narrative[:2000] or None,
        )
        if duplicate is not None:
            return _error_result(
                cleaned_prompt,
                f"A matching time entry already exists on this matter as entry #{duplicate.id}.",
                matter=matter,
            )
        preview_payload = {
            "action": "create_time_entry",
            "user_id": int(current_user.id),
            "matter_id": int(matter.id),
            "task_id": int(task.id) if task is not None else None,
            "start_at": start_at.isoformat(timespec="minutes"),
            "end_at": end_at.isoformat(timespec="minutes"),
            "hours": round(hours, 4),
            "rounded_hours": rounded_hours,
            "narrative": narrative[:2000],
            "is_billable": _extract_billable(cleaned_prompt),
        }
        warning_list = list(warnings)
        if len(task_matches) > 1:
            warning_list.append("More than one task matched the prompt, so the time entry will be saved without a linked task.")
        if policy and policy.require_activity_code:
            warning_list.append("This matter requires an activity code, so the time entry may save in needs-review status.")
        audit(
            "assistant_time_entry_preview",
            "Matter",
            matter.id,
            {
                "prompt": cleaned_prompt[:255],
                "hours": round(hours, 4),
                "rounded_hours": rounded_hours,
                "is_billable": bool(preview_payload["is_billable"]),
            },
        )
        return _result(
            status="ok",
            kind="create_time_entry_preview",
            headline="Time Entry Ready for Confirmation",
            summary=f"Prepared a draft time entry for {assistant_matter_label(matter)}. Confirm before it is written.",
            prompt=cleaned_prompt,
            matter=matter,
            warnings=warning_list,
            fields=[
                {"label": "Narrative", "value": narrative[:2000]},
                {"label": "Start", "value": start_at.isoformat(timespec="minutes")},
                {"label": "End", "value": end_at.isoformat(timespec="minutes")},
                {"label": "Hours", "value": f"{hours:.2f}"},
                {"label": "Rounded Hours", "value": f"{rounded_hours:.2f}"},
                {"label": "Billable", "value": "Yes" if preview_payload["is_billable"] else "No"},
                {"label": "Task", "value": task.title if task is not None else "No linked task"},
            ],
            links=[{"label": "Open Time Entries", "href": url_for("time_entries", matter_id=matter.id)}],
            requires_confirmation=True,
            confirm_token=_sign_confirmation_payload(preview_payload),
        )

    if intent == "create_timeline_event":
        if matter is None:
            return _error_result(
                cleaned_prompt,
                "Pick a matter or reference its matter number before adding a timeline event.",
            )
        if not role_is_case(getattr(current_user, "role", None)):
            return _blocked_result(
                cleaned_prompt,
                "Only legal case-team roles can add timeline events from the assistant.",
                matter=matter,
            )
        event_type = _extract_timeline_event_type(cleaned_prompt)
        event_date = _extract_due_date(cleaned_prompt) or dt.date.today()
        title, description = _timeline_title_and_description(cleaned_prompt, event_type=event_type)
        is_milestone = _extract_timeline_milestone_flag(cleaned_prompt, event_type)
        if not title:
            return _error_result(
                cleaned_prompt,
                "The assistant could not determine a timeline title from that prompt.",
                matter=matter,
            )
        preview_payload = {
            "action": "create_timeline_event",
            "user_id": int(current_user.id),
            "matter_id": int(matter.id),
            "title": title[:180],
            "description": description[:2000],
            "event_date": event_date.isoformat(),
            "event_type": event_type,
            "is_milestone": bool(is_milestone),
        }
        audit(
            "assistant_timeline_preview",
            "Matter",
            matter.id,
            {
                "prompt": cleaned_prompt[:255],
                "event_type": event_type,
                "event_date": event_date.isoformat(),
                "is_milestone": bool(is_milestone),
            },
        )
        return _result(
            status="ok",
            kind="create_timeline_event_preview",
            headline="Timeline Event Ready for Confirmation",
            summary=f"Prepared a timeline event for {assistant_matter_label(matter)}. Confirm before it is written.",
            prompt=cleaned_prompt,
            matter=matter,
            warnings=warnings,
            fields=[
                {"label": "Title", "value": title[:180]},
                {"label": "Event Date", "value": event_date.isoformat()},
                {"label": "Event Type", "value": event_type},
                {"label": "Milestone", "value": "Yes" if is_milestone else "No"},
            ],
            text_blocks=[{"title": "Description", "body": description or "No extra description was parsed."}],
            links=[{"label": "Open Matter", "href": url_for("matter_detail", matter_id=matter.id)}],
            requires_confirmation=True,
            confirm_token=_sign_confirmation_payload(preview_payload),
        )

    if intent == "add_note":
        if matter is None:
            return _error_result(cleaned_prompt, "Pick a matter or reference its matter number before adding a note.")
        if not role_is_case(getattr(current_user, "role", None)):
            return _blocked_result(
                cleaned_prompt,
                "Only legal case-team roles can add matter notes from the assistant.",
                matter=matter,
            )
        body, tags, privilege_label = _note_body_tags_and_privilege(cleaned_prompt)
        if not body:
            return _error_result(cleaned_prompt, "The assistant could not determine note text from that prompt.", matter=matter)
        preview_payload = {
            "action": "add_note",
            "user_id": int(current_user.id),
            "matter_id": int(matter.id),
            "body": body[:4000],
            "tags": tags[:255],
            "privilege_label": privilege_label or "",
        }
        audit(
            "assistant_note_preview",
            "Matter",
            matter.id,
            {"prompt": cleaned_prompt[:255], "privileged": bool(privilege_label), "has_tags": bool(tags)},
        )
        return _result(
            status="ok",
            kind="add_note_preview",
            headline="Matter Note Ready for Confirmation",
            summary=f"Prepared a matter note draft for {assistant_matter_label(matter)}. Confirm before it is written.",
            prompt=cleaned_prompt,
            matter=matter,
            warnings=warnings,
            fields=[
                {"label": "Tags", "value": tags or "No tags parsed"},
                {"label": "Privilege", "value": privilege_label or "Standard note"},
            ],
            text_blocks=[{"title": "Note Body", "body": body[:4000]}],
            links=[{"label": "Open Matter Notes", "href": url_for("matter_notes", matter_id=matter.id)}],
            requires_confirmation=True,
            confirm_token=_sign_confirmation_payload(preview_payload),
        )

    if intent == "matter_briefing":
        if matter is None:
            return _error_result(cleaned_prompt, "Pick a matter or reference its matter number to review next steps.")
        context = _matter_context(matter)
        sections, briefing_warnings = _matter_briefing_sections(matter)
        warning_list = list(warnings) + briefing_warnings
        audit(
            "assistant_matter_briefing",
            "Matter",
            matter.id,
            {
                "open_task_count": int(context.get("open_task_count") or 0),
                "overdue_task_count": int(context.get("overdue_task_count") or 0),
                "timeline_count": sum(
                    len(section.get("items") or [])
                    for section in sections
                    if section.get("title") == "Upcoming Timeline"
                ),
            },
        )
        return _result(
            status="ok",
            kind="matter_briefing",
            headline="Matter Briefing",
            summary=f"Prepared a next-steps briefing for {assistant_matter_label(matter)}.",
            prompt=cleaned_prompt,
            matter=matter,
            warnings=warning_list,
            fields=[
                {"label": "Status", "value": context.get("status") or "Open"},
                {"label": "Risk", "value": context.get("risk_level") or "Medium"},
                {"label": "Budget", "value": context.get("budget_status") or "On Track"},
                {"label": "Open Tasks", "value": str(context.get("open_task_count") or 0)},
                {"label": "Next Due Task", "value": context.get("next_due_task") or "No task deadline captured"},
                {"label": "Latest Milestone", "value": context.get("latest_timeline_title") or "No timeline milestone logged"},
                {
                    "label": "Recent Time Entries",
                    "value": str(len(context.get("recent_time_entries") or [])),
                },
            ],
            text_blocks=[
                {"title": "Objective", "body": context.get("objective") or "No objective is recorded on this matter yet."},
                {
                    "title": "Latest Update",
                    "body": context.get("last_update_note")
                    or "No update note is recorded on this matter yet.",
                },
            ],
            sections=sections,
            links=[
                {"label": "Open Matter", "href": url_for("matter_detail", matter_id=matter.id)},
                {"label": "Open Matter Tasks", "href": url_for("matter_tasks", matter_id=matter.id)},
            ],
        )

    sections = _search_sections(cleaned_prompt, matter)
    audit(
        "assistant_search",
        "Matter",
        int(matter.id) if matter is not None else None,
        {"prompt": cleaned_prompt[:255], "section_count": len(sections)},
    )
    if not sections:
        return _result(
            status="ok",
            kind="search",
            headline="No Direct Matches Found",
            summary="No matching matters, tasks, or documents were found in your current scope.",
            prompt=cleaned_prompt,
            matter=matter,
            warnings=warnings,
            links=[{"label": "Open Global Search", "href": url_for("search", q=_strip_search_prompt(cleaned_prompt))}],
        )
    return _result(
        status="ok",
        kind="search",
        headline="Assistant Search Results",
        summary="Matched records in your current scope.",
        prompt=cleaned_prompt,
        matter=matter,
        warnings=warnings,
        sections=sections,
        links=[{"label": "Open Global Search", "href": url_for("search", q=_strip_search_prompt(cleaned_prompt))}],
    )


def execute_assistant_confirmation(confirm_token: str, *, prompt: str) -> dict[str, Any]:
    cleaned_prompt = _clean_prompt(prompt)
    if not confirm_token:
        return _error_result(cleaned_prompt, "Confirmation token is missing.")
    if _confirmation_already_consumed(confirm_token):
        audit(
            "assistant_confirmation_replay_blocked",
            None,
            None,
            {"prompt": cleaned_prompt[:255]},
        )
        return _error_result(
            cleaned_prompt,
            "This confirmation has already been used. Re-run the preview if you want to create a fresh draft.",
        )
    try:
        payload = _load_confirmation_payload(confirm_token)
    except BadData:
        return _error_result(cleaned_prompt, "The confirmation token is invalid or has expired.")
    if int(payload.get("user_id") or 0) != int(getattr(current_user, "id", 0) or 0):
        return _error_result(cleaned_prompt, "This confirmation token belongs to a different user session.")

    matter = db.session.get(Matter, int(payload.get("matter_id") or 0))
    if matter is None or not can_access_matter(int(matter.id)):
        return _error_result(cleaned_prompt, "The target matter is no longer available in your current access scope.")
    if not role_is_case(getattr(current_user, "role", None)):
        return _blocked_result(cleaned_prompt, "Only legal case-team roles can confirm assistant write actions.", matter=matter)

    action = str(payload.get("action") or "").strip().lower()
    if action == "create_task":
        title = normalize_query(str(payload.get("title") or ""))[:255]
        description = str(payload.get("description") or "").strip()[:2000] or None
        if not title:
            return _error_result(cleaned_prompt, "Task title is missing from the confirmation payload.", matter=matter)
        due_date = None
        due_date_raw = str(payload.get("due_date") or "").strip()
        if due_date_raw:
            try:
                due_date = dt.date.fromisoformat(due_date_raw)
            except ValueError:
                return _error_result(cleaned_prompt, "Task due date is invalid.", matter=matter)
        assigned_to_user_id = payload.get("assigned_to_user_id")
        try:
            assigned_to_user_id = int(assigned_to_user_id) if assigned_to_user_id is not None else None
        except (TypeError, ValueError):
            assigned_to_user_id = None
        assignee = db.session.get(User, assigned_to_user_id) if assigned_to_user_id else None
        task = Task(
            matter_id=matter.id,
            title=title,
            description=description,
            due_date=due_date,
            assigned_to=int(assignee.id) if assignee is not None else None,
            created_by=current_user.id,
            priority=(str(payload.get("priority") or "Medium").strip() or "Medium")[:20],
        )
        db.session.add(task)
        db.session.flush()
        if assignee is not None:
            db.session.add(TaskAssignee(task_id=task.id, user_id=assignee.id, assigned_by=current_user.id))
        matter.last_updated_at = utc_now()
        db.session.commit()
        _mark_confirmation_consumed(confirm_token)
        audit(
            "task_create",
            "Task",
            task.id,
            {"matter_id": matter.id, "assignee_count": 1 if assignee is not None else 0, "source": "assistant"},
        )
        matter_activity(
            matter.id,
            f"Task created: {task.title}",
            "Created via assistant" if not due_date else f"Created via assistant. Due {due_date.isoformat()}",
        )
        return _result(
            status="ok",
            kind="task_created",
            headline="Task Created",
            summary=f"Created task '{task.title}' on {assistant_matter_label(matter)}.",
            prompt=cleaned_prompt,
            matter=matter,
            fields=[
                {"label": "Task", "value": task.title},
                {"label": "Due Date", "value": task.due_date.isoformat() if task.due_date else "No due date"},
                {"label": "Assignee", "value": assignee.full_name if assignee is not None else "Unassigned"},
            ],
            links=[{"label": "Open Matter Tasks", "href": url_for("matter_tasks", matter_id=matter.id)}],
        )

    if action == "update_task_status":
        task_id = int(payload.get("task_id") or 0)
        task = db.session.get(Task, task_id)
        if task is None or int(task.matter_id) != int(matter.id):
            return _error_result(cleaned_prompt, "The target task is no longer available.", matter=matter)
        status = normalize_query(str(payload.get("status") or "")) or ""
        if status not in _TASK_STATUSES:
            return _error_result(cleaned_prompt, "Task status is invalid.", matter=matter)
        previous_status = task.status or "Todo"
        if previous_status == status:
            _mark_confirmation_consumed(confirm_token)
            return _result(
                status="ok",
                kind="task_status_noop",
                headline="Task Already In Requested Status",
                summary=f"{task.title or f'Task #{task.id}'} is already {status} on {assistant_matter_label(matter)}.",
                prompt=cleaned_prompt,
                matter=matter,
                fields=[
                    {"label": "Task", "value": task.title or f"Task #{task.id}"},
                    {"label": "Current Status", "value": previous_status},
                ],
                links=[{"label": "Open Matter Tasks", "href": url_for("matter_tasks", matter_id=matter.id)}],
            )
        task.status = status
        db.session.commit()
        _mark_confirmation_consumed(confirm_token)
        audit("task_status", "Task", task.id, {"status": status, "matter_id": task.matter_id, "source": "assistant"})
        matter_activity(task.matter_id, f"Task status changed: {task.title}", f"Now {status}")
        return _result(
            status="ok",
            kind="task_status_updated",
            headline="Task Status Updated",
            summary=f"Updated task status on {assistant_matter_label(matter)}.",
            prompt=cleaned_prompt,
            matter=matter,
            fields=[
                {"label": "Task", "value": task.title or f"Task #{task.id}"},
                {"label": "Previous Status", "value": previous_status},
                {"label": "Current Status", "value": task.status or status},
            ],
            links=[{"label": "Open Matter Tasks", "href": url_for("matter_tasks", matter_id=matter.id)}],
        )

    if action == "create_time_entry":
        if (matter.status or "").strip().lower() == "closed":
            return _blocked_result(
                cleaned_prompt,
                "Closed matters cannot accept new time entries. Reopen the matter first.",
                matter=matter,
            )
        start_raw = str(payload.get("start_at") or "").strip()
        end_raw = str(payload.get("end_at") or "").strip()
        if not start_raw or not end_raw:
            return _error_result(cleaned_prompt, "Time entry timestamps are missing from the confirmation payload.", matter=matter)
        try:
            start_at = dt.datetime.fromisoformat(start_raw)
            end_at = dt.datetime.fromisoformat(end_raw)
        except ValueError:
            return _error_result(cleaned_prompt, "Time entry timestamps are invalid.", matter=matter)
        if end_at <= start_at:
            return _error_result(cleaned_prompt, "Time entry end time must be after start time.", matter=matter)
        task_id_raw = payload.get("task_id")
        task_id = int(task_id_raw) if task_id_raw is not None and str(task_id_raw).isdigit() else None
        task = db.session.get(Task, task_id) if task_id else None
        if task is not None and int(task.matter_id) != int(matter.id):
            return _error_result(cleaned_prompt, "Linked task does not belong to the target matter.", matter=matter)
        policy = _assistant_policy_for_matter(matter.id)
        hours = round(max(0.0, (end_at - start_at).total_seconds() / 3600.0), 4)
        rounded_hours = _assistant_round_hours(hours, float(policy.increment_hours if policy else 0.1))
        duplicate = _existing_time_entry_duplicate(
            user_id=int(current_user.id),
            matter_id=int(matter.id),
            start_at=start_at,
            end_at=end_at,
            narrative=(str(payload.get("narrative") or "").strip()[:2000] or None),
        )
        if duplicate is not None:
            return _error_result(
                cleaned_prompt,
                f"A matching time entry already exists on this matter as entry #{duplicate.id}.",
                matter=matter,
            )
        entry = TimeEntry(
            user_id=current_user.id,
            matter_id=matter.id,
            task_id=task.id if task is not None else None,
            start_at=start_at,
            end_at=end_at,
            hours=hours,
            rounded_hours=rounded_hours,
            narrative=str(payload.get("narrative") or "").strip()[:2000] or None,
            is_billable=bool(payload.get("is_billable")),
            status="draft",
        )
        db.session.add(entry)
        db.session.flush()
        issues = _assistant_validate_time_entry(entry, policy)
        for issue in issues:
            db.session.add(TimeValidationEvent(time_entry_id=entry.id, event_type="validation", message=issue))
        if issues:
            entry.status = "needs_review"
        db.session.commit()
        _mark_confirmation_consumed(confirm_token)
        audit(
            "time_entry_create",
            "TimeEntry",
            entry.id,
            {"issues": issues, "matter_id": matter.id, "source": "assistant"},
        )
        matter_activity(
            matter.id,
            "Time entry added",
            f"Created via assistant ({entry.rounded_hours:.2f}h)" if entry.rounded_hours else "Created via assistant",
        )
        warning_list = [f"Validation: {issue}" for issue in issues]
        return _result(
            status="ok",
            kind="time_entry_created",
            headline="Time Entry Added",
            summary=f"Added a draft time entry on {assistant_matter_label(matter)}.",
            prompt=cleaned_prompt,
            matter=matter,
            warnings=warning_list,
            fields=[
                {"label": "Narrative", "value": entry.narrative or "Time entry"},
                {"label": "Start", "value": entry.start_at.isoformat(timespec="minutes")},
                {"label": "End", "value": entry.end_at.isoformat(timespec="minutes") if entry.end_at else ""},
                {"label": "Rounded Hours", "value": f"{float(entry.rounded_hours or 0.0):.2f}"},
                {"label": "Status", "value": entry.status or "draft"},
            ],
            links=[{"label": "Open Time Entries", "href": url_for("time_entries", matter_id=matter.id)}],
        )

    if action == "create_timeline_event":
        title = normalize_query(str(payload.get("title") or ""))[:180]
        if not title:
            return _error_result(cleaned_prompt, "Timeline title is missing from the confirmation payload.", matter=matter)
        event_type = normalize_query(str(payload.get("event_type") or "Milestone")) or "Milestone"
        if event_type not in _TIMELINE_EVENT_TYPES:
            return _error_result(cleaned_prompt, "Timeline event type is invalid.", matter=matter)
        event_date_raw = str(payload.get("event_date") or "").strip()
        if not event_date_raw:
            return _error_result(cleaned_prompt, "Timeline event date is missing.", matter=matter)
        try:
            event_date = dt.date.fromisoformat(event_date_raw)
        except ValueError:
            return _error_result(cleaned_prompt, "Timeline event date is invalid.", matter=matter)
        event = MatterTimelineEvent(
            matter_id=matter.id,
            event_date=event_date,
            event_type=event_type,
            title=title,
            description=(str(payload.get("description") or "").strip()[:2000] or None),
            is_milestone=bool(payload.get("is_milestone")),
            created_by=current_user.id,
        )
        matter.last_updated_at = utc_now()
        db.session.add(event)
        db.session.commit()
        _mark_confirmation_consumed(confirm_token)
        audit(
            "matter_timeline_add",
            "MatterTimelineEvent",
            event.id,
            {
                "matter_id": matter.id,
                "event_type": event.event_type,
                "event_date": str(event.event_date),
                "source": "assistant",
            },
        )
        matter_activity(matter.id, f"Timeline event added: {event.title}", event.event_type)
        return _result(
            status="ok",
            kind="timeline_event_created",
            headline="Timeline Event Added",
            summary=f"Added a timeline event on {assistant_matter_label(matter)}.",
            prompt=cleaned_prompt,
            matter=matter,
            fields=[
                {"label": "Title", "value": event.title},
                {"label": "Event Date", "value": event.event_date.isoformat()},
                {"label": "Event Type", "value": event.event_type},
                {"label": "Milestone", "value": "Yes" if event.is_milestone else "No"},
            ],
            text_blocks=[{"title": "Description", "body": event.description or "No extra description was recorded."}],
            links=[{"label": "Open Matter", "href": url_for("matter_detail", matter_id=matter.id)}],
        )

    if action == "add_note":
        body = str(payload.get("body") or "").strip()[:4000]
        if not body:
            return _error_result(cleaned_prompt, "Note body is missing from the confirmation payload.", matter=matter)
        note = MatterNote(
            matter_id=matter.id,
            body=body,
            tags=(str(payload.get("tags") or "").strip()[:255] or None),
            privilege_label=(str(payload.get("privilege_label") or "").strip()[:120] or None),
            created_by=current_user.id,
        )
        db.session.add(note)
        matter.last_updated_at = utc_now()
        db.session.commit()
        _mark_confirmation_consumed(confirm_token)
        audit(
            "matter_note_create",
            "MatterNote",
            note.id,
            {"matter_id": matter.id, "source": "assistant"},
        )
        matter_activity(matter.id, "Matter note added", "Created via assistant")
        return _result(
            status="ok",
            kind="note_created",
            headline="Matter Note Added",
            summary=f"Added a matter note on {assistant_matter_label(matter)}.",
            prompt=cleaned_prompt,
            matter=matter,
            fields=[
                {"label": "Tags", "value": note.tags or "None"},
                {"label": "Privilege", "value": note.privilege_label or "Standard note"},
            ],
            text_blocks=[{"title": "Note Body", "body": note.body or ""}],
            links=[{"label": "Open Matter Notes", "href": url_for("matter_notes", matter_id=matter.id)}],
        )

    return _error_result(cleaned_prompt, "The requested assistant action is unsupported.", matter=matter)
