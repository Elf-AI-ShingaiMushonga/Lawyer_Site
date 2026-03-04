from __future__ import annotations

import datetime as dt
import json

from sqlalchemy import func

from ..extensions import db
from ..models import DocumentRecord, DocumentTemplate, FirmSetting

DMS_OPTION_LISTS_SETTING_KEY = "dms_option_lists"
DEFAULT_DMS_OPTION_LISTS: dict[str, list[str]] = {
    "document_types": [
        "General",
        "Contract",
        "Pleading",
        "Affidavit",
        "Notice",
        "Opinion",
        "Evidence",
        "Correspondence",
    ],
    "confidentialities": [
        "Internal",
        "Confidential",
        "Highly Confidential",
        "Privileged & Confidential",
        "Public",
    ],
    "privilege_labels": [
        "Attorney-Client",
        "Attorney Work Product",
        "Litigation Privilege",
        "Without Prejudice",
    ],
    "retention_categories": [
        "Matter Lifecycle",
        "Standard 7 Years",
        "Permanent",
        "Client Directed",
    ],
}


def _unique_preserve_order(values: list[str | None]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for raw in values:
        if raw is None:
            continue
        value = str(raw).strip()
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(value)
    return normalized


def _coerce_raw_list(raw: object) -> list[str]:
    if isinstance(raw, str):
        return [line.strip() for line in raw.splitlines() if line.strip()]
    if isinstance(raw, (list, tuple, set)):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def _distinct_values(column) -> list[str]:
    rows = (
        db.session.query(column)
        .filter(column.isnot(None))
        .distinct()
        .order_by(func.lower(column).asc())
        .all()
    )
    return _unique_preserve_order([row[0] for row in rows])


def _observed_option_values() -> dict[str, list[str]]:
    return {
        "document_types": _unique_preserve_order(
            _distinct_values(DocumentRecord.document_type) + _distinct_values(DocumentTemplate.template_type)
        ),
        "confidentialities": _distinct_values(DocumentRecord.confidentiality),
        "privilege_labels": _distinct_values(DocumentRecord.privilege_label),
        "retention_categories": _distinct_values(DocumentRecord.retention_category),
    }


def normalize_dms_option_lists(raw: dict | None) -> dict[str, list[str]]:
    payload = raw if isinstance(raw, dict) else {}
    observed = _observed_option_values()
    normalized: dict[str, list[str]] = {}
    for key, defaults in DEFAULT_DMS_OPTION_LISTS.items():
        configured = _coerce_raw_list(payload.get(key))
        base = configured if configured else list(defaults)
        merged = _unique_preserve_order(base + observed.get(key, []))
        normalized[key] = merged if merged else list(defaults)
    return normalized


def load_dms_option_lists() -> dict[str, list[str]]:
    row = FirmSetting.query.filter_by(setting_key=DMS_OPTION_LISTS_SETTING_KEY).first()
    parsed: dict = {}
    if row is not None:
        try:
            candidate = json.loads(row.setting_value_json or "{}")
            if isinstance(candidate, dict):
                parsed = candidate
        except json.JSONDecodeError:
            parsed = {}
    return normalize_dms_option_lists(parsed)


def save_dms_option_lists(raw: dict, *, updated_by: int | None) -> dict[str, list[str]]:
    payload = normalize_dms_option_lists(raw)
    now = dt.datetime.utcnow()
    row = FirmSetting.query.filter_by(setting_key=DMS_OPTION_LISTS_SETTING_KEY).first()
    if row is None:
        row = FirmSetting(
            setting_key=DMS_OPTION_LISTS_SETTING_KEY,
            setting_value_json=json.dumps(payload, sort_keys=True),
            updated_at=now,
            updated_by=updated_by,
        )
        db.session.add(row)
    else:
        row.setting_value_json = json.dumps(payload, sort_keys=True)
        row.updated_at = now
        row.updated_by = updated_by
    db.session.commit()
    return payload
