from __future__ import annotations

import json
import time
from typing import Any

from flask import current_app

from .ai_provider import log_ai_operation


def assistant_agent_settings() -> dict[str, Any]:
    ai_enabled = bool(current_app.config.get("AI_ENABLED", False))
    provider = str(current_app.config.get("AI_PROVIDER") or "openai").strip().lower()
    api_key = (current_app.config.get("AI_OPENAI_API_KEY") or "").strip()
    model = str(
        current_app.config.get("AI_ASSISTANT_MODEL")
        or current_app.config.get("AI_OPENAI_TEXT_MODEL")
        or "gpt-4o-mini"
    ).strip()
    timeout_seconds = max(1, int(current_app.config.get("AI_OPENAI_TIMEOUT_SECONDS", 20) or 20))
    max_retries = max(0, int(current_app.config.get("AI_OPENAI_MAX_RETRIES", 2) or 2))
    agent_enabled = bool(current_app.config.get("AI_ASSISTANT_AGENT_ENABLED", ai_enabled))
    reasoning_effort = str(current_app.config.get("AI_ASSISTANT_REASONING_EFFORT") or "medium").strip().lower()
    if reasoning_effort not in {"low", "medium", "high"}:
        reasoning_effort = "medium"
    return {
        "agent_enabled": agent_enabled,
        "ai_enabled": ai_enabled,
        "provider": provider,
        "api_key": api_key,
        "model": model,
        "timeout_seconds": timeout_seconds,
        "max_retries": max_retries,
        "reasoning_effort": reasoning_effort,
    }


def assistant_agent_meta() -> dict[str, Any]:
    settings = assistant_agent_settings()
    available = bool(
        settings["agent_enabled"] and settings["ai_enabled"] and settings["provider"] == "openai" and settings["api_key"]
    )
    return {
        "enabled": bool(settings["agent_enabled"]),
        "available": available,
        "provider": settings["provider"],
        "model": settings["model"],
        "reasoning_effort": settings["reasoning_effort"],
    }


def _assistant_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "matter_briefing",
                "description": "Review matter status, next steps, deadlines, tasks, notes, and time context.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "draft_summary",
                "description": "Prepare an executive summary draft for the current matter.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "draft_client_update",
                "description": "Prepare a client-facing update draft for the current matter.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tone_hint": {
                            "type": "string",
                            "description": "Optional tone guidance such as plain English, formal, or warm.",
                        }
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_workspace",
                "description": "Search matters, tasks, notes, activity, deadlines, documents, and the user's time entries.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Focused search query without extra filler words."}
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "prepare_task",
                "description": "Prepare a task draft that will still require user confirmation before writing.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "due_date": {"type": "string", "description": "ISO date YYYY-MM-DD when known."},
                        "priority": {"type": "string", "enum": ["High", "Medium", "Low"]},
                        "assignee_email": {"type": "string"},
                    },
                    "required": ["title"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "prepare_task_status_update",
                "description": "Prepare a task status update that will still require user confirmation before writing.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_reference": {
                            "type": "string",
                            "description": "Task number like Task #12 or a precise task title fragment.",
                        },
                        "status": {"type": "string", "enum": ["Todo", "Doing", "Done"]},
                    },
                    "required": ["task_reference", "status"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "prepare_note",
                "description": "Prepare a matter note draft that will still require user confirmation before writing.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "body": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "privilege_label": {"type": "string"},
                    },
                    "required": ["body"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "prepare_timeline_event",
                "description": "Prepare a timeline event draft that will still require user confirmation before writing.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "event_date": {"type": "string", "description": "ISO date YYYY-MM-DD."},
                        "event_type": {
                            "type": "string",
                            "enum": ["Milestone", "Filing", "Hearing", "Client Update", "Internal Review", "Delivery"],
                        },
                        "is_milestone": {"type": "boolean"},
                    },
                    "required": ["title", "event_date", "event_type"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "prepare_time_entry",
                "description": "Prepare a draft time entry that will still require user confirmation before writing.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "narrative": {"type": "string"},
                        "entry_date": {"type": "string", "description": "ISO date YYYY-MM-DD when known."},
                        "start_at": {"type": "string", "description": "ISO datetime when known."},
                        "end_at": {"type": "string", "description": "ISO datetime when known."},
                        "hours": {"type": "number"},
                        "is_billable": {"type": "boolean"},
                        "task_reference": {
                            "type": "string",
                            "description": "Optional task number or task title fragment to link the time entry.",
                        },
                    },
                    "required": ["narrative"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "blocked_action",
                "description": "Use when the user asks for a high-risk or unsupported action that must stay in native workflows.",
                "parameters": {
                    "type": "object",
                    "properties": {"reason": {"type": "string"}},
                    "required": ["reason"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "clarify_request",
                "description": "Use when the user request needs a clear follow-up question before any safe action can be prepared.",
                "parameters": {
                    "type": "object",
                    "properties": {"question": {"type": "string"}},
                    "required": ["question"],
                    "additionalProperties": False,
                },
            },
        },
    ]


_SYSTEM_PROMPT = """You are the planning brain for a supervised legal intranet assistant.

You must choose exactly one function call for each user request.

Rules:
- Prefer the user's selected or resolved matter when one is already provided in context.
- If the request needs a matter but no matter is resolved, call clarify_request.
- Never approve payments, settle invoices, move trust funds, override conflicts, delete documents, delete matters, close matters, or archive matters. Use blocked_action instead.
- Do not invent matter numbers, task numbers, dates, users, or emails.
- For write actions, prepare a draft only. The application will still require explicit user confirmation before writing.
- Use search_workspace for research and retrieval requests.
- Use matter_briefing for questions about next steps, current status, upcoming deadlines, or where things stand.
- Use draft_summary for executive or partner summaries.
- Use draft_client_update for client-facing updates or status emails.
- Use concise, explicit, production-safe arguments.
"""


def plan_assistant_request(
    *,
    prompt: str,
    matter_context: dict[str, Any] | None,
    recent_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    settings = assistant_agent_settings()
    if not settings["agent_enabled"] or not settings["ai_enabled"]:
        return None
    if settings["provider"] != "openai" or not settings["api_key"]:
        return None

    payload = {
        "user_prompt": str(prompt or "").strip(),
        "resolved_matter": matter_context or None,
        "recent_history": list(recent_history or [])[:4],
        "capabilities": [
            "matter_briefing",
            "draft_summary",
            "draft_client_update",
            "search_workspace",
            "prepare_task",
            "prepare_task_status_update",
            "prepare_note",
            "prepare_timeline_event",
            "prepare_time_entry",
            "blocked_action",
            "clarify_request",
        ],
    }
    request_chars = len(json.dumps(payload, ensure_ascii=True))
    started = time.perf_counter()
    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=settings["api_key"],
            timeout=float(settings["timeout_seconds"]),
            max_retries=settings["max_retries"],
        )
        response = client.chat.completions.create(
            model=settings["model"],
            temperature=0,
            reasoning_effort=settings["reasoning_effort"],
            parallel_tool_calls=False,
            tool_choice="required",
            max_completion_tokens=900,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=True)},
            ],
            tools=_assistant_tools(),
        )
        choice = response.choices[0] if response.choices else None
        message = getattr(choice, "message", None)
        tool_calls = list(getattr(message, "tool_calls", None) or [])
        if not tool_calls:
            latency_ms = int((time.perf_counter() - started) * 1000)
            log_ai_operation(
                operation_type="assistant_agent_plan",
                provider="openai",
                model=getattr(response, "model", None) or settings["model"],
                status="error",
                request_chars=request_chars,
                latency_ms=latency_ms,
                metadata={"reason": "missing_tool_call"},
                error_message="Assistant planner returned no tool call.",
            )
            return None

        first = tool_calls[0]
        tool_name = str(getattr(getattr(first, "function", None), "name", "") or "").strip()
        arguments_raw = str(getattr(getattr(first, "function", None), "arguments", "") or "").strip()
        try:
            arguments = json.loads(arguments_raw) if arguments_raw else {}
        except json.JSONDecodeError:
            arguments = {}
        latency_ms = int((time.perf_counter() - started) * 1000)
        log_ai_operation(
            operation_type="assistant_agent_plan",
            provider="openai",
            model=getattr(response, "model", None) or settings["model"],
            status="ok",
            request_chars=request_chars,
            response_units=1,
            latency_ms=latency_ms,
            metadata={"tool_name": tool_name},
        )
        return {
            "tool_name": tool_name,
            "arguments": arguments if isinstance(arguments, dict) else {},
            "model": getattr(response, "model", None) or settings["model"],
            "reasoning_effort": settings["reasoning_effort"],
        }
    except Exception as exc:  # pragma: no cover - provider/network dependency
        latency_ms = int((time.perf_counter() - started) * 1000)
        log_ai_operation(
            operation_type="assistant_agent_plan",
            provider="openai",
            model=settings["model"],
            status="error",
            request_chars=request_chars,
            latency_ms=latency_ms,
            error_message=str(exc),
        )
        current_app.logger.warning("OpenAI assistant planner fallback engaged: %s", exc)
        return None


__all__ = [
    "assistant_agent_meta",
    "assistant_agent_settings",
    "plan_assistant_request",
]
