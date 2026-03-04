from __future__ import annotations

import json
import re
import time
from typing import Any

from flask import current_app

from ..config import BUDGET_STATUSES, RISK_LEVELS
from .ai_provider import log_ai_operation


_SPACE_RE = re.compile(r"\s+")
_RISK_SET = {value for value in RISK_LEVELS}
_BUDGET_SET = {value for value in BUDGET_STATUSES}


def _clean_text(value: Any, *, limit: int) -> str:
    if value is None:
        return ""
    return _SPACE_RE.sub(" ", str(value)).strip()[: max(1, int(limit))]


def _normalize_risk(value: Any, *, default: str) -> str:
    candidate = _clean_text(value, limit=40)
    if candidate in _RISK_SET:
        return candidate
    return default if default in _RISK_SET else "Medium"


def _normalize_budget_status(value: Any, *, default: str) -> str:
    candidate = _clean_text(value, limit=60)
    if candidate in _BUDGET_SET:
        return candidate
    return default if default in _BUDGET_SET else "On Track"


def _fallback_with_reason(payload: dict[str, Any], *, reason: str, detail: str = "") -> dict[str, Any]:
    output = dict(payload)
    output["source"] = "fallback"
    output["fallback_reason"] = _clean_text(reason, limit=80) or "unknown"
    if detail:
        output["fallback_detail"] = _clean_text(detail, limit=280)
    return output


def _ai_request_settings() -> dict[str, Any]:
    return {
        "provider": str(current_app.config.get("AI_PROVIDER") or "openai").strip().lower(),
        "ai_enabled": bool(current_app.config.get("AI_ENABLED", False)),
        "api_key": (current_app.config.get("AI_OPENAI_API_KEY") or "").strip(),
        "model": str(current_app.config.get("AI_OPENAI_TEXT_MODEL") or "gpt-4o-mini").strip(),
        "timeout_seconds": max(1, int(current_app.config.get("AI_OPENAI_TIMEOUT_SECONDS", 20) or 20)),
        "max_retries": max(0, int(current_app.config.get("AI_OPENAI_MAX_RETRIES", 2) or 2)),
    }


def _fallback_executive_summary(
    *,
    matter_context: dict[str, Any],
    current_values: dict[str, Any],
) -> dict[str, Any]:
    matter_title = _clean_text(matter_context.get("title"), limit=180) or "the matter"
    client_name = _clean_text(matter_context.get("client_name"), limit=180) or "the client"
    status = _clean_text(matter_context.get("status"), limit=40) or "Open"
    open_task_count = int(matter_context.get("open_task_count") or 0)
    overdue_task_count = int(matter_context.get("overdue_task_count") or 0)
    next_due_task = _clean_text(matter_context.get("next_due_task"), limit=220) or "No task deadline captured."
    latest_timeline_title = _clean_text(matter_context.get("latest_timeline_title"), limit=220) or "No timeline milestone logged."

    objective = _clean_text(
        current_values.get("objective") or matter_context.get("objective"),
        limit=900,
    )
    if not objective:
        objective = f"Advance {matter_title} for {client_name} with controlled risk and predictable delivery."

    last_update_note = _clean_text(
        current_values.get("last_update_note") or matter_context.get("last_update_note"),
        limit=900,
    )
    if not last_update_note:
        last_update_note = (
            f"Status remains {status}. Open tasks: {open_task_count}, overdue: {overdue_task_count}. "
            f"Latest milestone: {latest_timeline_title}. Next due item: {next_due_task}."
        )

    outcome_summary = _clean_text(
        current_values.get("outcome_summary") or matter_context.get("outcome_summary"),
        limit=900,
    )
    if not outcome_summary:
        outcome_summary = (
            f"Business outcome focus: keep {matter_title} progressing while reducing execution risk "
            f"and closing upcoming commitments ({next_due_task})."
        )

    risk_level = _normalize_risk(
        current_values.get("risk_level") or matter_context.get("risk_level"),
        default=_normalize_risk(matter_context.get("risk_level"), default="Medium"),
    )
    budget_status = _normalize_budget_status(
        current_values.get("budget_status") or matter_context.get("budget_status"),
        default=_normalize_budget_status(matter_context.get("budget_status"), default="On Track"),
    )
    return {
        "objective": objective,
        "last_update_note": last_update_note,
        "outcome_summary": outcome_summary,
        "risk_level": risk_level,
        "budget_status": budget_status,
        "source": "fallback",
    }


def _normalize_executive_summary(payload: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    objective = _clean_text(payload.get("objective"), limit=900) or _clean_text(fallback.get("objective"), limit=900)
    last_update_note = _clean_text(payload.get("last_update_note"), limit=900) or _clean_text(
        fallback.get("last_update_note"), limit=900
    )
    outcome_summary = _clean_text(payload.get("outcome_summary"), limit=900) or _clean_text(
        fallback.get("outcome_summary"), limit=900
    )
    risk_level = _normalize_risk(payload.get("risk_level"), default=str(fallback.get("risk_level") or "Medium"))
    budget_status = _normalize_budget_status(
        payload.get("budget_status"),
        default=str(fallback.get("budget_status") or "On Track"),
    )
    return {
        "objective": objective,
        "last_update_note": last_update_note,
        "outcome_summary": outcome_summary,
        "risk_level": risk_level,
        "budget_status": budget_status,
    }


def suggest_matter_executive_summary(
    *,
    matter_context: dict[str, Any],
    current_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current_values = current_values or {}
    fallback = _fallback_executive_summary(matter_context=matter_context, current_values=current_values)
    settings = _ai_request_settings()
    request_chars = len(json.dumps(matter_context, ensure_ascii=True)) + len(
        json.dumps(current_values, ensure_ascii=True)
    )
    if not settings["ai_enabled"]:
        return _fallback_with_reason(
            fallback,
            reason="ai_disabled",
            detail="AI is disabled in server configuration.",
        )
    if settings["provider"] != "openai":
        return _fallback_with_reason(
            fallback,
            reason="unsupported_provider",
            detail=f"Configured provider '{settings['provider'] or 'unknown'}' is unsupported for this draft flow.",
        )
    if not settings["api_key"]:
        return _fallback_with_reason(
            fallback,
            reason="missing_api_key",
            detail="OpenAI API key is not configured.",
        )

    started = time.perf_counter()
    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=settings["api_key"],
            timeout=float(settings["timeout_seconds"]),
            max_retries=settings["max_retries"],
        )
        instructions = (
            "You draft legal matter executive summaries. Return strict JSON only with keys: "
            "objective, last_update_note, outcome_summary, risk_level, budget_status. "
            "Use concise business-ready language and avoid introducing unverifiable facts."
        )
        response = client.chat.completions.create(
            model=settings["model"],
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": instructions},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"matter_context": matter_context, "current_values": current_values},
                        ensure_ascii=True,
                    ),
                },
            ],
        )
        content = ""
        if response.choices:
            first = response.choices[0]
            if first and getattr(first, "message", None) is not None:
                content = str(first.message.content or "").strip()
        parsed = json.loads(content) if content else {}
        suggestion = _normalize_executive_summary(parsed if isinstance(parsed, dict) else {}, fallback)
        latency_ms = int((time.perf_counter() - started) * 1000)
        log_ai_operation(
            operation_type="matter_summary_suggest",
            provider="openai",
            model=settings["model"],
            status="ok",
            request_chars=request_chars,
            response_units=len(json.dumps(suggestion, ensure_ascii=True)),
            latency_ms=latency_ms,
            metadata={"matter_no": _clean_text(matter_context.get("matter_no"), limit=80)},
        )
        suggestion["source"] = "openai"
        return suggestion
    except Exception as exc:  # pragma: no cover - provider/network fallback
        latency_ms = int((time.perf_counter() - started) * 1000)
        log_ai_operation(
            operation_type="matter_summary_suggest",
            provider="openai",
            model=settings["model"],
            status="error",
            request_chars=request_chars,
            latency_ms=latency_ms,
            metadata={"matter_no": _clean_text(matter_context.get("matter_no"), limit=80)},
            error_message=str(exc),
        )
        current_app.logger.warning("OpenAI matter summary fallback engaged: %s", exc)
        return _fallback_with_reason(
            fallback,
            reason="openai_error",
            detail=str(exc),
        )


def _fallback_client_update(*, matter_context: dict[str, Any], tone_hint: str) -> dict[str, Any]:
    matter_no = _clean_text(matter_context.get("matter_no"), limit=80) or "Matter"
    matter_title = _clean_text(matter_context.get("title"), limit=200) or "Matter update"
    status = _clean_text(matter_context.get("status"), limit=40) or "Open"
    risk_level = _normalize_risk(matter_context.get("risk_level"), default="Medium")
    budget_status = _normalize_budget_status(matter_context.get("budget_status"), default="On Track")
    open_task_count = int(matter_context.get("open_task_count") or 0)
    overdue_task_count = int(matter_context.get("overdue_task_count") or 0)
    next_due_task = _clean_text(matter_context.get("next_due_task"), limit=220) or "No immediate deadline captured."
    latest_timeline_title = _clean_text(matter_context.get("latest_timeline_title"), limit=220) or "No recent milestone logged."

    subject = f"Update: {matter_no} - {matter_title}"
    body = (
        f"Matter status: {status} (Risk: {risk_level}, Budget: {budget_status}).\n\n"
        f"Progress update: {latest_timeline_title}.\n"
        f"Current task load: {open_task_count} open task(s), {overdue_task_count} overdue.\n\n"
        f"Next step: {next_due_task}\n\n"
        f"Tone: {_clean_text(tone_hint, limit=80) or 'Professional and concise'}."
    )
    return {
        "subject": _clean_text(subject, limit=220),
        "body": _clean_text(body, limit=5000),
        "source": "fallback",
    }


def _normalize_client_update(payload: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    subject = _clean_text(payload.get("subject"), limit=220) or _clean_text(fallback.get("subject"), limit=220)
    body = _clean_text(payload.get("body"), limit=5000) or _clean_text(fallback.get("body"), limit=5000)
    return {"subject": subject, "body": body}


def suggest_matter_client_update(
    *,
    matter_context: dict[str, Any],
    tone_hint: str = "",
) -> dict[str, Any]:
    fallback = _fallback_client_update(matter_context=matter_context, tone_hint=tone_hint)
    settings = _ai_request_settings()
    request_chars = len(json.dumps(matter_context, ensure_ascii=True)) + len(_clean_text(tone_hint, limit=120))
    if not settings["ai_enabled"]:
        return _fallback_with_reason(
            fallback,
            reason="ai_disabled",
            detail="AI is disabled in server configuration.",
        )
    if settings["provider"] != "openai":
        return _fallback_with_reason(
            fallback,
            reason="unsupported_provider",
            detail=f"Configured provider '{settings['provider'] or 'unknown'}' is unsupported for this draft flow.",
        )
    if not settings["api_key"]:
        return _fallback_with_reason(
            fallback,
            reason="missing_api_key",
            detail="OpenAI API key is not configured.",
        )

    started = time.perf_counter()
    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=settings["api_key"],
            timeout=float(settings["timeout_seconds"]),
            max_retries=settings["max_retries"],
        )
        instructions = (
            "You draft short client-facing legal matter updates. Return strict JSON with keys: subject, body. "
            "Body should be concise, plain-English, and suitable for email."
        )
        response = client.chat.completions.create(
            model=settings["model"],
            temperature=0.25,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": instructions},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"matter_context": matter_context, "tone_hint": _clean_text(tone_hint, limit=120)},
                        ensure_ascii=True,
                    ),
                },
            ],
        )
        content = ""
        if response.choices:
            first = response.choices[0]
            if first and getattr(first, "message", None) is not None:
                content = str(first.message.content or "").strip()
        parsed = json.loads(content) if content else {}
        suggestion = _normalize_client_update(parsed if isinstance(parsed, dict) else {}, fallback)
        latency_ms = int((time.perf_counter() - started) * 1000)
        log_ai_operation(
            operation_type="matter_client_update_suggest",
            provider="openai",
            model=settings["model"],
            status="ok",
            request_chars=request_chars,
            response_units=len(json.dumps(suggestion, ensure_ascii=True)),
            latency_ms=latency_ms,
            metadata={"matter_no": _clean_text(matter_context.get("matter_no"), limit=80)},
        )
        suggestion["source"] = "openai"
        return suggestion
    except Exception as exc:  # pragma: no cover - provider/network fallback
        latency_ms = int((time.perf_counter() - started) * 1000)
        log_ai_operation(
            operation_type="matter_client_update_suggest",
            provider="openai",
            model=settings["model"],
            status="error",
            request_chars=request_chars,
            latency_ms=latency_ms,
            metadata={"matter_no": _clean_text(matter_context.get("matter_no"), limit=80)},
            error_message=str(exc),
        )
        current_app.logger.warning("OpenAI client update fallback engaged: %s", exc)
        return _fallback_with_reason(
            fallback,
            reason="openai_error",
            detail=str(exc),
        )


def _fallback_time_narrative(
    *,
    matter_context: dict[str, Any],
    duration_hours: float | None,
    task_code: str,
    activity_code: str,
    current_narrative: str,
) -> dict[str, Any]:
    matter_no = _clean_text(matter_context.get("matter_no"), limit=80) or "matter"
    matter_title = _clean_text(matter_context.get("title"), limit=220) or "client matter"
    objective = _clean_text(matter_context.get("objective"), limit=220)
    hours_text = ""
    if duration_hours is not None and duration_hours > 0:
        hours_text = f" ({round(float(duration_hours), 2)}h)"
    code_text = " / ".join(item for item in [_clean_text(task_code, limit=30), _clean_text(activity_code, limit=30)] if item)
    if code_text:
        code_text = f" [Codes: {code_text}]"
    if current_narrative:
        base = _clean_text(current_narrative, limit=320)
    elif objective:
        base = f"Progressed {matter_no} {matter_title}{hours_text}; advanced objective: {objective}.{code_text}"
    else:
        base = f"Reviewed and progressed {matter_no} {matter_title}{hours_text}; prepared next legal actions.{code_text}"
    return {"narrative": _clean_text(base, limit=320), "source": "fallback"}


def _normalize_time_narrative(payload: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    narrative = _clean_text(payload.get("narrative"), limit=320) or _clean_text(fallback.get("narrative"), limit=320)
    return {"narrative": narrative}


def suggest_time_entry_narrative(
    *,
    matter_context: dict[str, Any],
    duration_hours: float | None,
    task_code: str = "",
    activity_code: str = "",
    current_narrative: str = "",
) -> dict[str, Any]:
    fallback = _fallback_time_narrative(
        matter_context=matter_context,
        duration_hours=duration_hours,
        task_code=task_code,
        activity_code=activity_code,
        current_narrative=current_narrative,
    )
    settings = _ai_request_settings()
    request_chars = len(json.dumps(matter_context, ensure_ascii=True)) + len(
        _clean_text(current_narrative, limit=1000)
    )
    if not settings["ai_enabled"]:
        return _fallback_with_reason(
            fallback,
            reason="ai_disabled",
            detail="AI is disabled in server configuration.",
        )
    if settings["provider"] != "openai":
        return _fallback_with_reason(
            fallback,
            reason="unsupported_provider",
            detail=f"Configured provider '{settings['provider'] or 'unknown'}' is unsupported for this draft flow.",
        )
    if not settings["api_key"]:
        return _fallback_with_reason(
            fallback,
            reason="missing_api_key",
            detail="OpenAI API key is not configured.",
        )

    started = time.perf_counter()
    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=settings["api_key"],
            timeout=float(settings["timeout_seconds"]),
            max_retries=settings["max_retries"],
        )
        instructions = (
            "You draft legal billing time-entry narratives. Return strict JSON with key: narrative. "
            "Use one concise past-tense sentence, 20-260 characters, professional and invoice-safe."
        )
        response = client.chat.completions.create(
            model=settings["model"],
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": instructions},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "matter_context": matter_context,
                            "duration_hours": round(float(duration_hours), 4) if duration_hours else None,
                            "task_code": _clean_text(task_code, limit=40),
                            "activity_code": _clean_text(activity_code, limit=40),
                            "current_narrative": _clean_text(current_narrative, limit=500),
                        },
                        ensure_ascii=True,
                    ),
                },
            ],
        )
        content = ""
        if response.choices:
            first = response.choices[0]
            if first and getattr(first, "message", None) is not None:
                content = str(first.message.content or "").strip()
        parsed = json.loads(content) if content else {}
        suggestion = _normalize_time_narrative(parsed if isinstance(parsed, dict) else {}, fallback)
        latency_ms = int((time.perf_counter() - started) * 1000)
        log_ai_operation(
            operation_type="time_narrative_suggest",
            provider="openai",
            model=settings["model"],
            status="ok",
            request_chars=request_chars,
            response_units=len(suggestion.get("narrative") or ""),
            latency_ms=latency_ms,
            metadata={"matter_no": _clean_text(matter_context.get("matter_no"), limit=80)},
        )
        suggestion["source"] = "openai"
        return suggestion
    except Exception as exc:  # pragma: no cover - provider/network fallback
        latency_ms = int((time.perf_counter() - started) * 1000)
        log_ai_operation(
            operation_type="time_narrative_suggest",
            provider="openai",
            model=settings["model"],
            status="error",
            request_chars=request_chars,
            latency_ms=latency_ms,
            metadata={"matter_no": _clean_text(matter_context.get("matter_no"), limit=80)},
            error_message=str(exc),
        )
        current_app.logger.warning("OpenAI time narrative fallback engaged: %s", exc)
        return _fallback_with_reason(
            fallback,
            reason="openai_error",
            detail=str(exc),
        )

