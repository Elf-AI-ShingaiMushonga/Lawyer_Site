from __future__ import annotations

import datetime as dt

from intranet.extensions import db
from intranet.jobs.worker import _handle_semantic_index_document_version
from intranet.models import (
    DocumentOCRText,
    DocumentRecord,
    DocumentVersion,
    Matter,
    MatterMember,
    SemanticIndexEntry,
    User,
)
from intranet.services.semantic_search import SemanticSearchService


def _seed_user(email: str, role: str = "lawyer") -> User:
    user = User(
        email=email,
        full_name=email.split("@", 1)[0],
        role=role,
        password_hash="x",
        is_active=True,
        mfa_enabled=True,
    )
    user.set_password("TestPassword123!")
    db.session.add(user)
    db.session.flush()
    return user


def _seed_matter(owner: User, matter_no: str, title: str) -> Matter:
    matter = Matter(
        matter_no=matter_no,
        title=title,
        client_name="Semantic Client",
        status="Open",
        created_by=owner.id,
        opened_at=dt.datetime.utcnow(),
        last_updated_at=dt.datetime.utcnow(),
    )
    db.session.add(matter)
    db.session.flush()
    db.session.add(MatterMember(matter_id=matter.id, user_id=owner.id, role_in_matter="Lead"))
    db.session.flush()
    return matter


def _seed_document_with_ocr(user: User, matter: Matter, *, title: str, ocr_text: str) -> DocumentVersion:
    record = DocumentRecord(
        matter_id=matter.id,
        title=title,
        document_type="Memo",
        confidentiality="Internal",
        created_by=user.id,
    )
    db.session.add(record)
    db.session.flush()

    version = DocumentVersion(
        document_id=record.id,
        version_no=1,
        original_filename=f"{title.lower().replace(' ', '-')}.txt",
        stored_filename=f"{title.lower().replace(' ', '-')}.txt",
        sha256="semantic-test-sha256",
        state="final",
        uploaded_by=user.id,
    )
    db.session.add(version)
    db.session.flush()

    db.session.add(
        DocumentOCRText(
            document_version_id=version.id,
            extracted_text=ocr_text,
        )
    )
    db.session.commit()
    return version


def _login(client, user_id: int) -> None:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["_csrf_token"] = "test-csrf"


def test_semantic_index_job_handler_creates_entries(app_ctx):
    app = app_ctx
    app.config.update(AI_ENABLED=False, AI_SEMANTIC_SEARCH_ENABLED=True)
    user = _seed_user("semantic-index@example.com")
    matter = _seed_matter(user, "2026-SEM-0001", "Semantic Index Matter")
    version = _seed_document_with_ocr(
        user,
        matter,
        title="Wage Arbitration Memo",
        ocr_text="Collective bargaining dispute regarding overtime and wage parity in arbitration proceedings.",
    )

    message = _handle_semantic_index_document_version({"document_version_id": version.id, "requested_by": user.id})
    rows = SemanticIndexEntry.query.filter_by(source_type="document_version", source_id=version.id).all()

    assert "semantic index:" in message
    assert rows
    assert all(int(row.embedding_dim or 0) > 0 for row in rows)
    assert all(row.matter_id == matter.id for row in rows)


def test_search_route_surfaces_semantic_hits(app_ctx):
    app = app_ctx
    app.config.update(AI_ENABLED=False, AI_SEMANTIC_SEARCH_ENABLED=True)
    user = _seed_user("semantic-search@example.com")
    matter = _seed_matter(user, "2026-SEM-0002", "Semantic Search Matter")
    version = _seed_document_with_ocr(
        user,
        matter,
        title="Union Arbitration Strategy",
        ocr_text=(
            "Prepared strategy memorandum for labour arbitration, overtime penalties, and union grievance settlement."
        ),
    )
    SemanticSearchService.index_document_version(version.id, requested_by=user.id)

    client = app.test_client()
    _login(client, user.id)
    response = client.get("/search?q=arbitration")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Semantic Results" in body
    assert "Union Arbitration Strategy" in body


def test_ai_job_status_endpoint_returns_semantic_job_state(app_ctx):
    app = app_ctx
    app.config.update(AI_ENABLED=False, AI_SEMANTIC_SEARCH_ENABLED=True)
    user = _seed_user("semantic-jobs@example.com", role="admin")
    matter = _seed_matter(user, "2026-SEM-0003", "Semantic Job Matter")
    version = _seed_document_with_ocr(
        user,
        matter,
        title="Collective Bargaining Notes",
        ocr_text="Notes on wage schedules and disciplinary hearings for collective bargaining.",
    )
    job_id = SemanticSearchService.enqueue_document_version_index(version.id, requested_by=user.id)
    assert job_id is not None

    client = app.test_client()
    _login(client, user.id)
    response = client.get(f"/api/ai/jobs/{job_id}")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert int(payload["job"]["id"]) == int(job_id)
    assert payload["job"]["job_type"] == "semantic_index_document_version"
    assert payload["job"]["status"] in {"queued", "running", "failed", "succeeded", "dead_letter"}


def test_nav_shows_ai_available_indicator_when_provider_ready(app_ctx, monkeypatch):
    app = app_ctx
    app.config.update(
        AI_ENABLED=True,
        AI_PROVIDER="openai",
        AI_OPENAI_API_KEY="test-api-key",
        AI_OPENAI_TEXT_MODEL="gpt-4o-mini",
    )
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object() if name == "openai" else None)
    user = _seed_user("ai-indicator-ready@example.com")
    db.session.commit()

    client = app.test_client()
    _login(client, user.id)
    response = client.get("/dashboard")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "AI Available" in body
    assert "tone-positive" in body


def test_nav_shows_ai_key_missing_indicator_when_ai_key_not_configured(app_ctx):
    app = app_ctx
    app.config.update(
        AI_ENABLED=True,
        AI_PROVIDER="openai",
        AI_OPENAI_API_KEY="",
    )
    user = _seed_user("ai-indicator-missing-key@example.com")
    db.session.commit()

    client = app.test_client()
    _login(client, user.id)
    response = client.get("/dashboard")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "AI Key Missing" in body
    assert "tone-warning" in body
