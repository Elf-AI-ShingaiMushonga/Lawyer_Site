from __future__ import annotations

import json

from flask import g

from intranet.extensions import db
from intranet.models import Matter, MatterMember, MatterTemplate, User


def _set_internal_session(client, user_id: int, csrf_token: str = "test-csrf") -> str:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["_csrf_token"] = csrf_token
    if hasattr(g, "_login_user"):
        delattr(g, "_login_user")
    return csrf_token


def _seed_admin() -> User:
    row = User(
        email="archetype-admin@example.com",
        full_name="Archetype Admin",
        role="admin",
        password_hash="x",
        mfa_enabled=True,
        mfa_secret="TESTMFATESTMFATESTMFATESTMFATEST12",
    )
    row.set_password("TestPassword123!")
    db.session.add(row)
    db.session.commit()
    return row


def _seed_archetype(created_by: int) -> MatterTemplate:
    row = MatterTemplate(
        name="Negligence Clause",
        legal_category="Labour Law",
        default_risk_level="Medium",
        required_fields_json=json.dumps(
            [
                {"key": "incident_date", "label": "Incident Date", "help": ""},
                {"key": "employment_role", "label": "Employment Role", "help": ""},
            ],
            ensure_ascii=True,
        ),
        boilerplate_template=(
            "Matter {{ matter_no }} for {{ client_name }} in {{ legal_category }}. "
            "Incident date: {{ incident_date }}. Employment role: {{ employment_role }}."
        ),
        created_by=created_by,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _seed_non_admin() -> User:
    row = User(
        email="archetype-lawyer@example.com",
        full_name="Archetype Lawyer",
        role="lawyer",
        password_hash="x",
        mfa_enabled=True,
    )
    row.set_password("TestPassword123!")
    db.session.add(row)
    db.session.commit()
    return row


def test_admin_can_create_matter_archetype(app_ctx):
    app = app_ctx
    admin = _seed_admin()
    client = app.test_client()
    csrf_token = _set_internal_session(client, admin.id)

    response = client.post(
        "/admin/templates/matters",
        data={
            "csrf_token": csrf_token,
            "name": "Settlement Clause",
            "legal_category": "Labour Law",
            "default_risk_level": "Medium",
            "required_fields": "settlement_amount|Settlement Amount",
            "boilerplate_template": "Settlement amount for {{ client_name }} is {{ settlement_amount }}.",
        },
    )
    assert response.status_code == 302
    template = MatterTemplate.query.filter_by(name="Settlement Clause").first()
    assert template is not None
    assert template.legal_category == "Labour Law"
    required = json.loads(template.required_fields_json or "[]")
    assert required and required[0]["key"] == "settlement_amount"


def test_admin_can_edit_matter_archetype(app_ctx):
    app = app_ctx
    admin = _seed_admin()
    archetype = _seed_archetype(admin.id)
    client = app.test_client()
    csrf_token = _set_internal_session(client, admin.id)

    response = client.post(
        "/admin/templates/matters",
        data={
            "csrf_token": csrf_token,
            "action": "save",
            "template_id": archetype.id,
            "name": "Negligence Clause Updated",
            "legal_category": "Labour Law",
            "default_risk_level": "High",
            "required_fields": "incident_date|Incident Date|Date of incident",
            "boilerplate_template": "Updated template for {{ client_name }} on {{ incident_date }}.",
        },
    )
    assert response.status_code == 302
    updated = db.session.get(MatterTemplate, archetype.id)
    assert updated is not None
    assert updated.name == "Negligence Clause Updated"
    assert updated.default_risk_level == "High"
    required = json.loads(updated.required_fields_json or "[]")
    assert required and required[0]["key"] == "incident_date"


def test_admin_can_delete_unused_matter_archetype(app_ctx):
    app = app_ctx
    admin = _seed_admin()
    archetype = _seed_archetype(admin.id)
    client = app.test_client()
    csrf_token = _set_internal_session(client, admin.id)

    response = client.post(
        "/admin/templates/matters",
        data={
            "csrf_token": csrf_token,
            "action": "delete",
            "template_id": archetype.id,
        },
    )
    assert response.status_code == 302
    assert db.session.get(MatterTemplate, archetype.id) is None


def test_admin_cannot_delete_archetype_in_use(app_ctx):
    app = app_ctx
    admin = _seed_admin()
    archetype = _seed_archetype(admin.id)
    db.session.add(
        Matter(
            matter_no="2026-ARC-0099",
            title="Linked Matter",
            client_name="Client Z",
            status="Open",
            risk_level="Medium",
            budget_status="On Track",
            created_by=admin.id,
            legal_category="Labour Law",
            archetype_id=archetype.id,
        )
    )
    db.session.commit()

    client = app.test_client()
    csrf_token = _set_internal_session(client, admin.id)
    response = client.post(
        "/admin/templates/matters",
        data={
            "csrf_token": csrf_token,
            "action": "delete",
            "template_id": archetype.id,
        },
    )
    assert response.status_code == 302
    assert db.session.get(MatterTemplate, archetype.id) is not None


def test_matter_creation_requires_archetype_specific_fields(app_ctx):
    app = app_ctx
    admin = _seed_admin()
    archetype = _seed_archetype(admin.id)
    client = app.test_client()
    csrf_token = _set_internal_session(client, admin.id)

    missing_field_response = client.post(
        "/matters/new",
        data={
            "csrf_token": csrf_token,
            "matter_no": "2026-ARC-0001",
            "title": "Archetype Matter",
            "client_name": "Client A",
            "status": "Open",
            "risk_level": "Medium",
            "budget_status": "On Track",
            "legal_category": "Labour Law",
            "archetype_id": archetype.id,
        },
    )
    assert missing_field_response.status_code == 302
    assert Matter.query.filter_by(matter_no="2026-ARC-0001").first() is None

    created_response = client.post(
        "/matters/new",
        data={
            "csrf_token": csrf_token,
            "matter_no": "2026-ARC-0002",
            "title": "Archetype Matter Complete",
            "client_name": "Client B",
            "status": "Open",
            "risk_level": "Medium",
            "budget_status": "On Track",
            "legal_category": "Labour Law",
            "archetype_id": archetype.id,
            "field_incident_date": "2026-02-01",
            "field_employment_role": "Senior Analyst",
        },
    )
    assert created_response.status_code == 302
    matter = Matter.query.filter_by(matter_no="2026-ARC-0002").first()
    assert matter is not None
    assert matter.archetype_id == archetype.id
    values = json.loads(matter.archetype_data_json or "{}")
    assert values.get("incident_date") == "2026-02-01"
    assert values.get("employment_role") == "Senior Analyst"


def test_matter_creation_allows_custom_no_archetype(app_ctx):
    app = app_ctx
    admin = _seed_admin()
    client = app.test_client()
    csrf_token = _set_internal_session(client, admin.id)

    created_response = client.post(
        "/matters/new",
        data={
            "csrf_token": csrf_token,
            "matter_no": "2026-ARC-CUSTOM-1",
            "title": "Custom Matter",
            "client_name": "Client Custom",
            "status": "Open",
            "risk_level": "Medium",
            "budget_status": "On Track",
            "legal_category": "Labour Law",
            "archetype_id": "custom",
        },
    )
    assert created_response.status_code == 302
    matter = Matter.query.filter_by(matter_no="2026-ARC-CUSTOM-1").first()
    assert matter is not None
    assert matter.archetype_id is None
    assert matter.archetype_data_json is None


def test_matter_intake_allows_custom_no_archetype(app_ctx):
    app = app_ctx
    admin = _seed_admin()
    client = app.test_client()
    csrf_token = _set_internal_session(client, admin.id)

    created_response = client.post(
        "/matters/intake",
        data={
            "csrf_token": csrf_token,
            "matter_no": "2026-ARC-CUSTOM-INTAKE-1",
            "title": "Custom Intake Matter",
            "client_name": "Client Intake",
            "legal_category": "Labour Law",
            "template_id": "custom",
        },
    )
    assert created_response.status_code == 302
    matter = Matter.query.filter_by(matter_no="2026-ARC-CUSTOM-INTAKE-1").first()
    assert matter is not None
    assert matter.archetype_id is None
    assert matter.archetype_data_json is None


def test_matter_creation_can_assign_lawyers_from_create_screen(app_ctx):
    app = app_ctx
    admin = _seed_admin()
    archetype = _seed_archetype(admin.id)
    lawyer = User(
        email="matter-assign-lawyer@example.com",
        full_name="Assigned Lawyer",
        role="lawyer",
        password_hash="x",
        mfa_enabled=True,
    )
    lawyer.set_password("TestPassword123!")
    partner = User(
        email="matter-assign-partner@example.com",
        full_name="Assigned Partner",
        role="partner",
        password_hash="x",
        mfa_enabled=True,
    )
    partner.set_password("TestPassword123!")
    db.session.add_all([lawyer, partner])
    db.session.commit()

    client = app.test_client()
    csrf_token = _set_internal_session(client, admin.id)

    created_response = client.post(
        "/matters/new",
        data={
            "csrf_token": csrf_token,
            "matter_no": "2026-ARC-ASSIGN-1",
            "title": "Matter With Assigned Lawyers",
            "client_name": "Client Team",
            "status": "Open",
            "risk_level": "Medium",
            "budget_status": "On Track",
            "legal_category": "Labour Law",
            "archetype_id": archetype.id,
            "field_incident_date": "2026-02-03",
            "field_employment_role": "Counsel",
            "lawyer_user_ids": [str(lawyer.id), str(partner.id)],
        },
    )
    assert created_response.status_code == 302

    matter = Matter.query.filter_by(matter_no="2026-ARC-ASSIGN-1").first()
    assert matter is not None
    members = MatterMember.query.filter_by(matter_id=matter.id).all()
    member_user_ids = {int(row.user_id) for row in members}
    assert admin.id in member_user_ids
    assert lawyer.id in member_user_ids
    assert partner.id in member_user_ids


def test_admin_can_generate_ai_archetype_draft_payload(app_ctx):
    app = app_ctx
    app.config.update(AI_ENABLED=False)
    admin = _seed_admin()
    client = app.test_client()
    csrf_token = _set_internal_session(client, admin.id)

    response = client.post(
        "/admin/templates/matters/ai/suggest",
        json={
            "prompt": "Create a labour law negligence clause archetype covering employee misconduct, damages, and reporting timelines.",
            "legal_category_hint": "Labour Law",
            "name_hint": "Negligence Clause",
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None and payload.get("ok") is True
    suggestion = payload.get("suggestion") or {}
    assert suggestion.get("name")
    assert suggestion.get("legal_category")
    assert suggestion.get("boilerplate_template")
    assert isinstance(suggestion.get("required_fields"), list)
    assert suggestion["required_fields"]
    assert suggestion.get("source") == "fallback"
    assert suggestion.get("fallback_reason") == "ai_disabled"
    assert "disabled" in str(suggestion.get("fallback_detail") or "").lower()
    assert int(payload.get("elapsed_ms") or 0) >= 0
    assert payload.get("fallback_reason") == "ai_disabled"


def test_matter_archetype_builder_renders_ai_widget_with_feedback_markers(app_ctx):
    app = app_ctx
    admin = _seed_admin()
    client = app.test_client()
    _set_internal_session(client, admin.id)

    response = client.get("/admin/templates/matters")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "data-archetype-ai-widget" in body
    assert "data-ai-archetype-generate" in body
    assert "data-ai-archetype-status" in body


def test_non_admin_cannot_generate_ai_archetype_draft(app_ctx):
    app = app_ctx
    non_admin = _seed_non_admin()
    client = app.test_client()
    csrf_token = _set_internal_session(client, non_admin.id)

    response = client.post(
        "/admin/templates/matters/ai/suggest",
        json={"prompt": "Create a contractual negligence archetype with required fields."},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 403


def test_matter_archetype_document_download_renders_required_values(app_ctx):
    app = app_ctx
    admin = _seed_admin()
    archetype = _seed_archetype(admin.id)
    matter = Matter(
        matter_no="2026-ARC-0003",
        title="Document Render Matter",
        client_name="Client C",
        status="Open",
        risk_level="Medium",
        budget_status="On Track",
        created_by=admin.id,
        legal_category="Labour Law",
        archetype_id=archetype.id,
        archetype_data_json=json.dumps(
            {"incident_date": "2026-02-10", "employment_role": "Supervisor"},
            ensure_ascii=True,
        ),
    )
    db.session.add(matter)
    db.session.commit()

    client = app.test_client()
    csrf_token = _set_internal_session(client, admin.id)
    response = client.post(
        f"/matters/{matter.id}/archetype-document",
        data={"csrf_token": csrf_token},
    )
    assert response.status_code == 200
    payload = response.get_data(as_text=True)
    assert "2026-ARC-0003" in payload
    assert "Client C" in payload
    assert "2026-02-10" in payload
    assert "Supervisor" in payload
