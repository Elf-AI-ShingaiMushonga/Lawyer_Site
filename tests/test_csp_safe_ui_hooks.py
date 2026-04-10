from __future__ import annotations

import json
import re
from pathlib import Path

from flask import g

from intranet.extensions import db
from intranet.models import Matter, MatterMember, MatterTemplate, MatterWorkspaceDocument, User
from intranet.timeutils import utc_now

REPO_ROOT = Path(__file__).resolve().parents[1]
INLINE_SCRIPT_RE = re.compile(
    r"<script\b(?![^>]*\bsrc=)(?![^>]*\btype=['\"]application/json['\"])[^>]*>",
    re.IGNORECASE,
)
INLINE_HANDLER_RE = re.compile(
    r"\bon(click|change|submit|input|keyup|keydown|blur|focus)\s*=",
    re.IGNORECASE,
)


def _set_internal_session(client, user_id: int, csrf_token: str = "test-csrf") -> str:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["_csrf_token"] = csrf_token
    if hasattr(g, "_login_user"):
        delattr(g, "_login_user")
    return csrf_token


def _seed_user(email: str, role: str = "director") -> User:
    row = User(
        email=email,
        full_name=email.split("@", 1)[0],
        role=role,
        password_hash="x",
        is_active=True,
    )
    row.set_password("TestPassword123!")
    db.session.add(row)
    db.session.commit()
    return row


def _seed_matter(user: User, matter_no: str, title: str) -> Matter:
    now = utc_now()
    row = Matter(
        matter_no=matter_no,
        title=title,
        client_name="CSP Client",
        status="Open",
        created_by=user.id,
        opened_at=now,
        last_updated_at=now,
    )
    db.session.add(row)
    db.session.flush()
    db.session.add(MatterMember(matter_id=row.id, user_id=user.id, role_in_matter="Lead"))
    db.session.commit()
    return row


def test_templates_avoid_inline_executable_scripts_and_handlers():
    template_root = REPO_ROOT / "intranet" / "templates"
    violations: list[str] = []

    for path in sorted(template_root.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        if INLINE_SCRIPT_RE.search(text):
            violations.append(f"{path.relative_to(REPO_ROOT)} has inline executable script markup")
        if INLINE_HANDLER_RE.search(text):
            violations.append(f"{path.relative_to(REPO_ROOT)} has inline event handler markup")

    assert violations == []


def test_matter_intake_page_renders_csp_safe_hooks(app_ctx):
    user = _seed_user("csp-intake@example.com")
    archetype = MatterTemplate(
        name="CSP Intake Archetype",
        legal_category="Commercial Litigation",
        required_fields_json=json.dumps(
            [{"key": "incident_date", "label": "Incident Date", "help": ""}],
            ensure_ascii=True,
        ),
        boilerplate_template="Template",
        created_by=user.id,
    )
    db.session.add(archetype)
    db.session.commit()

    client = app_ctx.test_client()
    _set_internal_session(client, user.id)
    response = client.get("/matters/intake")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Matter Intake" in body
    assert "data-matter-intake-form" in body
    assert "data-template-payload=" in body
    assert 'data-ai-parse-url="/matters/intake/ai/parse"' in body
    assert "<script>" not in body


def test_mobile_hub_template_exposes_csp_safe_hooks():
    template_path = REPO_ROOT / "intranet" / "templates" / "integrations" / "mobile_hub.html"
    body = template_path.read_text(encoding="utf-8")

    assert "data-mobile-matter-select=" in body
    assert "data-mobile-duration=" in body
    assert "data-mobile-task-title=" in body
    assert "<script>" not in body


def test_document_workbench_page_renders_csp_safe_hooks(app_ctx):
    user = _seed_user("csp-workbench@example.com")
    matter = _seed_matter(user, "2026-CSP-WORKBENCH-1", "Workbench CSP Matter")
    document = MatterWorkspaceDocument(
        matter_id=matter.id,
        title="CSP Draft",
        body="Draft body",
        status="draft",
        document_type="General",
        confidentiality="Internal",
        created_by=user.id,
        last_edited_by=user.id,
        updated_at=utc_now(),
    )
    db.session.add(document)
    db.session.commit()

    client = app_ctx.test_client()
    _set_internal_session(client, user.id)
    response = client.get(f"/matters/{matter.id}/documents/workbench?document_id={document.id}")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Matter Document Workbench" in body
    assert "data-workspace-editor" in body
    assert f'data-presence-url="/matters/{matter.id}/documents/workbench/presence"' in body
    assert "<script>" not in body
