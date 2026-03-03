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


def _title_case_words(value: str, *, limit: int = 8) -> str:
    words = _WORD_RE.findall(value or "")
    if not words:
        return ""
    return " ".join(word.capitalize() for word in words[: max(1, limit)])


def _normalize_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    candidate = str(value).strip().lower()
    if candidate in {"1", "true", "yes", "on"}:
        return True
    if candidate in {"0", "false", "no", "off"}:
        return False
    return default


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


def _fallback_with_reason(
    payload: dict[str, Any],
    *,
    reason: str,
    detail: str = "",
) -> dict[str, Any]:
    out = dict(payload)
    out["source"] = "fallback"
    out["fallback_reason"] = " ".join(str(reason or "").split()).strip() or "unknown"
    if detail:
        out["fallback_detail"] = " ".join(str(detail).split()).strip()[:280]
    return out


def _build_contract_required_fields(prompt: str, legal_category: str) -> list[dict[str, str]]:
    source = f"{prompt} {legal_category}".lower()
    fields: list[dict[str, str]] = []

    def add_field(key: str, label: str, help_text: str = "") -> None:
        normalized = normalize_archetype_field_key(key)
        if not normalized or any(item["key"] == normalized for item in fields):
            return
        fields.append({"key": normalized, "label": label, "help": help_text})

    add_field("counterparty_name", "Counterparty Name", "Primary external party name.")
    add_field("effective_date", "Effective Date", "Date on which this contract starts.")

    if any(token in source for token in ["fee", "payment", "invoice", "amount", "salary", "compensation"]):
        add_field("fee_amount", "Fee Amount")
        add_field("currency", "Currency")
        add_field("payment_due_days", "Payment Due (Days)")
    if any(token in source for token in ["employee", "employment", "labour", "termination"]):
        add_field("employee_name", "Employee Name")
        add_field("position_title", "Position Title")
        add_field("notice_period", "Notice Period")
    if any(token in source for token in ["nda", "confidential", "confidentiality", "ip"]):
        add_field("confidential_information_scope", "Confidential Information Scope")
        add_field("confidentiality_term", "Confidentiality Term")

    return fields[:_MAX_REQUIRED_FIELDS]


def _fallback_contract_template(
    prompt: str,
    *,
    legal_category_hint: str,
    name_hint: str,
    contract_type_hint: str,
) -> dict[str, Any]:
    legal_category = (legal_category_hint or "").strip() or "General Legal"
    contract_type = (contract_type_hint or "").strip() or "Contract"
    base_name = (name_hint or "").strip() or _title_case_words(prompt) or f"Standard {contract_type}"
    if "template" not in base_name.lower():
        base_name = f"{base_name} Template"

    required_fields = _build_contract_required_fields(prompt, legal_category)
    merge_examples = ", ".join(f"{{{{{item['key']}}}}}" for item in required_fields[:4]) or "{{counterparty_name}}"
    body = (
        "{{contract_type}} between {{client_name}} and {{counterparty_name}} for matter {{matter_no}}.\n\n"
        "Legal category: {{legal_category}}\n"
        "Effective date: {{effective_date}}\n\n"
        "Matter-specific values:\n"
        f"{merge_examples}\n\n"
        "This agreement is drafted subject to applicable law and internal approval controls."
    )

    return {
        "name": base_name[:120],
        "legal_category": legal_category[:120],
        "contract_type": contract_type[:80],
        "required_fields": required_fields,
        "body": body[:12000],
        "requires_signature": True,
        "auto_create_on_matter_open": True,
        "is_active": True,
        "source": "fallback",
    }


def _normalize_contract_suggestion(payload: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip() or str(fallback.get("name") or "Generated Contract Template")
    legal_category = str(payload.get("legal_category") or "").strip() or str(fallback.get("legal_category") or "")
    contract_type = (
        str(payload.get("contract_type") or "").strip()
        or str(fallback.get("contract_type") or "Contract")
        or "Contract"
    )
    required_fields = _clean_required_fields(payload.get("required_fields")) or list(fallback.get("required_fields") or [])
    body = str(payload.get("body") or "").strip() or str(fallback.get("body") or "").strip()
    return {
        "name": name[:120],
        "legal_category": legal_category[:120],
        "contract_type": contract_type[:80],
        "required_fields": required_fields[:_MAX_REQUIRED_FIELDS],
        "body": body[:12000],
        "requires_signature": _normalize_bool(
            payload.get("requires_signature"),
            default=bool(fallback.get("requires_signature", True)),
        ),
        "auto_create_on_matter_open": _normalize_bool(
            payload.get("auto_create_on_matter_open"),
            default=bool(fallback.get("auto_create_on_matter_open", True)),
        ),
        "is_active": _normalize_bool(
            payload.get("is_active"),
            default=bool(fallback.get("is_active", True)),
        ),
    }


def suggest_contract_template(
    *,
    prompt: str,
    legal_category_hint: str = "",
    name_hint: str = "",
    contract_type_hint: str = "",
) -> dict[str, Any]:
    prompt_clean = " ".join((prompt or "").split()).strip()
    legal_category_hint = " ".join((legal_category_hint or "").split()).strip()
    name_hint = " ".join((name_hint or "").split()).strip()
    contract_type_hint = " ".join((contract_type_hint or "").split()).strip()
    fallback = _fallback_contract_template(
        prompt_clean,
        legal_category_hint=legal_category_hint,
        name_hint=name_hint,
        contract_type_hint=contract_type_hint,
    )

    provider = str(current_app.config.get("AI_PROVIDER") or "openai").strip().lower()
    ai_enabled = bool(current_app.config.get("AI_ENABLED", False))
    api_key = (current_app.config.get("AI_OPENAI_API_KEY") or "").strip()
    model = str(current_app.config.get("AI_OPENAI_TEXT_MODEL") or "gpt-4o-mini").strip()
    timeout_seconds = max(1, int(current_app.config.get("AI_OPENAI_TIMEOUT_SECONDS", 20) or 20))
    max_retries = max(0, int(current_app.config.get("AI_OPENAI_MAX_RETRIES", 2) or 2))
    request_chars = len(prompt_clean) + len(legal_category_hint) + len(name_hint) + len(contract_type_hint)

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
            "You generate legal contract template drafts. Return strict JSON with keys: "
            "name, legal_category, contract_type, required_fields, body, requires_signature, "
            "auto_create_on_matter_open, is_active. "
            "required_fields must be a list of objects with key,label,help. "
            "body must include merge fields like {{matter_no}} and {{client_name}}."
        )
        user_payload = {
            "prompt": prompt_clean,
            "legal_category_hint": legal_category_hint,
            "name_hint": name_hint,
            "contract_type_hint": contract_type_hint,
            "max_required_fields": _MAX_REQUIRED_FIELDS,
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
        suggestion = _normalize_contract_suggestion(parsed if isinstance(parsed, dict) else {}, fallback)
        latency_ms = int((time.perf_counter() - started) * 1000)
        log_ai_operation(
            operation_type="contract_template_suggest",
            provider="openai",
            model=model,
            status="ok",
            request_chars=request_chars,
            response_units=len(json.dumps(suggestion, ensure_ascii=True)),
            latency_ms=latency_ms,
            metadata={"required_fields": len(suggestion["required_fields"])},
        )
        suggestion["source"] = "openai"
        return suggestion
    except Exception as exc:  # pragma: no cover - provider/network fallback
        latency_ms = int((time.perf_counter() - started) * 1000)
        log_ai_operation(
            operation_type="contract_template_suggest",
            provider="openai",
            model=model,
            status="error",
            request_chars=request_chars,
            latency_ms=latency_ms,
            metadata={},
            error_message=str(exc),
        )
        current_app.logger.warning("OpenAI contract template suggestion fallback engaged: %s", exc)
        return _fallback_with_reason(
            fallback,
            reason="openai_error",
            detail=str(exc),
        )


def _fallback_document_template(
    prompt: str,
    *,
    name_hint: str,
    template_type_hint: str,
) -> dict[str, Any]:
    template_type = (template_type_hint or "").strip() or "general"
    base_name = (name_hint or "").strip() or _title_case_words(prompt, limit=10) or "General Legal Document"
    if "template" not in base_name.lower():
        base_name = f"{base_name} Template"

    body = (
        "Document for matter {{matter_no}} ({{matter_title}}) on behalf of {{client_name}}.\n\n"
        "Jurisdiction: {{jurisdiction}}\n"
        "Stage: {{stage}}\n"
        "Date: {{today}}\n\n"
        "Background:\n{{matter_summary}}\n\n"
        "Prepared by {{generated_by_name}}."
    )
    return {
        "name": base_name[:120],
        "template_type": template_type[:80],
        "body": body[:12000],
        "requires_signature": False,
        "source": "fallback",
    }


def _normalize_document_suggestion(payload: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip() or str(fallback.get("name") or "Generated Document Template")
    template_type = (
        str(payload.get("template_type") or "").strip()
        or str(fallback.get("template_type") or "general")
        or "general"
    )
    body = str(payload.get("body") or "").strip() or str(fallback.get("body") or "").strip()
    return {
        "name": name[:120],
        "template_type": template_type[:80],
        "body": body[:12000],
        "requires_signature": _normalize_bool(
            payload.get("requires_signature"),
            default=bool(fallback.get("requires_signature", False)),
        ),
    }


def suggest_document_template(
    *,
    prompt: str,
    name_hint: str = "",
    template_type_hint: str = "",
) -> dict[str, Any]:
    prompt_clean = " ".join((prompt or "").split()).strip()
    name_hint = " ".join((name_hint or "").split()).strip()
    template_type_hint = " ".join((template_type_hint or "").split()).strip()
    fallback = _fallback_document_template(
        prompt_clean,
        name_hint=name_hint,
        template_type_hint=template_type_hint,
    )

    provider = str(current_app.config.get("AI_PROVIDER") or "openai").strip().lower()
    ai_enabled = bool(current_app.config.get("AI_ENABLED", False))
    api_key = (current_app.config.get("AI_OPENAI_API_KEY") or "").strip()
    model = str(current_app.config.get("AI_OPENAI_TEXT_MODEL") or "gpt-4o-mini").strip()
    timeout_seconds = max(1, int(current_app.config.get("AI_OPENAI_TIMEOUT_SECONDS", 20) or 20))
    max_retries = max(0, int(current_app.config.get("AI_OPENAI_MAX_RETRIES", 2) or 2))
    request_chars = len(prompt_clean) + len(name_hint) + len(template_type_hint)

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
            "You generate legal document template drafts. Return strict JSON with keys: "
            "name, template_type, body, requires_signature. "
            "body must include merge fields like {{matter_no}} and {{client_name}}."
        )
        user_payload = {
            "prompt": prompt_clean,
            "name_hint": name_hint,
            "template_type_hint": template_type_hint,
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
        suggestion = _normalize_document_suggestion(parsed if isinstance(parsed, dict) else {}, fallback)
        latency_ms = int((time.perf_counter() - started) * 1000)
        log_ai_operation(
            operation_type="document_template_suggest",
            provider="openai",
            model=model,
            status="ok",
            request_chars=request_chars,
            response_units=len(json.dumps(suggestion, ensure_ascii=True)),
            latency_ms=latency_ms,
            metadata={},
        )
        suggestion["source"] = "openai"
        return suggestion
    except Exception as exc:  # pragma: no cover - provider/network fallback
        latency_ms = int((time.perf_counter() - started) * 1000)
        log_ai_operation(
            operation_type="document_template_suggest",
            provider="openai",
            model=model,
            status="error",
            request_chars=request_chars,
            latency_ms=latency_ms,
            metadata={},
            error_message=str(exc),
        )
        current_app.logger.warning("OpenAI document template suggestion fallback engaged: %s", exc)
        return _fallback_with_reason(
            fallback,
            reason="openai_error",
            detail=str(exc),
        )

