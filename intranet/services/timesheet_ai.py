from __future__ import annotations

import base64
import json
import re
import time
from typing import Any

from flask import current_app

from .ai_provider import log_ai_operation


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


def _clean_text(value: Any, *, limit: int = 400) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()[: max(1, int(limit))]


def _normalize_bool(value: Any, *, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    candidate = _clean_text(value, limit=20).lower()
    if not candidate:
        return default
    if candidate in {"1", "true", "yes", "on", "billable"}:
        return True
    if candidate in {"0", "false", "no", "off", "non-billable", "non billable"}:
        return False
    return default


def _normalize_hours(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return round(min(parsed, 24.0), 4)


def _normalize_date(value: Any) -> str:
    candidate = _clean_text(value, limit=20)
    if _DATE_RE.match(candidate):
        return candidate
    return ""


def _normalize_time(value: Any) -> str:
    candidate = _clean_text(value, limit=10)
    if _TIME_RE.match(candidate):
        return candidate
    return ""


def _normalize_entries(raw_entries: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_entries, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in raw_entries:
        if not isinstance(item, dict):
            continue
        date_value = _normalize_date(item.get("date"))
        start_time = _normalize_time(item.get("start_time"))
        end_time = _normalize_time(item.get("end_time"))
        hours = _normalize_hours(item.get("hours"))
        narrative = _clean_text(item.get("narrative"), limit=500)
        task_code = _clean_text(item.get("task_code"), limit=40)
        activity_code = _clean_text(item.get("activity_code"), limit=40)
        matter_no = _clean_text(item.get("matter_no"), limit=80).upper()
        matter_ref = _clean_text(item.get("matter_reference"), limit=120)
        matter_id_raw = item.get("matter_id")
        try:
            matter_id = int(matter_id_raw) if matter_id_raw is not None else None
        except (TypeError, ValueError):
            matter_id = None
        if matter_id is not None and matter_id <= 0:
            matter_id = None

        if not narrative and not (task_code or activity_code):
            continue

        normalized.append(
            {
                "matter_no": matter_no,
                "matter_reference": matter_ref,
                "matter_id": matter_id,
                "date": date_value,
                "start_time": start_time,
                "end_time": end_time,
                "hours": hours,
                "narrative": narrative,
                "task_code": task_code,
                "activity_code": activity_code,
                "is_billable": _normalize_bool(item.get("is_billable"), default=True),
            }
        )
        if len(normalized) >= 150:
            break
    return normalized


def _fallback(reason: str, detail: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "entries": [],
        "source": "fallback",
        "fallback_reason": _clean_text(reason, limit=80) or "unknown",
    }
    if detail:
        payload["fallback_detail"] = _clean_text(detail, limit=280)
    return payload


def parse_timesheet_image_entries(
    *,
    image_bytes: bytes,
    mime_type: str,
    filename: str,
) -> dict[str, Any]:
    request_chars = len(image_bytes or b"")
    provider = str(current_app.config.get("AI_PROVIDER") or "openai").strip().lower()
    ai_enabled = bool(current_app.config.get("AI_ENABLED", False))
    api_key = (current_app.config.get("AI_OPENAI_API_KEY") or "").strip()
    model = str(current_app.config.get("AI_OPENAI_TEXT_MODEL") or "gpt-4o-mini").strip()
    timeout_seconds = max(1, int(current_app.config.get("AI_OPENAI_TIMEOUT_SECONDS", 20) or 20))
    max_retries = max(0, int(current_app.config.get("AI_OPENAI_MAX_RETRIES", 2) or 2))

    if not ai_enabled:
        return _fallback("ai_disabled", "AI is disabled in server configuration.")
    if provider != "openai":
        return _fallback(
            "unsupported_provider",
            f"Configured provider '{provider or 'unknown'}' is unsupported for timesheet parsing.",
        )
    if not api_key:
        return _fallback("missing_api_key", "OpenAI API key is not configured.")
    if not image_bytes:
        return _fallback("missing_image", "No image bytes were provided.")

    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    safe_mime = _clean_text(mime_type, limit=80).lower() or "image/jpeg"
    if not safe_mime.startswith("image/"):
        safe_mime = "image/jpeg"
    data_url = f"data:{safe_mime};base64,{image_b64}"
    prompt_payload = {
        "task": "extract_timesheet_rows",
        "filename": _clean_text(filename, limit=180),
        "required_fields": [
            "matter_no",
            "matter_reference",
            "matter_id",
            "date",
            "start_time",
            "end_time",
            "hours",
            "narrative",
            "task_code",
            "activity_code",
            "is_billable",
        ],
        "format_rules": {
            "date": "YYYY-MM-DD",
            "start_time": "HH:MM (24-hour)",
            "end_time": "HH:MM (24-hour)",
            "matter_no": "uppercase if present",
        },
        "notes": "Return only rows that are visible and reasonably legible.",
    }
    instructions = (
        "You extract legal timesheet entries from an uploaded photo. "
        "Return strict JSON with a top-level key 'entries' containing a list of row objects. "
        "Do not invent matter numbers. If a field is unreadable, return an empty string for that field."
    )

    started = time.perf_counter()
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, timeout=float(timeout_seconds), max_retries=max_retries)
        response = client.chat.completions.create(
            model=model,
            temperature=0.0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": instructions},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": json.dumps(prompt_payload, ensure_ascii=True)},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
        )
        content = ""
        if response.choices:
            first = response.choices[0]
            if first and getattr(first, "message", None) is not None:
                content = str(first.message.content or "").strip()
        parsed = json.loads(content) if content else {}
        entries = _normalize_entries(parsed.get("entries") if isinstance(parsed, dict) else None)
        latency_ms = int((time.perf_counter() - started) * 1000)
        result: dict[str, Any] = {
            "entries": entries,
            "source": "openai",
        }
        log_ai_operation(
            operation_type="timesheet_photo_parse",
            provider="openai",
            model=model,
            status="ok",
            request_chars=request_chars,
            response_units=len(entries),
            latency_ms=latency_ms,
            metadata={"filename": _clean_text(filename, limit=120), "rows": len(entries)},
        )
        return result
    except Exception as exc:  # pragma: no cover - provider/network fallback
        latency_ms = int((time.perf_counter() - started) * 1000)
        log_ai_operation(
            operation_type="timesheet_photo_parse",
            provider="openai",
            model=model,
            status="error",
            request_chars=request_chars,
            latency_ms=latency_ms,
            metadata={"filename": _clean_text(filename, limit=120)},
            error_message=str(exc),
        )
        current_app.logger.warning("OpenAI timesheet photo parse fallback engaged: %s", exc)
        return _fallback("openai_error", str(exc))

