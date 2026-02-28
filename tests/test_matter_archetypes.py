from __future__ import annotations

import json

from flask import g

from intranet.extensions import db
from intranet.models import Matter, MatterTemplate, User


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
