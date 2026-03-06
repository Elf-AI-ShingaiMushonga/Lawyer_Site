from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Mapping

from ..models import Matter, MatterTemplate

TOKEN_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}")


def normalize_archetype_field_key(raw: str) -> str:
    candidate = (raw or "").strip().lower()
    for prefix in ("contract_field_", "field_"):
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix) :]
    candidate = candidate.replace("-", "_").replace(" ", "_")
    candidate = re.sub(r"[^a-z0-9_]", "_", candidate)
    candidate = re.sub(r"_+", "_", candidate).strip("_")
    if not candidate:
        return ""
    if candidate[0].isdigit():
        candidate = f"field_{candidate}"
    return candidate[:64]


def parse_required_fields_definition(raw: str) -> tuple[list[dict[str, str]], list[str]]:
    fields: list[dict[str, str]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for line_no, line in enumerate((raw or "").splitlines(), start=1):
        candidate = line.strip()
        if not candidate:
            continue
        parts = [part.strip() for part in candidate.split("|", 2)]
        raw_key = parts[0] if len(parts) > 1 else parts[0]
        key = normalize_archetype_field_key(raw_key)
        if len(parts) == 1:
            label = parts[0]
            help_text = ""
        elif len(parts) == 2:
            label = parts[1] or raw_key
            help_text = ""
        else:
            label = parts[1] or raw_key
            help_text = parts[2]

        if not key:
            errors.append(f"Line {line_no}: invalid field key.")
            continue
        if key in seen:
            errors.append(f"Line {line_no}: duplicate field key '{key}'.")
            continue
        seen.add(key)
        fields.append({"key": key, "label": label or key.replace("_", " ").title(), "help": help_text})
    return fields, errors


def load_required_fields(raw_json: str | None) -> list[dict[str, str]]:
    if not raw_json:
        return []
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    fields: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in parsed:
        if not isinstance(item, Mapping):
            continue
        key = normalize_archetype_field_key(str(item.get("key") or ""))
        if not key or key in seen:
            continue
        seen.add(key)
        label = str(item.get("label") or key.replace("_", " ").title()).strip()
        help_text = str(item.get("help") or "").strip()
        fields.append({"key": key, "label": label, "help": help_text})
    return fields


def parse_matter_archetype_values(raw_json: str | None) -> dict[str, str]:
    if not raw_json:
        return {}
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, Mapping):
        return {}
    values: dict[str, str] = {}
    for key, value in parsed.items():
        normalized_key = normalize_archetype_field_key(str(key))
        if not normalized_key:
            continue
        values[normalized_key] = str(value or "").strip()
    return values


def collect_required_field_values(form_data: Mapping[str, str], field_defs: list[dict[str, str]]) -> dict[str, str]:
    values: dict[str, str] = {}
    for field in field_defs:
        key = normalize_archetype_field_key(field.get("key") or "")
        if not key:
            continue
        value = str(form_data.get(f"field_{key}") or "").strip()
        if value:
            values[key] = value
    return values


def validate_required_field_values(field_defs: list[dict[str, str]], values: Mapping[str, str]) -> list[str]:
    missing: list[str] = []
    for field in field_defs:
        key = normalize_archetype_field_key(field.get("key") or "")
        if not key:
            continue
        if not str(values.get(key) or "").strip():
            missing.append(field.get("label") or key)
    return missing


def humanize_required_field_label(raw_label: str) -> str:
    text = str(raw_label or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered.startswith("contract_field_"):
        text = text[len("contract_field_") :]
        lowered = text.lower()
    if lowered.startswith("field_"):
        text = text[6:]
        lowered = text.lower()
    if "_" in text and lowered == text:
        text = " ".join(part for part in text.split("_") if part).strip().title()
    return text or str(raw_label or "").strip()


def build_document_context(
    matter: Matter,
    *,
    archetype: MatterTemplate | None = None,
    required_values: Mapping[str, str] | None = None,
) -> dict[str, str]:
    now = dt.datetime.utcnow()
    context = {
        "matter_id": str(matter.id),
        "matter_no": matter.matter_no or "",
        "matter_title": matter.title or "",
        "title": matter.title or "",
        "client_name": matter.client_name or "",
        "status": matter.status or "",
        "objective": matter.objective or "",
        "risk_level": matter.risk_level or "",
        "budget_status": matter.budget_status or "",
        "legal_category": matter.legal_category or "",
        "archetype_name": archetype.name if archetype else "",
        "today": now.date().isoformat(),
        "now": now.replace(microsecond=0).isoformat(),
    }
    if required_values:
        for key, value in required_values.items():
            normalized_key = normalize_archetype_field_key(key)
            if not normalized_key:
                continue
            context[normalized_key] = str(value or "")
    return context


def render_template_text(template: str | None, context: Mapping[str, str]) -> tuple[str, list[str]]:
    missing: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        key = normalize_archetype_field_key(match.group(1) or "")
        if not key:
            return ""
        if key in context:
            return str(context[key])
        missing.append(key)
        return ""

    rendered = TOKEN_PATTERN.sub(_replace, template or "")
    return rendered, sorted(set(missing))
