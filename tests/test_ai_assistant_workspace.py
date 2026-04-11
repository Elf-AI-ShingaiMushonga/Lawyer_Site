from __future__ import annotations

import datetime as dt
import io
import re

import intranet.services.assistant_hub as assistant_hub
from intranet.extensions import db
from intranet.models import (
    Contact,
    Deadline,
    DocumentFile,
    Entity,
    EntityRelationship,
    Invoice,
    InvoiceLine,
    KnowledgeBase,
    Matter,
    MatterActivity,
    MatterMember,
    MatterNote,
    MatterParty,
    MatterStageHistory,
    MatterTimelineEvent,
    MatterWorkspaceDocument,
    PaymentAllocation,
    PortalMessage,
    PortalMessageThread,
    PortalUser,
    Task,
    TimeEntry,
    User,
)
from intranet.timeutils import utc_now


def _login(client, user_id: int, csrf_token: str = "test-csrf") -> str:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user_id)
        sess["_fresh"] = True
        sess["_csrf_token"] = csrf_token
    return csrf_token


def _seed_user(*, email: str, role: str) -> User:
    user = User(
        email=email,
        full_name=email.split("@", 1)[0].replace(".", " ").title(),
        role=role,
        password_hash="x",
        is_active=True,
        mfa_enabled=True,
    )
    user.set_password("TestPassword123!")
    db.session.add(user)
    db.session.flush()
    return user


def _seed_matter(user: User, *, matter_no: str = "2026-AST-0001", title: str = "Assistant Matter") -> Matter:
    matter = Matter(
        matter_no=matter_no,
        title=title,
        client_name="Assistant Client",
        status="Open",
        risk_level="Medium",
        budget_status="On Track",
        created_by=user.id,
        opened_at=utc_now(),
        last_updated_at=utc_now(),
    )
    db.session.add(matter)
    db.session.flush()
    db.session.add(MatterMember(matter_id=matter.id, user_id=user.id, role_in_matter="Lead"))
    db.session.commit()
    return matter


def _extract_confirm_token(body: str) -> str:
    match = re.search(r'name="confirm_token"\s+value="([^"]+)"', body)
    assert match, "confirm_token missing from assistant response"
    return match.group(1)


def _extract_artifact_href(body: str) -> str:
    match = re.search(r'href="([^"]*/assistant/artifacts/[^"]+)"', body)
    assert match, "artifact download href missing from assistant response"
    return match.group(1)


def test_assistant_page_renders(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.page@example.com", role="junior_attorney")
    client = app.test_client()
    _login(client, user.id)

    response = client.get("/assistant")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "AI workspace for legal analysis and matter execution" in body
    assert "Create a task to file the affidavit by tomorrow." in body
    assert "Upload Source File" in body
    assert "Preferred Output" in body


def test_assistant_page_shows_global_fallback_reason_when_planner_is_unavailable(app_ctx):
    app = app_ctx
    app.config.update(AI_ENABLED=True, AI_ASSISTANT_AGENT_ENABLED=True, AI_OPENAI_API_KEY="")
    user = _seed_user(email="assistant.page.fallback@example.com", role="junior_attorney")
    client = app.test_client()
    _login(client, user.id)

    response = client.get("/assistant")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Deterministic fallback active because the OpenAI planner is unavailable" in body
    assert "OpenAI API key is not configured for assistant planning." in body


def test_assistant_can_analyze_pasted_source_material_without_matter(app_ctx):
    app = app_ctx
    app.config.update(AI_ENABLED=False)
    user = _seed_user(email="assistant.source.text@example.com", role="junior_attorney")
    client = app.test_client()
    csrf_token = _login(client, user.id)

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "prompt": "Analyze the pasted source material and extract the key issues.",
            "source_text": "Witness statement notes: hearing moved to 2026-05-12. Client disputes annexure B and funding timeline.",
            "preferred_output": "markdown",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Source Material Analysis" in body
    assert "What I Did" in body
    assert "I completed the request and packaged the output." in body
    assert "Notified you about the non-AI fallback" in body
    assert "Inputs Used" in body
    assert "Pasted source text" in body
    assert "Output Files" in body
    assert "Source material analysis used the non-AI fallback" in body


def test_assistant_can_analyze_uploaded_file_and_download_artifact(app_ctx):
    app = app_ctx
    app.config.update(AI_ENABLED=False)
    user = _seed_user(email="assistant.source.file@example.com", role="junior_attorney")
    client = app.test_client()
    csrf_token = _login(client, user.id)

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "prompt": "Analyze the uploaded document and extract the key issues.",
            "preferred_output": "plain_text",
            "source_file": (io.BytesIO(b"Draft hearing memo\nIssue one: service defect\nIssue two: witness timing"), "hearing-memo.txt"),
            "action_mode": "preview",
        },
        content_type="multipart/form-data",
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Source Material Analysis" in body
    assert "What I Did" in body
    assert "hearing-memo.txt" in body
    artifact_href = _extract_artifact_href(body)

    download_response = client.get(artifact_href)
    download_body = download_response.get_data(as_text=True)

    assert download_response.status_code == 200
    assert "attachment" in (download_response.headers.get("Content-Disposition") or "").lower()
    assert "Source Material Analysis" in download_body
    assert "What I Did" in download_body


def test_assistant_summary_draft_uses_selected_matter(app_ctx):
    app = app_ctx
    app.config.update(AI_ENABLED=False)
    user = _seed_user(email="assistant.summary@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0002", title="Summary Matter")
    client = app.test_client()
    csrf_token = _login(client, user.id)

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "Summarize this matter for partner review.",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Executive Summary Draft" in body
    assert "What I Did" in body
    assert matter.matter_no in body
    assert "Objective" in body
    assert "Executive summary draft used the non-AI fallback" in body
    assert "AI is disabled in server configuration." in body


def test_assistant_can_resolve_matter_from_prompt_title(app_ctx):
    app = app_ctx
    app.config.update(AI_ENABLED=False)
    user = _seed_user(email="assistant.resolve@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0016", title="Resolution Matter")
    client = app.test_client()
    csrf_token = _login(client, user.id)

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "prompt": "Summarize Resolution Matter for partner review.",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Executive Summary Draft" in body
    assert matter.matter_no in body
    assert "Resolved matter focus from the prompt" in body


def test_assistant_case_strategy_preview_uses_workspace_context(app_ctx):
    app = app_ctx
    app.config.update(AI_ENABLED=False)
    user = _seed_user(email="assistant.strategy@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0023", title="Strategy Matter")
    db.session.add(
        Task(
            matter_id=matter.id,
            title="Prepare hearing bundle",
            status="Todo",
            due_date=dt.date.today() + dt.timedelta(days=2),
            created_by=user.id,
        )
    )
    db.session.add(
        DocumentFile(
            matter_id=matter.id,
            original_filename="hearing-outline.pdf",
            stored_filename="hearing-outline.pdf",
            sha256="strategy123",
            content_type="application/pdf",
            category="Outline",
            owner_name="Litigation Team",
            uploaded_by=user.id,
        )
    )
    db.session.add(
        MatterNote(
            matter_id=matter.id,
            body="Witness prep notes and likely opposition themes.",
            created_by=user.id,
        )
    )
    db.session.commit()

    client = app.test_client()
    csrf_token = _login(client, user.id)

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "Build a case strategy memo for this matter focused on hearing prep.",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Case Strategy Brief" in body
    assert "Case Theory" in body
    assert "Strengths" in body
    assert "Evidence Gaps" in body
    assert "Recommended Actions" in body
    assert "Case strategy used the non-AI fallback" in body


def test_assistant_research_preview_surfaces_workspace_sources(app_ctx):
    app = app_ctx
    app.config.update(AI_ENABLED=False)
    user = _seed_user(email="assistant.research@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0024", title="Research Matter")
    db.session.add(
        DocumentFile(
            matter_id=matter.id,
            original_filename="arbitration-strategy-memo.pdf",
            stored_filename="arbitration-strategy-memo.pdf",
            sha256="research123",
            content_type="application/pdf",
            category="Memo",
            owner_name="Strategy Team",
            uploaded_by=user.id,
        )
    )
    db.session.add(
        KnowledgeBase(
            title="Arbitration Strategy Checklist",
            tags="arbitration, strategy",
            body="Checklist for arbitration posture, evidence planning, and hearing preparation.",
            created_by=user.id,
        )
    )
    db.session.commit()

    client = app.test_client()
    csrf_token = _login(client, user.id)

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "Research the arbitration strategy issues in this file.",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Research &amp; File Analysis" in body or "Research & File Analysis" in body
    assert "Supporting Sources" in body
    assert "Arbitration Strategy Checklist" in body
    assert "not external legal databases" in body
    assert "Research memo used the non-AI fallback" in body


def test_assistant_case_workup_preview_builds_integrated_dossier(app_ctx):
    app = app_ctx
    app.config.update(AI_ENABLED=False)
    user = _seed_user(email="assistant.workup@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0030", title="Workup Matter")
    matter.stage = "Pleadings"
    witness = Entity(name="Jane Witness", entity_type="person", email="jane@example.com")
    client_entity = Entity(name="Workup Client", entity_type="organization")
    db.session.add_all([witness, client_entity])
    db.session.flush()
    db.session.add(MatterParty(matter_id=matter.id, entity_id=witness.id, party_role="Witness", is_primary=True))
    db.session.add(MatterParty(matter_id=matter.id, entity_id=client_entity.id, party_role="Client", is_primary=False))
    db.session.add(EntityRelationship(src_entity_id=witness.id, dst_entity_id=client_entity.id, relationship_type="Witness for"))
    db.session.add(
        Deadline(
            matter_id=matter.id,
            title="File answering affidavit",
            due_at=dt.date.today() + dt.timedelta(days=4),
            is_critical=True,
            status="open",
            created_by=user.id,
        )
    )
    db.session.add(
        MatterStageHistory(
            matter_id=matter.id,
            from_stage="Intake",
            to_stage="Pleadings",
            reason="Statement of claim settled and pleadings opened.",
            changed_by=user.id,
        )
    )
    db.session.add(
        MatterWorkspaceDocument(
            matter_id=matter.id,
            title="Witness Outline Draft",
            body="Initial witness outline and hearing notes.",
            status="draft",
            document_type="General",
            confidentiality="Internal",
            created_by=user.id,
            last_edited_by=user.id,
        )
    )
    db.session.flush()
    thread = PortalMessageThread(matter_id=matter.id, subject="Client questions on pleadings", created_by_user_id=user.id)
    db.session.add(thread)
    db.session.flush()
    db.session.add(
        PortalMessage(
            thread_id=thread.id,
            body="Client asked whether the answering affidavit changes the hearing timetable.",
            from_user_id=user.id,
        )
    )
    db.session.commit()

    client = app.test_client()
    csrf_token = _login(client, user.id)

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "Construct the case for this matter and give me a full workup.",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Case Workup" in body
    assert "Case Theory" in body
    assert "Research Position" in body
    assert "Key Parties" in body
    assert "Collaborative Drafts" in body
    assert "Client Communications" in body
    assert "Stage History" in body
    assert "Case strategy used the non-AI fallback" in body


def test_assistant_chronology_preview_builds_matter_history(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.chronology@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0025", title="Chronology Matter")
    db.session.add(
        MatterTimelineEvent(
            matter_id=matter.id,
            event_date=dt.date(2026, 4, 1),
            event_type="Filing",
            title="Statement of claim filed",
            is_milestone=True,
            created_by=user.id,
        )
    )
    db.session.add(
        Deadline(
            matter_id=matter.id,
            title="Serve response notice",
            due_at=dt.date(2026, 4, 10),
            status="open",
            created_by=user.id,
        )
    )
    db.session.commit()

    client = app.test_client()
    csrf_token = _login(client, user.id)

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "Create a chronology of this matter focused on the filing history.",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Matter Chronology" in body
    assert "Chronology" in body
    assert "Statement of claim filed" in body


def test_assistant_portal_reply_draft_uses_thread_context(app_ctx):
    app = app_ctx
    app.config.update(AI_ENABLED=False)
    user = _seed_user(email="assistant.portal.reply@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0040", title="Portal Reply Matter")
    matter.last_update_note = "We are finalising the hearing pack and witness bundle."
    portal_user = PortalUser(
        email="client.portal@example.com",
        full_name="Client Portal User",
        password_hash="x",
        is_active=True,
    )
    portal_user.set_password("ClientPortal123!")
    db.session.add(portal_user)
    db.session.flush()
    thread = PortalMessageThread(
        matter_id=matter.id,
        subject="Documents needed for hearing",
        created_by_portal_user_id=portal_user.id,
    )
    db.session.add(thread)
    db.session.flush()
    db.session.add(
        PortalMessage(
            thread_id=thread.id,
            body="Can you confirm the hearing date and what documents you still need from me?",
            from_portal_user_id=portal_user.id,
        )
    )
    db.session.commit()

    client = app.test_client()
    csrf_token = _login(client, user.id)
    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "Draft a reply to the client's latest portal message on this matter.",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Portal Reply Draft" in body
    assert "Documents needed for hearing" in body
    assert "Thank you for your message" in body
    assert "Client Message Center" in body
    assert "Portal reply draft used the non-AI fallback" in body


def test_assistant_financial_snapshot_surfaces_unbilled_and_outstanding(app_ctx):
    app = app_ctx
    app.config.update(AI_ENABLED=False)
    user = _seed_user(email="assistant.finance@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0041", title="Finance Matter")
    ready_entry = TimeEntry(
        user_id=user.id,
        matter_id=matter.id,
        start_at=dt.datetime(2026, 4, 8, 9, 0),
        end_at=dt.datetime(2026, 4, 8, 11, 0),
        hours=2.0,
        rounded_hours=2.0,
        narrative="Prepared settlement chronology",
        is_billable=True,
        status="approved",
    )
    billed_entry = TimeEntry(
        user_id=user.id,
        matter_id=matter.id,
        start_at=dt.datetime(2026, 4, 7, 9, 0),
        end_at=dt.datetime(2026, 4, 7, 10, 0),
        hours=1.0,
        rounded_hours=1.0,
        narrative="Reviewed settlement proposal",
        is_billable=True,
        status="approved",
    )
    review_entry = TimeEntry(
        user_id=user.id,
        matter_id=matter.id,
        start_at=dt.datetime(2026, 4, 9, 13, 0),
        end_at=dt.datetime(2026, 4, 9, 13, 30),
        hours=0.5,
        rounded_hours=0.5,
        narrative="Client call notes",
        is_billable=True,
        status="draft",
    )
    db.session.add_all([ready_entry, billed_entry, review_entry])
    db.session.flush()
    invoice = Invoice(
        matter_id=matter.id,
        client_name=matter.client_name or "Assistant Client",
        period_start=dt.date(2026, 4, 1),
        period_end=dt.date(2026, 4, 30),
        status="approved",
        subtotal=1000.0,
        tax_total=150.0,
        total=1150.0,
        created_by=user.id,
    )
    db.session.add(invoice)
    db.session.flush()
    db.session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            time_entry_id=billed_entry.id,
            description="Reviewed settlement proposal",
            hours=1.0,
            rate=1000.0,
            amount=1000.0,
            tax_amount=150.0,
        )
    )
    db.session.add(
        PaymentAllocation(
            invoice_id=invoice.id,
            amount=400.0,
            status="settled",
            created_by=user.id,
        )
    )
    db.session.commit()

    client = app.test_client()
    csrf_token = _login(client, user.id)
    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "What can I bill on this matter right now?",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Matter Financial Snapshot" in body
    assert "Ready To Bill" in body
    assert "2.00h" in body
    assert "Prepared settlement chronology" in body
    assert "Total Outstanding" in body
    assert "ZAR 750.00" in body
    assert "Recent Invoices" in body


def test_assistant_task_bundle_confirmation_creates_multiple_tasks(app_ctx):
    app = app_ctx
    app.config.update(AI_ENABLED=False)
    user = _seed_user(email="assistant.taskbundle@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0042", title="Task Bundle Matter")
    client = app.test_client()
    csrf_token = _login(client, user.id)
    prompt = "Create a hearing prep task checklist for this matter."

    preview_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "action_mode": "preview",
        },
    )
    preview_body = preview_response.get_data(as_text=True)
    token = _extract_confirm_token(preview_body)

    confirm_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "confirm_token": token,
            "action_mode": "confirm",
        },
    )
    body = confirm_response.get_data(as_text=True)
    created_tasks = Task.query.filter_by(matter_id=matter.id).order_by(Task.id.asc()).all()

    assert preview_response.status_code == 200
    assert "Task Bundle Ready for Confirmation" in preview_body
    assert "Finalize witness list and prep notes" in preview_body
    assert confirm_response.status_code == 200
    assert "Task Bundle Created" in body
    assert len(created_tasks) >= 5
    assert any(task.title == "Finalize witness list and prep notes" for task in created_tasks)


def test_assistant_task_confirmation_creates_task(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.task@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0003", title="Task Matter")
    client = app.test_client()
    csrf_token = _login(client, user.id)

    preview_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "Create a task to file the affidavit by tomorrow.",
            "action_mode": "preview",
        },
    )
    preview_body = preview_response.get_data(as_text=True)
    token = _extract_confirm_token(preview_body)

    assert preview_response.status_code == 200
    assert "Task Ready for Confirmation" in preview_body

    confirm_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "Create a task to file the affidavit by tomorrow.",
            "confirm_token": token,
            "action_mode": "confirm",
        },
    )
    confirm_body = confirm_response.get_data(as_text=True)

    created = Task.query.filter_by(matter_id=matter.id).order_by(Task.id.desc()).first()
    assert confirm_response.status_code == 200
    assert "Task Created" in confirm_body
    assert created is not None
    assert "file the affidavit" in (created.title or "").lower()
    assert created.due_date == dt.date.today() + dt.timedelta(days=1)


def test_assistant_matter_summary_confirmation_updates_matter(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.summary.update@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0026", title="Summary Update Matter")
    client = app.test_client()
    csrf_token = _login(client, user.id)
    prompt = (
        "Update the matter summary: objective: Secure interim relief. "
        "latest update: Witness interviews are complete. risk High budget Watch on hold."
    )

    preview_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "action_mode": "preview",
        },
    )
    preview_body = preview_response.get_data(as_text=True)
    token = _extract_confirm_token(preview_body)

    confirm_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "confirm_token": token,
            "action_mode": "confirm",
        },
    )
    body = confirm_response.get_data(as_text=True)
    db.session.refresh(matter)

    assert preview_response.status_code == 200
    assert "Matter Summary Update Ready for Confirmation" in preview_body
    assert confirm_response.status_code == 200
    assert "Matter Summary Updated" in body
    assert matter.objective == "Secure interim relief"
    assert matter.last_update_note == "Witness interviews are complete"
    assert matter.risk_level == "High"
    assert matter.budget_status == "Watch"
    assert matter.status == "On Hold"


def test_assistant_note_confirmation_creates_note(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.note@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0004", title="Note Matter")
    client = app.test_client()
    csrf_token = _login(client, user.id)
    prompt = "Add note that client approved the settlement range #settlement #client"

    preview_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "action_mode": "preview",
        },
    )
    token = _extract_confirm_token(preview_response.get_data(as_text=True))

    confirm_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "confirm_token": token,
            "action_mode": "confirm",
        },
    )
    body = confirm_response.get_data(as_text=True)

    note = MatterNote.query.filter_by(matter_id=matter.id).order_by(MatterNote.id.desc()).first()
    assert confirm_response.status_code == 200
    assert "Matter Note Added" in body
    assert note is not None
    assert "client approved the settlement range" in (note.body or "").lower()
    assert note.tags == "client, settlement"


def test_assistant_deadline_confirmation_creates_deadline(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.deadline@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0027", title="Deadline Matter")
    client = app.test_client()
    csrf_token = _login(client, user.id)
    prompt = "Create a critical deadline to serve the notice by 2026-05-09."

    preview_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "action_mode": "preview",
        },
    )
    preview_body = preview_response.get_data(as_text=True)
    token = _extract_confirm_token(preview_body)

    confirm_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "confirm_token": token,
            "action_mode": "confirm",
        },
    )
    body = confirm_response.get_data(as_text=True)
    deadline = Deadline.query.filter_by(matter_id=matter.id).order_by(Deadline.id.desc()).first()

    assert preview_response.status_code == 200
    assert "Deadline Ready for Confirmation" in preview_body
    assert "serve the notice" in preview_body.lower()
    assert confirm_response.status_code == 200
    assert "Deadline Added" in body
    assert deadline is not None
    assert deadline.title == "serve the notice"
    assert deadline.due_at == dt.date(2026, 5, 9)
    assert deadline.is_critical is True


def test_assistant_party_confirmation_creates_party_link(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.party@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0028", title="Party Matter")
    client = app.test_client()
    csrf_token = _login(client, user.id)
    prompt = "Add party John Smith as Witness john.smith@example.com +27 11 555 0101."

    preview_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "action_mode": "preview",
        },
    )
    preview_body = preview_response.get_data(as_text=True)
    token = _extract_confirm_token(preview_body)

    confirm_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "confirm_token": token,
            "action_mode": "confirm",
        },
    )
    body = confirm_response.get_data(as_text=True)
    entity = Entity.query.filter_by(name="John Smith").first()
    party = MatterParty.query.filter_by(matter_id=matter.id, entity_id=entity.id if entity else None).first()

    assert preview_response.status_code == 200
    assert "Matter Party Ready for Confirmation" in preview_body
    assert confirm_response.status_code == 200
    assert "Matter Party Added" in body
    assert entity is not None
    assert entity.email == "john.smith@example.com"
    assert entity.phone in {"+27 11 555 0101", "27 11 555 0101"}
    assert party is not None
    assert party.party_role == "Witness"


def test_assistant_workspace_document_confirmation_creates_collaborative_draft(app_ctx):
    app = app_ctx
    app.config.update(AI_ENABLED=False)
    user = _seed_user(email="assistant.workbench@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0031", title="Workbench Matter")
    client = app.test_client()
    csrf_token = _login(client, user.id)
    prompt = "Create a collaborative draft called Hearing Prep Strategy in the matter workbench."

    preview_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "action_mode": "preview",
        },
    )
    preview_body = preview_response.get_data(as_text=True)
    token = _extract_confirm_token(preview_body)

    confirm_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "confirm_token": token,
            "action_mode": "confirm",
        },
    )
    body = confirm_response.get_data(as_text=True)
    row = MatterWorkspaceDocument.query.filter_by(matter_id=matter.id).order_by(MatterWorkspaceDocument.id.desc()).first()

    assert preview_response.status_code == 200
    assert "Collaborative Draft Ready for Confirmation" in preview_body
    assert "Hearing Prep Strategy" in preview_body
    assert confirm_response.status_code == 200
    assert "Collaborative Draft Created" in body
    assert row is not None
    assert row.title == "Hearing Prep Strategy"
    assert row.document_type == "General"
    assert "Hearing Prep Strategy" in (row.body or "")
    assert row.status == "draft"


def test_assistant_task_status_confirmation_updates_task(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.taskstatus@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0011", title="Task Status Matter")
    task = Task(
        matter_id=matter.id,
        title="Prepare witness bundle",
        status="Todo",
        created_by=user.id,
    )
    db.session.add(task)
    db.session.commit()

    client = app.test_client()
    csrf_token = _login(client, user.id)
    prompt = "Mark task prepare witness bundle done."

    preview_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "action_mode": "preview",
        },
    )
    preview_body = preview_response.get_data(as_text=True)
    token = _extract_confirm_token(preview_body)

    confirm_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "confirm_token": token,
            "action_mode": "confirm",
        },
    )
    db.session.refresh(task)
    body = confirm_response.get_data(as_text=True)

    assert preview_response.status_code == 200
    assert "Task Status Change Ready for Confirmation" in preview_body
    assert confirm_response.status_code == 200
    assert "Task Status Updated" in body
    assert task.status == "Done"


def test_assistant_task_status_noop_does_not_require_confirmation(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.taskstatus.noop@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0017", title="Task Status Noop Matter")
    task = Task(
        matter_id=matter.id,
        title="Prepare witness bundle",
        status="Done",
        created_by=user.id,
    )
    db.session.add(task)
    db.session.commit()

    client = app.test_client()
    csrf_token = _login(client, user.id)

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "Mark task prepare witness bundle done.",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Task Already In Requested Status" in body
    assert "confirm_token" not in body


def test_assistant_time_entry_confirmation_creates_draft_entry(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.time@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0012", title="Time Matter")
    task = Task(
        matter_id=matter.id,
        title="Draft affidavit",
        status="Doing",
        created_by=user.id,
    )
    db.session.add(task)
    db.session.commit()

    client = app.test_client()
    csrf_token = _login(client, user.id)
    prompt = "Log 1.5 hours drafting affidavit today."

    preview_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "action_mode": "preview",
        },
    )
    preview_body = preview_response.get_data(as_text=True)
    token = _extract_confirm_token(preview_body)

    confirm_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "confirm_token": token,
            "action_mode": "confirm",
        },
    )
    body = confirm_response.get_data(as_text=True)
    entry = TimeEntry.query.filter_by(matter_id=matter.id).order_by(TimeEntry.id.desc()).first()

    assert preview_response.status_code == 200
    assert "Time Entry Ready for Confirmation" in preview_body
    assert confirm_response.status_code == 200
    assert "Time Entry Added" in body
    assert entry is not None
    assert entry.status == "draft"
    assert abs(float(entry.rounded_hours or 0.0) - 1.5) < 0.001
    assert "drafting affidavit" in (entry.narrative or "").lower()


def test_assistant_blocks_time_entry_on_closed_matter(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.time.closed@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0018", title="Closed Time Matter")
    matter.status = "Closed"
    db.session.commit()

    client = app.test_client()
    csrf_token = _login(client, user.id)

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "Log 1.5 hours drafting affidavit today.",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Closed matters cannot accept new time entries" in body
    assert "confirm_token" not in body


def test_assistant_time_entry_duplicate_is_blocked(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.time.duplicate@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0014", title="Duplicate Time Matter")
    start_at = dt.datetime(2026, 4, 9, 9, 0)
    end_at = dt.datetime(2026, 4, 9, 10, 30)
    db.session.add(
        TimeEntry(
            user_id=user.id,
            matter_id=matter.id,
            start_at=start_at,
            end_at=end_at,
            hours=1.5,
            rounded_hours=1.5,
            narrative="drafting affidavit",
            status="draft",
        )
    )
    db.session.commit()

    client = app.test_client()
    csrf_token = _login(client, user.id)
    prompt = "Log time from 09:00 to 10:30 on 2026-04-09 drafting affidavit."

    preview_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "action_mode": "preview",
        },
    )
    body = preview_response.get_data(as_text=True)

    assert preview_response.status_code == 200
    assert "already exists" in body
    assert "confirm_token" not in body
    assert TimeEntry.query.filter_by(matter_id=matter.id).count() == 1


def test_assistant_time_entry_duplicate_is_blocked_on_confirmation(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.time.race@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0015", title="Duplicate Confirm Matter")
    client = app.test_client()
    csrf_token = _login(client, user.id)
    prompt = "Log time from 09:00 to 10:30 on 2026-04-09 drafting affidavit."

    preview_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "action_mode": "preview",
        },
    )
    token = _extract_confirm_token(preview_response.get_data(as_text=True))

    db.session.add(
        TimeEntry(
            user_id=user.id,
            matter_id=matter.id,
            start_at=dt.datetime(2026, 4, 9, 9, 0),
            end_at=dt.datetime(2026, 4, 9, 10, 30),
            hours=1.5,
            rounded_hours=1.5,
            narrative="drafting affidavit",
            status="draft",
        )
    )
    db.session.commit()

    confirm_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "confirm_token": token,
            "action_mode": "confirm",
        },
    )
    body = confirm_response.get_data(as_text=True)

    assert confirm_response.status_code == 200
    assert "already exists" in body
    assert TimeEntry.query.filter_by(matter_id=matter.id).count() == 1


def test_assistant_confirmation_token_cannot_be_replayed(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.replay@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0006", title="Replay Matter")
    client = app.test_client()
    csrf_token = _login(client, user.id)
    prompt = "Create a task to prepare the draft witness statement by tomorrow."

    preview_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "action_mode": "preview",
        },
    )
    token = _extract_confirm_token(preview_response.get_data(as_text=True))

    first_confirm = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "confirm_token": token,
            "action_mode": "confirm",
        },
    )
    second_confirm = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "confirm_token": token,
            "action_mode": "confirm",
        },
    )
    second_body = second_confirm.get_data(as_text=True)

    assert first_confirm.status_code == 200
    assert second_confirm.status_code == 200
    assert "already been used" in second_body
    assert Task.query.filter_by(matter_id=matter.id).count() == 1


def test_assistant_recent_history_renders_after_interaction(app_ctx):
    app = app_ctx
    app.config.update(AI_ENABLED=False)
    user = _seed_user(email="assistant.history@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0019", title="History Matter")
    client = app.test_client()
    csrf_token = _login(client, user.id)
    prompt = "Summarize this matter for partner review."

    client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "action_mode": "preview",
        },
    )
    response = client.get("/assistant")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Recent Assistant Activity" in body
    assert prompt in body
    assert "Executive Summary Draft" in body


def test_assistant_search_prompt_with_summary_keyword_stays_in_search_mode(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.search@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0007", title="Search Matter")
    db.session.add(
        DocumentFile(
            matter_id=matter.id,
            original_filename="summary-judgment-strategy.pdf",
            stored_filename="summary-judgment-strategy.pdf",
            sha256="abc123",
            content_type="application/pdf",
            category="Memo",
            doc_version="v1",
            lifecycle_stage="Draft",
            owner_name="Litigation Team",
            is_privileged=False,
            uploaded_by=user.id,
        )
    )
    db.session.commit()

    client = app.test_client()
    csrf_token = _login(client, user.id)

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "Find documents about summary judgment.",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Assistant Search Results" in body
    assert "Executive Summary Draft" not in body
    assert "summary-judgment-strategy.pdf" in body


def test_assistant_matter_briefing_surfaces_next_steps(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.briefing@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0008", title="Briefing Matter")
    db.session.add(
        Task(
            matter_id=matter.id,
            title="Prepare witness bundle",
            status="Todo",
            due_date=dt.date.today() + dt.timedelta(days=1),
            priority="High",
            created_by=user.id,
        )
    )
    db.session.add(
        MatterTimelineEvent(
            matter_id=matter.id,
            event_date=dt.date.today() + dt.timedelta(days=3),
            event_type="Hearing",
            title="Summary judgment hearing",
            is_milestone=True,
            created_by=user.id,
        )
    )
    db.session.commit()

    client = app.test_client()
    csrf_token = _login(client, user.id)

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "What are the next deadlines on this matter?",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Matter Briefing" in body
    assert "Summary judgment hearing" in body
    assert "Prepare witness bundle" in body
    assert "Upcoming Timeline" in body


def test_assistant_timeline_event_confirmation_creates_event(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.timeline@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0009", title="Timeline Matter")
    client = app.test_client()
    csrf_token = _login(client, user.id)
    prompt = "Schedule a hearing for summary judgment on 2026-05-14."

    preview_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "action_mode": "preview",
        },
    )
    preview_body = preview_response.get_data(as_text=True)
    token = _extract_confirm_token(preview_body)

    confirm_response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": prompt,
            "confirm_token": token,
            "action_mode": "confirm",
        },
    )
    body = confirm_response.get_data(as_text=True)
    event = MatterTimelineEvent.query.filter_by(matter_id=matter.id).order_by(MatterTimelineEvent.id.desc()).first()

    assert preview_response.status_code == 200
    assert "Timeline Event Ready for Confirmation" in preview_body
    assert confirm_response.status_code == 200
    assert "Timeline Event Added" in body
    assert event is not None
    assert event.event_type == "Hearing"
    assert event.event_date == dt.date(2026, 5, 14)
    assert "summary judgment" in (event.title or "").lower()


def test_assistant_search_surfaces_timeline_and_activity_results(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.timeline.search@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0010", title="Timeline Search Matter")
    db.session.add(
        MatterTimelineEvent(
            matter_id=matter.id,
            event_date=dt.date.today() + dt.timedelta(days=5),
            event_type="Hearing",
            title="Mediation strategy hearing",
            description="Court appearance on mediation posture",
            is_milestone=True,
            created_by=user.id,
        )
    )
    db.session.add(
        MatterActivity(
            matter_id=matter.id,
            action="Mediation pack updated",
            details="Negotiation strategy and counsel notes added.",
        )
    )
    db.session.commit()

    client = app.test_client()
    csrf_token = _login(client, user.id)

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "Find mediation",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Assistant Search Results" in body
    assert "Timeline &amp; Deadlines" in body or "Timeline & Deadlines" in body
    assert "Mediation strategy hearing" in body
    assert "Recent Activity" in body
    assert "Mediation pack updated" in body


def test_assistant_search_surfaces_my_time_entries(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.timesearch@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0013", title="Time Search Matter")
    db.session.add(
        TimeEntry(
            user_id=user.id,
            matter_id=matter.id,
            start_at=utc_now() - dt.timedelta(hours=2),
            end_at=utc_now() - dt.timedelta(hours=1),
            hours=1.0,
            rounded_hours=1.0,
            narrative="Reviewed mediation pack and prepared chronology",
            status="draft",
        )
    )
    db.session.commit()

    client = app.test_client()
    csrf_token = _login(client, user.id)

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "Find mediation",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Assistant Search Results" in body
    assert "My Time Entries" in body
    assert "Reviewed mediation pack and prepared chronology" in body


def test_assistant_search_surfaces_deadlines_contacts_and_knowledge_base(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.search.expanded@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0029", title="Expanded Search Matter")
    db.session.add(
        Deadline(
            matter_id=matter.id,
            title="Serve arbitration notice",
            due_at=dt.date(2026, 5, 20),
            status="open",
            is_critical=True,
            created_by=user.id,
        )
    )
    db.session.add(
        KnowledgeBase(
            title="Arbitration Strategy Checklist",
            tags="arbitration, strategy",
            body="Practical checklist for arbitration pleadings and hearing readiness.",
            created_by=user.id,
        )
    )
    db.session.add(
        Contact(
            name="Arbitration Counsel",
            organization="Johannesburg Chambers",
            email="counsel@example.com",
            phone="+27 10 555 0110",
            notes="External arbitration specialist.",
            created_by=user.id,
        )
    )
    db.session.commit()

    client = app.test_client()
    csrf_token = _login(client, user.id)

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "Find arbitration",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Assistant Search Results" in body
    assert "Deadlines" in body
    assert "Serve arbitration notice" in body
    assert "Knowledge Base" in body
    assert "Arbitration Strategy Checklist" in body
    assert "Contacts" in body
    assert "Arbitration Counsel" in body


def test_assistant_search_surfaces_collaborative_drafts_and_client_communications(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.search.canvas@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0032", title="Canvas Search Matter")
    db.session.add(
        MatterWorkspaceDocument(
            matter_id=matter.id,
            title="Witness Outline Draft",
            body="Witness outline and hearing posture notes.",
            status="draft",
            document_type="General",
            confidentiality="Internal",
            created_by=user.id,
            last_edited_by=user.id,
        )
    )
    db.session.flush()
    thread = PortalMessageThread(matter_id=matter.id, subject="Witness questions from client", created_by_user_id=user.id)
    db.session.add(thread)
    db.session.flush()
    db.session.add(
        PortalMessage(
            thread_id=thread.id,
            body="Client asked for the latest witness outline and hearing plan.",
            from_user_id=user.id,
        )
    )
    db.session.commit()

    client = app.test_client()
    csrf_token = _login(client, user.id)

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "Find witness outline",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Assistant Search Results" in body
    assert "Collaborative Drafts" in body
    assert "Witness Outline Draft" in body
    assert "Client Communications" in body
    assert "Witness questions from client" in body


def test_assistant_blocks_task_creation_for_support_role(app_ctx):
    app = app_ctx
    user = _seed_user(email="assistant.staff@example.com", role="staff")
    matter = _seed_matter(user, matter_no="2026-AST-0005", title="Support Matter")
    client = app.test_client()
    csrf_token = _login(client, user.id)

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "Create a task to prepare the hearing pack by tomorrow.",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Only legal case-team roles can create tasks from the assistant." in body
    assert Task.query.filter_by(matter_id=matter.id).count() == 0


def test_assistant_openai_planner_can_prepare_task_from_freeform_prompt(app_ctx, monkeypatch):
    app = app_ctx
    app.config.update(AI_ENABLED=True, AI_ASSISTANT_AGENT_ENABLED=True, AI_OPENAI_API_KEY="test-key")
    user = _seed_user(email="assistant.planner.task@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0020", title="Planner Task Matter")
    client = app.test_client()
    csrf_token = _login(client, user.id)

    def _fake_plan_assistant_request(**kwargs):
        assert kwargs["prompt"] == "Please handle the witness bundle follow-up."
        assert kwargs["matter_context"]["matter_no"] == matter.matter_no
        return {
            "tool_name": "prepare_task",
            "arguments": {
                "title": "Prepare witness bundle",
                "description": "Compile and review the witness bundle for filing readiness.",
                "due_date": "2026-04-15",
                "priority": "High",
            },
            "model": "gpt-test",
        }

    monkeypatch.setattr(assistant_hub, "plan_assistant_request", _fake_plan_assistant_request)

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "Please handle the witness bundle follow-up.",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Task Ready for Confirmation" in body
    assert "Prepare witness bundle" in body
    assert "2026-04-15" in body
    assert "Compile and review the witness bundle for filing readiness." in body
    assert "Planning Source" in body
    assert "OpenAI planner" in body
    assert "prepare_task" in body
    assert "gpt-test" in body


def test_assistant_openai_planner_can_request_clarification(app_ctx, monkeypatch):
    app = app_ctx
    app.config.update(AI_ENABLED=True, AI_ASSISTANT_AGENT_ENABLED=True, AI_OPENAI_API_KEY="test-key")
    user = _seed_user(email="assistant.planner.clarify@example.com", role="junior_attorney")
    client = app.test_client()
    csrf_token = _login(client, user.id)

    monkeypatch.setattr(
        assistant_hub,
        "plan_assistant_request",
        lambda **kwargs: {
            "tool_name": "clarify_request",
            "arguments": {"question": "Pick a matter before I draft that update."},
            "model": "gpt-test",
        },
    )

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "prompt": "Could you draft that update?",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Assistant Request Needs Attention" in body
    assert "Pick a matter before I draft that update." in body


def test_assistant_shows_deterministic_fallback_when_planner_is_unavailable(app_ctx, monkeypatch):
    app = app_ctx
    app.config.update(AI_ENABLED=True, AI_ASSISTANT_AGENT_ENABLED=True, AI_OPENAI_API_KEY="test-key")
    user = _seed_user(email="assistant.planner.fallback@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0021", title="Planner Fallback Matter")
    client = app.test_client()
    csrf_token = _login(client, user.id)

    monkeypatch.setattr(
        assistant_hub,
        "plan_assistant_request",
        lambda **kwargs: {
            "tool_name": "",
            "arguments": {},
            "model": "gpt-test",
            "reasoning_effort": "medium",
            "fallback_reason": "openai_error",
            "fallback_detail": "OpenAI request timed out after 20 seconds.",
        },
    )

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "Find assistant client",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Deterministic fallback" in body
    assert "rule_router" in body
    assert "Non-AI fallback used" in body
    assert "OpenAI request timed out after 20 seconds." in body


def test_assistant_planner_accepts_timezone_tagged_time_entry_arguments(app_ctx, monkeypatch):
    app = app_ctx
    app.config.update(AI_ENABLED=True, AI_ASSISTANT_AGENT_ENABLED=True, AI_OPENAI_API_KEY="test-key")
    user = _seed_user(email="assistant.planner.timezone@example.com", role="junior_attorney")
    matter = _seed_matter(user, matter_no="2026-AST-0022", title="Planner Timezone Matter")
    client = app.test_client()
    csrf_token = _login(client, user.id)

    monkeypatch.setattr(
        assistant_hub,
        "plan_assistant_request",
        lambda **kwargs: {
            "tool_name": "prepare_time_entry",
            "arguments": {
                "narrative": "Drafted hearing outline",
                "start_at": "2026-04-10T09:00:00+02:00",
                "end_at": "2026-04-10T10:30:00+02:00",
                "is_billable": True,
            },
            "model": "gpt-test",
            "reasoning_effort": "medium",
        },
    )

    response = client.post(
        "/assistant",
        data={
            "csrf_token": csrf_token,
            "matter_id": str(matter.id),
            "prompt": "Please log the hearing outline work.",
            "action_mode": "preview",
        },
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Time Entry Ready for Confirmation" in body
    assert "2026-04-10T09:00" in body
    assert "2026-04-10T10:30" in body
    assert "Drafted hearing outline" in body
