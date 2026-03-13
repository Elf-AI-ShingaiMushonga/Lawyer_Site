from __future__ import annotations

import json
from typing import Any

from flask import url_for

from ..extensions import db
from ..models import FirmSetting, Matter
from ..roles import canonical_role, role_can_access_finance, role_is_admin, role_is_director, role_is_lawyer, role_is_support
from ..timeutils import utc_now

WORKSPACE_SETTING_PREFIX = "workspace_pref:user:"

WORKSPACE_MODES: dict[str, dict[str, str]] = {
    "practice": {
        "label": "My Practice",
        "summary": "Run matters, deadlines, drafting, and billable work from one home screen.",
    },
    "team": {
        "label": "Team Command",
        "summary": "Run team capacity, at-risk matters, and assignment coverage.",
    },
    "revenue": {
        "label": "Revenue & Risk",
        "summary": "Run billing, trust, collections, and time capture controls.",
    },
}


def _setting_key(user_id: int) -> str:
    return f"{WORKSPACE_SETTING_PREFIX}{int(user_id)}"


def default_workspace_mode_for_role(role: str | None) -> str:
    canonical = canonical_role(role)
    if canonical == "finance_cost_admin":
        return "revenue"
    if role_is_director(canonical):
        return "team"
    if canonical == "operations_staff":
        return "team"
    if canonical == "candidate_attorney":
        return "practice"
    return "practice"


def allowed_workspace_modes(role: str | None) -> list[str]:
    canonical = canonical_role(role)
    modes = ["practice"]
    if role_is_director(canonical) or canonical == "operations_staff":
        modes.insert(1, "team")
    if role_can_access_finance(canonical):
        if "revenue" not in modes:
            modes.append("revenue")
    if role_is_admin(canonical):
        for mode in ("team", "revenue"):
            if mode not in modes:
                modes.append(mode)
    return [mode for mode in ("practice", "team", "revenue") if mode in modes]


def workspace_mode_options(role: str | None) -> list[dict[str, str]]:
    return [
        {
            "key": mode,
            "label": WORKSPACE_MODES[mode]["label"],
            "summary": WORKSPACE_MODES[mode]["summary"],
        }
        for mode in allowed_workspace_modes(role)
    ]


def load_user_workspace_mode(user_id: int, role: str | None) -> str:
    allowed = set(allowed_workspace_modes(role))
    default_mode = default_workspace_mode_for_role(role)
    row = FirmSetting.query.filter_by(setting_key=_setting_key(user_id)).first()
    if row is None:
        return default_mode
    try:
        payload = json.loads(row.setting_value_json or "{}")
    except json.JSONDecodeError:
        payload = {}
    mode = str(payload.get("mode") or "").strip().lower()
    if mode in allowed:
        return mode
    return default_mode


def save_user_workspace_mode(user_id: int, role: str | None, mode: str, *, updated_by: int | None) -> str:
    allowed = set(allowed_workspace_modes(role))
    normalized = str(mode or "").strip().lower()
    if normalized not in allowed:
        normalized = default_workspace_mode_for_role(role)
    payload = {"mode": normalized}
    row = FirmSetting.query.filter_by(setting_key=_setting_key(user_id)).first()
    if row is None:
        row = FirmSetting(
            setting_key=_setting_key(user_id),
            setting_value_json=json.dumps(payload, sort_keys=True),
            updated_at=utc_now(),
            updated_by=updated_by,
        )
        db.session.add(row)
    else:
        row.setting_value_json = json.dumps(payload, sort_keys=True)
        row.updated_at = utc_now()
        row.updated_by = updated_by
    db.session.commit()
    return normalized


def _action(title: str, summary: str, href: str, *, badge: str = "", emphasis: str = "") -> dict[str, str]:
    return {
        "title": title,
        "summary": summary,
        "href": href,
        "badge": badge,
        "emphasis": emphasis,
    }


def build_workspace_quick_actions(
    role: str | None,
    workspace_mode: str,
    *,
    active_matter: Matter | None = None,
    focus_matter: Matter | None = None,
) -> list[dict[str, str]]:
    canonical = canonical_role(role)
    primary_matter = active_matter or focus_matter
    actions: list[dict[str, str]] = []

    if workspace_mode == "practice":
        if primary_matter is not None:
            actions.append(
                _action(
                    f"Resume {primary_matter.matter_no}",
                    primary_matter.title,
                    url_for("matter_workspace", matter_id=primary_matter.id),
                    badge="Workspace",
                    emphasis="strong",
                )
            )
            actions.append(
                _action(
                    "Task Radar",
                    "Move the next task on the current matter.",
                    url_for("matter_tasks", matter_id=primary_matter.id),
                    badge="Tasks",
                )
            )
        actions.append(_action("Start Timer", "Capture live billable work immediately.", url_for("time_timers", matter_id=primary_matter.id) if primary_matter else url_for("time_timers"), badge="Time"))
        actions.append(_action("Log Time", "Record completed work with matter coding.", url_for("time_entries", matter_id=primary_matter.id) if primary_matter else url_for("time_entries"), badge="Time"))
        actions.append(_action("My Calendar", "Open today’s deadlines and hearings.", url_for("calendar_my", filter="today"), badge="Today"))
        actions.append(_action("Global Search", "Jump straight to matters, docs, and tasks.", url_for("search"), badge="Search"))
        if role_is_lawyer(canonical):
            actions.append(_action("Open Matter", "Create a new engagement or instruction.", url_for("matter_create"), badge="New"))

    elif workspace_mode == "team":
        actions.append(_action("Matter Directory", "Triage active matters and staffing gaps.", url_for("matters"), badge="Matters"))
        actions.append(_action("Team Calendar", "Review shared deadlines and docket pressure.", url_for("calendar_team"), badge="Docket"))
        if role_is_lawyer(canonical):
            actions.append(_action("Workload Analytics", "Review utilization and capacity.", url_for("analytics_workload"), badge="Analytics"))
        if primary_matter is not None:
            actions.append(_action("At-Risk Matter", "Jump into the highest-pressure matter.", url_for("matter_workspace", matter_id=primary_matter.id), badge=primary_matter.matter_no))
        actions.append(_action("Global Search", "Find the exact record before assigning work.", url_for("search"), badge="Search"))

    elif workspace_mode == "revenue":
        actions.append(_action("Invoices", "Generate, approve, and follow up on invoices.", url_for("billing_invoices"), badge="Billing", emphasis="strong"))
        actions.append(_action("AR Aging", "Review overdue debtor exposure.", url_for("billing_ar_aging"), badge="AR"))
        actions.append(_action("Time Review", "Approve or clean up time before billing.", url_for("time_review"), badge="Time"))
        if role_can_access_finance(canonical):
            actions.append(_action("Trust Ledger", "Control trust activity and reconciliations.", url_for("trust_ledger"), badge="Trust"))
            actions.append(_action("Expenses", "Review disbursements and receipts.", url_for("expenses"), badge="Ops"))
        if primary_matter is not None:
            actions.append(_action("Matter Billing", "Open billing for the current matter.", url_for("billing_invoices", matter_id=primary_matter.id), badge=primary_matter.matter_no))

    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, str]] = []
    for row in actions:
        key = (row["title"], row["href"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped[:6]


def workspace_mode_meta(mode: str) -> dict[str, str]:
    return WORKSPACE_MODES.get(mode, WORKSPACE_MODES["practice"])
