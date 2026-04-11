from __future__ import annotations

import json
import time
from typing import Any

from flask import current_app

from .ai_provider import log_ai_operation

DEFAULT_ASSISTANT_MODEL = "gpt-5.2"


def _fallback_reason_for_settings(settings: dict[str, Any]) -> tuple[str, str]:
    if not settings["agent_enabled"]:
        return "assistant_agent_disabled", "Assistant AI planning is disabled in server configuration."
    if not settings["ai_enabled"]:
        return "ai_disabled", "AI is disabled in server configuration."
    if settings["provider"] != "openai":
        return (
            "unsupported_provider",
            f"Configured AI provider '{settings['provider'] or 'unknown'}' does not support assistant planning.",
        )
    if not settings["api_key"]:
        return "missing_api_key", "OpenAI API key is not configured for assistant planning."
    return "", ""


def _fallback_plan_result(settings: dict[str, Any], *, reason: str, detail: str = "") -> dict[str, Any]:
    reason_text = str(reason or "unknown").strip() or "unknown"
    detail_text = str(detail or "").strip()
    return {
        "tool_name": "",
        "arguments": {},
        "model": settings["model"],
        "reasoning_effort": settings["reasoning_effort"],
        "fallback_reason": reason_text,
        "fallback_detail": detail_text,
    }


def assistant_agent_settings() -> dict[str, Any]:
    ai_enabled = bool(current_app.config.get("AI_ENABLED", False))
    provider = str(current_app.config.get("AI_PROVIDER") or "openai").strip().lower()
    api_key = (current_app.config.get("AI_OPENAI_API_KEY") or "").strip()
    model = str(
        current_app.config.get("AI_ASSISTANT_MODEL")
        or DEFAULT_ASSISTANT_MODEL
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
    fallback_reason, fallback_detail = _fallback_reason_for_settings(settings)
    return {
        "enabled": bool(settings["agent_enabled"]),
        "available": available,
        "provider": settings["provider"],
        "model": settings["model"],
        "reasoning_effort": settings["reasoning_effort"],
        "fallback_reason": fallback_reason,
        "fallback_detail": fallback_detail,
    }


def _assistant_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "analyze_source_material",
                "description": "Analyze uploaded or pasted source material when the user wants a document brief, issue extraction, summary, or file review.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "analysis_goal": {
                            "type": "string",
                            "description": "Specific ask such as summarize this brief, extract issues, build chronology, or identify risks.",
                        },
                        "preferred_output": {
                            "type": "string",
                            "enum": ["interactive", "markdown", "plain_text"],
                        },
                    },
                    "additionalProperties": False,
                },
            },
        },
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
                "name": "matter_case_workup",
                "description": "Prepare an integrated case-construction dossier that combines strategy, research, chronology, parties, deadlines, drafts, and communications.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "focus_hint": {
                            "type": "string",
                            "description": "Optional focus such as hearing prep, witness plan, pleading posture, or settlement leverage.",
                        }
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "matter_strategy",
                "description": "Construct a case strategy or case-theory brief for the current matter.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "focus_hint": {
                            "type": "string",
                            "description": "Optional focus such as hearing prep, opposition strategy, or witness plan.",
                        }
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "matter_research",
                "description": "Prepare a workspace-grounded research memo using the matter file, semantic hits, and knowledge base.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "research_query": {
                            "type": "string",
                            "description": "Focused legal or factual research question to answer from the workspace context.",
                        }
                    },
                    "required": ["research_query"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "matter_chronology",
                "description": "Prepare a chronology or procedural-history view of the current matter.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "focus_hint": {
                            "type": "string",
                            "description": "Optional focus such as pleadings, hearings, evidence, or client communications.",
                        }
                    },
                    "additionalProperties": False,
                },
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
                "name": "draft_portal_reply",
                "description": "Draft an internal-side reply to the latest relevant client portal thread for the current matter.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "thread_focus": {
                            "type": "string",
                            "description": "Optional subject or issue focus to help match the correct client thread.",
                        },
                        "tone_hint": {
                            "type": "string",
                            "description": "Optional tone guidance such as plain English, formal, or warm.",
                        },
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
                "name": "matter_financial_snapshot",
                "description": "Review matter billing status, approved unbilled time, draft review queue, invoice totals, and outstanding balances.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "prepare_matter_summary_update",
                "description": "Prepare a matter executive-summary update that still requires user confirmation before writing.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "objective": {"type": "string"},
                        "last_update_note": {"type": "string"},
                        "outcome_summary": {"type": "string"},
                        "risk_level": {"type": "string", "enum": ["Low", "Medium", "High", "Critical"]},
                        "budget_status": {
                            "type": "string",
                            "enum": ["On Track", "Watch", "Over Budget", "Needs Review"],
                        },
                        "status": {"type": "string", "enum": ["Open", "On Hold"]},
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "prepare_workspace_document",
                "description": "Prepare a collaborative matter workbench draft that still requires user confirmation before writing.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "document_goal": {
                            "type": "string",
                            "description": "What this draft should achieve, such as case workup, hearing plan, chronology, or research memo.",
                        },
                        "document_type": {"type": "string"},
                        "confidentiality": {"type": "string"},
                        "privilege_label": {"type": "string"},
                        "status": {"type": "string", "enum": ["draft", "review", "final"]},
                        "legal_hold": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "prepare_task_bundle",
                "description": "Prepare a multi-task checklist or task pack that still requires user confirmation before writing.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "bundle_goal": {
                            "type": "string",
                            "description": "What the checklist or task bundle is for, such as hearing prep, discovery, settlement, or filing readiness.",
                        },
                        "target_due_date": {"type": "string", "description": "Optional ISO date YYYY-MM-DD for the overall workstream."},
                        "tasks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "description": {"type": "string"},
                                    "due_date": {"type": "string"},
                                    "priority": {"type": "string", "enum": ["High", "Medium", "Low"]},
                                },
                                "required": ["title"],
                                "additionalProperties": False,
                            },
                        },
                    },
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
                "name": "prepare_deadline",
                "description": "Prepare a matter deadline that will still require user confirmation before writing.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "due_date": {"type": "string", "description": "ISO date YYYY-MM-DD."},
                        "is_critical": {"type": "boolean"},
                        "task_reference": {
                            "type": "string",
                            "description": "Optional task number or task title fragment to link the deadline.",
                        },
                    },
                    "required": ["title", "due_date"],
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
                "name": "prepare_party",
                "description": "Prepare a matter-party link that will still require user confirmation before writing.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "entity_name": {"type": "string"},
                        "party_role": {"type": "string"},
                        "entity_type": {"type": "string", "enum": ["person", "organization"]},
                        "email": {"type": "string"},
                        "phone": {"type": "string"},
                        "is_primary": {"type": "boolean"},
                    },
                    "required": ["entity_name", "party_role"],
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


def _assistant_response_tools() -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for item in _assistant_tools():
        function = item.get("function") if isinstance(item, dict) else None
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        parameters = function.get("parameters")
        if not name or not isinstance(parameters, dict):
            continue
        tools.append(
            {
                "type": "function",
                "name": name,
                "description": str(function.get("description") or "").strip() or None,
                "parameters": parameters,
                "strict": True,
            }
        )
    return tools


def _extract_response_tool_call(response: Any) -> tuple[str, dict[str, Any]]:
    output_items = list(getattr(response, "output", None) or [])
    for item in output_items:
        if str(getattr(item, "type", "") or "").strip() != "function_call":
            continue
        tool_name = str(getattr(item, "name", "") or "").strip()
        arguments_raw = str(getattr(item, "arguments", "") or "").strip()
        if not tool_name:
            continue
        try:
            arguments = json.loads(arguments_raw) if arguments_raw else {}
        except json.JSONDecodeError:
            arguments = {}
        return tool_name, arguments if isinstance(arguments, dict) else {}
    return "", {}


_SYSTEM_PROMPT = """You are the planning brain for a supervised legal intranet assistant.

You must choose exactly one function call for each user request.

Rules:
- Prefer the user's selected or resolved matter when one is already provided in context.
- If the request needs a matter but no matter is resolved, call clarify_request.
- Never approve payments, settle invoices, move trust funds, override conflicts, delete documents, delete matters, close matters, or archive matters. Use blocked_action instead.
- Do not invent matter numbers, task numbers, dates, users, or emails.
- If uploaded or pasted source material is provided and the user wants a document brief, issue extraction, chronology, or file review, use analyze_source_material.
- For write actions, prepare a draft only. The application will still require explicit user confirmation before writing.
- Use matter_case_workup for integrated case construction, hearing-plan dossiers, issue maps, litigation plans, or war-room style matter analysis.
- Use search_workspace for research and retrieval requests.
- Use matter_strategy for case construction, case theory, witness strategy, hearing prep, or litigation planning.
- Use matter_research for workspace-grounded legal or factual research across the matter file and knowledge base.
- Use matter_chronology for chronology, procedural history, or sequence-of-events requests.
- Use matter_briefing for questions about next steps, current status, upcoming deadlines, or where things stand.
- Use draft_summary for executive or partner summaries.
- Use draft_client_update for client-facing updates or status emails.
- Use draft_portal_reply for replies to a client portal thread, a client message, or the latest inbound client question on the matter.
- Use matter_financial_snapshot for unbilled time, billing status, invoice status, outstanding balances, or "what can I bill" questions.
- Use prepare_workspace_document when the user wants to save or create a collaborative draft, workbench note, memo, brief, outline, or internal working document.
- If the user explicitly asks to create or save a collaborative draft in the workbench or workspace, choose prepare_workspace_document even if the requested title contains phrases like hearing prep, strategy, research memo, or chronology.
- Use prepare_task_bundle when the user asks for a checklist, task plan, task pack, workstream, or multi-step prep list.
- Use concise, explicit, production-safe arguments.
"""


def plan_assistant_request(
    *,
    prompt: str,
    matter_context: dict[str, Any] | None,
    source_context: dict[str, Any] | None = None,
    preferred_output: str = "",
    recent_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    settings = assistant_agent_settings()
    fallback_reason, fallback_detail = _fallback_reason_for_settings(settings)
    if fallback_reason:
        return _fallback_plan_result(settings, reason=fallback_reason, detail=fallback_detail)

    payload = {
        "user_prompt": str(prompt or "").strip(),
        "resolved_matter": matter_context or None,
        "source_material": source_context or None,
        "preferred_output": str(preferred_output or "").strip() or "interactive",
        "recent_history": list(recent_history or [])[:4],
        "capabilities": [
            "analyze_source_material",
            "matter_briefing",
            "matter_case_workup",
            "matter_strategy",
            "matter_research",
            "matter_chronology",
            "draft_summary",
            "draft_client_update",
            "draft_portal_reply",
            "search_workspace",
            "matter_financial_snapshot",
            "prepare_workspace_document",
            "prepare_matter_summary_update",
            "prepare_task_bundle",
            "prepare_task",
            "prepare_task_status_update",
            "prepare_deadline",
            "prepare_note",
            "prepare_party",
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
        response = client.responses.create(
            model=settings["model"],
            reasoning={"effort": settings["reasoning_effort"]},
            parallel_tool_calls=False,
            tool_choice="required",
            max_output_tokens=900,
            instructions=_SYSTEM_PROMPT,
            input=[
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": json.dumps(payload, ensure_ascii=True)}],
                }
            ],
            tools=_assistant_response_tools(),
        )
        tool_name, arguments = _extract_response_tool_call(response)
        if not tool_name:
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
            return _fallback_plan_result(
                settings,
                reason="missing_tool_call",
                detail="OpenAI assistant planning returned no tool call, so deterministic fallback handled the request.",
            )

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
        return _fallback_plan_result(
            settings,
            reason="openai_error",
            detail=str(exc),
        )


__all__ = [
    "assistant_agent_meta",
    "assistant_agent_settings",
    "plan_assistant_request",
]
