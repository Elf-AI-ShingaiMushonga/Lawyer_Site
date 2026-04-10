from __future__ import annotations

import datetime as dt
import hashlib
import re
from typing import Any

from flask import current_app, session, url_for
from flask_login import current_user
from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy import or_
from sqlalchemy.orm import aliased

from ..config import BUDGET_STATUSES, RISK_LEVELS
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
    Contact,
    Deadline,
    DocumentFile,
    Entity,
    EntityRelationship,
    KnowledgeBase,
    Matter,
    MatterActivity,
    MatterNote,
    MatterParty,
    MatterStageHistory,
    MatterTimelineEvent,
    MatterWorkspaceDocument,
    PortalMessage,
    PortalMessageThread,
    Task,
    TaskAssignee,
    TimeEntry,
    TimeRoundingPolicy,
    TimeValidationEvent,
    User,
)
from ..policies import has_permission, visible_matter_ids
from ..roles import role_is_case
from .dms_option_lists import DEFAULT_DMS_OPTION_LISTS, load_dms_option_lists
from ..timeutils import utc_now
from .assistant_agent import assistant_agent_meta, plan_assistant_request
from .assist_ai import (
    suggest_matter_case_strategy,
    suggest_matter_client_update,
    suggest_matter_executive_summary,
    suggest_matter_research_memo,
)
from .semantic_search import SemanticSearchService

_MATTER_NO_RE = re.compile(r"\b\d{4}-[A-Z]{2,8}-\d{2,8}\b", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\s\-()]?){8,}\d\b")
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
_WORKUP_PREFIX_RE = re.compile(
    r"^(?:please\s+)?(?:(?:build|prepare|draft|create|construct)\s+(?:a\s+)?)?(?:case workup|case dossier|case build|litigation plan|hearing plan|trial plan|issue map|war room)(?:\s+for(?:\s+this|\s+the)?\s+matter)?(?:\s+focused on|\s+on|\s+about)?\s*",
    re.IGNORECASE,
)
_STRATEGY_PREFIX_RE = re.compile(
    r"^(?:please\s+)?(?:(?:build|prepare|draft|create|construct)\s+(?:a\s+)?)?(?:case strategy|strategy memo|case theory)(?:\s+memo)?(?:\s+for(?:\s+this|\s+the)?\s+matter)?(?:\s+focused on|\s+on|\s+about)?\s*",
    re.IGNORECASE,
)
_RESEARCH_PREFIX_RE = re.compile(
    r"^(?:please\s+)?(?:(?:prepare|draft|create)\s+(?:a\s+)?)?(?:research memo|legal research|research(?:\s+the)?|file analysis|analy[sz]e(?:\s+the)?\s+file|authority review)(?:\s+for(?:\s+this|\s+the)?\s+matter)?(?:\s+on|\s+about)?\s*",
    re.IGNORECASE,
)
_CHRONOLOGY_PREFIX_RE = re.compile(
    r"^(?:please\s+)?(?:(?:create|prepare|build|draft)\s+(?:a\s+)?)?(?:chronology|procedural history|sequence of events|timeline summary|chronological summary)(?:\s+of(?:\s+this|\s+the)?\s+matter)?(?:\s+focused on|\s+on|\s+about)?\s*",
    re.IGNORECASE,
)
_DEADLINE_PREFIX_RE = re.compile(
    r"^(?:please\s+)?(?:add|create|set|schedule|record)\s+(?:a\s+)?(?:(?:critical|urgent|hard)\s+)?deadline(?:\s+for|\s+to|\s+on)?\s*",
    re.IGNORECASE,
)
_WORKSPACE_DOCUMENT_PREFIX_RE = re.compile(
    r"^(?:please\s+)?(?:create|save|draft|prepare|open|make)\s+(?:a\s+)?(?:(?:collaborative|workspace|workbench)\s+)?(?:document|draft|memo|brief|outline|note)(?:\s+(?:called|titled))?\s*",
    re.IGNORECASE,
)
_PARTY_PREFIX_RE = re.compile(
    r"^(?:please\s+)?(?:add|link|create)\s+(?:a\s+)?(?:matter\s+)?party(?:\s+for|\s+to)?\s*",
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
_WORKUP_INTENT_RE = re.compile(
    r"\b(?:case workup|case dossier|case build|litigation plan|hearing plan|trial plan|issue map|war room|construct (?:the )?case(?:\s+for)?|build (?:the )?case(?:\s+for)?)\b",
    re.IGNORECASE,
)
_STRATEGY_INTENT_RE = re.compile(
    r"\b(?:case strategy|strategy memo|case theory|hearing prep|trial prep|build (?:the )?case|construct (?:the )?case|opposition strategy|defence strategy|defense strategy)\b",
    re.IGNORECASE,
)
_RESEARCH_INTENT_RE = re.compile(
    r"\b(?:research memo|legal research|research this|research the|file analysis|analyze the file|analy[sz]e the file|knowledge base|precedent|authority review)\b",
    re.IGNORECASE,
)
_CHRONOLOGY_INTENT_RE = re.compile(
    r"\b(?:chronology|procedural history|sequence of events|timeline summary|chronological summary)\b",
    re.IGNORECASE,
)
_BRIEFING_INTENT_RE = re.compile(
    r"\b(?:what(?:'s| is)\s+next|next steps|next deadlines?|upcoming deadlines?|upcoming dates|upcoming timeline|deadline snapshot|matter briefing|brief me on (?:this|the) matter|where do things stand)\b",
    re.IGNORECASE,
)
_MATTER_SUMMARY_UPDATE_INTENT_RE = re.compile(
    r"\b(?:update|revise|refresh|set)\b.*\b(?:matter summary|objective|risk|budget|outcome|latest update|last update)\b",
    re.IGNORECASE,
)
_WORKSPACE_DOCUMENT_INTENT_RE = re.compile(
    r"\b(?:create|save|draft|prepare|open|make)\b.*\b(?:workbench|workspace|collaborative)\b.*\b(?:document|draft|memo|brief|outline|note)\b|\b(?:save|store)\b.*\b(?:in|to)\b.*\b(?:workbench|workspace)\b",
    re.IGNORECASE,
)
_TASK_INTENT_RE = re.compile(
    r"\b(?:create|add|open|make)\s+(?:a\s+)?task\b|\bremind me to\b|\btodo\b",
    re.IGNORECASE,
)
_DEADLINE_INTENT_RE = re.compile(
    r"\b(?:add|create|set|schedule|record)\b.*\bdeadline\b",
    re.IGNORECASE,
)
_PARTY_INTENT_RE = re.compile(
    r"\b(?:add|link|create)\b.*\bparty\b|\badd\b.*\bas\b.*\b(?:client|claimant|plaintiff|defendant|respondent|applicant|witness|expert|counsel)\b",
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
_ASSISTANT_SUMMARY_STATUSES = {"Open", "On Hold"}
_WORKSPACE_DOCUMENT_STATUSES = {"draft", "review", "final"}
_PLANNER_TOOL_TO_INTENT = {
    "matter_briefing": "matter_briefing",
    "matter_case_workup": "matter_case_workup",
    "matter_strategy": "matter_strategy",
    "matter_research": "matter_research",
    "matter_chronology": "matter_chronology",
    "draft_summary": "draft_summary",
    "draft_client_update": "draft_client_update",
    "search_workspace": "search",
    "prepare_workspace_document": "create_workspace_document",
    "prepare_matter_summary_update": "update_matter_summary",
    "prepare_task": "create_task",
    "prepare_task_status_update": "update_task_status",
    "prepare_deadline": "create_deadline",
    "prepare_note": "add_note",
    "prepare_party": "add_party",
    "prepare_timeline_event": "create_timeline_event",
    "prepare_time_entry": "create_time_entry",
}

_EXAMPLES = [
    "What are the next deadlines on this matter?",
    "Construct the case for this matter and give me a full workup.",
    "Build a case strategy memo for this matter focused on hearing prep.",
    "Create a chronology of this matter focused on the filing history.",
    "Research the arbitration strategy issues in this file.",
    "Create a collaborative draft called Hearing Prep Strategy in the matter workbench.",
    "Summarize this matter for partner review.",
    "Draft a client update for this matter in plain English.",
    "Update the matter summary: risk High, budget Watch, latest update: witness interviews are complete.",
    "Create a task to file the affidavit by tomorrow.",
    "Create a critical deadline to serve the notice by 2026-05-09.",
    "Add party John Smith as Witness john.smith@example.com.",
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
    planning: dict[str, str] | None = None,
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
        "planning": planning or {},
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


def _draft_fallback_warning(label: str, suggestion: dict[str, Any]) -> str:
    detail = normalize_query(str(suggestion.get("fallback_detail") or "")).strip()
    reason = normalize_query(str(suggestion.get("fallback_reason") or "")).strip()
    if detail:
        return f"{label} used the non-AI fallback: {detail}"
    if reason:
        return f"{label} used the non-AI fallback because {reason.replace('_', ' ')}."
    return f"{label} used the non-AI fallback."


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
    if _WORKSPACE_DOCUMENT_INTENT_RE.search(prompt or ""):
        return "create_workspace_document"
    if _WORKUP_INTENT_RE.search(prompt or ""):
        return "matter_case_workup"
    if _STRATEGY_INTENT_RE.search(prompt or ""):
        return "matter_strategy"
    if _RESEARCH_INTENT_RE.search(prompt or ""):
        return "matter_research"
    if _CHRONOLOGY_INTENT_RE.search(prompt or ""):
        return "matter_chronology"
    if _CLIENT_UPDATE_INTENT_RE.search(prompt or ""):
        return "draft_client_update"
    if _SUMMARY_INTENT_RE.search(prompt or ""):
        return "draft_summary"
    if _MATTER_SUMMARY_UPDATE_INTENT_RE.search(prompt or ""):
        return "update_matter_summary"
    if _TIME_ENTRY_INTENT_RE.search(prompt or ""):
        return "create_time_entry"
    if _DEADLINE_INTENT_RE.search(prompt or ""):
        return "create_deadline"
    if _TIMELINE_INTENT_RE.search(prompt or ""):
        return "create_timeline_event"
    if _TASK_STATUS_INTENT_RE.search(prompt or ""):
        return "update_task_status"
    if _PARTY_INTENT_RE.search(prompt or ""):
        return "add_party"
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


def _strip_workup_prompt(prompt: str) -> str:
    stripped = _WORKUP_PREFIX_RE.sub("", prompt or "").strip()
    return normalize_query(stripped).strip(" .,:;-") or normalize_query(str(prompt or "")).strip()


def _strip_strategy_prompt(prompt: str) -> str:
    stripped = _STRATEGY_PREFIX_RE.sub("", prompt or "").strip()
    return normalize_query(stripped).strip(" .,:;-") or normalize_query(str(prompt or "")).strip()


def _strip_research_prompt(prompt: str) -> str:
    stripped = _RESEARCH_PREFIX_RE.sub("", prompt or "").strip()
    stripped = re.sub(r"\b(?:in|from)\s+this\s+file\b", "", stripped, flags=re.IGNORECASE)
    return normalize_query(stripped).strip(" .,:;-") or normalize_query(str(prompt or "")).strip()


def _strip_chronology_prompt(prompt: str) -> str:
    stripped = _CHRONOLOGY_PREFIX_RE.sub("", prompt or "").strip()
    return normalize_query(stripped).strip(" .,:;-") or normalize_query(str(prompt or "")).strip()


def _strip_deadline_prompt(prompt: str) -> str:
    stripped = _DEADLINE_PREFIX_RE.sub("", prompt or "").strip()
    return stripped or str(prompt or "").strip()


def _strip_workspace_document_prompt(prompt: str) -> str:
    stripped = _WORKSPACE_DOCUMENT_PREFIX_RE.sub("", prompt or "").strip()
    stripped = re.sub(
        r"\b(?:in|to)\s+(?:the\s+)?(?:matter\s+)?(?:workbench|workspace)(?:\s+(?:document|draft))?\b",
        "",
        stripped,
        flags=re.IGNORECASE,
    )
    return normalize_query(stripped).strip(" .,:;-") or normalize_query(str(prompt or "")).strip()


def _strip_party_prompt(prompt: str) -> str:
    stripped = _PARTY_PREFIX_RE.sub("", prompt or "").strip()
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


def _choice_from_prompt(prompt: str, options: list[str] | tuple[str, ...]) -> str | None:
    lowered = f" {normalize_query(prompt or '').lower()} "
    normalized_options = sorted(((str(item), str(item).lower()) for item in options), key=lambda row: len(row[1]), reverse=True)
    for original, lowered_option in normalized_options:
        if f" {lowered_option} " in lowered:
            return original
    return None


def _extract_labeled_segment(prompt: str, labels: list[str], stop_labels: list[str]) -> str:
    label_pattern = "|".join(re.escape(label) for label in labels if label)
    stop_pattern = "|".join(re.escape(label) for label in stop_labels if label)
    if not label_pattern:
        return ""
    pattern = rf"(?:{label_pattern})\s*(?:\:|to)\s*(.+?)(?=(?:\b(?:{stop_pattern})\b\s*(?:\:|to))|$)"
    match = re.search(pattern, prompt or "", flags=re.IGNORECASE)
    if not match:
        return ""
    return normalize_query(match.group(1)).strip(" .,:;-")


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


def _parse_iso_date_arg(value: Any) -> dt.date | None:
    token = normalize_query(str(value or "")).strip()
    if not token:
        return None
    try:
        return dt.date.fromisoformat(token)
    except ValueError:
        return None


def _parse_iso_datetime_arg(value: Any) -> dt.datetime | None:
    token = normalize_query(str(value or "")).strip()
    if not token:
        return None
    if token.endswith("Z"):
        token = f"{token[:-1]}+00:00"
    try:
        parsed = dt.datetime.fromisoformat(token)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.replace(tzinfo=None)
    return parsed


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


def _extract_deadline_critical_flag(prompt: str) -> bool:
    lowered = str(prompt or "").lower()
    return any(token in lowered for token in ("critical deadline", "urgent deadline", "critical", "urgent", "hard deadline"))


def _deadline_title(prompt: str) -> str:
    body = _strip_deadline_prompt(prompt)
    working = body
    working = re.sub(
        r"^(?:please\s+)?(?:add|create|set|schedule|record)\s+(?:a\s+)?(?:(?:critical|urgent|hard)\s+)?deadline(?:\s+for|\s+to|\s+on)?\s*",
        "",
        working,
        flags=re.IGNORECASE,
    )
    working = re.sub(r"\b(?:by|on)\s+\d{4}-\d{2}-\d{2}\b", "", working, flags=re.IGNORECASE)
    working = re.sub(r"\b(?:by|on)\s+\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", "", working, flags=re.IGNORECASE)
    working = re.sub(r"\b(?:by|on)\s+tomorrow\b", "", working, flags=re.IGNORECASE)
    working = re.sub(r"\b(?:by|on)\s+today\b", "", working, flags=re.IGNORECASE)
    working = re.sub(r"\bcritical\b|\burgent\b|\bhard deadline\b", "", working, flags=re.IGNORECASE)
    working = re.sub(r"^(?:to|for|about)\s+", "", working, flags=re.IGNORECASE)
    working = normalize_query(working).strip(" .,:;-")
    return working[:255]


def _extract_summary_update_values(prompt: str) -> dict[str, str]:
    def _strip_summary_tail(value: str) -> str:
        cleaned = normalize_query(value).strip(" .,:;-")
        if not cleaned:
            return ""
        cleaned = re.sub(
            r"(.*?)(?:\s+\b(?:risk|budget)\b\s+[A-Za-z].*|\s+\bon hold\b.*|\s+\bstatus\b\s+[A-Za-z].*)$",
            r"\1",
            cleaned,
            flags=re.IGNORECASE,
        )
        return normalize_query(cleaned).strip(" .,:;-")

    stop_labels = ["objective", "risk", "budget", "last update", "latest update", "outcome", "status"]
    values = {
        "objective": _strip_summary_tail(_extract_labeled_segment(prompt, ["objective"], stop_labels)),
        "last_update_note": _strip_summary_tail(
            _extract_labeled_segment(prompt, ["last update", "latest update", "update note"], stop_labels)
        ),
        "outcome_summary": _strip_summary_tail(_extract_labeled_segment(prompt, ["outcome", "outcome summary"], stop_labels)),
    }
    risk_level = _choice_from_prompt(prompt, list(RISK_LEVELS))
    budget_status = _choice_from_prompt(prompt, list(BUDGET_STATUSES))
    status = None
    lowered = normalize_query(prompt or "").lower()
    if re.search(r"\bon hold\b", lowered) or re.search(r"\bhold the matter\b", lowered):
        status = "On Hold"
    elif any(
        re.search(pattern, lowered)
        for pattern in (r"\breopen\b", r"\breopen matter\b", r"\bopen matter\b", r"\bstatus open\b")
    ):
        status = "Open"
    if risk_level:
        values["risk_level"] = risk_level
    if budget_status:
        values["budget_status"] = budget_status
    if status:
        values["status"] = status
    return {key: value for key, value in values.items() if normalize_query(str(value or "")).strip()}


def _extract_party_details(prompt: str) -> dict[str, Any]:
    body = _strip_party_prompt(prompt)
    email_match = _EMAIL_RE.search(body)
    phone_match = _PHONE_RE.search(body)
    email = str(email_match.group(0)).strip().lower() if email_match else ""
    phone = normalize_query(phone_match.group(0) if phone_match else "").strip()
    working = body
    if email:
        working = working.replace(email, " ")
    if phone:
        working = working.replace(phone, " ")
    role_match = re.search(r"\bas\s+([a-z][a-z /-]{2,80})", working, flags=re.IGNORECASE)
    party_role = normalize_query(role_match.group(1) if role_match else "").strip(" .,:;-").title() if role_match else ""
    if role_match:
        working = working[: role_match.start()]
    working = re.sub(r"\bprimary\b|\blead\b", "", working, flags=re.IGNORECASE)
    entity_name = normalize_query(working).strip(" .,:;-")
    entity_type = "organization" if re.search(r"\b(?:inc|ltd|llc|corp|company|group|holdings|pty)\b", entity_name, flags=re.IGNORECASE) else "person"
    return {
        "entity_name": entity_name[:255],
        "party_role": party_role[:80],
        "entity_type": entity_type,
        "email": email[:255],
        "phone": phone[:80],
        "is_primary": bool(re.search(r"\bprimary\b|\blead\b", body, flags=re.IGNORECASE)),
    }


def _assistant_dms_option_lists() -> dict[str, list[str]]:
    try:
        loaded = load_dms_option_lists()
    except Exception:  # pragma: no cover - defensive fallback
        db.session.rollback()
        current_app.logger.exception("Failed to load DMS option lists for assistant drafting.")
        loaded = {}
    normalized: dict[str, list[str]] = {}
    for key, defaults in DEFAULT_DMS_OPTION_LISTS.items():
        values = [str(item).strip() for item in list((loaded or {}).get(key) or []) if str(item).strip()]
        normalized[key] = values if values else list(defaults)
    return normalized


def _assistant_option_match(value: str | None, options: list[str]) -> str | None:
    cleaned = normalize_query(str(value or "")).strip()
    if not cleaned:
        return None
    lookup = {str(option).strip().casefold(): str(option).strip() for option in options if str(option).strip()}
    return lookup.get(cleaned.casefold())


def _workspace_document_goal(prompt: str) -> str:
    working = _strip_workspace_document_prompt(prompt)
    working = re.sub(r"^(?:called|titled)\s+", "", working, flags=re.IGNORECASE)
    return normalize_query(working).strip(" .,:;-")


def _workspace_document_title(prompt: str, matter: Matter, *, document_goal: str = "") -> str:
    titled_match = re.search(
        r"\b(?:called|titled)\s+(.+?)(?=(?:\s+\b(?:in|to)\b\s+(?:the\s+)?(?:matter\s+)?(?:workbench|workspace)\b)|$)",
        prompt or "",
        flags=re.IGNORECASE,
    )
    if titled_match:
        return normalize_query(titled_match.group(1)).strip(" .,:;-")[:255]
    goal = normalize_query(document_goal or _workspace_document_goal(prompt)).strip(" .,:;-")
    if not goal:
        return f"Collaborative Draft - {matter.matter_no}"[:255]
    lowered = goal.lower()
    if any(
        token in lowered
        for token in ("case workup", "case dossier", "construct the case", "build the case", "litigation plan", "war room")
    ):
        return f"Case Workup - {matter.matter_no}"[:255]
    return goal[:255]


def _bullet_lines(items: list[str]) -> str:
    lines = [f"- {normalize_query(str(item or '')).strip()}" for item in items if normalize_query(str(item or "")).strip()]
    return "\n".join(lines)


def _build_case_workup_packet(matter: Matter, *, focus_hint: str = "") -> dict[str, Any]:
    focus = normalize_query(focus_hint).strip()
    research_query = focus or f"Case construction, procedural posture, evidence gaps, and client communications for {matter.matter_no}"
    context = _matter_analysis_context(matter, research_query=research_query)
    strategy = suggest_matter_case_strategy(matter_context=context, focus_hint=focus)
    memo = suggest_matter_research_memo(matter_context=context, research_query=research_query)
    chronology = _matter_chronology_entries(matter, focus_hint=focus)[:10]
    warnings: list[str] = [
        "Case workup is grounded in the current matter file and should still be reviewed by counsel.",
    ]
    if str(strategy.get("source") or "").strip().lower() != "openai":
        warnings.append(_draft_fallback_warning("Case strategy", strategy))
    if str(memo.get("source") or "").strip().lower() != "openai":
        warnings.append(_draft_fallback_warning("Research memo", memo))
    return {
        "focus": focus,
        "research_query": research_query,
        "context": context,
        "strategy": strategy,
        "memo": memo,
        "chronology": chronology,
        "warnings": warnings,
    }


def _render_case_workup_document_body(title: str, matter: Matter, packet: dict[str, Any]) -> str:
    context = packet.get("context") or {}
    strategy = packet.get("strategy") or {}
    memo = packet.get("memo") or {}
    chronology = list(packet.get("chronology") or [])[:8]
    parties = list(context.get("parties") or [])[:8]
    deadlines = list(context.get("upcoming_deadlines") or [])[:8]
    relationships = list(context.get("entity_relationships") or [])[:8]
    workspace_documents = list(context.get("workspace_documents") or [])[:6]
    portal_messages = list(context.get("portal_messages") or [])[:6]

    sections = [
        title,
        f"Matter: {assistant_matter_label(matter)}",
        "",
        "Objective",
        normalize_query(str(context.get("objective") or "")).strip() or "No objective recorded.",
        "",
        "Case Theory",
        normalize_query(str(strategy.get("case_theory") or "")).strip() or "No case theory was generated.",
        "",
        "Recommended Actions",
        _bullet_lines(list(strategy.get("recommended_actions") or [])) or "- Review the matter file and convert this plan into concrete workstreams.",
        "",
        "Strengths",
        _bullet_lines(list(strategy.get("strengths") or [])) or "- None captured.",
        "",
        "Risks",
        _bullet_lines(list(strategy.get("risks") or [])) or "- None captured.",
        "",
        "Evidence Gaps",
        _bullet_lines(list(strategy.get("evidence_gaps") or [])) or "- None captured.",
        "",
        "Research Position",
        normalize_query(str(memo.get("answer") or "")).strip() or "No research position was generated.",
        "",
        "Supporting Sources",
        _bullet_lines(list(memo.get("sources") or [])) or "- None captured.",
        "",
        "Key Parties",
        _bullet_lines(
            [
                " • ".join(
                    item
                    for item in [
                        str(row.get("name") or "Party"),
                        str(row.get("role") or ""),
                        "Primary" if row.get("is_primary") else "",
                    ]
                    if item
                )
                for row in parties
            ]
        )
        or "- None captured.",
        "",
        "Relationships",
        _bullet_lines(
            [
                " -> ".join(
                    item
                    for item in [
                        str(row.get("from") or ""),
                        str(row.get("relationship_type") or ""),
                        str(row.get("to") or ""),
                    ]
                    if item
                )
                for row in relationships
            ]
        )
        or "- None captured.",
        "",
        "Upcoming Deadlines",
        _bullet_lines(
            [
                " • ".join(
                    item
                    for item in [
                        str(row.get("title") or "Deadline"),
                        str(row.get("due_at") or ""),
                        "Critical" if row.get("is_critical") else "",
                    ]
                    if item
                )
                for row in deadlines
            ]
        )
        or "- None captured.",
        "",
        "Collaborative Drafts",
        _bullet_lines(
            [
                " • ".join(
                    item
                    for item in [
                        str(row.get("title") or "Draft"),
                        str(row.get("status") or ""),
                        str(row.get("document_type") or ""),
                    ]
                    if item
                )
                for row in workspace_documents
            ]
        )
        or "- None captured.",
        "",
        "Client Communications",
        _bullet_lines(
            [
                " • ".join(
                    item
                    for item in [
                        str(row.get("subject") or "Thread"),
                        str(row.get("excerpt") or ""),
                    ]
                    if item
                )
                for row in portal_messages
            ]
        )
        or "- None captured.",
        "",
        "Chronology",
        _bullet_lines(
            [
                " • ".join(
                    item
                    for item in [
                        str(row.get("date") or ""),
                        str(row.get("title") or ""),
                        str(row.get("meta") or ""),
                    ]
                    if item
                )
                for row in chronology
            ]
        )
        or "- None captured.",
    ]
    return "\n".join(section for section in sections if section is not None).strip()[:12000]


def _workspace_document_body_from_goal(
    matter: Matter,
    *,
    title: str,
    document_goal: str,
) -> tuple[str, list[str]]:
    warnings: list[str] = []
    goal = normalize_query(document_goal).strip()
    lowered = goal.lower()
    if any(
        token in lowered
        for token in ("case workup", "case dossier", "construct the case", "build the case", "litigation plan", "war room")
    ):
        packet = _build_case_workup_packet(matter, focus_hint=_strip_workup_prompt(goal))
        warnings.extend(list(packet.get("warnings") or []))
        return _render_case_workup_document_body(title, matter, packet), warnings
    if any(token in lowered for token in ("research memo", "legal research", "research", "authority review")):
        research_query = _strip_research_prompt(goal) or goal
        context = _matter_analysis_context(matter, research_query=research_query)
        memo = suggest_matter_research_memo(matter_context=context, research_query=research_query)
        if str(memo.get("source") or "").strip().lower() != "openai":
            warnings.append(_draft_fallback_warning("Research memo", memo))
        body = "\n".join(
            [
                title,
                f"Matter: {assistant_matter_label(matter)}",
                "",
                "Research Question",
                str(memo.get("research_question") or research_query),
                "",
                "Answer",
                str(memo.get("answer") or ""),
                "",
                "Supporting Sources",
                _bullet_lines(list(memo.get("sources") or [])) or "- None captured.",
                "",
                "Recommended Next Steps",
                _bullet_lines(list(memo.get("next_steps") or [])) or "- None captured.",
            ]
        )
        return body[:12000], warnings
    if any(token in lowered for token in ("chronology", "procedural history", "timeline summary")):
        entries = _matter_chronology_entries(matter, focus_hint=_strip_chronology_prompt(goal))[:12]
        body = "\n".join(
            [
                title,
                f"Matter: {assistant_matter_label(matter)}",
                "",
                "Chronology",
                _bullet_lines(
                    [
                        " • ".join(
                            item
                            for item in [str(row.get("date") or ""), str(row.get("title") or ""), str(row.get("meta") or "")]
                            if item
                        )
                        for row in entries
                    ]
                )
                or "- None captured.",
            ]
        )
        return body[:12000], warnings
    if any(token in lowered for token in ("case strategy", "strategy memo", "hearing prep", "trial prep", "case theory")):
        focus_hint = _strip_strategy_prompt(goal) or goal
        context = _matter_analysis_context(matter, research_query=focus_hint)
        strategy = suggest_matter_case_strategy(matter_context=context, focus_hint=focus_hint)
        if str(strategy.get("source") or "").strip().lower() != "openai":
            warnings.append(_draft_fallback_warning("Case strategy", strategy))
        body = "\n".join(
            [
                title,
                f"Matter: {assistant_matter_label(matter)}",
                "",
                "Case Theory",
                str(strategy.get("case_theory") or ""),
                "",
                "Strengths",
                _bullet_lines(list(strategy.get("strengths") or [])) or "- None captured.",
                "",
                "Risks",
                _bullet_lines(list(strategy.get("risks") or [])) or "- None captured.",
                "",
                "Evidence Gaps",
                _bullet_lines(list(strategy.get("evidence_gaps") or [])) or "- None captured.",
                "",
                "Recommended Actions",
                _bullet_lines(list(strategy.get("recommended_actions") or [])) or "- None captured.",
            ]
        )
        return body[:12000], warnings
    body = "\n".join(
        [
            title,
            f"Matter: {assistant_matter_label(matter)}",
            "",
            "Objective",
            normalize_query(str(matter.objective or "")).strip() or "-",
            "",
            "Working Draft",
            goal or "Add draft content here.",
            "",
            "Notes",
            f"Status: {matter.status or 'Open'}",
            f"Stage: {matter.stage or 'Unspecified'}",
            f"Risk: {matter.risk_level or 'Medium'}",
            f"Budget: {matter.budget_status or 'On Track'}",
        ]
    )
    return body[:12000], warnings


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


def _tag_list_to_csv(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    tags: list[str] = []
    for item in value:
        token = normalize_query(str(item or "")).strip(" #,.;:").lower()
        if not token or token in tags:
            continue
        tags.append(token)
    return ", ".join(tags)[:255]


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


def _matter_analysis_context(matter: Matter, *, research_query: str = "") -> dict[str, Any]:
    context = dict(_matter_context(matter))
    can_view_dms = has_permission("dms", "read")
    parties = (
        db.session.query(MatterParty, Entity)
        .join(Entity, Entity.id == MatterParty.entity_id)
        .filter(MatterParty.matter_id == matter.id)
        .order_by(MatterParty.is_primary.desc(), MatterParty.id.desc())
        .limit(12)
        .all()
    )
    party_entity_ids = [int(party.entity_id) for party, _entity in parties if int(party.entity_id or 0) > 0]
    deadlines = (
        Deadline.query.filter(Deadline.matter_id == matter.id, Deadline.status == "open")
        .order_by(Deadline.due_at.asc(), Deadline.id.desc())
        .limit(12)
        .all()
    )
    context["parties"] = [
        {
            "name": entity.name or "",
            "role": party.party_role or "",
            "is_primary": bool(party.is_primary),
            "email": entity.email or "",
            "phone": entity.phone or "",
        }
        for party, entity in parties
    ]
    context["upcoming_deadlines"] = [
        {
            "title": row.title or "",
            "due_at": row.due_at.isoformat() if row.due_at else "",
            "is_critical": bool(row.is_critical),
            "status": row.status or "",
        }
        for row in deadlines
    ]
    context["stage_history"] = [
        {
            "from_stage": row.from_stage or "",
            "to_stage": row.to_stage or "",
            "reason": (row.reason or "")[:220],
            "changed_at": row.changed_at.isoformat() if row.changed_at else "",
        }
        for row in (
            MatterStageHistory.query.filter_by(matter_id=matter.id)
            .order_by(MatterStageHistory.changed_at.desc(), MatterStageHistory.id.desc())
            .limit(10)
            .all()
        )
    ]
    context["workspace_documents"] = (
        [
            {
                "title": row.title or "",
                "status": row.status or "",
                "document_type": row.document_type or "",
                "updated_at": row.updated_at.isoformat() if row.updated_at else "",
            }
            for row in (
                MatterWorkspaceDocument.query.filter_by(matter_id=matter.id)
                .order_by(MatterWorkspaceDocument.updated_at.desc(), MatterWorkspaceDocument.id.desc())
                .limit(8)
                .all()
            )
        ]
        if can_view_dms
        else []
    )
    if party_entity_ids:
        src_entity = aliased(Entity)
        dst_entity = aliased(Entity)
        relationship_rows = (
            db.session.query(EntityRelationship, src_entity, dst_entity)
            .join(src_entity, src_entity.id == EntityRelationship.src_entity_id)
            .join(dst_entity, dst_entity.id == EntityRelationship.dst_entity_id)
            .filter(
                EntityRelationship.src_entity_id.in_(party_entity_ids),
                EntityRelationship.dst_entity_id.in_(party_entity_ids),
            )
            .order_by(EntityRelationship.id.desc())
            .limit(12)
            .all()
        )
    else:
        relationship_rows = []
    context["entity_relationships"] = [
        {
            "from": src.name or "",
            "to": dst.name or "",
            "relationship_type": rel.relationship_type or "",
        }
        for rel, src, dst in relationship_rows
    ]
    portal_threads = (
        PortalMessageThread.query.filter_by(matter_id=matter.id)
        .order_by(PortalMessageThread.created_at.desc(), PortalMessageThread.id.desc())
        .limit(6)
        .all()
    )
    thread_map = {int(row.id): row for row in portal_threads}
    portal_messages = (
        PortalMessage.query.filter(PortalMessage.thread_id.in_(sorted(thread_map.keys())))
        .order_by(PortalMessage.created_at.desc(), PortalMessage.id.desc())
        .limit(10)
        .all()
        if thread_map
        else []
    )
    context["portal_messages"] = [
        {
            "subject": (thread_map.get(int(row.thread_id)).subject or "") if thread_map.get(int(row.thread_id)) else "",
            "excerpt": (row.body or "").strip().replace("\n", " ")[:220],
            "created_at": row.created_at.isoformat() if row.created_at else "",
        }
        for row in portal_messages
    ]

    knowledge_hits: list[dict[str, str]] = []
    semantic_hits: list[dict[str, Any]] = []
    research_query_clean = _strip_search_prompt(research_query)
    if len(research_query_clean) >= 3:
        like = f"%{research_query_clean}%"
        knowledge_hits = [
            {
                "title": row.title or "",
                "tags": row.tags or "",
                "body": (row.body or "")[:280],
            }
            for row in (
                KnowledgeBase.query.filter(
                    or_(
                        KnowledgeBase.title.ilike(like),
                        KnowledgeBase.tags.ilike(like),
                        KnowledgeBase.body.ilike(like),
                    )
                )
                .order_by(KnowledgeBase.updated_at.desc())
                .limit(6)
                .all()
            )
        ]
        if not knowledge_hits:
            token_hits = _search_tokens(research_query_clean)
            if token_hits:
                candidate_articles = KnowledgeBase.query.order_by(KnowledgeBase.updated_at.desc()).limit(40).all()
                scored_articles: list[tuple[int, KnowledgeBase]] = []
                min_token_hits = len(token_hits) if len(token_hits) <= 2 else max(2, (len(token_hits) + 1) // 2)
                for row in candidate_articles:
                    haystack = normalize_query(" ".join([row.title or "", row.tags or "", row.body or ""])).lower()
                    score = sum(1 for token in token_hits if token in haystack)
                    if score >= min_token_hits:
                        scored_articles.append((score, row))
                scored_articles.sort(key=lambda item: (item[0], item[1].updated_at or dt.datetime.min), reverse=True)
                knowledge_hits = [
                    {
                        "title": row.title or "",
                        "tags": row.tags or "",
                        "body": (row.body or "")[:280],
                    }
                    for _, row in scored_articles[:6]
                ]
        if bool(current_app.config.get("AI_SEMANTIC_SEARCH_ENABLED", False)):
            semantic_hits = SemanticSearchService.search(research_query_clean, matter_scope_ids={int(matter.id)}, limit=6)
    context["knowledge_hits"] = knowledge_hits
    context["semantic_hits"] = semantic_hits
    return context


def _matter_chronology_entries(matter: Matter, *, focus_hint: str = "") -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    focus_tokens = _search_tokens(focus_hint)

    def _include(*parts: str) -> bool:
        if not focus_tokens:
            return True
        haystack = normalize_query(" ".join(part for part in parts if part)).lower()
        return any(token in haystack for token in focus_tokens)

    timeline_rows = (
        MatterTimelineEvent.query.filter_by(matter_id=matter.id)
        .order_by(MatterTimelineEvent.event_date.desc(), MatterTimelineEvent.id.desc())
        .limit(20)
        .all()
    )
    for row in timeline_rows:
        if not _include(row.title or "", row.description or "", row.event_type or ""):
            continue
        entries.append(
            {
                "date": row.event_date.isoformat() if row.event_date else "",
                "title": row.title or f"{row.event_type or 'Timeline'} event",
                "meta": " • ".join(item for item in [row.event_type or "", "Milestone" if row.is_milestone else ""] if item),
                "href": url_for("matter_detail", matter_id=matter.id),
            }
        )

    deadline_rows = (
        Deadline.query.filter_by(matter_id=matter.id)
        .order_by(Deadline.due_at.desc(), Deadline.id.desc())
        .limit(20)
        .all()
    )
    for row in deadline_rows:
        if not _include(row.title or "", row.status or ""):
            continue
        entries.append(
            {
                "date": row.due_at.isoformat() if row.due_at else "",
                "title": row.title or "Deadline",
                "meta": " • ".join(item for item in ["Deadline", row.status or "", "Critical" if row.is_critical else ""] if item),
                "href": url_for("calendar_matter", matter_id=matter.id),
            }
        )

    activity_rows = (
        MatterActivity.query.filter_by(matter_id=matter.id)
        .order_by(MatterActivity.created_at.desc(), MatterActivity.id.desc())
        .limit(20)
        .all()
    )
    for row in activity_rows:
        if not _include(row.action or "", row.details or ""):
            continue
        entries.append(
            {
                "date": row.created_at.date().isoformat() if row.created_at else "",
                "title": row.action or "Activity",
                "meta": (row.details or "").strip()[:180],
                "href": url_for("matter_detail", matter_id=matter.id),
            }
        )

    note_rows = filter_accessible_matter_notes(
        MatterNote.query.filter_by(matter_id=matter.id)
        .order_by(MatterNote.updated_at.desc().nullslast(), MatterNote.id.desc())
        .limit(12)
        .all()
    )
    for row in note_rows:
        note_text = (row.body or "").strip().replace("\n", " ")
        if not _include(note_text, row.tags or "", row.privilege_label or ""):
            continue
        entries.append(
            {
                "date": row.updated_at.date().isoformat() if row.updated_at else "",
                "title": note_text[:120] or "Matter note",
                "meta": " • ".join(item for item in [f"Tags {row.tags}" if row.tags else "", row.privilege_label or ""] if item),
                "href": url_for("matter_notes", matter_id=matter.id),
            }
        )

    doc_rows = filter_accessible_document_files(
        DocumentFile.query.filter_by(matter_id=matter.id)
        .order_by(DocumentFile.uploaded_at.desc(), DocumentFile.id.desc())
        .limit(12)
        .all()
    )
    for row in doc_rows:
        if not _include(row.original_filename or "", row.category or "", row.owner_name or ""):
            continue
        entries.append(
            {
                "date": row.uploaded_at.date().isoformat() if row.uploaded_at else "",
                "title": row.original_filename or "Document upload",
                "meta": " • ".join(item for item in [row.category or "", row.doc_version or "", row.lifecycle_stage or ""] if item),
                "href": url_for("matter_dms", matter_id=matter.id),
            }
        )

    entries.sort(key=lambda item: (item.get("date") or "", item.get("title") or ""), reverse=True)
    return entries[:18]


def _search_sections(query: str, matter: Matter | None) -> list[dict[str, Any]]:
    q = _strip_search_prompt(query)
    if len(q) < 3:
        return []

    like = f"%{q}%"
    search_tokens = _search_tokens(q)
    can_view_dms = has_permission("dms", "read")
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
    deadline_query = Deadline.query
    if matter_scope_ids is not None:
        note_query = note_query.filter(MatterNote.matter_id.in_(sorted(matter_scope_ids)))
        deadline_query = deadline_query.filter(Deadline.matter_id.in_(sorted(matter_scope_ids)))
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
    deadlines = (
        deadline_query.filter(
            or_(
                Deadline.title.ilike(like),
                Deadline.status.ilike(like),
            )
        )
        .order_by(Deadline.due_at.asc(), Deadline.id.desc())
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
    workspace_documents = []
    if can_view_dms:
        workspace_query = MatterWorkspaceDocument.query
        if matter_scope_ids is not None:
            workspace_query = workspace_query.filter(MatterWorkspaceDocument.matter_id.in_(sorted(matter_scope_ids)))
        workspace_documents = (
            workspace_query.filter(
                or_(
                    MatterWorkspaceDocument.title.ilike(like),
                    MatterWorkspaceDocument.body.ilike(like),
                    MatterWorkspaceDocument.status.ilike(like),
                    MatterWorkspaceDocument.document_type.ilike(like),
                    MatterWorkspaceDocument.privilege_label.ilike(like),
                )
            )
            .order_by(MatterWorkspaceDocument.updated_at.desc(), MatterWorkspaceDocument.id.desc())
            .limit(8)
            .all()
        )
    portal_thread_query = PortalMessageThread.query
    if matter_scope_ids is not None:
        portal_thread_query = portal_thread_query.filter(PortalMessageThread.matter_id.in_(sorted(matter_scope_ids)))
    portal_threads = (
        portal_thread_query.filter(PortalMessageThread.subject.ilike(like))
        .order_by(PortalMessageThread.created_at.desc(), PortalMessageThread.id.desc())
        .limit(8)
        .all()
    )
    portal_message_query = db.session.query(PortalMessage, PortalMessageThread).join(
        PortalMessageThread, PortalMessageThread.id == PortalMessage.thread_id
    )
    if matter_scope_ids is not None:
        portal_message_query = portal_message_query.filter(PortalMessageThread.matter_id.in_(sorted(matter_scope_ids)))
    portal_messages = (
        portal_message_query.filter(or_(PortalMessage.body.ilike(like), PortalMessageThread.subject.ilike(like)))
        .order_by(PortalMessage.created_at.desc(), PortalMessage.id.desc())
        .limit(10)
        .all()
    )
    knowledge_hits = (
        KnowledgeBase.query.filter(
            or_(
                KnowledgeBase.title.ilike(like),
                KnowledgeBase.tags.ilike(like),
                KnowledgeBase.body.ilike(like),
            )
        )
        .order_by(KnowledgeBase.updated_at.desc())
        .limit(8)
        .all()
    )
    contacts = (
        Contact.query.filter(
            or_(
                Contact.name.ilike(like),
                Contact.organization.ilike(like),
                Contact.email.ilike(like),
                Contact.phone.ilike(like),
                Contact.notes.ilike(like),
            )
        )
        .order_by(Contact.created_at.desc())
        .limit(8)
        .all()
    )

    matter_by_id = {
        int(row.id): row
        for row in Matter.query.filter(
            Matter.id.in_(
                sorted(
                    {
                        int(row.matter_id)
                        for row in [*tasks, *docs, *notes, *timeline, *deadlines, *activity, *time_entries, *workspace_documents, *portal_threads]
                        if getattr(row, "matter_id", None)
                    }
                    | {int(thread.matter_id) for _message, thread in portal_messages if getattr(thread, "matter_id", None)}
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
    if deadlines:
        sections.append(
            {
                "title": "Deadlines",
                "items": [
                    {
                        "title": row.title or "Deadline",
                        "meta": " • ".join(
                            item
                            for item in [
                                assistant_matter_label(matter_by_id.get(int(row.matter_id))),
                                row.due_at.isoformat() if row.due_at else "",
                                row.status or "",
                                "Critical" if row.is_critical else "",
                            ]
                            if item
                        ),
                        "href": url_for("calendar_matter", matter_id=row.matter_id),
                    }
                    for row in deadlines[:6]
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
    if workspace_documents:
        sections.append(
            {
                "title": "Collaborative Drafts",
                "items": [
                    {
                        "title": row.title or f"Draft {row.id}",
                        "meta": " • ".join(
                            item
                            for item in [
                                assistant_matter_label(matter_by_id.get(int(row.matter_id))),
                                row.status or "",
                                row.document_type or "",
                                row.updated_at.date().isoformat() if row.updated_at else "",
                            ]
                            if item
                        ),
                        "href": url_for("matter_document_workbench", matter_id=row.matter_id, document_id=row.id),
                    }
                    for row in workspace_documents[:6]
                ],
            }
        )
    if portal_messages or portal_threads:
        communication_items: list[dict[str, str]] = []
        seen_thread_ids: set[int] = set()
        for row, thread in portal_messages[:6]:
            thread_id = int(thread.id)
            seen_thread_ids.add(thread_id)
            communication_items.append(
                {
                    "title": thread.subject or f"Thread {thread.id}",
                    "meta": " • ".join(
                        item
                        for item in [
                            assistant_matter_label(matter_by_id.get(int(thread.matter_id))),
                            (row.body or "").strip().replace("\n", " ")[:140],
                            row.created_at.date().isoformat() if row.created_at else "",
                        ]
                        if item
                    ),
                    "href": url_for("portal_message_center", matter_id=thread.matter_id, thread_id=thread.id),
                }
            )
        for thread in portal_threads:
            thread_id = int(thread.id)
            if thread_id in seen_thread_ids or len(communication_items) >= 6:
                continue
            communication_items.append(
                {
                    "title": thread.subject or f"Thread {thread.id}",
                    "meta": " • ".join(
                        item
                        for item in [
                            assistant_matter_label(matter_by_id.get(int(thread.matter_id))),
                            thread.created_at.date().isoformat() if thread.created_at else "",
                        ]
                        if item
                    ),
                    "href": url_for("portal_message_center", matter_id=thread.matter_id, thread_id=thread.id),
                }
            )
        if communication_items:
            sections.append({"title": "Client Communications", "items": communication_items})
    if knowledge_hits:
        sections.append(
            {
                "title": "Knowledge Base",
                "items": [
                    {
                        "title": row.title or f"Article {row.id}",
                        "meta": " • ".join(item for item in [row.tags or "", (row.body or "").strip()[:140]] if item),
                        "href": url_for("kb_view", kb_id=row.id),
                    }
                    for row in knowledge_hits
                ],
            }
        )
    if contacts:
        sections.append(
            {
                "title": "Contacts",
                "items": [
                    {
                        "title": row.name or f"Contact {row.id}",
                        "meta": " • ".join(
                            item for item in [row.organization or "", row.email or "", row.phone or ""] if item
                        ),
                        "href": url_for("contacts"),
                    }
                    for row in contacts
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


def _planner_matter_context(matter: Matter | None) -> dict[str, Any] | None:
    if matter is None:
        return None
    return {
        "matter_id": int(matter.id),
        "matter_no": matter.matter_no or "",
        "title": matter.title or "",
        "client_name": matter.client_name or "",
        "status": matter.status or "",
        "risk_level": matter.risk_level or "",
        "budget_status": matter.budget_status or "",
    }


def _planning_payload(
    *,
    planner_meta: dict[str, Any],
    planner_result: dict[str, Any] | None,
    planner_tool_name: str,
) -> dict[str, str]:
    fallback_reason = normalize_query(str((planner_result or {}).get("fallback_reason") or "")).strip()
    fallback_detail = normalize_query(str((planner_result or {}).get("fallback_detail") or "")).strip()
    if planner_tool_name:
        return {
            "source": "OpenAI planner",
            "tool": planner_tool_name,
            "model": normalize_query(str((planner_result or {}).get("model") or planner_meta.get("model") or "")).strip(),
            "reasoning_effort": normalize_query(
                str((planner_result or {}).get("reasoning_effort") or planner_meta.get("reasoning_effort") or "")
            ).strip(),
            "fallback_reason": "",
            "fallback_detail": "",
        }
    if planner_meta.get("enabled"):
        return {
            "source": "Deterministic fallback",
            "tool": "rule_router",
            "model": normalize_query(str(planner_meta.get("model") or "")).strip(),
            "reasoning_effort": normalize_query(str(planner_meta.get("reasoning_effort") or "")).strip(),
            "fallback_reason": fallback_reason or normalize_query(str(planner_meta.get("fallback_reason") or "")).strip(),
            "fallback_detail": fallback_detail or normalize_query(str(planner_meta.get("fallback_detail") or "")).strip(),
        }
    return {
        "source": "Deterministic routing",
        "tool": "rule_router",
        "model": "",
        "reasoning_effort": "",
        "fallback_reason": "",
        "fallback_detail": "",
    }


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

    planner_meta = assistant_agent_meta()
    planner_result = plan_assistant_request(
        prompt=cleaned_prompt,
        matter_context=_planner_matter_context(matter),
        recent_history=assistant_recent_history(),
    )
    planner_tool_name = normalize_query(str((planner_result or {}).get("tool_name") or "")).strip()
    plan_args = (planner_result or {}).get("arguments") if isinstance((planner_result or {}).get("arguments"), dict) else {}
    warning_list = list(warnings)
    planning = _planning_payload(
        planner_meta=planner_meta,
        planner_result=planner_result,
        planner_tool_name=planner_tool_name,
    )
    if not planner_tool_name and planning.get("fallback_detail"):
        warning_list.append(f"Non-AI fallback used: {planning['fallback_detail']}")
    elif planner_meta.get("available") and planner_result is None:
        warning_list.append("Non-AI fallback used because OpenAI planning was unavailable for this request.")
    if planner_tool_name == "blocked_action":
        planner_reason = normalize_query(str(plan_args.get("reason") or "")).strip()
        return _blocked_result(
            cleaned_prompt,
            planner_reason or "This action stays in the native workflow.",
            matter=matter,
        )
    if planner_tool_name == "clarify_request":
        planner_question = normalize_query(str(plan_args.get("question") or "")).strip()
        return _error_result(
            cleaned_prompt,
            planner_question or "Clarify what you want the assistant to do next.",
            matter=matter,
        )

    intent = _PLANNER_TOOL_TO_INTENT.get(planner_tool_name) or _classify_intent(cleaned_prompt)
    if intent == "matter_case_workup":
        if matter is None:
            return _error_result(cleaned_prompt, "Pick a matter or reference its matter number to construct the case.")
        focus_hint = normalize_query(str(plan_args.get("focus_hint") or "")).strip()
        if not focus_hint and len(cleaned_prompt) > 12:
            focus_hint = _strip_workup_prompt(cleaned_prompt)
        packet = _build_case_workup_packet(matter, focus_hint=focus_hint)
        context = packet.get("context") or {}
        strategy = packet.get("strategy") or {}
        memo = packet.get("memo") or {}
        chronology = list(packet.get("chronology") or [])
        workup_warnings = list(warning_list) + list(packet.get("warnings") or [])
        workup_warnings.append(
            "This workup uses internal matter records, collaborative drafts, and portal communications; external legal databases are not searched here."
        )
        analysis_sources = {str(strategy.get("source") or "").strip().lower(), str(memo.get("source") or "").strip().lower()}
        analysis_source = "OpenAI" if analysis_sources == {"openai"} else "Mixed / fallback"
        sections: list[dict[str, Any]] = []
        if list(strategy.get("recommended_actions") or []):
            sections.append(
                {
                    "title": "Recommended Actions",
                    "items": [{"title": item} for item in list(strategy.get("recommended_actions") or [])],
                }
            )
        if list(strategy.get("strengths") or []):
            sections.append({"title": "Strengths", "items": [{"title": item} for item in list(strategy.get("strengths") or [])]})
        if list(strategy.get("risks") or []):
            sections.append({"title": "Risks", "items": [{"title": item} for item in list(strategy.get("risks") or [])]})
        if list(strategy.get("evidence_gaps") or []):
            sections.append(
                {"title": "Evidence Gaps", "items": [{"title": item} for item in list(strategy.get("evidence_gaps") or [])]}
            )
        if list(context.get("parties") or []):
            sections.append(
                {
                    "title": "Key Parties",
                    "items": [
                        {
                            "title": row.get("name") or "Party",
                            "meta": " • ".join(
                                item
                                for item in [
                                    str(row.get("role") or ""),
                                    "Primary" if row.get("is_primary") else "",
                                    str(row.get("email") or ""),
                                    str(row.get("phone") or ""),
                                ]
                                if item
                            ),
                            "href": url_for("matter_parties", matter_id=matter.id),
                        }
                        for row in list(context.get("parties") or [])[:8]
                    ],
                }
            )
        if list(context.get("entity_relationships") or []):
            sections.append(
                {
                    "title": "Party Relationships",
                    "items": [
                        {
                            "title": f"{row.get('from') or 'Party'} -> {row.get('to') or 'Party'}",
                            "meta": str(row.get("relationship_type") or "Relationship"),
                            "href": url_for("matter_parties", matter_id=matter.id),
                        }
                        for row in list(context.get("entity_relationships") or [])[:8]
                    ],
                }
            )
        if list(context.get("upcoming_deadlines") or []):
            sections.append(
                {
                    "title": "Upcoming Deadlines",
                    "items": [
                        {
                            "title": row.get("title") or "Deadline",
                            "meta": " • ".join(
                                item
                                for item in [
                                    str(row.get("due_at") or ""),
                                    str(row.get("status") or ""),
                                    "Critical" if row.get("is_critical") else "",
                                ]
                                if item
                            ),
                            "href": url_for("calendar_matter", matter_id=matter.id),
                        }
                        for row in list(context.get("upcoming_deadlines") or [])[:8]
                    ],
                }
            )
        if list(context.get("stage_history") or []):
            sections.append(
                {
                    "title": "Stage History",
                    "items": [
                        {
                            "title": row.get("to_stage") or "Stage change",
                            "meta": " • ".join(
                                item
                                for item in [
                                    f"From {row.get('from_stage')}" if row.get("from_stage") else "",
                                    str(row.get("reason") or ""),
                                    str(row.get("changed_at") or "")[:10],
                                ]
                                if item
                            ),
                            "href": url_for("matter_workspace", matter_id=matter.id),
                        }
                        for row in list(context.get("stage_history") or [])[:8]
                    ],
                }
            )
        if list(context.get("workspace_documents") or []):
            sections.append(
                {
                    "title": "Collaborative Drafts",
                    "items": [
                        {
                            "title": row.get("title") or "Draft",
                            "meta": " • ".join(
                                item
                                for item in [
                                    str(row.get("status") or ""),
                                    str(row.get("document_type") or ""),
                                    str(row.get("updated_at") or "")[:10],
                                ]
                                if item
                            ),
                            "href": url_for("matter_document_workbench", matter_id=matter.id),
                        }
                        for row in list(context.get("workspace_documents") or [])[:6]
                    ],
                }
            )
        if list(context.get("portal_messages") or []):
            sections.append(
                {
                    "title": "Client Communications",
                    "items": [
                        {
                            "title": row.get("subject") or "Portal thread",
                            "meta": " • ".join(
                                item for item in [str(row.get("excerpt") or ""), str(row.get("created_at") or "")[:10]] if item
                            ),
                            "href": url_for("portal_message_center", matter_id=matter.id),
                        }
                        for row in list(context.get("portal_messages") or [])[:6]
                    ],
                }
            )
        if chronology:
            sections.append({"title": "Chronology", "items": chronology[:10]})
        audit(
            "assistant_case_workup",
            "Matter",
            matter.id,
            {"strategy_source": strategy.get("source"), "memo_source": memo.get("source"), "focus": focus_hint[:180]},
        )
        links = [
            {"label": "Open Matter", "href": url_for("matter_detail", matter_id=matter.id)},
            {"label": "Open Matter Workspace", "href": url_for("matter_workspace", matter_id=matter.id)},
            {"label": "Open Portal Messages", "href": url_for("portal_message_center", matter_id=matter.id)},
        ]
        if has_permission("dms", "read"):
            links.insert(1, {"label": "Open Workbench", "href": url_for("matter_document_workbench", matter_id=matter.id)})
        return _result(
            status="ok",
            kind="matter_case_workup",
            headline="Case Workup",
            summary=f"Prepared an integrated case-construction dossier for {assistant_matter_label(matter)}.",
            prompt=cleaned_prompt,
            matter=matter,
            warnings=workup_warnings,
            fields=[
                {"label": "Focus", "value": focus_hint or "Full case build"},
                {"label": "Stage", "value": context.get("stage") or matter.stage or "Unspecified"},
                {"label": "Status", "value": context.get("status") or matter.status or "Open"},
                {"label": "Risk", "value": context.get("risk_level") or matter.risk_level or "Medium"},
                {"label": "Budget", "value": context.get("budget_status") or matter.budget_status or "On Track"},
                {"label": "Open Tasks", "value": str(context.get("open_task_count") or 0)},
                {"label": "Upcoming Deadlines", "value": str(len(context.get("upcoming_deadlines") or []))},
                {"label": "Analysis Source", "value": analysis_source},
            ],
            text_blocks=[
                {"title": "Case Theory", "body": strategy.get("case_theory") or ""},
                {"title": "Research Position", "body": memo.get("answer") or ""},
            ],
            sections=sections,
            links=links,
            planning=planning,
        )

    if intent == "matter_strategy":
        if matter is None:
            return _error_result(cleaned_prompt, "Pick a matter or reference its matter number to build case strategy.")
        focus_hint = normalize_query(str(plan_args.get("focus_hint") or "")).strip()
        if not focus_hint and len(cleaned_prompt) > 12:
            focus_hint = _strip_strategy_prompt(cleaned_prompt)
        context = _matter_analysis_context(matter, research_query=focus_hint)
        strategy = suggest_matter_case_strategy(matter_context=context, focus_hint=focus_hint)
        strategy_warnings = list(warning_list)
        strategy_warnings.append("Case strategy is grounded in the current matter file and should still be reviewed by counsel.")
        if str(strategy.get("source") or "").strip().lower() != "openai":
            strategy_warnings.append(_draft_fallback_warning("Case strategy", strategy))
        audit(
            "assistant_case_strategy",
            "Matter",
            matter.id,
            {"source": strategy.get("source"), "fallback_reason": strategy.get("fallback_reason")},
        )
        return _result(
            status="ok",
            kind="matter_strategy",
            headline="Case Strategy Brief",
            summary=f"Prepared a case-construction brief for {assistant_matter_label(matter)}.",
            prompt=cleaned_prompt,
            matter=matter,
            warnings=strategy_warnings,
            fields=[
                {"label": "Focus", "value": focus_hint or "General case strategy"},
                {"label": "Source", "value": str(strategy.get("source") or "fallback").title()},
            ],
            text_blocks=[{"title": "Case Theory", "body": strategy.get("case_theory") or ""}],
            sections=[
                {"title": "Strengths", "items": [{"title": item} for item in list(strategy.get("strengths") or [])]},
                {"title": "Risks", "items": [{"title": item} for item in list(strategy.get("risks") or [])]},
                {"title": "Evidence Gaps", "items": [{"title": item} for item in list(strategy.get("evidence_gaps") or [])]},
                {
                    "title": "Recommended Actions",
                    "items": [{"title": item} for item in list(strategy.get("recommended_actions") or [])],
                },
            ],
            links=[
                {"label": "Open Matter", "href": url_for("matter_detail", matter_id=matter.id)},
                {"label": "Open Matter Tasks", "href": url_for("matter_tasks", matter_id=matter.id)},
            ],
            planning=planning,
        )

    if intent == "matter_research":
        if matter is None:
            return _error_result(cleaned_prompt, "Pick a matter or reference its matter number to run research on the file.")
        research_query = normalize_query(str(plan_args.get("research_query") or "")).strip() or _strip_research_prompt(cleaned_prompt)
        if len(research_query) < 3:
            return _error_result(cleaned_prompt, "State the research question or issue you want analyzed.", matter=matter)
        context = _matter_analysis_context(matter, research_query=research_query)
        memo = suggest_matter_research_memo(matter_context=context, research_query=research_query)
        research_warnings = list(warning_list)
        research_warnings.append(
            "This research memo is grounded in internal matter records, semantic hits, and knowledge-base content, not external legal databases."
        )
        if str(memo.get("source") or "").strip().lower() != "openai":
            research_warnings.append(_draft_fallback_warning("Research memo", memo))
        audit(
            "assistant_research_memo",
            "Matter",
            matter.id,
            {"source": memo.get("source"), "fallback_reason": memo.get("fallback_reason"), "query": research_query[:180]},
        )
        return _result(
            status="ok",
            kind="matter_research",
            headline="Research & File Analysis",
            summary=f"Prepared a workspace-grounded research memo for {assistant_matter_label(matter)}.",
            prompt=cleaned_prompt,
            matter=matter,
            warnings=research_warnings,
            fields=[
                {"label": "Question", "value": memo.get("research_question") or research_query},
                {"label": "Source", "value": str(memo.get("source") or "fallback").title()},
            ],
            text_blocks=[{"title": "Answer", "body": memo.get("answer") or ""}],
            sections=[
                {"title": "Supporting Sources", "items": [{"title": item} for item in list(memo.get("sources") or [])]},
                {"title": "Recommended Next Steps", "items": [{"title": item} for item in list(memo.get("next_steps") or [])]},
            ],
            links=[
                {"label": "Open Matter DMS", "href": url_for("matter_dms", matter_id=matter.id)},
                {"label": "Open Knowledge Base", "href": url_for("kb", q=research_query)},
            ],
            planning=planning,
        )

    if intent == "matter_chronology":
        if matter is None:
            return _error_result(cleaned_prompt, "Pick a matter or reference its matter number to build chronology.")
        focus_hint = normalize_query(str(plan_args.get("focus_hint") or "")).strip()
        if not focus_hint and len(cleaned_prompt) > 12:
            focus_hint = _strip_chronology_prompt(cleaned_prompt)
        entries = _matter_chronology_entries(matter, focus_hint=focus_hint)
        if not entries:
            return _result(
                status="ok",
                kind="matter_chronology",
                headline="Matter Chronology",
                summary=f"No chronology entries matched the requested focus on {assistant_matter_label(matter)}.",
                prompt=cleaned_prompt,
                matter=matter,
                warnings=warning_list,
                fields=[{"label": "Focus", "value": focus_hint or "Full matter history"}],
                links=[{"label": "Open Matter", "href": url_for("matter_detail", matter_id=matter.id)}],
                planning=planning,
            )
        return _result(
            status="ok",
            kind="matter_chronology",
            headline="Matter Chronology",
            summary=f"Prepared a chronology view for {assistant_matter_label(matter)}.",
            prompt=cleaned_prompt,
            matter=matter,
            warnings=warning_list,
            fields=[
                {"label": "Focus", "value": focus_hint or "Full matter history"},
                {"label": "Entries", "value": str(len(entries))},
            ],
            sections=[{"title": "Chronology", "items": entries}],
            links=[
                {"label": "Open Matter", "href": url_for("matter_detail", matter_id=matter.id)},
                {"label": "Open Calendar", "href": url_for("calendar_matter", matter_id=matter.id)},
            ],
            planning=planning,
        )

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
        if str(suggestion.get("source") or "").strip().lower() != "openai":
            warning_list.append(_draft_fallback_warning("Executive summary draft", suggestion))
        return _result(
            status="ok",
            kind="draft_summary",
            headline="Executive Summary Draft",
            summary=f"Prepared a matter summary draft for {assistant_matter_label(matter)}.",
            prompt=cleaned_prompt,
            matter=matter,
            warnings=warning_list,
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
            planning=planning,
        )

    if intent == "draft_client_update":
        if matter is None:
            return _error_result(cleaned_prompt, "Pick a matter or reference its matter number to draft a client update.")
        context = _matter_context(matter)
        tone_hint = normalize_query(str(plan_args.get("tone_hint") or "")).strip() or _tone_hint_from_prompt(cleaned_prompt)
        suggestion = suggest_matter_client_update(matter_context=context, tone_hint=tone_hint)
        warning_list = list(warning_list)
        if "send " in cleaned_prompt.lower():
            warning_list.append("The assistant drafts the update here. Sending still happens through the native workflow.")
        audit(
            "assistant_client_update_draft",
            "Matter",
            matter.id,
            {"source": suggestion.get("source"), "fallback_reason": suggestion.get("fallback_reason")},
        )
        if str(suggestion.get("source") or "").strip().lower() != "openai":
            warning_list.append(_draft_fallback_warning("Client update draft", suggestion))
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
            planning=planning,
        )

    if intent == "create_workspace_document":
        if matter is None:
            return _error_result(cleaned_prompt, "Pick a matter or reference its matter number before creating a collaborative draft.")
        if not role_is_case(getattr(current_user, "role", None)):
            return _blocked_result(
                cleaned_prompt,
                "Only legal case-team roles can create collaborative drafts from the assistant.",
                matter=matter,
            )
        if not has_permission("dms", "write"):
            return _blocked_result(
                cleaned_prompt,
                "Collaborative draft creation requires DMS write permission.",
                matter=matter,
            )
        option_lists = _assistant_dms_option_lists()
        document_type_options = list(option_lists.get("document_types") or [])
        confidentiality_options = list(option_lists.get("confidentialities") or [])
        privilege_label_options = list(option_lists.get("privilege_labels") or [])
        document_goal = normalize_query(str(plan_args.get("document_goal") or "")).strip() or _workspace_document_goal(cleaned_prompt)
        title = normalize_query(str(plan_args.get("title") or "")).strip()[:255] or _workspace_document_title(
            cleaned_prompt,
            matter,
            document_goal=document_goal,
        )
        if not title:
            return _error_result(cleaned_prompt, "The assistant could not determine a collaborative draft title.", matter=matter)
        requested_status = normalize_query(str(plan_args.get("status") or "")).strip().lower() or "draft"
        if requested_status not in _WORKSPACE_DOCUMENT_STATUSES:
            return _error_result(cleaned_prompt, "Collaborative draft status must be draft, review, or final.", matter=matter)
        raw_document_type = normalize_query(str(plan_args.get("document_type") or "")).strip()
        raw_confidentiality = normalize_query(str(plan_args.get("confidentiality") or "")).strip()
        raw_privilege = normalize_query(str(plan_args.get("privilege_label") or "")).strip()
        document_type = _assistant_option_match(raw_document_type, document_type_options) or (
            document_type_options[0] if document_type_options else "General"
        )
        confidentiality = _assistant_option_match(raw_confidentiality, confidentiality_options) or (
            confidentiality_options[0] if confidentiality_options else "Internal"
        )
        privilege_label = _assistant_option_match(raw_privilege, privilege_label_options)
        if raw_document_type and not _assistant_option_match(raw_document_type, document_type_options):
            return _error_result(cleaned_prompt, "Document type must match a configured DMS option.", matter=matter)
        if raw_confidentiality and not _assistant_option_match(raw_confidentiality, confidentiality_options):
            return _error_result(cleaned_prompt, "Confidentiality must match a configured DMS option.", matter=matter)
        if raw_privilege and not privilege_label:
            return _error_result(cleaned_prompt, "Privilege label must match a configured DMS option.", matter=matter)
        provided_body = str(plan_args.get("body") or "").strip()[:12000]
        workspace_warnings = list(warning_list)
        body = provided_body
        if not body:
            body, generated_warnings = _workspace_document_body_from_goal(
                matter,
                title=title,
                document_goal=document_goal or title,
            )
            workspace_warnings.extend(generated_warnings)
        preview_payload = {
            "action": "create_workspace_document",
            "user_id": int(current_user.id),
            "matter_id": int(matter.id),
            "title": title,
            "body": body[:12000],
            "status": requested_status,
            "document_type": document_type,
            "confidentiality": confidentiality,
            "privilege_label": privilege_label or "",
            "legal_hold": bool(plan_args.get("legal_hold")),
        }
        audit(
            "assistant_workspace_document_preview",
            "Matter",
            matter.id,
            {"title": title, "status": requested_status, "document_type": document_type},
        )
        return _result(
            status="ok",
            kind="create_workspace_document_preview",
            headline="Collaborative Draft Ready for Confirmation",
            summary=f"Prepared a workbench draft for {assistant_matter_label(matter)}. Confirm before it is written.",
            prompt=cleaned_prompt,
            matter=matter,
            warnings=workspace_warnings,
            fields=[
                {"label": "Title", "value": title},
                {"label": "Goal", "value": document_goal or "General collaborative draft"},
                {"label": "Status", "value": requested_status},
                {"label": "Document Type", "value": document_type},
                {"label": "Confidentiality", "value": confidentiality},
                {"label": "Privilege", "value": privilege_label or "None"},
                {"label": "Legal Hold", "value": "Yes" if preview_payload["legal_hold"] else "No"},
            ],
            text_blocks=[{"title": "Draft Body", "body": body or "No draft body generated."}],
            links=[{"label": "Open Workbench", "href": url_for("matter_document_workbench", matter_id=matter.id)}],
            requires_confirmation=True,
            confirm_token=_sign_confirmation_payload(preview_payload),
            planning=planning,
        )

    if intent == "update_matter_summary":
        if matter is None:
            return _error_result(cleaned_prompt, "Pick a matter or reference its matter number before updating the matter summary.")
        if not role_is_case(getattr(current_user, "role", None)):
            return _blocked_result(
                cleaned_prompt,
                "Only legal case-team roles can update matter summaries from the assistant.",
                matter=matter,
            )
        extracted_changes = _extract_summary_update_values(cleaned_prompt)
        requested_changes: dict[str, str] = {}
        for field_name in ("objective", "last_update_note", "outcome_summary", "risk_level", "budget_status", "status"):
            planned_value = normalize_query(str(plan_args.get(field_name) or "")).strip()
            value = planned_value or normalize_query(str(extracted_changes.get(field_name) or "")).strip()
            if value:
                requested_changes[field_name] = value
        if requested_changes.get("risk_level") and requested_changes["risk_level"] not in set(RISK_LEVELS):
            return _error_result(cleaned_prompt, "Risk level must be Low, Medium, High, or Critical.", matter=matter)
        if requested_changes.get("budget_status") and requested_changes["budget_status"] not in set(BUDGET_STATUSES):
            return _error_result(cleaned_prompt, "Budget status must be On Track, Watch, Over Budget, or Needs Review.", matter=matter)
        if requested_changes.get("status") and requested_changes["status"] not in _ASSISTANT_SUMMARY_STATUSES:
            return _blocked_result(
                cleaned_prompt,
                "The assistant can only set matter status to Open or On Hold. Closing stays in the native workflow.",
                matter=matter,
            )
        if not requested_changes:
            return _error_result(
                cleaned_prompt,
                "Specify the summary fields to update, such as objective, risk, budget, latest update, outcome, or status.",
                matter=matter,
            )
        effective_changes: dict[str, str] = {}
        current_value_map = {
            "objective": matter.objective or "",
            "last_update_note": matter.last_update_note or "",
            "outcome_summary": matter.outcome_summary or "",
            "risk_level": matter.risk_level or "",
            "budget_status": matter.budget_status or "",
            "status": matter.status or "",
        }
        for field_name, value in requested_changes.items():
            if normalize_query(current_value_map.get(field_name, "")) != normalize_query(value):
                effective_changes[field_name] = value
        if not effective_changes:
            return _result(
                status="ok",
                kind="matter_summary_noop",
                headline="Matter Summary Already Matches Request",
                summary=f"No summary changes are needed on {assistant_matter_label(matter)}.",
                prompt=cleaned_prompt,
                matter=matter,
                warnings=warning_list,
                links=[{"label": "Open Matter", "href": url_for("matter_detail", matter_id=matter.id)}],
                planning=planning,
            )
        preview_payload = {
            "action": "update_matter_summary",
            "user_id": int(current_user.id),
            "matter_id": int(matter.id),
            **effective_changes,
        }
        audit(
            "assistant_matter_summary_preview",
            "Matter",
            matter.id,
            {"fields": sorted(effective_changes.keys())},
        )
        field_rows = []
        for field_name in ("status", "risk_level", "budget_status"):
            if field_name in effective_changes:
                field_rows.append(
                    {
                        "label": field_name.replace("_", " ").title(),
                        "value": f"{current_value_map.get(field_name) or 'Blank'} -> {effective_changes[field_name]}",
                    }
                )
        text_blocks = []
        for field_name, title in (
            ("objective", "Objective"),
            ("last_update_note", "Latest Update"),
            ("outcome_summary", "Outcome Summary"),
        ):
            if field_name in effective_changes:
                text_blocks.append({"title": title, "body": effective_changes[field_name]})
        return _result(
            status="ok",
            kind="update_matter_summary_preview",
            headline="Matter Summary Update Ready for Confirmation",
            summary=f"Prepared summary updates for {assistant_matter_label(matter)}. Confirm before they are written.",
            prompt=cleaned_prompt,
            matter=matter,
            warnings=warning_list,
            fields=field_rows,
            text_blocks=text_blocks,
            links=[{"label": "Open Matter", "href": url_for("matter_detail", matter_id=matter.id)}],
            requires_confirmation=True,
            confirm_token=_sign_confirmation_payload(preview_payload),
            planning=planning,
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
        extracted_title, extracted_description = _task_title_and_description(cleaned_prompt)
        title = normalize_query(str(plan_args.get("title") or "")).strip()[:255] or extracted_title
        description = (
            normalize_query(str(plan_args.get("description") or "")).strip()[:2000] or extracted_description
        )
        if not title:
            return _error_result(cleaned_prompt, "The assistant could not determine a task title from that prompt.", matter=matter)
        due_date = _parse_iso_date_arg(plan_args.get("due_date")) or _extract_due_date(cleaned_prompt)
        assignee = None
        assignee_email = normalize_query(str(plan_args.get("assignee_email") or "")).strip().lower()
        if assignee_email:
            assignee = User.query.filter_by(email=assignee_email).first()
        if assignee is None:
            assignee = _extract_assignee(cleaned_prompt)
        planner_priority = normalize_query(str(plan_args.get("priority") or "")).strip().title()
        priority = planner_priority if planner_priority in {"High", "Medium", "Low"} else _extract_priority(cleaned_prompt)
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
            warnings=warning_list,
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
            planning=planning,
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
        status = normalize_query(str(plan_args.get("status") or "")).strip().title() or _extract_task_status(cleaned_prompt)
        if status is None:
            return _error_result(
                cleaned_prompt,
                "Specify the target task status in the prompt, for example Done, Doing, or Todo.",
                matter=matter,
            )
        if status not in _TASK_STATUSES:
            return _error_result(
                cleaned_prompt,
                "Specify the target task status in the prompt, for example Done, Doing, or Todo.",
                matter=matter,
            )
        task_reference = normalize_query(str(plan_args.get("task_reference") or "")).strip()
        matches = _task_match_candidates(task_reference or cleaned_prompt, matter=matter)
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
                warnings=warning_list,
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
                planning=planning,
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
                warnings=warning_list,
                fields=[
                    {"label": "Task", "value": task.title or f"Task #{task.id}"},
                    {"label": "Current Status", "value": task.status or "Todo"},
                ],
                links=[{"label": "Open Matter Tasks", "href": url_for("matter_tasks", matter_id=matter.id)}],
                planning=planning,
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
            warnings=warning_list,
            fields=[
                {"label": "Task", "value": task.title or f"Task #{task.id}"},
                {"label": "Current Status", "value": task.status or "Todo"},
                {"label": "New Status", "value": status},
            ],
            links=[{"label": "Open Matter Tasks", "href": url_for("matter_tasks", matter_id=matter.id)}],
            requires_confirmation=True,
            confirm_token=_sign_confirmation_payload(preview_payload),
            planning=planning,
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
        entry_date = _parse_iso_date_arg(plan_args.get("entry_date")) or _extract_entry_date(cleaned_prompt) or dt.date.today()
        planner_start_at = _parse_iso_datetime_arg(plan_args.get("start_at"))
        planner_end_at = _parse_iso_datetime_arg(plan_args.get("end_at"))
        start_at: dt.datetime | None = None
        end_at: dt.datetime | None = None
        hours = 0.0
        if planner_start_at is not None and planner_end_at is not None:
            start_at, end_at = planner_start_at, planner_end_at
            hours = max(0.0, (end_at - start_at).total_seconds() / 3600.0)
        else:
            time_range = _extract_time_range(cleaned_prompt, default_date=entry_date)
            parsed_hours = None
            try:
                parsed_hours = float(plan_args.get("hours")) if plan_args.get("hours") is not None else None
            except (TypeError, ValueError):
                parsed_hours = None
            parsed_hours = parsed_hours if parsed_hours and parsed_hours > 0 else _extract_hours(cleaned_prompt)
            if time_range is not None:
                start_at, end_at = time_range
                hours = max(0.0, (end_at - start_at).total_seconds() / 3600.0)
            elif parsed_hours and parsed_hours > 0:
                start_at, end_at = _time_entry_defaults_for_date(entry_date, hours=parsed_hours)
                hours = parsed_hours
        if start_at is None or end_at is None:
            return _error_result(
                cleaned_prompt,
                "Include either a duration like 1.5 hours or a time range like 09:00 to 10:30 when logging time.",
                matter=matter,
            )
        if hours <= 0:
            return _error_result(cleaned_prompt, "Time entry duration must be greater than zero.", matter=matter)
        narrative = normalize_query(str(plan_args.get("narrative") or "")).strip()[:2000] or _time_entry_narrative(cleaned_prompt)
        if not narrative:
            return _error_result(cleaned_prompt, "The assistant could not determine a time-entry narrative.", matter=matter)
        task_reference = normalize_query(str(plan_args.get("task_reference") or "")).strip()
        task_matches = _task_match_candidates(task_reference or cleaned_prompt, matter=matter)
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
            "is_billable": bool(plan_args.get("is_billable")) if "is_billable" in plan_args else _extract_billable(cleaned_prompt),
        }
        warning_list = list(warning_list)
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
            planning=planning,
        )

    if intent == "create_deadline":
        if matter is None:
            return _error_result(cleaned_prompt, "Pick a matter or reference its matter number before adding a deadline.")
        if not role_is_case(getattr(current_user, "role", None)):
            return _blocked_result(
                cleaned_prompt,
                "Only legal case-team roles can add deadlines from the assistant.",
                matter=matter,
            )
        title = normalize_query(str(plan_args.get("title") or "")).strip()[:255] or _deadline_title(cleaned_prompt)
        due_date = _parse_iso_date_arg(plan_args.get("due_date")) or _extract_due_date(cleaned_prompt)
        if not title or due_date is None:
            return _error_result(
                cleaned_prompt,
                "Specify a deadline title and due date, for example 'Create a critical deadline to serve the notice by 2026-05-09'.",
                matter=matter,
            )
        duplicate_deadline = Deadline.query.filter_by(matter_id=matter.id, title=title, due_at=due_date, status="open").first()
        if duplicate_deadline is not None:
            return _result(
                status="ok",
                kind="deadline_noop",
                headline="Matching Deadline Already Exists",
                summary=f"{title} is already open on {assistant_matter_label(matter)} for {due_date.isoformat()}.",
                prompt=cleaned_prompt,
                matter=matter,
                warnings=warning_list,
                links=[{"label": "Open Matter Calendar", "href": url_for("calendar_matter", matter_id=matter.id)}],
                planning=planning,
            )
        task_reference = normalize_query(str(plan_args.get("task_reference") or "")).strip()
        task_matches = _task_match_candidates(task_reference or cleaned_prompt, matter=matter)
        task = task_matches[0] if len(task_matches) == 1 else None
        is_critical = bool(plan_args.get("is_critical")) if "is_critical" in plan_args else _extract_deadline_critical_flag(cleaned_prompt)
        preview_payload = {
            "action": "create_deadline",
            "user_id": int(current_user.id),
            "matter_id": int(matter.id),
            "title": title,
            "due_at": due_date.isoformat(),
            "is_critical": bool(is_critical),
            "task_id": int(task.id) if task is not None else None,
        }
        deadline_warnings = list(warning_list)
        if len(task_matches) > 1:
            deadline_warnings.append("More than one task matched the request, so the deadline will be saved without a linked task.")
        audit(
            "assistant_deadline_preview",
            "Matter",
            matter.id,
            {"title": title, "due_at": due_date.isoformat(), "is_critical": bool(is_critical)},
        )
        return _result(
            status="ok",
            kind="create_deadline_preview",
            headline="Deadline Ready for Confirmation",
            summary=f"Prepared a deadline for {assistant_matter_label(matter)}. Confirm before it is written.",
            prompt=cleaned_prompt,
            matter=matter,
            warnings=deadline_warnings,
            fields=[
                {"label": "Title", "value": title},
                {"label": "Due Date", "value": due_date.isoformat()},
                {"label": "Critical", "value": "Yes" if is_critical else "No"},
                {"label": "Linked Task", "value": task.title if task is not None else "No linked task"},
            ],
            links=[{"label": "Open Matter Calendar", "href": url_for("calendar_matter", matter_id=matter.id)}],
            requires_confirmation=True,
            confirm_token=_sign_confirmation_payload(preview_payload),
            planning=planning,
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
        planner_event_type = normalize_query(str(plan_args.get("event_type") or "")).strip()
        event_type = planner_event_type if planner_event_type in _TIMELINE_EVENT_TYPES else _extract_timeline_event_type(cleaned_prompt)
        event_date = _parse_iso_date_arg(plan_args.get("event_date")) or _extract_due_date(cleaned_prompt) or dt.date.today()
        extracted_title, extracted_description = _timeline_title_and_description(cleaned_prompt, event_type=event_type)
        title = normalize_query(str(plan_args.get("title") or "")).strip()[:180] or extracted_title
        description = normalize_query(str(plan_args.get("description") or "")).strip()[:2000] or extracted_description
        is_milestone = bool(plan_args.get("is_milestone")) if "is_milestone" in plan_args else _extract_timeline_milestone_flag(cleaned_prompt, event_type)
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
            warnings=warning_list,
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
            planning=planning,
        )

    if intent == "add_party":
        if matter is None:
            return _error_result(cleaned_prompt, "Pick a matter or reference its matter number before adding a party.")
        if not role_is_case(getattr(current_user, "role", None)):
            return _blocked_result(
                cleaned_prompt,
                "Only legal case-team roles can add matter parties from the assistant.",
                matter=matter,
            )
        extracted_party = _extract_party_details(cleaned_prompt)
        entity_name = normalize_query(str(plan_args.get("entity_name") or "")).strip()[:255] or str(extracted_party.get("entity_name") or "")[:255]
        party_role = normalize_query(str(plan_args.get("party_role") or "")).strip()[:80] or str(extracted_party.get("party_role") or "")[:80] or "Interested Party"
        entity_type = normalize_query(str(plan_args.get("entity_type") or "")).strip().lower()
        if entity_type not in {"person", "organization"}:
            entity_type = str(extracted_party.get("entity_type") or "person")
        email = normalize_query(str(plan_args.get("email") or "")).strip().lower()[:255] or str(extracted_party.get("email") or "")[:255]
        phone = normalize_query(str(plan_args.get("phone") or "")).strip()[:80] or str(extracted_party.get("phone") or "")[:80]
        is_primary = bool(plan_args.get("is_primary")) if "is_primary" in plan_args else bool(extracted_party.get("is_primary"))
        if not entity_name:
            return _error_result(cleaned_prompt, "The assistant could not determine the party name from that prompt.", matter=matter)
        existing_entity = Entity.query.filter(db.func.lower(Entity.name) == entity_name.lower()).first()
        if existing_entity is not None:
            existing_link = MatterParty.query.filter_by(
                matter_id=matter.id,
                entity_id=existing_entity.id,
                party_role=party_role,
            ).first()
            if existing_link is not None:
                return _result(
                    status="ok",
                    kind="party_noop",
                    headline="Party Already Linked",
                    summary=f"{entity_name} is already linked to {assistant_matter_label(matter)} as {party_role}.",
                    prompt=cleaned_prompt,
                    matter=matter,
                    warnings=warning_list,
                    links=[{"label": "Open Matter Parties", "href": url_for("matter_parties", matter_id=matter.id)}],
                    planning=planning,
                )
        preview_payload = {
            "action": "add_party",
            "user_id": int(current_user.id),
            "matter_id": int(matter.id),
            "entity_name": entity_name,
            "party_role": party_role,
            "entity_type": entity_type,
            "email": email,
            "phone": phone,
            "is_primary": bool(is_primary),
        }
        audit(
            "assistant_party_preview",
            "Matter",
            matter.id,
            {"entity_name": entity_name, "party_role": party_role, "is_primary": bool(is_primary)},
        )
        return _result(
            status="ok",
            kind="add_party_preview",
            headline="Matter Party Ready for Confirmation",
            summary=f"Prepared a party link for {assistant_matter_label(matter)}. Confirm before it is written.",
            prompt=cleaned_prompt,
            matter=matter,
            warnings=warning_list,
            fields=[
                {"label": "Name", "value": entity_name},
                {"label": "Role", "value": party_role},
                {"label": "Entity Type", "value": entity_type.title()},
                {"label": "Primary", "value": "Yes" if is_primary else "No"},
                {"label": "Email", "value": email or "Not provided"},
                {"label": "Phone", "value": phone or "Not provided"},
            ],
            links=[{"label": "Open Matter Parties", "href": url_for("matter_parties", matter_id=matter.id)}],
            requires_confirmation=True,
            confirm_token=_sign_confirmation_payload(preview_payload),
            planning=planning,
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
        extracted_body, extracted_tags, extracted_privilege_label = _note_body_tags_and_privilege(cleaned_prompt)
        body = normalize_query(str(plan_args.get("body") or "")).strip()[:4000] or extracted_body
        tags = _tag_list_to_csv(plan_args.get("tags")) or extracted_tags
        privilege_label = normalize_query(str(plan_args.get("privilege_label") or "")).strip()[:120] or extracted_privilege_label
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
            warnings=warning_list,
            fields=[
                {"label": "Tags", "value": tags or "No tags parsed"},
                {"label": "Privilege", "value": privilege_label or "Standard note"},
            ],
            text_blocks=[{"title": "Note Body", "body": body[:4000]}],
            links=[{"label": "Open Matter Notes", "href": url_for("matter_notes", matter_id=matter.id)}],
            requires_confirmation=True,
            confirm_token=_sign_confirmation_payload(preview_payload),
            planning=planning,
        )

    if intent == "matter_briefing":
        if matter is None:
            return _error_result(cleaned_prompt, "Pick a matter or reference its matter number to review next steps.")
        context = _matter_context(matter)
        sections, briefing_warnings = _matter_briefing_sections(matter)
        warning_list = list(warning_list) + briefing_warnings
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
            planning=planning,
        )

    search_query = normalize_query(str(plan_args.get("query") or "")).strip() or cleaned_prompt
    sections = _search_sections(search_query, matter)
    audit(
        "assistant_search",
        "Matter",
        int(matter.id) if matter is not None else None,
        {"prompt": search_query[:255], "section_count": len(sections)},
    )
    if not sections:
        return _result(
            status="ok",
            kind="search",
            headline="No Direct Matches Found",
            summary="No matching records were found in your current scope.",
            prompt=cleaned_prompt,
            matter=matter,
            warnings=warning_list,
            links=[{"label": "Open Global Search", "href": url_for("search", q=_strip_search_prompt(search_query))}],
            planning=planning,
        )
    return _result(
        status="ok",
        kind="search",
        headline="Assistant Search Results",
        summary="Matched records in your current scope.",
        prompt=cleaned_prompt,
        matter=matter,
        warnings=warning_list,
        sections=sections,
        links=[{"label": "Open Global Search", "href": url_for("search", q=_strip_search_prompt(search_query))}],
        planning=planning,
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
    if action == "update_matter_summary":
        changes: dict[str, str | None] = {}
        for field_name in ("objective", "last_update_note", "outcome_summary", "risk_level", "budget_status", "status"):
            raw_value = normalize_query(str(payload.get(field_name) or "")).strip()
            if raw_value:
                changes[field_name] = raw_value
        if not changes:
            return _error_result(cleaned_prompt, "No summary updates were supplied in the confirmation payload.", matter=matter)
        if changes.get("risk_level") and changes["risk_level"] not in set(RISK_LEVELS):
            return _error_result(cleaned_prompt, "Matter risk level is invalid.", matter=matter)
        if changes.get("budget_status") and changes["budget_status"] not in set(BUDGET_STATUSES):
            return _error_result(cleaned_prompt, "Matter budget status is invalid.", matter=matter)
        if changes.get("status") and changes["status"] not in _ASSISTANT_SUMMARY_STATUSES:
            return _blocked_result(
                cleaned_prompt,
                "The assistant can only set matter status to Open or On Hold. Closing stays in the native workflow.",
                matter=matter,
            )
        if "objective" in changes:
            matter.objective = changes["objective"] or None
        if "last_update_note" in changes:
            matter.last_update_note = changes["last_update_note"] or None
        if "outcome_summary" in changes:
            matter.outcome_summary = changes["outcome_summary"] or None
        if "risk_level" in changes:
            matter.risk_level = str(changes["risk_level"])
        if "budget_status" in changes:
            matter.budget_status = str(changes["budget_status"])
        if "status" in changes:
            matter.status = str(changes["status"])
        matter.last_updated_at = utc_now()
        db.session.commit()
        _mark_confirmation_consumed(confirm_token)
        audit(
            "matter_summary_update",
            "Matter",
            matter.id,
            {"source": "assistant", "fields": sorted(changes.keys())},
        )
        matter_activity(matter.id, "Executive summary updated", "Updated via assistant")
        return _result(
            status="ok",
            kind="matter_summary_updated",
            headline="Matter Summary Updated",
            summary=f"Updated the matter summary on {assistant_matter_label(matter)}.",
            prompt=cleaned_prompt,
            matter=matter,
            fields=[
                {"label": "Status", "value": matter.status or "Open"},
                {"label": "Risk", "value": matter.risk_level or "Medium"},
                {"label": "Budget", "value": matter.budget_status or "On Track"},
            ],
            text_blocks=[
                {"title": "Objective", "body": matter.objective or "No objective recorded."},
                {"title": "Latest Update", "body": matter.last_update_note or "No update note recorded."},
                {"title": "Outcome Summary", "body": matter.outcome_summary or "No outcome summary recorded."},
            ],
            links=[{"label": "Open Matter", "href": url_for("matter_detail", matter_id=matter.id)}],
        )

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

    if action == "create_deadline":
        title = normalize_query(str(payload.get("title") or ""))[:255]
        due_at_raw = str(payload.get("due_at") or "").strip()
        if not title or not due_at_raw:
            return _error_result(cleaned_prompt, "Deadline title or due date is missing from the confirmation payload.", matter=matter)
        try:
            due_at = dt.date.fromisoformat(due_at_raw)
        except ValueError:
            return _error_result(cleaned_prompt, "Deadline due date is invalid.", matter=matter)
        task_id_raw = payload.get("task_id")
        task_id = int(task_id_raw) if task_id_raw is not None and str(task_id_raw).isdigit() else None
        task = db.session.get(Task, task_id) if task_id else None
        if task is not None and int(task.matter_id) != int(matter.id):
            return _error_result(cleaned_prompt, "Linked task does not belong to the target matter.", matter=matter)
        duplicate_deadline = Deadline.query.filter_by(matter_id=matter.id, title=title, due_at=due_at, status="open").first()
        if duplicate_deadline is not None:
            return _result(
                status="ok",
                kind="deadline_noop",
                headline="Matching Deadline Already Exists",
                summary=f"{title} is already open on {assistant_matter_label(matter)} for {due_at.isoformat()}.",
                prompt=cleaned_prompt,
                matter=matter,
                links=[{"label": "Open Matter Calendar", "href": url_for("calendar_matter", matter_id=matter.id)}],
            )
        deadline = Deadline(
            matter_id=matter.id,
            task_id=task.id if task is not None else None,
            title=title,
            due_at=due_at,
            is_critical=bool(payload.get("is_critical")),
            status="open",
            created_by=current_user.id,
        )
        matter.last_updated_at = utc_now()
        db.session.add(deadline)
        db.session.commit()
        _mark_confirmation_consumed(confirm_token)
        audit(
            "deadline_create",
            "Deadline",
            deadline.id,
            {"matter_id": matter.id, "source": "assistant"},
        )
        matter_activity(matter.id, f"Deadline added: {deadline.title}", deadline.due_at.isoformat())
        return _result(
            status="ok",
            kind="deadline_created",
            headline="Deadline Added",
            summary=f"Added a deadline on {assistant_matter_label(matter)}.",
            prompt=cleaned_prompt,
            matter=matter,
            fields=[
                {"label": "Title", "value": deadline.title},
                {"label": "Due Date", "value": deadline.due_at.isoformat()},
                {"label": "Critical", "value": "Yes" if deadline.is_critical else "No"},
                {"label": "Linked Task", "value": task.title if task is not None else "No linked task"},
            ],
            links=[{"label": "Open Matter Calendar", "href": url_for("calendar_matter", matter_id=matter.id)}],
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

    if action == "add_party":
        entity_name = normalize_query(str(payload.get("entity_name") or ""))[:255]
        party_role = normalize_query(str(payload.get("party_role") or ""))[:80]
        if not entity_name or not party_role:
            return _error_result(cleaned_prompt, "Party name or role is missing from the confirmation payload.", matter=matter)
        entity_type = normalize_query(str(payload.get("entity_type") or "person")).strip().lower() or "person"
        if entity_type not in {"person", "organization"}:
            entity_type = "person"
        email = normalize_query(str(payload.get("email") or "")).strip().lower()[:255] or None
        phone = normalize_query(str(payload.get("phone") or "")).strip()[:80] or None
        entity = Entity.query.filter(db.func.lower(Entity.name) == entity_name.lower()).first()
        if entity is None:
            entity = Entity(
                name=entity_name,
                entity_type=entity_type,
                email=email,
                phone=phone,
            )
            db.session.add(entity)
            db.session.flush()
        elif email and not entity.email:
            entity.email = email
        if phone and not entity.phone:
            entity.phone = phone
        existing_link = MatterParty.query.filter_by(matter_id=matter.id, entity_id=entity.id, party_role=party_role).first()
        if existing_link is None:
            existing_link = MatterParty(
                matter_id=matter.id,
                entity_id=entity.id,
                party_role=party_role,
                is_primary=bool(payload.get("is_primary")),
            )
            matter.last_updated_at = utc_now()
            db.session.add(existing_link)
            db.session.commit()
            _mark_confirmation_consumed(confirm_token)
            audit(
                "matter_party_add",
                "Matter",
                matter.id,
                {"entity_id": entity.id, "role": party_role, "source": "assistant"},
            )
            matter_activity(matter.id, "Party linked", f"{entity.name} ({party_role})")
            return _result(
                status="ok",
                kind="party_added",
                headline="Matter Party Added",
                summary=f"Linked {entity.name} to {assistant_matter_label(matter)}.",
                prompt=cleaned_prompt,
                matter=matter,
                fields=[
                    {"label": "Name", "value": entity.name},
                    {"label": "Role", "value": party_role},
                    {"label": "Primary", "value": "Yes" if existing_link.is_primary else "No"},
                ],
                links=[{"label": "Open Matter Parties", "href": url_for("matter_parties", matter_id=matter.id)}],
            )
        _mark_confirmation_consumed(confirm_token)
        return _result(
            status="ok",
            kind="party_noop",
            headline="Party Already Linked",
            summary=f"{entity.name} is already linked to {assistant_matter_label(matter)} as {party_role}.",
            prompt=cleaned_prompt,
            matter=matter,
            links=[{"label": "Open Matter Parties", "href": url_for("matter_parties", matter_id=matter.id)}],
        )

    if action == "create_workspace_document":
        if not has_permission("dms", "write"):
            return _blocked_result(
                cleaned_prompt,
                "Collaborative draft creation requires DMS write permission.",
                matter=matter,
            )
        option_lists = _assistant_dms_option_lists()
        document_type_options = list(option_lists.get("document_types") or [])
        confidentiality_options = list(option_lists.get("confidentialities") or [])
        privilege_label_options = list(option_lists.get("privilege_labels") or [])
        title = normalize_query(str(payload.get("title") or "")).strip()[:255]
        body = str(payload.get("body") or "").strip()[:12000]
        status = normalize_query(str(payload.get("status") or "")).strip().lower() or "draft"
        if not title or not body:
            return _error_result(cleaned_prompt, "Collaborative draft title or body is missing from the confirmation payload.", matter=matter)
        if status not in _WORKSPACE_DOCUMENT_STATUSES:
            return _error_result(cleaned_prompt, "Collaborative draft status is invalid.", matter=matter)
        raw_document_type = normalize_query(str(payload.get("document_type") or "")).strip()
        raw_confidentiality = normalize_query(str(payload.get("confidentiality") or "")).strip()
        raw_privilege = normalize_query(str(payload.get("privilege_label") or "")).strip()
        document_type = _assistant_option_match(raw_document_type, document_type_options) or (
            document_type_options[0] if document_type_options else "General"
        )
        confidentiality = _assistant_option_match(raw_confidentiality, confidentiality_options) or (
            confidentiality_options[0] if confidentiality_options else "Internal"
        )
        privilege_label = _assistant_option_match(raw_privilege, privilege_label_options)
        if raw_document_type and not _assistant_option_match(raw_document_type, document_type_options):
            return _error_result(cleaned_prompt, "Collaborative draft document type is invalid.", matter=matter)
        if raw_confidentiality and not _assistant_option_match(raw_confidentiality, confidentiality_options):
            return _error_result(cleaned_prompt, "Collaborative draft confidentiality is invalid.", matter=matter)
        if raw_privilege and not privilege_label:
            return _error_result(cleaned_prompt, "Collaborative draft privilege label is invalid.", matter=matter)
        row = MatterWorkspaceDocument(
            matter_id=matter.id,
            title=title,
            body=body,
            status=status,
            document_type=document_type,
            confidentiality=confidentiality,
            privilege_label=privilege_label,
            legal_hold=bool(payload.get("legal_hold")),
            created_by=current_user.id,
            last_edited_by=current_user.id,
            updated_at=utc_now(),
        )
        matter.last_updated_at = utc_now()
        db.session.add(row)
        db.session.commit()
        _mark_confirmation_consumed(confirm_token)
        audit(
            "matter_workspace_document_create",
            "MatterWorkspaceDocument",
            row.id,
            {"matter_id": matter.id, "status": row.status, "document_type": row.document_type, "source": "assistant"},
        )
        matter_activity(matter.id, "Collaborative draft created", row.title)
        return _result(
            status="ok",
            kind="workspace_document_created",
            headline="Collaborative Draft Created",
            summary=f"Created a workbench draft on {assistant_matter_label(matter)}.",
            prompt=cleaned_prompt,
            matter=matter,
            fields=[
                {"label": "Title", "value": row.title},
                {"label": "Status", "value": row.status},
                {"label": "Document Type", "value": row.document_type or "General"},
                {"label": "Confidentiality", "value": row.confidentiality or "Internal"},
                {"label": "Privilege", "value": row.privilege_label or "None"},
            ],
            text_blocks=[{"title": "Draft Body", "body": row.body or ""}],
            links=[
                {"label": "Open Workbench", "href": url_for("matter_document_workbench", matter_id=matter.id, document_id=row.id)},
                {"label": "Open Matter Workspace", "href": url_for("matter_workspace", matter_id=matter.id)},
            ],
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
