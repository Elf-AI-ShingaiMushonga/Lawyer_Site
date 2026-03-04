from __future__ import annotations

import json

from ..extensions import db
from ..models import Matter, MatterClosingChecklistItem, MatterTemplate
from .archetypes import load_required_fields, normalize_archetype_field_key, parse_matter_archetype_values


def _clean_checklist_text(value: str | None) -> str:
    return " ".join(str(value or "").split()).strip()


def template_checklist_items(template: MatterTemplate | None) -> list[str]:
    if template is None or not template.checklist_json:
        return []
    try:
        parsed = json.loads(template.checklist_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for raw in parsed:
        text = _clean_checklist_text(str(raw))
        if not text:
            continue
        token = text.casefold()
        if token in seen:
            continue
        seen.add(token)
        items.append(text[:255])
    return items


def ensure_matter_closing_checklist_items(matter_id: int, template: MatterTemplate | None) -> int:
    expected_items = template_checklist_items(template)
    if not expected_items:
        return 0
    existing_rows = (
        MatterClosingChecklistItem.query.filter_by(matter_id=int(matter_id))
        .order_by(MatterClosingChecklistItem.id.asc())
        .all()
    )
    existing_tokens = {_clean_checklist_text(row.item_text).casefold() for row in existing_rows if row.item_text}
    created = 0
    for item_text in expected_items:
        token = item_text.casefold()
        if token in existing_tokens:
            continue
        db.session.add(MatterClosingChecklistItem(matter_id=int(matter_id), item_text=item_text))
        existing_tokens.add(token)
        created += 1
    if created > 0:
        db.session.flush()
    return created


def archetype_required_field_gaps(matter: Matter, template: MatterTemplate | None) -> list[str]:
    if template is None:
        return []
    field_defs = load_required_fields(template.required_fields_json)
    if not field_defs:
        return []
    field_values = parse_matter_archetype_values(matter.archetype_data_json)
    missing_labels: list[str] = []
    for field in field_defs:
        key = normalize_archetype_field_key(str(field.get("key") or ""))
        if not key:
            continue
        if not str(field_values.get(key) or "").strip():
            missing_labels.append(str(field.get("label") or key))
    return missing_labels


def build_archetype_compliance_snapshot(matter: Matter, template: MatterTemplate | None) -> dict[str, object]:
    if template is None:
        return {
            "enabled": False,
            "compliance_pct": 100,
            "required_total": 0,
            "required_filled": 0,
            "required_missing_labels": [],
            "checklist_total": 0,
            "checklist_done": 0,
            "checklist_remaining": 0,
            "checklist_unsynced": 0,
        }

    field_defs = load_required_fields(template.required_fields_json)
    field_values = parse_matter_archetype_values(matter.archetype_data_json)
    required_total = len(field_defs)
    required_filled = 0
    required_missing_labels: list[str] = []
    for field in field_defs:
        key = normalize_archetype_field_key(str(field.get("key") or ""))
        if not key:
            continue
        value = str(field_values.get(key) or "").strip()
        if value:
            required_filled += 1
        else:
            required_missing_labels.append(str(field.get("label") or key))

    expected_checklist = template_checklist_items(template)
    expected_tokens = [item.casefold() for item in expected_checklist]
    checklist_rows = (
        MatterClosingChecklistItem.query.filter_by(matter_id=int(matter.id))
        .order_by(MatterClosingChecklistItem.id.asc())
        .all()
    )
    checklist_done_lookup: dict[str, bool] = {}
    for row in checklist_rows:
        token = _clean_checklist_text(row.item_text).casefold()
        if not token:
            continue
        checklist_done_lookup[token] = checklist_done_lookup.get(token, False) or bool(row.is_done)

    checklist_total = len(expected_tokens)
    checklist_done = 0
    checklist_unsynced = 0
    if checklist_total > 0:
        for token in expected_tokens:
            if token not in checklist_done_lookup:
                checklist_unsynced += 1
                continue
            if checklist_done_lookup.get(token):
                checklist_done += 1
    else:
        checklist_total = len(checklist_rows)
        checklist_done = sum(1 for row in checklist_rows if row.is_done)

    checklist_remaining = max(0, checklist_total - checklist_done)

    components: list[float] = []
    if required_total > 0:
        components.append(required_filled / required_total)
    if checklist_total > 0:
        components.append(checklist_done / checklist_total)
    compliance_pct = int(round((sum(components) / len(components)) * 100)) if components else 100
    compliance_pct = max(0, min(100, compliance_pct))

    return {
        "enabled": True,
        "compliance_pct": compliance_pct,
        "required_total": required_total,
        "required_filled": required_filled,
        "required_missing_labels": required_missing_labels,
        "checklist_total": checklist_total,
        "checklist_done": checklist_done,
        "checklist_remaining": checklist_remaining,
        "checklist_unsynced": checklist_unsynced,
    }
