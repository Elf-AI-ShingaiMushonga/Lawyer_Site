from __future__ import annotations

import json

from flask import g

from intranet.extensions import db
from intranet.models import MatterTemplate, User


def _set_internal_session(client, user_id: int, csrf_token: str = "test-csrf") -> str:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["_csrf_token"] = csrf_token
    if hasattr(g, "_login_user"):
        delattr(g, "_login_user")
    return csrf_token


def _seed_user() -> User:
    row = User(
        email="intake-ai-user@example.com",
        full_name="Intake AI User",
        role="lawyer",
        password_hash="x",
        mfa_enabled=True,
    )
    row.set_password("TestPassword123!")
    db.session.add(row)
    db.session.commit()
    return row


def _seed_archetype(created_by: int) -> MatterTemplate:
    row = MatterTemplate(
        name="Labour Negligence Clause",
        legal_category="Labour Law",
        default_risk_level="Medium",
        required_fields_json=json.dumps(
            [
                {"key": "incident_date", "label": "Incident Date", "help": ""},
                {"key": "claim_amount", "label": "Claim Amount", "help": ""},
            ],
            ensure_ascii=True,
        ),
        boilerplate_template="Matter {{ matter_no }} against {{ counterparty_name }}.",
        created_by=created_by,
    )
    db.session.add(row)
    db.session.commit()
    return row


def test_matter_intake_ai_parse_returns_structured_payload(app_ctx):
    app = app_ctx
    app.config.update(AI_ENABLED=False)
    user = _seed_user()
    archetype = _seed_archetype(user.id)
    client = app.test_client()
    csrf_token = _set_internal_session(client, user.id)

    response = client.post(
        "/matters/intake/ai/parse",
        json={
            "prompt": (
                "Labour Law negligence matter for Orion Manufacturing against Apex Staffing. "
                "Incident date 2026-02-11 with damages of R 120000 for unsafe disciplinary process."
            )
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None and payload.get("ok") is True
    suggestion = payload.get("suggestion") or {}
    assert suggestion.get("template_id") == archetype.id
    assert suggestion.get("legal_category") in {"Labour Law", "General Legal"}
    assert suggestion.get("title")
    assert suggestion.get("description")
    values = suggestion.get("archetype_required_values") or {}
    assert isinstance(values, dict)
    assert values.get("incident_date")


def test_matter_intake_ai_parse_rejects_short_prompt(app_ctx):
    app = app_ctx
    user = _seed_user()
    _seed_archetype(user.id)
    client = app.test_client()
    csrf_token = _set_internal_session(client, user.id)

    response = client.post(
        "/matters/intake/ai/parse",
        json={"prompt": "too short"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 400
    payload = response.get_json()
    assert payload is not None and payload.get("ok") is False


def test_matter_intake_ai_parse_requires_authentication(app_ctx):
    app = app_ctx
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_csrf_token"] = "test-csrf"
    response = client.post(
        "/matters/intake/ai/parse",
        json={"prompt": "Labour dispute matter intake details for client and counterparty."},
        headers={"X-CSRF-Token": "test-csrf"},
    )
    assert response.status_code == 302
    assert "/login" in (response.headers.get("Location") or "")
