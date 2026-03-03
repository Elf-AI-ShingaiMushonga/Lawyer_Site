from __future__ import annotations

import json

from flask import g

from intranet.extensions import db
from intranet.models import (
    ContractTemplate,
    DocumentOCRText,
    DocumentRecord,
    DocumentVersion,
    Matter,
    MatterTemplate,
    User,
)


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
        email="contracts-admin@example.com",
        full_name="Contracts Admin",
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
        name="Contracts Archetype",
        legal_category="Labour Law",
        default_risk_level="Medium",
        required_fields_json=json.dumps(
            [
                {"key": "incident_date", "label": "Incident Date", "help": ""},
            ],
            ensure_ascii=True,
        ),
        boilerplate_template="Archetype boilerplate for {{ matter_no }}",
        created_by=created_by,
    )
    db.session.add(row)
    db.session.commit()
    return row


def test_admin_can_create_standalone_and_linked_contract_templates(app_ctx):
    app = app_ctx
    admin = _seed_admin()
    archetype = _seed_archetype(admin.id)
    client = app.test_client()
    csrf_token = _set_internal_session(client, admin.id)

    standalone_resp = client.post(
        "/admin/templates/contracts",
        data={
            "csrf_token": csrf_token,
            "action": "save",
            "name": "Standalone Contract",
            "contract_type": "Contract",
            "required_fields": "counterparty_name|Counterparty Name",
            "body": "Contract for {{ client_name }} and {{ counterparty_name }}",
            "auto_create_on_matter_open": "1",
            "is_active": "1",
        },
    )
    assert standalone_resp.status_code == 302
    standalone = ContractTemplate.query.filter_by(name="Standalone Contract").first()
    assert standalone is not None
    assert standalone.archetype_id is None

    linked_resp = client.post(
        "/admin/templates/contracts",
        data={
            "csrf_token": csrf_token,
            "action": "save",
            "name": "Linked Contract",
            "legal_category": "Labour Law",
            "archetype_id": str(archetype.id),
            "contract_type": "Employment Agreement",
            "required_fields": "counterparty_name|Counterparty Name",
            "body": "Agreement for {{ client_name }} vs {{ counterparty_name }}",
            "requires_signature": "1",
            "auto_create_on_matter_open": "1",
            "is_active": "1",
        },
    )
    assert linked_resp.status_code == 302
    linked = ContractTemplate.query.filter_by(name="Linked Contract").first()
    assert linked is not None
    assert linked.archetype_id == archetype.id
    assert linked.auto_create_on_matter_open is True


def test_matter_creation_requires_attached_contract_fields(app_ctx):
    app = app_ctx
    admin = _seed_admin()
    archetype = _seed_archetype(admin.id)
    db.session.add(
        ContractTemplate(
            name="Auto Contract Required Field",
            legal_category="Labour Law",
            archetype_id=archetype.id,
            contract_type="Contract",
            required_fields_json=json.dumps(
                [{"key": "counterparty_name", "label": "Counterparty Name", "help": ""}],
                ensure_ascii=True,
            ),
            body="Contract for {{ client_name }} against {{ counterparty_name }}",
            auto_create_on_matter_open=True,
            is_active=True,
            created_by=admin.id,
        )
    )
    db.session.commit()

    client = app.test_client()
    csrf_token = _set_internal_session(client, admin.id)
    response = client.post(
        "/matters/new",
        data={
            "csrf_token": csrf_token,
            "matter_no": "2026-CONTRACT-0001",
            "title": "Contract Missing Field",
            "client_name": "Client C",
            "status": "Open",
            "risk_level": "Medium",
            "budget_status": "On Track",
            "legal_category": "Labour Law",
            "archetype_id": str(archetype.id),
            "field_incident_date": "2026-03-01",
        },
    )
    assert response.status_code == 302
    assert Matter.query.filter_by(matter_no="2026-CONTRACT-0001").first() is None


def test_matter_creation_autogenerates_attached_contract_document(app_ctx):
    app = app_ctx
    admin = _seed_admin()
    archetype = _seed_archetype(admin.id)
    contract_template = ContractTemplate(
        name="Auto Contract Generation",
        legal_category="Labour Law",
        archetype_id=archetype.id,
        contract_type="Contract",
        required_fields_json=json.dumps(
            [{"key": "counterparty_name", "label": "Counterparty Name", "help": ""}],
            ensure_ascii=True,
        ),
        body="Contract for {{ client_name }} against {{ counterparty_name }} on {{ incident_date }}.",
        auto_create_on_matter_open=True,
        is_active=True,
        created_by=admin.id,
    )
    db.session.add(contract_template)
    db.session.commit()

    client = app.test_client()
    csrf_token = _set_internal_session(client, admin.id)
    response = client.post(
        "/matters/new",
        data={
            "csrf_token": csrf_token,
            "matter_no": "2026-CONTRACT-0002",
            "title": "Contract Auto Generation",
            "client_name": "Client D",
            "status": "Open",
            "risk_level": "Medium",
            "budget_status": "On Track",
            "legal_category": "Labour Law",
            "archetype_id": str(archetype.id),
            "field_incident_date": "2026-03-02",
            "contract_field_counterparty_name": "Counterparty Ltd",
        },
    )
    assert response.status_code == 302

    matter = Matter.query.filter_by(matter_no="2026-CONTRACT-0002").first()
    assert matter is not None
    document = DocumentRecord.query.filter_by(matter_id=matter.id).first()
    assert document is not None
    assert document.document_type == "Contract"
    version = DocumentVersion.query.filter_by(document_id=document.id, version_no=1).first()
    assert version is not None
    ocr = DocumentOCRText.query.filter_by(document_version_id=version.id).first()
    assert ocr is not None
    assert "Counterparty Ltd" in ocr.extracted_text
    assert "2026-03-02" in ocr.extracted_text


def test_matter_intake_autogenerates_attached_contract_document(app_ctx):
    app = app_ctx
    admin = _seed_admin()
    archetype = _seed_archetype(admin.id)
    db.session.add(
        ContractTemplate(
            name="Intake Auto Contract",
            legal_category="Labour Law",
            archetype_id=archetype.id,
            contract_type="Service Contract",
            required_fields_json=json.dumps(
                [{"key": "counterparty_name", "label": "Counterparty Name", "help": ""}],
                ensure_ascii=True,
            ),
            body="Intake contract between {{ client_name }} and {{ counterparty_name }}.",
            auto_create_on_matter_open=True,
            is_active=True,
            created_by=admin.id,
        )
    )
    db.session.commit()

    client = app.test_client()
    csrf_token = _set_internal_session(client, admin.id)
    response = client.post(
        "/matters/intake",
        data={
            "csrf_token": csrf_token,
            "matter_no": "2026-CONTRACT-INTAKE-1",
            "title": "Intake Contract Matter",
            "client_name": "Client Intake",
            "legal_category": "Labour Law",
            "template_id": str(archetype.id),
            "field_incident_date": "2026-03-03",
            "contract_field_counterparty_name": "Intake Counterparty",
        },
    )
    assert response.status_code == 302

    matter = Matter.query.filter_by(matter_no="2026-CONTRACT-INTAKE-1").first()
    assert matter is not None
    document = DocumentRecord.query.filter_by(matter_id=matter.id).first()
    assert document is not None
    assert document.document_type == "Service Contract"
