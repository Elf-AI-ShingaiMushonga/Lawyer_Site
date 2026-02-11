from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import sqlalchemy as sa

from .config import VALID_ROLES, is_valid_email
from .extensions import db
from .models import (
    Announcement,
    AuditLog,
    Contact,
    DocumentFile,
    GovernanceIncident,
    KnowledgeBase,
    Matter,
    MatterActivity,
    MatterMember,
    MatterTimelineEvent,
    Task,
    User,
)


def _detect_schema_gaps():
    inspector = sa.inspect(db.engine)
    table_names = set(inspector.get_table_names())
    required = {
        "matter": {"objective", "risk_level", "budget_status", "outcome_summary", "last_update_note", "last_updated_at"},
        "document_file": {"category", "doc_version", "lifecycle_stage", "owner_name", "is_privileged"},
        "matter_timeline_event": {"id", "matter_id", "event_date", "event_type", "title", "created_by"},
        "matter_activity": {"id", "matter_id", "action", "created_at"},
        "governance_incident": {"id", "title", "incident_type", "severity", "status", "summary", "created_by"},
    }
    missing_tables = []
    missing_columns = []
    for table_name, required_columns in required.items():
        if table_name not in table_names:
            missing_tables.append(table_name)
            continue
        existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
        for column_name in sorted(required_columns):
            if column_name not in existing_columns:
                missing_columns.append(f"{table_name}.{column_name}")
    return missing_tables, missing_columns


def _schema_not_ready_error(app, missing_tables: list[str], missing_columns: list[str]) -> str:
    current_db = app.config.get("SQLALCHEMY_DATABASE_URI", "(unknown)")
    lines = [
        "Database schema is not ready for demo seeding.",
        f"Current database: {current_db}",
    ]
    if missing_tables:
        lines.append(f"Missing tables: {', '.join(missing_tables)}")
    if missing_columns:
        lines.append(f"Missing columns: {', '.join(missing_columns)}")
    lines.extend(
        [
            "",
            "Fix:",
            "  1) Ensure your env vars are loaded so DATABASE_URL points to the intended database:",
            "     set -a; source .env; set +a",
            "  2) Apply migrations:",
            "     flask --app app.py db upgrade -d migrations",
            "  3) Re-run seed:",
            "     python app.py seed-demo --reset --password \"ClientDemo2026!\"",
            "",
            "If you intentionally use local SQLite and can reset it safely:",
            "  rm -f intranet.db",
            "  flask --app app.py db upgrade -d migrations",
        ]
    )
    return "\n".join(lines)


def init_db(app):
    with app.app_context():
        db.create_all()


def create_user(app, email: str, password: str, role: str, full_name: str = "(Unnamed)"):
    email = email.strip().lower()
    if not is_valid_email(email):
        raise SystemExit("Invalid email format")
    if role not in VALID_ROLES:
        raise SystemExit(f"Role must be one of: {', '.join(sorted(VALID_ROLES))}")
    if len(password) < 12:
        raise SystemExit("Password must be at least 12 characters")

    with app.app_context():
        if User.query.filter_by(email=email).first():
            raise SystemExit("User already exists")
        user = User(email=email, role=role, full_name=full_name, password_hash="x")
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        return user.id


def run_server(app, host: str = "127.0.0.1", port: int = 5000, debug: bool = False):
    app.run(host=host, port=port, debug=debug)


def _build_minimal_pdf(text_lines: list[str]) -> bytes:
    def _escape_pdf_text(value: str) -> str:
        return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    content_rows = ["BT", "/F1 11 Tf", "50 760 Td", "14 TL"]
    if not text_lines:
        text_lines = ["Demo document"]
    content_rows.append(f"({_escape_pdf_text(text_lines[0])}) Tj")
    for line in text_lines[1:]:
        content_rows.append(f"T* ({_escape_pdf_text(line)}) Tj")
    content_rows.append("ET")
    stream = "\n".join(content_rows).encode("utf-8")

    objects: list[bytes] = []
    objects.append(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
    objects.append(b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n")
    objects.append(
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n"
    )
    objects.append(
        b"4 0 obj\n<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"\nendstream\nendobj\n"
    )
    objects.append(b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(out.tell())
        out.write(obj)
    xref_pos = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    out.write(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        out.write(f"{offset:010d} 00000 n \n".encode("ascii"))
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii")
    )
    return out.getvalue()


def _build_minimal_docx(title: str, paragraphs: list[str]) -> bytes:
    def _p(text: str) -> str:
        return (
            "<w:p><w:r><w:t xml:space=\"preserve\">"
            + escape(text)
            + "</w:t></w:r></w:p>"
        )

    body = [_p(title)] + [_p(paragraph) for paragraph in paragraphs]
    document_xml = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
        "<w:document xmlns:wpc=\"http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas\" "
        "xmlns:mc=\"http://schemas.openxmlformats.org/markup-compatibility/2006\" "
        "xmlns:o=\"urn:schemas-microsoft-com:office:office\" "
        "xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\" "
        "xmlns:m=\"http://schemas.openxmlformats.org/officeDocument/2006/math\" "
        "xmlns:v=\"urn:schemas-microsoft-com:vml\" "
        "xmlns:wp14=\"http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing\" "
        "xmlns:wp=\"http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing\" "
        "xmlns:w10=\"urn:schemas-microsoft-com:office:word\" "
        "xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\" "
        "xmlns:w14=\"http://schemas.microsoft.com/office/word/2010/wordml\" "
        "xmlns:wpg=\"http://schemas.microsoft.com/office/word/2010/wordprocessingGroup\" "
        "xmlns:wpi=\"http://schemas.microsoft.com/office/word/2010/wordprocessingInk\" "
        "xmlns:wne=\"http://schemas.microsoft.com/office/word/2006/wordml\" "
        "xmlns:wps=\"http://schemas.microsoft.com/office/word/2010/wordprocessingShape\" "
        "mc:Ignorable=\"w14 wp14\">"
        "<w:body>"
        + "".join(body)
        + "<w:sectPr><w:pgSz w:w=\"12240\" w:h=\"15840\"/><w:pgMar w:top=\"1440\" w:right=\"1440\" "
        "w:bottom=\"1440\" w:left=\"1440\" w:header=\"708\" w:footer=\"708\" w:gutter=\"0\"/>"
        "<w:cols w:space=\"708\"/><w:docGrid w:linePitch=\"360\"/></w:sectPr>"
        "</w:body></w:document>"
    )

    content_types_xml = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">"
        "<Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>"
        "<Default Extension=\"xml\" ContentType=\"application/xml\"/>"
        "<Override PartName=\"/word/document.xml\" "
        "ContentType=\"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml\"/>"
        "</Types>"
    )
    rels_xml = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
        "<Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" "
        "Target=\"word/document.xml\"/></Relationships>"
    )
    document_rels_xml = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\"></Relationships>"
    )

    out = io.BytesIO()
    with zipfile.ZipFile(out, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml)
        zf.writestr("_rels/.rels", rels_xml)
        zf.writestr("word/document.xml", document_xml)
        zf.writestr("word/_rels/document.xml.rels", document_rels_xml)
    return out.getvalue()


def seed_demo_data(app, password: str, reset: bool = False):
    if len(password) < 12:
        raise SystemExit("Password must be at least 12 characters")

    with app.app_context():
        # Keep local/demo workflows frictionless even before migrations are run.
        db.create_all()
        missing_tables, missing_columns = _detect_schema_gaps()
        if missing_tables or missing_columns:
            raise SystemExit(_schema_not_ready_error(app, missing_tables, missing_columns))
        if reset:
            _reset_demo_dataset(app)
        elif User.query.first():
            raise SystemExit("Database already has users. Re-run with --reset to replace data.")

        now = dt.datetime.utcnow()

        users = {}
        user_specs = [
            ("admin@elf-ai-demo.co.za", "Alicia Mokoena", "admin"),
            ("partner@elf-ai-demo.co.za", "Daniel Naidoo", "lawyer"),
            ("associate@elf-ai-demo.co.za", "Nandi Maseko", "lawyer"),
            ("paralegal@elf-ai-demo.co.za", "Sipho Khumalo", "paralegal"),
            ("staff@elf-ai-demo.co.za", "Leah Pillay", "staff"),
        ]
        for i, (email, full_name, role) in enumerate(user_specs):
            user = User(
                email=email,
                full_name=full_name,
                role=role,
                password_hash="x",
                created_at=now - dt.timedelta(days=30 - (i * 2)),
                last_login_at=now - dt.timedelta(hours=2 + i),
            )
            user.set_password(password)
            db.session.add(user)
            users[email] = user

        db.session.flush()

        admin_id = users["admin@elf-ai-demo.co.za"].id
        partner_id = users["partner@elf-ai-demo.co.za"].id
        associate_id = users["associate@elf-ai-demo.co.za"].id
        paralegal_id = users["paralegal@elf-ai-demo.co.za"].id
        staff_id = users["staff@elf-ai-demo.co.za"].id

        announcements = [
            (
                "Quarterly Compliance Review",
                "All teams should complete POPIA evidence collection by Friday. Use the updated checklist in Knowledge Base.",
                now - dt.timedelta(days=1),
            ),
            (
                "High Court Filing Window",
                "Court filing cutoff is 14:30 this week due to registry maintenance. Escalate urgent filings to litigation lead.",
                now - dt.timedelta(days=3),
            ),
            (
                "Client Demo Environment",
                "This environment is preloaded with realistic matters and tasks for presentation purposes.",
                now - dt.timedelta(days=5),
            ),
            (
                "Document Classification Reminder",
                "Tag each upload with category and lifecycle stage to improve retrieval speed and governance reporting.",
                now - dt.timedelta(days=7),
            ),
            (
                "Partner Risk Roundtable",
                "Flag all matters with High/Critical risk levels before tomorrow's partner review.",
                now - dt.timedelta(days=9),
            ),
            (
                "Ops Maintenance Window",
                "Minor maintenance planned Saturday 22:00-23:00 UTC for indexing optimization.",
                now - dt.timedelta(days=11),
            ),
        ]
        for title, body, created_at in announcements:
            db.session.add(Announcement(title=title, body=body, created_by=admin_id, created_at=created_at))

        matters = []
        matter_specs = [
            (
                "2026-LIT-0142",
                "Acme Holdings v. Mkhize Engineering",
                "Acme Holdings (Pty) Ltd",
                "Open",
                "Commercial dispute involving delayed delivery penalties and disputed variation orders.",
                "Secure a commercially viable settlement while preserving operational continuity.",
                "High",
                "Watch",
                "Witness pack 80% complete. Settlement prep underway.",
                "Projected outcome: 22% reduction in claimed penalties and a 9-month service framework reset.",
                now - dt.timedelta(days=20),
            ),
            (
                "2026-EMP-0071",
                "Molefe Labour Arbitration",
                "Molefe Retail Group",
                "On Hold",
                "Unfair dismissal dispute pending CCMA scheduling confirmation.",
                "Protect employer position with procedurally defensible timeline and evidence chain.",
                "Medium",
                "On Track",
                "Awaiting CCMA date confirmation, prep memo complete.",
                "Expected outcome: reduced arbitration exposure with documented procedural compliance.",
                now - dt.timedelta(days=14),
            ),
            (
                "2026-CORP-0033",
                "Silverstream Acquisition Due Diligence",
                "Silverstream Capital",
                "Open",
                "Cross-border acquisition due diligence with regulatory and tax workstreams.",
                "Deliver board-ready red flag report and closing condition tracker before signing.",
                "Critical",
                "Needs Review",
                "Tax and sanctions checks escalated to partner review.",
                "Expected outcome: controlled signing with quantified risk-mitigation covenants.",
                now - dt.timedelta(days=9),
            ),
            (
                "2025-PROB-0119",
                "Estate of Jacob Petersen",
                "Petersen Family Office",
                "Closed",
                "Estate administration completed. Final distribution confirmations filed.",
                "Complete estate administration and close with documented beneficiary approvals.",
                "Low",
                "On Track",
                "Matter closed and archive package issued.",
                "Outcome delivered: estate distributed and compliance filings completed without dispute.",
                now - dt.timedelta(days=65),
            ),
            (
                "2026-COM-0055",
                "Ntuli Logistics Contract Dispute",
                "Ntuli Logistics",
                "Open",
                "Commercial disagreement on SLA penalties and delayed warehousing service credits.",
                "Resolve contract dispute with commercially workable revised SLA and claims closure.",
                "Medium",
                "Watch",
                "Counterparty legal response received, mediation prep underway.",
                "Expected outcome: revised SLA with capped liability and reduced litigation exposure.",
                now - dt.timedelta(days=16),
            ),
            (
                "2026-REG-0021",
                "Blue Dune Licensing Compliance",
                "Blue Dune Energy",
                "On Hold",
                "Regulatory licensing variance requires supplementary filings and authority feedback.",
                "Complete corrective filings and obtain regulator confirmation to resume operations.",
                "High",
                "Needs Review",
                "Awaiting regulator response on supplementary compliance package.",
                "Expected outcome: licensing continuity with reduced enforcement risk.",
                now - dt.timedelta(days=12),
            ),
        ]
        for (
            matter_no,
            title,
            client_name,
            status,
            description,
            objective,
            risk_level,
            budget_status,
            last_update_note,
            outcome_summary,
            opened_at,
        ) in matter_specs:
            matter = Matter(
                matter_no=matter_no,
                title=title,
                client_name=client_name,
                status=status,
                description=description,
                objective=objective,
                risk_level=risk_level,
                budget_status=budget_status,
                last_update_note=last_update_note,
                outcome_summary=outcome_summary,
                created_by=partner_id,
                opened_at=opened_at,
                last_updated_at=now - dt.timedelta(days=1),
                closed_at=(now - dt.timedelta(days=7)) if status == "Closed" else None,
            )
            db.session.add(matter)
            matters.append(matter)

        db.session.flush()

        matter_map = {m.matter_no: m for m in matters}
        memberships = [
            ("2026-LIT-0142", partner_id, "Lead Counsel"),
            ("2026-LIT-0142", associate_id, "Associate"),
            ("2026-LIT-0142", paralegal_id, "Case Support"),
            ("2026-EMP-0071", associate_id, "Lead Counsel"),
            ("2026-EMP-0071", paralegal_id, "Case Support"),
            ("2026-CORP-0033", partner_id, "Transaction Lead"),
            ("2026-CORP-0033", associate_id, "Due Diligence"),
            ("2026-CORP-0033", staff_id, "Coordination"),
            ("2025-PROB-0119", partner_id, "Supervising Partner"),
            ("2026-COM-0055", partner_id, "Lead Counsel"),
            ("2026-COM-0055", associate_id, "Associate"),
            ("2026-COM-0055", staff_id, "Operations Liaison"),
            ("2026-REG-0021", partner_id, "Regulatory Lead"),
            ("2026-REG-0021", paralegal_id, "Filing Support"),
            ("2026-REG-0021", staff_id, "Client Coordination"),
        ]
        for matter_no, user_id, role_in_matter in memberships:
            db.session.add(
                MatterMember(
                    matter_id=matter_map[matter_no].id,
                    user_id=user_id,
                    role_in_matter=role_in_matter,
                )
            )

        task_specs = [
            ("2026-LIT-0142", "Prepare witness bundle", "Compile and index affidavits for the hearing set.", "Doing", 5, paralegal_id),
            ("2026-LIT-0142", "Draft settlement position", "Prepare opening settlement position and risk notes.", "Todo", 3, associate_id),
            ("2026-LIT-0142", "Client strategy call", "Run strategy briefing with CFO and operations lead.", "Done", -1, partner_id),
            ("2026-LIT-0142", "Court filing QC", "Final review before filing package submission.", "Todo", -2, None),
            ("2026-EMP-0071", "Review disciplinary record", "Cross-check charge sheet and hearing transcript.", "Doing", 4, associate_id),
            ("2026-EMP-0071", "Prepare prep memo", "Brief counsel on procedural objections and timeline.", "Todo", 6, paralegal_id),
            ("2026-CORP-0033", "Corporate registry search", "Confirm entity status across SA and Mauritius.", "Todo", 2, staff_id),
            ("2026-CORP-0033", "Draft risk matrix", "Summarize key diligence findings for board update.", "Doing", 1, associate_id),
            ("2026-CORP-0033", "Supplier concentration note", "Escalate supplier concentration mitigation paths.", "Todo", -1, None),
            ("2025-PROB-0119", "Archive signed letters", "Final archive and closure checklist.", "Done", -20, paralegal_id),
            ("2026-COM-0055", "Mediation prep binder", "Assemble mediation chronology and contract amendments.", "Doing", 3, paralegal_id),
            ("2026-COM-0055", "Without-prejudice offer draft", "Draft structured offer and fallback options.", "Todo", 1, associate_id),
            ("2026-COM-0055", "Client operations workshop", "Align legal and operations positions pre-mediation.", "Todo", 4, staff_id),
            ("2026-REG-0021", "Supplementary filing checklist", "Confirm all mandatory annexures are complete.", "Doing", 2, paralegal_id),
            ("2026-REG-0021", "Regulator response tracker", "Track open regulator clarifications and owners.", "Todo", 5, staff_id),
            ("2026-REG-0021", "Risk committee briefing", "Prepare executive memo for licensing risk exposure.", "Todo", -3, partner_id),
        ]
        for matter_no, title, description, status, due_in_days, assigned_to in task_specs:
            db.session.add(
                Task(
                    matter_id=matter_map[matter_no].id,
                    title=title,
                    description=description,
                    status=status,
                    due_date=(now.date() + dt.timedelta(days=due_in_days)) if due_in_days else None,
                    assigned_to=assigned_to,
                    created_by=associate_id,
                    created_at=now - dt.timedelta(days=2),
                )
            )

        contacts = [
            ("Zanele Dube", "Acme Holdings", "zanele.dube@acme.co.za", "+27 11 555 0101", "Primary GC contact for litigation updates."),
            ("Ethan Ross", "Mkhize Engineering", "ethan.ross@mkhizeeng.co.za", "+27 11 555 0122", "External counsel opposite side."),
            ("Nomsa Mabuza", "CCMA Johannesburg", "nomsa.mabuza@ccma.org.za", "+27 11 555 0188", "Case manager for labour matter."),
            ("Harriet de Vos", "Silverstream Capital", "harriet.devos@silverstream.vc", "+27 21 555 0159", "Deal lead for acquisition workstream."),
            ("Bongani Ndlovu", "Ntuli Logistics", "bongani.ndlovu@ntulilogistics.co.za", "+27 31 555 0112", "Operations director for contract dispute matter."),
            ("Emma James", "Blue Dune Energy", "emma.james@bluedune.energy", "+27 11 555 0199", "Primary regulatory liaison for licensing matter."),
        ]
        for name, organization, email, phone, notes in contacts:
            db.session.add(
                Contact(
                    name=name,
                    organization=organization,
                    email=email,
                    phone=phone,
                    notes=notes,
                    created_by=admin_id,
                    created_at=now - dt.timedelta(days=3),
                )
            )

        kb_specs = [
            (
                "POPIA Client Intake Checklist",
                "POPIA, compliance, intake",
                "Use this checklist at matter intake:\n1. Confirm lawful basis.\n2. Confirm retention period.\n3. Confirm cross-border transfer controls.",
            ),
            (
                "Litigation Hearing Prep Workflow",
                "litigation, hearings, process",
                "Internal prep sequence:\n- Build chronology\n- Validate bundle references\n- Confirm witness availability\n- Pre-brief lead counsel",
            ),
            (
                "Transaction Due Diligence Red Flags",
                "corporate, due-diligence, m&a",
                "Escalate immediately when you spot:\n- unresolved tax disputes\n- sanctions exposure\n- missing beneficial ownership documents",
            ),
            (
                "Regulatory Filing QA Checklist",
                "regulatory, compliance, filing",
                "Before submitting regulatory filings:\n- Validate signature authority\n- Verify annexure numbering\n- Confirm statutory response deadlines",
            ),
            (
                "Commercial Mediation Playbook",
                "commercial, mediation, strategy",
                "Mediation readiness structure:\n- Align settlement range\n- Define non-negotiables\n- Prepare concession sequencing and approval path",
            ),
        ]
        for title, tags, body in kb_specs:
            db.session.add(
                KnowledgeBase(
                    title=title,
                    tags=tags,
                    body=body,
                    created_by=associate_id,
                    created_at=now - dt.timedelta(days=6),
                    updated_at=now - dt.timedelta(days=1),
                )
            )

        timeline_specs = [
            ("2026-LIT-0142", now.date() - dt.timedelta(days=18), "Milestone", "Matter intake approved", "Client onboarding and scope confirmed.", True, partner_id),
            ("2026-LIT-0142", now.date() - dt.timedelta(days=7), "Filing", "Founding affidavit filed", "Filed with supporting annexures A-H.", False, associate_id),
            ("2026-LIT-0142", now.date() + dt.timedelta(days=4), "Hearing", "Case management hearing", "Court slot confirmed; witness bundle in final QA.", True, partner_id),
            ("2026-CORP-0033", now.date() - dt.timedelta(days=8), "Internal Review", "Risk committee review", "Escalated tax exposure for partner decision.", True, partner_id),
            ("2026-CORP-0033", now.date() + dt.timedelta(days=2), "Delivery", "Board red-flag memo due", "Submit investment committee-ready report.", True, associate_id),
            ("2026-EMP-0071", now.date() - dt.timedelta(days=10), "Client Update", "Employer witness interviews completed", "Prepared chronology and contradiction matrix.", False, paralegal_id),
            ("2026-COM-0055", now.date() - dt.timedelta(days=12), "Milestone", "Mediation clause invoked", "Parties agreed to attempt mediation before litigation.", True, partner_id),
            ("2026-COM-0055", now.date() + dt.timedelta(days=3), "Hearing", "Mediation session", "Lead counsel to present revised settlement framework.", True, associate_id),
            ("2026-REG-0021", now.date() - dt.timedelta(days=9), "Filing", "Supplementary compliance filing submitted", "Additional regulator package lodged with annexures.", False, paralegal_id),
            ("2026-REG-0021", now.date() + dt.timedelta(days=6), "Client Update", "Regulatory status checkpoint", "Client executive update on response timeline and options.", False, staff_id),
        ]
        for matter_no, event_date, event_type, title, description, is_milestone, created_by in timeline_specs:
            db.session.add(
                MatterTimelineEvent(
                    matter_id=matter_map[matter_no].id,
                    event_date=event_date,
                    event_type=event_type,
                    title=title,
                    description=description,
                    is_milestone=is_milestone,
                    created_by=created_by,
                    created_at=now - dt.timedelta(days=1),
                )
            )

        activity_specs = [
            ("2026-LIT-0142", partner_id, "Executive summary updated", "Settlement strategy and risk posture refreshed."),
            ("2026-LIT-0142", paralegal_id, "Document uploaded: witness-index-v3.pdf", "Evidence / Final"),
            ("2026-CORP-0033", associate_id, "Task status changed: Draft risk matrix", "Now Doing"),
            ("2026-CORP-0033", partner_id, "Timeline event added: Board red-flag memo due", "Delivery"),
            ("2026-EMP-0071", associate_id, "Team member added", "Sipho Khumalo (Case Support)"),
            ("2026-COM-0055", partner_id, "Executive summary updated", "Mediation strategy and budget watch status set."),
            ("2026-COM-0055", staff_id, "Task created: Client operations workshop", "Cross-team alignment scheduled."),
            ("2026-REG-0021", paralegal_id, "Timeline event added: Supplementary compliance filing submitted", "Filing"),
            ("2026-REG-0021", partner_id, "Risk escalation", "Licensing response delay flagged for leadership."),
        ]
        for matter_no, actor_user_id, action, details in activity_specs:
            db.session.add(
                MatterActivity(
                    matter_id=matter_map[matter_no].id,
                    actor_user_id=actor_user_id,
                    action=action,
                    details=details,
                    created_at=now - dt.timedelta(hours=6),
                )
            )

        upload_dir = Path(app.config["UPLOAD_DIR"])
        upload_dir.mkdir(parents=True, exist_ok=True)
        doc_specs = [
            {
                "matter_no": "2026-LIT-0142",
                "original_filename": "acme-hearing-pack.pdf",
                "kind": "pdf",
                "category": "Court Filing",
                "version": "v3.1",
                "lifecycle_stage": "Final",
                "owner_name": "Daniel Naidoo",
                "is_privileged": True,
                "lines": [
                    "Acme Holdings v. Mkhize Engineering",
                    "Hearing Preparation Pack",
                    "Bundle completeness: 100%",
                    "Settlement posture: Conditional",
                ],
            },
            {
                "matter_no": "2026-CORP-0033",
                "original_filename": "silverstream-dd-brief.docx",
                "kind": "docx",
                "category": "Advisory",
                "version": "v2.0",
                "lifecycle_stage": "For Review",
                "owner_name": "Nandi Maseko",
                "is_privileged": True,
                "lines": [
                    "Silverstream Acquisition - Due Diligence Brief",
                    "Top red flags escalated to transaction committee.",
                    "Mitigation actions attached per workstream.",
                ],
            },
            {
                "matter_no": "2026-EMP-0071",
                "original_filename": "labour-arbitration-brief.txt",
                "kind": "txt",
                "category": "General",
                "version": "v1.2",
                "lifecycle_stage": "Final",
                "owner_name": "Sipho Khumalo",
                "is_privileged": False,
                "lines": [
                    "Arbitration prep notes",
                    "Focus points:",
                    "- procedural fairness chronology",
                    "- evidentiary gaps in warning record",
                ],
            },
            {
                "matter_no": "2026-COM-0055",
                "original_filename": "ntuli-mediation-brief.pdf",
                "kind": "pdf",
                "category": "Advisory",
                "version": "v1.4",
                "lifecycle_stage": "For Review",
                "owner_name": "Daniel Naidoo",
                "is_privileged": True,
                "lines": [
                    "Ntuli Logistics Contract Dispute",
                    "Mediation Brief",
                    "Proposed concession path and fallback plan.",
                ],
            },
            {
                "matter_no": "2026-REG-0021",
                "original_filename": "blue-dune-filing-checklist.docx",
                "kind": "docx",
                "category": "Court Filing",
                "version": "v1.1",
                "lifecycle_stage": "Final",
                "owner_name": "Sipho Khumalo",
                "is_privileged": False,
                "lines": [
                    "Blue Dune Licensing Compliance",
                    "Supplementary filing quality checklist.",
                    "All annexures verified and signed.",
                ],
            },
            {
                "matter_no": "2026-CORP-0033",
                "original_filename": "board-risk-brief.txt",
                "kind": "txt",
                "category": "Advisory",
                "version": "v1.0",
                "lifecycle_stage": "Draft",
                "owner_name": "Nandi Maseko",
                "is_privileged": True,
                "lines": [
                    "Board risk briefing draft",
                    "Summary of critical diligence findings and mitigations.",
                    "Pending partner approval.",
                ],
            },
        ]
        for i, spec in enumerate(doc_specs, start=1):
            original_filename = spec["original_filename"]
            stored_filename = f"demo_{i}_{original_filename}"
            file_path = upload_dir / stored_filename
            lines = spec["lines"]
            if spec["kind"] == "pdf":
                payload = _build_minimal_pdf(lines)
                file_path.write_bytes(payload)
                content_type = "application/pdf"
            elif spec["kind"] == "docx":
                payload = _build_minimal_docx(lines[0], lines[1:])
                file_path.write_bytes(payload)
                content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            else:
                text_payload = "\n".join(lines) + "\n"
                file_path.write_text(text_payload, encoding="utf-8")
                payload = text_payload.encode("utf-8")
                content_type = "text/plain"
            digest = hashlib.sha256(payload).hexdigest()
            db.session.add(
                DocumentFile(
                    matter_id=matter_map[spec["matter_no"]].id,
                    original_filename=original_filename,
                    stored_filename=stored_filename,
                    sha256=digest,
                    content_type=content_type,
                    category=spec["category"],
                    doc_version=spec["version"],
                    lifecycle_stage=spec["lifecycle_stage"],
                    owner_name=spec["owner_name"],
                    is_privileged=spec["is_privileged"],
                    uploaded_by=paralegal_id,
                    uploaded_at=now - dt.timedelta(days=2),
                )
            )

        incident_specs = [
            (
                "Quarterly penetration-test remediation",
                "Change",
                "Medium",
                "Open",
                "Applying hardening recommendations from penetration test results.",
                "Temporary maintenance window planned for authentication service.",
                None,
                None,
            ),
            (
                "Document sync latency alert",
                "Incident",
                "High",
                "Closed",
                "Users experienced delayed document listing in one availability zone.",
                "Delay in client reporting package preparation for 27 minutes.",
                "Cache refresh interval corrected and alert thresholds updated.",
                now - dt.timedelta(days=2),
            ),
            (
                "Retention policy update rollout",
                "Compliance",
                "Low",
                "Closed",
                "Retention labels aligned with revised legal hold policy.",
                "No client service interruption.",
                "Change approved and verified in audit review.",
                now - dt.timedelta(days=5),
            ),
            (
                "Authentication throttling false positives",
                "Incident",
                "Medium",
                "Closed",
                "A burst of legitimate logins triggered temporary throttling for two user cohorts.",
                "Short login delays for staff cohort during peak window.",
                "Adjusted rate-limit bucket thresholds and added allowlist for internal network range.",
                now - dt.timedelta(days=1),
            ),
            (
                "Matter export workflow enhancement",
                "Change",
                "Low",
                "Open",
                "Planned enhancement to export packet generation with metadata headers.",
                "No service impact expected; read-only maintenance window required.",
                None,
                None,
            ),
        ]
        for title, incident_type, severity, status, summary, impact, resolution, closed_at in incident_specs:
            db.session.add(
                GovernanceIncident(
                    title=title,
                    incident_type=incident_type,
                    severity=severity,
                    status=status,
                    summary=summary,
                    impact=impact,
                    resolution=resolution,
                    opened_at=now - dt.timedelta(days=6),
                    closed_at=closed_at,
                    created_by=admin_id,
                    updated_by=admin_id,
                )
            )

        audit_seed_entries = [
            ("demo_seed", "System", None, admin_id, {"seeded": True, "version": 3}),
            ("login", "User", admin_id, admin_id, {"channel": "web"}),
            ("matter_summary_update", "Matter", matter_map["2026-LIT-0142"].id, partner_id, {"risk_level": "High"}),
            ("document_upload", "DocumentFile", None, paralegal_id, {"filename": "acme-hearing-pack.pdf"}),
            ("incident_create", "GovernanceIncident", None, admin_id, {"type": "Change"}),
        ]
        for i, (action, entity_type, entity_id, actor_user_id, details) in enumerate(audit_seed_entries):
            db.session.add(
                AuditLog(
                    at=now - dt.timedelta(minutes=30 - (i * 3)),
                    actor_user_id=actor_user_id,
                    action=action,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    ip="127.0.0.1",
                    user_agent="seed-script",
                    details_json=json.dumps(details),
                )
            )

        db.session.commit()
        return {
            "users": len(user_specs),
            "announcements": len(announcements),
            "matters": len(matter_specs),
            "matter_memberships": len(memberships),
            "tasks": len(task_specs),
            "documents": len(doc_specs),
            "contacts": len(contacts),
            "knowledge_articles": len(kb_specs),
            "timeline_events": len(timeline_specs),
            "matter_activity": len(activity_specs),
            "incidents": len(incident_specs),
            "audit_logs": len(audit_seed_entries),
            "password": password,
        }


def _reset_demo_dataset(app):
    delete_order = [
        AuditLog,
        GovernanceIncident,
        MatterTimelineEvent,
        MatterActivity,
        DocumentFile,
        Task,
        MatterMember,
        Contact,
        KnowledgeBase,
        Announcement,
        Matter,
        User,
    ]
    for model in delete_order:
        db.session.query(model).delete(synchronize_session=False)
    db.session.commit()

    upload_dir = Path(app.config["UPLOAD_DIR"])
    if upload_dir.exists():
        for path in upload_dir.glob("demo_*"):
            if path.is_file():
                path.unlink()
