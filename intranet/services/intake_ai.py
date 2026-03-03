from __future__ import annotations

import json
import re
import time
from typing import Any

from flask import current_app

from ..models import MatterTemplate
from .ai_provider import log_ai_operation
from .archetypes import load_required_fields, normalize_archetype_field_key


_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9&.\-]{1,}")
_MATTER_NO_RE = re.compile(r"\b\d{4}-[A-Z]{2,8}-\d{2,8}\b", re.IGNORECASE)
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b")
_AMOUNT_RE = re.compile(r"\b(?:R|ZAR|USD|\$|EUR)?\s?\d{1,3}(?:[,\s]\d{3})*(?:\.\d{2})?\b", re.IGNORECASE)
_COUNTERPARTY_RE = re.compile(r"\b(?:against|versus|vs\.?|v\.?)\s+([A-Z][A-Za-z0-9&.,'()\- ]{2,120})")
_CLIENT_RE = re.compile(r"\b(?:for|client[:\s])\s+([A-Z][A-Za-z0-9&.,'()\- ]{2,120})")

_RISK_LEVELS = {"low": "Low", "medium": "Medium", "high": "High", "critical": "Critical"}
_BUDGET_STATUSES = {
    "on track": "On Track",
    "watch": "Watch",
    "over budget": "Over Budget",
    "needs review": "Needs Review",
}

_LEGAL_CATEGORY_KEYWORDS: list[tuple[str, set[str]]] = [
    ("Labour Law", {"labour", "employment", "employee", "disciplinary", "dismissal", "union"}),
    ("Commercial Law", {"commercial", "contract", "supplier", "procurement", "agreement"}),
    ("Property Law", {"property", "conveyancing", "lease", "tenant", "landlord"}),
    ("Family Law", {"family", "divorce", "custody", "maintenance", "spouse"}),
    ("Criminal Law", {"criminal", "prosecution", "bail", "charge", "offence"}),
    ("Litigation", {"litigation", "summons", "claim", "damages", "negligence", "dispute"}),
    ("Data Privacy", {"privacy", "popia", "gdpr", "breach", "data"}),
]


def _tokenize(value: str) -> set[str]:
    if not value:
        return set()
    return {token for token in _TOKEN_RE.findall(value.lower()) if token}


def _normalize_budget_status(value: str) -> str:
    candidate = (value or "").strip().lower()
    return _BUDGET_STATUSES.get(candidate, "On Track")


def _normalize_risk_level(value: str) -> str:
    candidate = (value or "").strip().lower()
    return _RISK_LEVELS.get(candidate, "Medium")


def _normalize_stage(value: str) -> str:
    candidate = " ".join((value or "").split()).strip()
    return candidate[:80] if candidate else "Intake"


def _infer_legal_category(prompt: str) -> str:
    tokens = _tokenize(prompt)
    best = ""
    best_score = 0
    for category, keywords in _LEGAL_CATEGORY_KEYWORDS:
        score = len(tokens.intersection(keywords))
        if score > best_score:
            best = category
            best_score = score
    return best or "General Legal"


def _infer_stage(prompt: str) -> str:
    lower = prompt.lower()
    if any(token in lower for token in ("appeal", "hearing", "trial", "summons")):
        return "Litigation"
    if any(token in lower for token in ("settle", "settlement", "mediation")):
        return "Settlement"
    if any(token in lower for token in ("draft", "negotiate", "negotiation", "contract")):
        return "Drafting"
    return "Intake"


def _extract_title(prompt: str) -> str:
    cleaned = " ".join(prompt.split())
    if not cleaned:
        return ""
    sentence = cleaned.split(".", 1)[0].strip()
    if len(sentence) > 120:
        sentence = sentence[:120].rsplit(" ", 1)[0]
    return sentence


def _extract_client_name(prompt: str) -> str:
    match = _CLIENT_RE.search(prompt)
    if match:
        return " ".join(match.group(1).split())[:255]
    return ""


def _extract_counterparty_name(prompt: str) -> str:
    match = _COUNTERPARTY_RE.search(prompt)
    if match:
        return " ".join(match.group(1).split())[:255]
    return ""


def _extract_date(prompt: str) -> str:
    match = _DATE_RE.search(prompt)
    return (match.group(1) if match else "").strip()[:40]


def _extract_amount(prompt: str) -> str:
    match = _AMOUNT_RE.search(prompt)
    if not match:
        return ""
    value = " ".join(match.group(0).split())
    return value[:60]


def _build_template_catalog(templates: list[MatterTemplate]) -> list[dict[str, Any]]:
    catalog: list[dict[str, Any]] = []
    for row in templates:
        required_fields = load_required_fields(row.required_fields_json)
        catalog.append(
            {
                "id": int(row.id),
                "name": row.name or "",
                "legal_category": row.legal_category or "",
                "required_fields": required_fields,
            }
        )
    return catalog


def _template_score(*, prompt_tokens: set[str], template: dict[str, Any], legal_category: str, name_hint: str) -> int:
    text_tokens = _tokenize(
        " ".join(
            [
                str(template.get("name") or ""),
                str(template.get("legal_category") or ""),
                " ".join(str(field.get("label") or field.get("key") or "") for field in (template.get("required_fields") or [])),
            ]
        )
    )
    overlap_score = len(prompt_tokens.intersection(text_tokens))
    category_score = 3 if legal_category and legal_category.lower() == str(template.get("legal_category") or "").lower() else 0
    hint_score = 2 if name_hint and name_hint.lower() in str(template.get("name") or "").lower() else 0
    return overlap_score + category_score + hint_score


def _select_template(
    *,
    prompt: str,
    legal_category: str,
    templates: list[dict[str, Any]],
    template_id_hint: Any = None,
    template_name_hint: str = "",
) -> dict[str, Any] | None:
    if not templates:
        return None

    try:
        template_id = int(template_id_hint)
    except (TypeError, ValueError):
        template_id = 0
    if template_id > 0:
        for row in templates:
            if int(row.get("id") or 0) == template_id:
                return row

    if template_name_hint:
        candidate = template_name_hint.strip().lower()
        for row in templates:
            if candidate and candidate == str(row.get("name") or "").strip().lower():
                return row

    prompt_tokens = _tokenize(prompt)
    scored = sorted(
        (
            (_template_score(prompt_tokens=prompt_tokens, template=row, legal_category=legal_category, name_hint=template_name_hint), row)
            for row in templates
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    best_score, best_row = scored[0]
    if best_score > 0:
        return best_row

    category_matches = [
        row for row in templates if legal_category and legal_category.lower() == str(row.get("legal_category") or "").lower()
    ]
    if len(category_matches) == 1:
        return category_matches[0]
    return None


def _normalize_required_values(raw: Any, template: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(raw, dict) or not template:
        return {}
    allowed_keys = {
        normalize_archetype_field_key(str(field.get("key") or ""))
        for field in (template.get("required_fields") or [])
    }
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        field_key = normalize_archetype_field_key(str(key))
        if not field_key or field_key not in allowed_keys:
            continue
        text_value = " ".join(str(value or "").split()).strip()
        if not text_value:
            continue
        normalized[field_key] = text_value[:240]
    return normalized


def _fallback_required_values(prompt: str, template: dict[str, Any] | None, defaults: dict[str, str]) -> dict[str, str]:
    if not template:
        return {}
    date_value = _extract_date(prompt)
    amount_value = _extract_amount(prompt)
    counterparty = defaults.get("counterparty_name") or _extract_counterparty_name(prompt)
    description = defaults.get("description") or prompt

    values: dict[str, str] = {}
    for field in template.get("required_fields") or []:
        key = normalize_archetype_field_key(str(field.get("key") or ""))
        label = str(field.get("label") or "").lower()
        joined = f"{key} {label}"
        if not key:
            continue
        if "date" in joined and date_value:
            values[key] = date_value
            continue
        if any(token in joined for token in ("amount", "damages", "fee", "value", "penalty", "cost")) and amount_value:
            values[key] = amount_value
            continue
        if any(token in joined for token in ("counterparty", "respondent", "defendant", "opponent", "opposing")) and counterparty:
            values[key] = counterparty
            continue
        if "client" in joined and defaults.get("client_name"):
            values[key] = defaults["client_name"][:240]
            continue
        if any(token in joined for token in ("summary", "facts", "description", "objective")):
            values[key] = description[:240]
            continue
        if "matter_no" in joined and defaults.get("matter_no"):
            values[key] = defaults["matter_no"][:240]
            continue
    return values


def _fallback_parse(prompt: str, templates: list[dict[str, Any]]) -> dict[str, Any]:
    cleaned = " ".join((prompt or "").split()).strip()
    matter_no_match = _MATTER_NO_RE.search(cleaned)
    legal_category = _infer_legal_category(cleaned)

    risk_level = "Medium"
    lower = cleaned.lower()
    if any(token in lower for token in ("urgent", "critical", "severe", "immediate")):
        risk_level = "High"
    if any(token in lower for token in ("catastrophic", "extreme")):
        risk_level = "Critical"
    if any(token in lower for token in ("low risk", "minor")):
        risk_level = "Low"

    budget_status = "On Track"
    if any(token in lower for token in ("over budget", "exceeded budget")):
        budget_status = "Over Budget"
    elif any(token in lower for token in ("review budget", "budget risk")):
        budget_status = "Watch"

    defaults = {
        "matter_no": (matter_no_match.group(0).upper() if matter_no_match else ""),
        "title": _extract_title(cleaned) or "New Matter Intake",
        "client_name": _extract_client_name(cleaned),
        "legal_category": legal_category,
        "jurisdiction": "ZA" if any(token in lower for token in ("south africa", "za", "cape town", "johannesburg")) else "ZA",
        "stage": _infer_stage(cleaned),
        "practice_area": legal_category,
        "case_type": "General",
        "description": cleaned[:2000],
        "objective": _extract_title(cleaned)[:500],
        "risk_level": risk_level,
        "budget_status": budget_status,
        "counterparty_name": _extract_counterparty_name(cleaned),
    }

    selected = _select_template(prompt=cleaned, legal_category=legal_category, templates=templates)
    required_values = _fallback_required_values(cleaned, selected, defaults)
    return {
        "matter_no": defaults["matter_no"],
        "title": defaults["title"][:255],
        "client_name": defaults["client_name"][:255],
        "legal_category": defaults["legal_category"][:120],
        "template_id": int(selected["id"]) if selected else None,
        "template_name": str(selected.get("name") or "")[:120] if selected else "",
        "jurisdiction": defaults["jurisdiction"][:40],
        "stage": defaults["stage"][:80],
        "practice_area": defaults["practice_area"][:120],
        "case_type": defaults["case_type"][:120],
        "description": defaults["description"][:2000],
        "objective": defaults["objective"][:800],
        "risk_level": defaults["risk_level"],
        "budget_status": defaults["budget_status"],
        "archetype_required_values": required_values,
        "source": "fallback",
    }


def _normalize_suggestion(payload: dict[str, Any], fallback: dict[str, Any], templates: list[dict[str, Any]], prompt: str) -> dict[str, Any]:
    legal_category = " ".join(str(payload.get("legal_category") or fallback.get("legal_category") or "").split()).strip()[:120]
    template = _select_template(
        prompt=prompt,
        legal_category=legal_category,
        templates=templates,
        template_id_hint=payload.get("template_id"),
        template_name_hint=str(payload.get("template_name") or ""),
    )
    required_values = _normalize_required_values(payload.get("archetype_required_values"), template)
    if not required_values:
        required_values = _fallback_required_values(prompt, template, fallback)

    risk_level = _normalize_risk_level(str(payload.get("risk_level") or fallback.get("risk_level") or "Medium"))
    budget_status = _normalize_budget_status(str(payload.get("budget_status") or fallback.get("budget_status") or "On Track"))
    stage = _normalize_stage(str(payload.get("stage") or fallback.get("stage") or "Intake"))

    return {
        "matter_no": " ".join(str(payload.get("matter_no") or fallback.get("matter_no") or "").split()).strip().upper()[:80],
        "title": " ".join(str(payload.get("title") or fallback.get("title") or "").split()).strip()[:255],
        "client_name": " ".join(str(payload.get("client_name") or fallback.get("client_name") or "").split()).strip()[:255],
        "legal_category": legal_category,
        "template_id": int(template["id"]) if template else None,
        "template_name": str(template.get("name") or "")[:120] if template else "",
        "jurisdiction": " ".join(
            str(payload.get("jurisdiction") or fallback.get("jurisdiction") or "ZA").split()
        ).strip()[:40],
        "stage": stage,
        "practice_area": " ".join(
            str(payload.get("practice_area") or fallback.get("practice_area") or legal_category).split()
        ).strip()[:120],
        "case_type": " ".join(str(payload.get("case_type") or fallback.get("case_type") or "General").split()).strip()[:120],
        "description": str(payload.get("description") or fallback.get("description") or "").strip()[:2000],
        "objective": str(payload.get("objective") or fallback.get("objective") or "").strip()[:800],
        "risk_level": risk_level,
        "budget_status": budget_status,
        "archetype_required_values": required_values,
    }


def suggest_matter_intake(*, prompt: str, templates: list[MatterTemplate]) -> dict[str, Any]:
    cleaned_prompt = " ".join((prompt or "").split()).strip()
    template_catalog = _build_template_catalog(templates)
    fallback = _fallback_parse(cleaned_prompt, template_catalog)

    provider = str(current_app.config.get("AI_PROVIDER") or "openai").strip().lower()
    ai_enabled = bool(current_app.config.get("AI_ENABLED", False))
    api_key = (current_app.config.get("AI_OPENAI_API_KEY") or "").strip()
    model = str(current_app.config.get("AI_OPENAI_TEXT_MODEL") or "gpt-4o-mini").strip()
    timeout_seconds = max(1, int(current_app.config.get("AI_OPENAI_TIMEOUT_SECONDS", 20) or 20))
    max_retries = max(0, int(current_app.config.get("AI_OPENAI_MAX_RETRIES", 2) or 2))
    request_chars = len(cleaned_prompt)

    if ai_enabled and provider == "openai" and api_key:
        started = time.perf_counter()
        try:
            from openai import OpenAI

            client = OpenAI(api_key=api_key, timeout=float(timeout_seconds), max_retries=max_retries)
            instructions = (
                "You parse legal matter intake text into structured JSON. "
                "Return only JSON with keys: matter_no, title, client_name, legal_category, template_id, template_name, "
                "jurisdiction, stage, practice_area, case_type, description, objective, risk_level, budget_status, "
                "archetype_required_values. Use template_id only from provided templates."
            )
            user_payload = {
                "prompt": cleaned_prompt,
                "templates": template_catalog,
                "allowed_risk_levels": sorted(set(_RISK_LEVELS.values())),
                "allowed_budget_statuses": sorted(set(_BUDGET_STATUSES.values())),
            }
            response = client.chat.completions.create(
                model=model,
                temperature=0.1,
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
            normalized = _normalize_suggestion(parsed if isinstance(parsed, dict) else {}, fallback, template_catalog, cleaned_prompt)
            normalized["source"] = "openai"
            latency_ms = int((time.perf_counter() - started) * 1000)
            log_ai_operation(
                operation_type="matter_intake_parse",
                provider="openai",
                model=model,
                status="ok",
                request_chars=request_chars,
                response_units=len(json.dumps(normalized, ensure_ascii=True)),
                latency_ms=latency_ms,
                metadata={"template_count": len(template_catalog), "template_id": normalized.get("template_id")},
            )
            return normalized
        except Exception as exc:  # pragma: no cover - provider/network fallback
            latency_ms = int((time.perf_counter() - started) * 1000)
            log_ai_operation(
                operation_type="matter_intake_parse",
                provider="openai",
                model=model,
                status="error",
                request_chars=request_chars,
                latency_ms=latency_ms,
                metadata={"template_count": len(template_catalog)},
                error_message=str(exc),
            )
            current_app.logger.warning("OpenAI intake parsing fallback engaged: %s", exc)

    log_ai_operation(
        operation_type="matter_intake_parse",
        provider=f"{provider}_fallback",
        model="heuristic_v1",
        status="ok",
        request_chars=request_chars,
        response_units=len(json.dumps(fallback, ensure_ascii=True)),
        metadata={"template_count": len(template_catalog), "template_id": fallback.get("template_id")},
    )
    return fallback

