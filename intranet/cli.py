from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

from .config import VALID_ROLES, is_valid_email
from .extensions import db
from .models import Announcement, AuditLog, Contact, DocumentFile, KnowledgeBase, Matter, MatterMember, Task, User


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


def seed_demo_data(app, password: str, reset: bool = False):
    if len(password) < 12:
        raise SystemExit("Password must be at least 12 characters")

    with app.app_context():
        # Keep local/demo workflows frictionless even before migrations are run.
        db.create_all()
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
                now - dt.timedelta(days=20),
            ),
            (
                "2026-EMP-0071",
                "Molefe Labour Arbitration",
                "Molefe Retail Group",
                "On Hold",
                "Unfair dismissal dispute pending CCMA scheduling confirmation.",
                now - dt.timedelta(days=14),
            ),
            (
                "2026-CORP-0033",
                "Silverstream Acquisition Due Diligence",
                "Silverstream Capital",
                "Open",
                "Cross-border acquisition due diligence with regulatory and tax workstreams.",
                now - dt.timedelta(days=9),
            ),
            (
                "2025-PROB-0119",
                "Estate of Jacob Petersen",
                "Petersen Family Office",
                "Closed",
                "Estate administration completed. Final distribution confirmations filed.",
                now - dt.timedelta(days=65),
            ),
        ]
        for matter_no, title, client_name, status, description, opened_at in matter_specs:
            matter = Matter(
                matter_no=matter_no,
                title=title,
                client_name=client_name,
                status=status,
                description=description,
                created_by=partner_id,
                opened_at=opened_at,
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
            ("2026-CORP-0033", users["staff@elf-ai-demo.co.za"].id, "Coordination"),
            ("2025-PROB-0119", partner_id, "Supervising Partner"),
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
            ("2026-EMP-0071", "Review disciplinary record", "Cross-check charge sheet and hearing transcript.", "Doing", 4, associate_id),
            ("2026-EMP-0071", "Prepare prep memo", "Brief counsel on procedural objections and timeline.", "Todo", 6, paralegal_id),
            ("2026-CORP-0033", "Corporate registry search", "Confirm entity status across SA and Mauritius.", "Todo", 2, users["staff@elf-ai-demo.co.za"].id),
            ("2026-CORP-0033", "Draft risk matrix", "Summarize key diligence findings for board update.", "Doing", 1, associate_id),
            ("2025-PROB-0119", "Archive signed letters", "Final archive and closure checklist.", "Done", -20, paralegal_id),
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

        upload_dir = Path(app.config["UPLOAD_DIR"])
        upload_dir.mkdir(parents=True, exist_ok=True)
        doc_specs = [
            (
                "2026-LIT-0142",
                "acme-litigation-strategy.txt",
                "Litigation strategy memo\n\nKey hearing objectives:\n- Narrow disputed variation scope\n- Preserve settlement leverage\n",
            ),
            (
                "2026-EMP-0071",
                "labour-arbitration-brief.txt",
                "Arbitration prep notes\n\nFocus points:\n- procedural fairness chronology\n- evidentiary gaps in warning record\n",
            ),
            (
                "2026-CORP-0033",
                "dd-risk-summary.txt",
                "Due diligence risk summary\n\nTop risks:\n- unresolved VAT matter\n- supplier concentration exposure\n",
            ),
        ]
        for i, (matter_no, original_filename, content) in enumerate(doc_specs, start=1):
            stored_filename = f"demo_{i}_{original_filename}"
            file_path = upload_dir / stored_filename
            file_path.write_text(content, encoding="utf-8")
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            db.session.add(
                DocumentFile(
                    matter_id=matter_map[matter_no].id,
                    original_filename=original_filename,
                    stored_filename=stored_filename,
                    sha256=digest,
                    content_type="text/plain",
                    uploaded_by=paralegal_id,
                    uploaded_at=now - dt.timedelta(days=2),
                )
            )

        db.session.add(
            AuditLog(
                at=now - dt.timedelta(minutes=30),
                actor_user_id=admin_id,
                action="demo_seed",
                entity_type="System",
                entity_id=None,
                ip="127.0.0.1",
                user_agent="seed-script",
                details_json=json.dumps({"seeded": True, "version": 1}),
            )
        )

        db.session.commit()
        return {
            "users": len(user_specs),
            "matters": len(matter_specs),
            "tasks": len(task_specs),
            "documents": len(doc_specs),
            "contacts": len(contacts),
            "knowledge_articles": len(kb_specs),
            "password": password,
        }


def _reset_demo_dataset(app):
    delete_order = [AuditLog, DocumentFile, Task, MatterMember, Contact, KnowledgeBase, Announcement, Matter, User]
    for model in delete_order:
        db.session.query(model).delete(synchronize_session=False)
    db.session.commit()

    upload_dir = Path(app.config["UPLOAD_DIR"])
    if upload_dir.exists():
        for path in upload_dir.glob("demo_*"):
            if path.is_file():
                path.unlink()
