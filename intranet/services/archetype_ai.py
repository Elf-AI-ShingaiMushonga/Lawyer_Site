from __future__ import annotations

import json
import re
import time
from typing import Any

from flask import current_app

from .ai_provider import log_ai_operation
from .archetypes import normalize_archetype_field_key


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'/-]{1,}")
_MAX_REQUIRED_FIELDS = 14
_MAX_CHECKLIST_ITEMS = 16


def _title_case_words(value: str) -> str:
    words = _WORD_RE.findall(value or "")
    if not words:
        return ""
    return " ".join(word.capitalize() for word in words[:8])


def _normalize_risk_level(value: str) -> str:
    candidate = (value or "").strip().lower()
    if candidate in {"low", "minor"}:
        return "Low"
    if candidate in {"high", "major", "severe"}:
        return "High"
    if candidate in {"critical", "extreme"}:
        return "Critical"
    return "Medium"


def _normalize_stage(value: str) -> str:
    candidate = (value or "").strip()
    if not candidate:
        return "Intake"
    return candidate[:80]


def _clean_required_fields(raw_fields: Any) -> list[dict[str, str]]:
    if not isinstance(raw_fields, list):
        return []
    cleaned: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_fields:
        if not isinstance(item, dict):
            continue
        key = normalize_archetype_field_key(str(item.get("key") or ""))
        label = str(item.get("label") or "").strip()
        help_text = str(item.get("help") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        cleaned.append(
            {
                "key": key,
                "label": (label or key.replace("_", " ").title())[:120],
                "help": help_text[:240],
            }
        )
        if len(cleaned) >= _MAX_REQUIRED_FIELDS:
            break
    return cleaned


def _clean_checklist(raw_items: Any) -> list[str]:
    if not isinstance(raw_items, list):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = " ".join(str(item or "").split()).strip()
        if len(text) < 3:
            continue
        marker = text.lower()
        if marker in seen:
            continue
        seen.add(marker)
        cleaned.append(text[:180])
        if len(cleaned) >= _MAX_CHECKLIST_ITEMS:
            break
    return cleaned


def _build_fallback_required_fields(prompt: str, legal_category: str) -> list[dict[str, str]]:
    source = f"{prompt} {legal_category}".lower()
    fields: list[dict[str, str]] = []

    def add_field(key: str, label: str, help_text: str = "") -> None:
        normalized = normalize_archetype_field_key(key)
        if not normalized or any(item["key"] == normalized for item in fields):
            return
        fields.append({"key": normalized, "label": label, "help": help_text})

    add_field("counterparty_name", "Counterparty Name", "Primary opposing party or respondent.")
    add_field("matter_summary", "Matter Summary", "Key facts to insert into the clause.")

    if any(token in source for token in ["employee", "labour", "employment", "dismissal", "union"]):
        add_field("employee_name", "Employee Name")
        add_field("employee_role", "Employee Role")
        add_field("incident_date", "Incident Date")
    if any(token in source for token in ["contract", "agreement", "service level", "sla"]):
        add_field("contract_start_date", "Contract Start Date")
        add_field("contract_end_date", "Contract End Date")
        add_field("termination_notice_period", "Termination Notice Period")
    if any(token in source for token in ["damages", "amount", "fee", "payment", "compensation", "penalty"]):
        add_field("claim_amount", "Claim Amount")
        add_field("currency", "Currency")
    if any(token in source for token in ["privacy", "data", "popia", "gdpr"]):
        add_field("data_controller", "Data Controller")
        add_field("data_purpose", "Data Processing Purpose")

    return fields[:_MAX_REQUIRED_FIELDS]


def _fallback_suggestion(prompt: str, *, legal_category_hint: str, name_hint: str) -> dict[str, Any]:
    legal_category = (legal_category_hint or "").strip() or "General Legal"
    base_name = (name_hint or "").strip() or _title_case_words(prompt) or "General Clause"
    if not base_name.lower().endswith("archetype") and "clause" not in base_name.lower():
        base_name = f"{base_name} Clause"

    required_fields = _build_fallback_required_fields(prompt, legal_category)
    checklist = [
        "Validate the legal category and jurisdiction for this matter.",
        "Confirm all archetype required fields are completed and factual.",
        "Review wording for risk, liability, and enforceability.",
        "Obtain internal sign-off before finalizing client-facing output.",
    ]
    merge_examples = ", ".join(f"{{{{{field['key']}}}}}" for field in required_fields[:4]) or "{{matter_summary}}"
    boilerplate = (
        "This clause is issued for matter {{matter_no}} on behalf of {{client_name}} within {{legal_category}}.\n\n"
        "Context:\n{{matter_summary}}\n\n"
        "Matter-specific values to complete:\n"
        f"{merge_examples}\n\n"
        "Drafted for stage {{status}} and aligned to {{risk_level}} risk controls."
    )

    return {
        "name": base_name[:120],
        "legal_category": legal_category[:120],
        "practice_area": legal_category[:120],
        "default_stage": "Intake",
        "default_risk_level": "Medium",
        "required_fields": required_fields,
        "checklist": checklist,
        "boilerplate_template": boilerplate[:12000],
        "source": "fallback",
    }


def _normalize_suggestion(payload: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip() or str(fallback.get("name") or "Generated Clause")
    legal_category = (
        str(payload.get("legal_category") or "").strip()
        or str(fallback.get("legal_category") or "General Legal")
    )
    practice_area = str(payload.get("practice_area") or "").strip() or legal_category
    default_stage = _normalize_stage(str(payload.get("default_stage") or fallback.get("default_stage") or "Intake"))
    default_risk_level = _normalize_risk_level(
        str(payload.get("default_risk_level") or fallback.get("default_risk_level") or "Medium")
    )
    required_fields = _clean_required_fields(payload.get("required_fields")) or fallback.get("required_fields", [])
    checklist = _clean_checklist(payload.get("checklist")) or fallback.get("checklist", [])
    boilerplate_template = (
        str(payload.get("boilerplate_template") or "").strip()
        or str(fallback.get("boilerplate_template") or "").strip()
    )
    return {
        "name": name[:120],
        "legal_category": legal_category[:120],
        "practice_area": practice_area[:120],
        "default_stage": default_stage,
        "default_risk_level": default_risk_level,
        "required_fields": required_fields[:_MAX_REQUIRED_FIELDS],
        "checklist": checklist[:_MAX_CHECKLIST_ITEMS],
        "boilerplate_template": boilerplate_template[:12000],
    }


def _fallback_with_reason(
    fallback: dict[str, Any],
    *,
    reason: str,
    detail: str = "",
) -> dict[str, Any]:
    payload = dict(fallback)
    payload["source"] = "fallback"
    payload["fallback_reason"] = " ".join(str(reason or "").split()).strip() or "unknown"
    if detail:
        payload["fallback_detail"] = " ".join(str(detail).split()).strip()[:280]
    return payload


def suggest_matter_archetype(*, prompt: str, legal_category_hint: str = "", name_hint: str = "") -> dict[str, Any]:
    prompt_clean = " ".join((prompt or "").split()).strip()
    legal_category_hint = " ".join((legal_category_hint or "").split()).strip()
    name_hint = " ".join((name_hint or "").split()).strip()

    fallback = _fallback_suggestion(prompt_clean, legal_category_hint=legal_category_hint, name_hint=name_hint)
    provider = str(current_app.config.get("AI_PROVIDER") or "openai").strip().lower()
    ai_enabled = bool(current_app.config.get("AI_ENABLED", False))
    api_key = (current_app.config.get("AI_OPENAI_API_KEY") or "").strip()
    model = str(current_app.config.get("AI_OPENAI_TEXT_MODEL") or "gpt-4o-mini").strip()
    timeout_seconds = max(1, int(current_app.config.get("AI_OPENAI_TIMEOUT_SECONDS", 20) or 20))
    max_retries = max(0, int(current_app.config.get("AI_OPENAI_MAX_RETRIES", 2) or 2))
    request_chars = len(prompt_clean) + len(legal_category_hint) + len(name_hint)

    if not ai_enabled:
        return _fallback_with_reason(
            fallback,
            reason="ai_disabled",
            detail="AI is disabled in server configuration.",
        )
    if provider != "openai":
        return _fallback_with_reason(
            fallback,
            reason="unsupported_provider",
            detail=f"Configured provider '{provider or 'unknown'}' is unsupported for this draft flow.",
        )
    if not api_key:
        return _fallback_with_reason(
            fallback,
            reason="missing_api_key",
            detail="OpenAI API key is not configured.",
        )

    started = time.perf_counter()
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, timeout=float(timeout_seconds), max_retries=max_retries)
        instructions = (
            "You generate legal matter archetype drafts. Return strict JSON with keys: "
            "name, legal_category, practice_area, default_stage, default_risk_level, required_fields, checklist, boilerplate_template. "
            "required_fields must be a list of objects with key,label,help. "
            "boilerplate_template must include merge fields like {{matter_no}}, {{client_name}} and required field keys."
        )
        user_payload = {
            "prompt": prompt_clean,
            "legal_category_hint": legal_category_hint,
            "name_hint": name_hint,
            "max_required_fields": _MAX_REQUIRED_FIELDS,
            "max_checklist_items": _MAX_CHECKLIST_ITEMS,
        }
        response = client.chat.completions.create(
            model=model,
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": instructions},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=True)},
            ],
        )
        content = ""
        if response.choices:
            first = response.choices[0]
            if first and getattr(first, "message", None) is not None:
                content = str(first.message.content or "").strip()
        parsed = json.loads(content) if content else {}
        suggestion = _normalize_suggestion(parsed if isinstance(parsed, dict) else {}, fallback)
        latency_ms = int((time.perf_counter() - started) * 1000)
        log_ai_operation(
            operation_type="archetype_suggest",
            provider="openai",
            model=model,
            status="ok",
            request_chars=request_chars,
            response_units=len(json.dumps(suggestion, ensure_ascii=True)),
            latency_ms=latency_ms,
            metadata={"required_fields": len(suggestion["required_fields"]), "checklist": len(suggestion["checklist"])},
        )
        suggestion["source"] = "openai"
        return suggestion
    except Exception as exc:  # pragma: no cover - provider/network fallback
        latency_ms = int((time.perf_counter() - started) * 1000)
        log_ai_operation(
            operation_type="archetype_suggest",
            provider="openai",
            model=model,
            status="error",
            request_chars=request_chars,
            latency_ms=latency_ms,
            metadata={},
            error_message=str(exc),
        )
        current_app.logger.warning("OpenAI archetype suggestion fallback engaged: %s", exc)
        return _fallback_with_reason(
            fallback,
            reason="openai_error",
            detail=str(exc),
        )
