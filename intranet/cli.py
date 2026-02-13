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
from .mfa import hash_backup_code
from .models import (
    Announcement,
    AnalyticsMetricSnapshot,
    ARSnapshot,
    AuditLog,
    BackupRun,
    BatesRange,
    BurnoutSignal,
    CRMFollowUp,
    CRMLead,
    ConflictCheck,
    ConflictSemanticHit,
    Contact,
    DRTarget,
    DataResidencyPolicy,
    Deadline,
    DeadlineRule,
    DocumentFile,
    DocumentLock,
    DocumentOCRText,
    DocumentRecord,
    DocumentTemplate,
    DocumentVersion,
    EmailCapture,
    EngagementLetter,
    Entity,
    EntityRelationship,
    EthicalWall,
    EthicalWallMatter,
    EthicalWallRule,
    ExpenseEntry,
    FeeArrangement,
    FirmSetting,
    GovernanceIncident,
    HolidayCalendar,
    IntakeForm,
    Invoice,
    InvoiceAdjustment,
    InvoiceLine,
    JobHistory,
    JobQueue,
    KnowledgeBase,
    LEDESExport,
    LegalHold,
    Matter,
    MatterActivity,
    MatterClosingChecklistItem,
    MatterMember,
    MatterNote,
    MatterNoteACL,
    MatterParty,
    MatterStageHistory,
    MatterTemplate,
    MatterTimelineEvent,
    Notification,
    Office,
    PaymentAllocation,
    PermissionGrant,
    PortalInvoiceView,
    PortalLinkToken,
    PortalMatterAccess,
    PortalMessage,
    PortalMessageThread,
    PortalPaymentReceipt,
    PortalUpload,
    PortalUser,
    PracticeArea,
    ProductionItem,
    ProductionSet,
    RateCard,
    RestoreVerification,
    RetentionPolicy,
    SSOApplication,
    SSOAuthorizationCode,
    SSOToken,
    SavedSearch,
    ScheduledJob,
    SuspiciousActivityAlert,
    Task,
    TaskApproval,
    TaskAssignee,
    TaskChecklistItem,
    TaskDependency,
    TaskTemplate,
    TaskTemplateItem,
    TaxRule,
    TimeEntry,
    TimeRoundingPolicy,
    TimeTimer,
    TimeValidationEvent,
    TimekeeperRole,
    TrustAccount,
    TrustApprovalRequest,
    TrustBankStatementImport,
    TrustBankStatementLine,
    TrustClientLedger,
    TrustLedgerEntry,
    TrustReconciliationRun,
    Section86Investment,
    Section86Accrual,
    TrustThresholdAlert,
    TrustedDevice,
    User,
    UserMFABackupCode,
    UserSession,
    WorkloadForecast,
)
from .schema_sync import sync_schema_compatibility


def _detect_schema_gaps():
    inspector = sa.inspect(db.engine)
    table_names = set(inspector.get_table_names())
    required = {
        "matter": {"objective", "risk_level", "budget_status", "outcome_summary", "last_update_note", "last_updated_at"},
        "document_file": {"category", "doc_version", "lifecycle_stage", "owner_name", "is_privileged"},
        "payment_allocation": {"status", "settled_at", "settled_by", "external_txn_id", "processor_note"},
        "trust_reconciliation_run": {"bank_statement_import_id"},
        "matter_timeline_event": {"id", "matter_id", "event_date", "event_type", "title", "created_by"},
        "matter_activity": {"id", "matter_id", "action", "created_at"},
        "governance_incident": {"id", "title", "incident_type", "severity", "status", "summary", "created_by"},
        "trust_bank_statement_import": {"id", "trust_account_id", "source_filename", "row_count", "imported_by", "imported_at"},
        "trust_bank_statement_line": {"id", "import_id", "posted_on", "signed_amount"},
        "section86_investment": {"id", "trust_account_id", "client_ledger_id", "investment_ref", "principal_amount", "annual_rate_percent"},
        "section86_accrual": {"id", "investment_id", "accrual_date", "net_interest_amount"},
        "conflict_semantic_hit": {
            "id",
            "conflict_check_id",
            "document_ocr_text_id",
            "candidate_entity",
            "similarity_score",
            "semantic_rank",
        },
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
        try:
            # Additive compatibility sync for legacy databases that are a few columns behind.
            sync_schema_compatibility()
        except Exception as exc:
            raise SystemExit(
                "Failed to run additive schema compatibility sync. "
                "Run 'flask --app app.py db upgrade -d migrations' and retry."
            ) from exc
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
            {
                "email": "admin@elf-ai-demo.co.za",
                "full_name": "Alicia Mokoena",
                "role": "admin",
                "mfa_enabled": True,
                "mfa_secret": "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
                "failed_login_attempts": 0,
            },
            {
                "email": "partner@elf-ai-demo.co.za",
                "full_name": "Daniel Naidoo",
                "role": "lawyer",
                "mfa_enabled": True,
                "mfa_secret": "KRUGS4ZANFZSAYJAON2XEZLSON2XEZLS",
                "failed_login_attempts": 0,
            },
            {
                "email": "associate@elf-ai-demo.co.za",
                "full_name": "Nandi Maseko",
                "role": "lawyer",
                "mfa_enabled": False,
                "mfa_secret": None,
                "failed_login_attempts": 0,
            },
            {
                "email": "paralegal@elf-ai-demo.co.za",
                "full_name": "Sipho Khumalo",
                "role": "paralegal",
                "mfa_enabled": False,
                "mfa_secret": None,
                "failed_login_attempts": 1,
            },
            {
                "email": "staff@elf-ai-demo.co.za",
                "full_name": "Leah Pillay",
                "role": "staff",
                "mfa_enabled": False,
                "mfa_secret": None,
                "failed_login_attempts": 0,
            },
        ]
        for i, spec in enumerate(user_specs):
            email = spec["email"]
            user = User(
                email=email,
                full_name=spec["full_name"],
                role=spec["role"],
                password_hash="x",
                created_at=now - dt.timedelta(days=30 - (i * 2)),
                last_login_at=now - dt.timedelta(hours=2 + i),
                mfa_enabled=bool(spec["mfa_enabled"]),
                mfa_secret=spec["mfa_secret"],
                failed_login_attempts=int(spec["failed_login_attempts"]),
                last_failed_login_at=(
                    now - dt.timedelta(hours=6)
                    if int(spec["failed_login_attempts"]) > 0
                    else None
                ),
                password_changed_at=now - dt.timedelta(days=15 - i),
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
        matter_metadata = {
            "2026-LIT-0142": {
                "court_name": "Gauteng Division, Pretoria",
                "judge_name": "Acting Judge M. Khoza",
                "jurisdiction": "ZA-GP",
                "stage": "Discovery",
                "practice_area": "Commercial Litigation",
                "case_type": "Contractual Damages",
                "risk_taxonomy": "Counterparty-Default",
                "archival_status": "active",
                "archival_due_at": None,
                "closing_checklist": [
                    "Final costs memo",
                    "Client closeout letter",
                    "Archive hearing bundle",
                ],
            },
            "2026-EMP-0071": {
                "court_name": "CCMA Johannesburg",
                "judge_name": "Commissioner N. Mabuza",
                "jurisdiction": "ZA-GP",
                "stage": "Pre-Arbitration",
                "practice_area": "Employment Law",
                "case_type": "Unfair Dismissal",
                "risk_taxonomy": "Labour-Procedure",
                "archival_status": "active",
                "archival_due_at": None,
                "closing_checklist": [
                    "Arbitration award analysis",
                    "Client policy update memo",
                    "Closure approval",
                ],
            },
            "2026-CORP-0033": {
                "court_name": None,
                "judge_name": None,
                "jurisdiction": "ZA-WC",
                "stage": "Due Diligence",
                "practice_area": "Corporate and M&A",
                "case_type": "Acquisition",
                "risk_taxonomy": "M&A-Regulatory",
                "archival_status": "active",
                "archival_due_at": None,
                "closing_checklist": [
                    "Board resolution pack",
                    "Closing conditions tracker",
                    "Transaction bible export",
                ],
            },
            "2025-PROB-0119": {
                "court_name": "Master of the High Court, Cape Town",
                "judge_name": None,
                "jurisdiction": "ZA-WC",
                "stage": "Closed",
                "practice_area": "Estates",
                "case_type": "Property Transfer (Estate)",
                "risk_taxonomy": "Probate-Distribution",
                "archival_status": "ready_for_archive",
                "archival_due_at": now + dt.timedelta(days=30),
                "closing_checklist": [
                    "Beneficiary confirmations",
                    "Tax clearance lodged",
                    "Vault transfer complete",
                ],
            },
            "2026-COM-0055": {
                "court_name": "KZN High Court (Durban)",
                "judge_name": "Judge P. Moodley",
                "jurisdiction": "ZA-KZN",
                "stage": "Mediation",
                "practice_area": "Commercial Litigation",
                "case_type": "Service Agreement Dispute",
                "risk_taxonomy": "SLA-Penalties",
                "archival_status": "active",
                "archival_due_at": None,
                "closing_checklist": [
                    "Mediation statement",
                    "Revised SLA draft",
                    "Client sign-off",
                ],
            },
            "2026-REG-0021": {
                "court_name": "NERSA Licensing Panel",
                "judge_name": "Panel Chair R. Msimang",
                "jurisdiction": "ZA-GP",
                "stage": "Regulatory Response",
                "practice_area": "Regulatory",
                "case_type": "Licensing Compliance",
                "risk_taxonomy": "Regulatory-Enforcement",
                "archival_status": "active",
                "archival_due_at": None,
                "closing_checklist": [
                    "Regulator response packet",
                    "Operational reactivation memo",
                    "Compliance monitoring plan",
                ],
            },
        }
        for matter_no, metadata in matter_metadata.items():
            row = matter_map[matter_no]
            row.court_name = metadata["court_name"]
            row.judge_name = metadata["judge_name"]
            row.jurisdiction = metadata["jurisdiction"]
            row.stage = metadata["stage"]
            row.practice_area = metadata["practice_area"]
            row.case_type = metadata["case_type"]
            row.risk_taxonomy = metadata["risk_taxonomy"]
            row.archival_status = metadata["archival_status"]
            row.archival_due_at = metadata["archival_due_at"]
            row.closing_checklist_json = json.dumps(metadata["closing_checklist"])
            row.originating_partner_id = partner_id
            row.supervising_partner_id = partner_id

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
            {
                "matter_no": "2026-LIT-0142",
                "title": "Prepare witness bundle",
                "description": "Compile and index affidavits for the hearing set.",
                "status": "Doing",
                "due_in_days": 5,
                "assigned_to": paralegal_id,
                "priority": "High",
                "sla_hours": 36,
                "approval_state": "pending",
                "requires_two_person_review": False,
            },
            {
                "matter_no": "2026-LIT-0142",
                "title": "Draft settlement position",
                "description": "Prepare opening settlement position and risk notes.",
                "status": "Todo",
                "due_in_days": 3,
                "assigned_to": associate_id,
                "priority": "High",
                "sla_hours": 24,
                "approval_state": "draft",
                "requires_two_person_review": False,
            },
            {
                "matter_no": "2026-LIT-0142",
                "title": "Client strategy call",
                "description": "Run strategy briefing with CFO and operations lead.",
                "status": "Done",
                "due_in_days": -1,
                "assigned_to": partner_id,
                "priority": "Medium",
                "sla_hours": 12,
                "approval_state": "approved",
                "requires_two_person_review": False,
            },
            {
                "matter_no": "2026-LIT-0142",
                "title": "Court filing QC",
                "description": "Final review before filing package submission.",
                "status": "Todo",
                "due_in_days": -2,
                "assigned_to": None,
                "priority": "Critical",
                "sla_hours": 6,
                "approval_state": "pending",
                "requires_two_person_review": True,
            },
            {
                "matter_no": "2026-EMP-0071",
                "title": "Review disciplinary record",
                "description": "Cross-check charge sheet and hearing transcript.",
                "status": "Doing",
                "due_in_days": 4,
                "assigned_to": associate_id,
                "priority": "High",
                "sla_hours": 24,
                "approval_state": "pending",
                "requires_two_person_review": False,
            },
            {
                "matter_no": "2026-EMP-0071",
                "title": "Prepare prep memo",
                "description": "Brief counsel on procedural objections and timeline.",
                "status": "Todo",
                "due_in_days": 6,
                "assigned_to": paralegal_id,
                "priority": "Medium",
                "sla_hours": 48,
                "approval_state": "draft",
                "requires_two_person_review": False,
            },
            {
                "matter_no": "2026-CORP-0033",
                "title": "Corporate registry search",
                "description": "Confirm entity status across SA and Mauritius.",
                "status": "Todo",
                "due_in_days": 2,
                "assigned_to": staff_id,
                "priority": "High",
                "sla_hours": 20,
                "approval_state": "draft",
                "requires_two_person_review": False,
            },
            {
                "matter_no": "2026-CORP-0033",
                "title": "Draft risk matrix",
                "description": "Summarize key diligence findings for board update.",
                "status": "Doing",
                "due_in_days": 1,
                "assigned_to": associate_id,
                "priority": "Critical",
                "sla_hours": 18,
                "approval_state": "pending",
                "requires_two_person_review": False,
            },
            {
                "matter_no": "2026-CORP-0033",
                "title": "Supplier concentration note",
                "description": "Escalate supplier concentration mitigation paths.",
                "status": "Todo",
                "due_in_days": -1,
                "assigned_to": None,
                "priority": "Critical",
                "sla_hours": 8,
                "approval_state": "pending",
                "requires_two_person_review": True,
            },
            {
                "matter_no": "2025-PROB-0119",
                "title": "Archive signed letters",
                "description": "Final archive and closure checklist.",
                "status": "Done",
                "due_in_days": -20,
                "assigned_to": paralegal_id,
                "priority": "Low",
                "sla_hours": 12,
                "approval_state": "approved",
                "requires_two_person_review": False,
            },
            {
                "matter_no": "2026-COM-0055",
                "title": "Mediation prep binder",
                "description": "Assemble mediation chronology and contract amendments.",
                "status": "Doing",
                "due_in_days": 3,
                "assigned_to": paralegal_id,
                "priority": "High",
                "sla_hours": 30,
                "approval_state": "pending",
                "requires_two_person_review": False,
            },
            {
                "matter_no": "2026-COM-0055",
                "title": "Without-prejudice offer draft",
                "description": "Draft structured offer and fallback options.",
                "status": "Todo",
                "due_in_days": 1,
                "assigned_to": associate_id,
                "priority": "High",
                "sla_hours": 18,
                "approval_state": "draft",
                "requires_two_person_review": False,
            },
            {
                "matter_no": "2026-COM-0055",
                "title": "Client operations workshop",
                "description": "Align legal and operations positions pre-mediation.",
                "status": "Todo",
                "due_in_days": 4,
                "assigned_to": staff_id,
                "priority": "Medium",
                "sla_hours": 24,
                "approval_state": "draft",
                "requires_two_person_review": False,
            },
            {
                "matter_no": "2026-REG-0021",
                "title": "Supplementary filing checklist",
                "description": "Confirm all mandatory annexures are complete.",
                "status": "Doing",
                "due_in_days": 2,
                "assigned_to": paralegal_id,
                "priority": "High",
                "sla_hours": 16,
                "approval_state": "pending",
                "requires_two_person_review": False,
            },
            {
                "matter_no": "2026-REG-0021",
                "title": "Regulator response tracker",
                "description": "Track open regulator clarifications and owners.",
                "status": "Todo",
                "due_in_days": 5,
                "assigned_to": staff_id,
                "priority": "Medium",
                "sla_hours": 24,
                "approval_state": "draft",
                "requires_two_person_review": False,
                "recurrence_rule": "FREQ=WEEKLY;COUNT=4",
            },
            {
                "matter_no": "2026-REG-0021",
                "title": "Risk committee briefing",
                "description": "Prepare executive memo for licensing risk exposure.",
                "status": "Todo",
                "due_in_days": -3,
                "assigned_to": partner_id,
                "priority": "Critical",
                "sla_hours": 8,
                "approval_state": "pending",
                "requires_two_person_review": True,
            },
        ]
        task_rows: list[Task] = []
        task_map: dict[tuple[str, str], Task] = {}
        for spec in task_specs:
            due_in_days = spec["due_in_days"]
            created_at = now - dt.timedelta(days=2)
            task = Task(
                matter_id=matter_map[spec["matter_no"]].id,
                title=spec["title"],
                description=spec["description"],
                status=spec["status"],
                due_date=(now.date() + dt.timedelta(days=due_in_days)),
                assigned_to=spec["assigned_to"],
                created_by=associate_id,
                created_at=created_at,
                priority=spec["priority"],
                sla_hours=spec["sla_hours"],
                approval_state=spec["approval_state"],
                requires_two_person_review=bool(spec["requires_two_person_review"]),
                recurrence_rule=spec.get("recurrence_rule"),
                approved_by=partner_id if spec["status"] == "Done" else None,
                approved_at=(created_at + dt.timedelta(hours=10)) if spec["status"] == "Done" else None,
                locked_at=(created_at + dt.timedelta(hours=12)) if spec["status"] == "Done" else None,
            )
            db.session.add(task)
            task_rows.append(task)
            task_map[(spec["matter_no"], spec["title"])] = task

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
        doc_file_rows: list[DocumentFile] = []
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
            row = DocumentFile(
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
            db.session.add(row)
            doc_file_rows.append(row)

        db.session.flush()
        doc_file_map = {row.original_filename: row for row in doc_file_rows}

        expanded_counts: dict[str, int] = {}

        # -------------------------------------------------------------------
        # Identity, Sessions, Federation, and Policy Baseline
        # -------------------------------------------------------------------
        session_specs = [
            (admin_id, "10.0.10.21", "Mozilla/5.0 (Macintosh; Intel Mac OS X)", 10, 480, None),
            (partner_id, "10.0.10.31", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", 8, 300, None),
            (associate_id, "10.0.10.41", "Mozilla/5.0 (X11; Linux x86_64)", 4, 180, None),
            (paralegal_id, "10.0.10.51", "Mozilla/5.0 (iPad; CPU OS 17_2)", 2, 120, now - dt.timedelta(hours=2)),
            (staff_id, "10.0.10.61", "Mozilla/5.0 (Android 14; Mobile)", 1, 120, None),
        ]
        for idx, (user_id, ip, user_agent, created_hours_ago, ttl_minutes, revoked_at) in enumerate(session_specs, start=1):
            token_seed = f"demo-session-{idx}-{user_id}"
            db.session.add(
                UserSession(
                    user_id=user_id,
                    session_token_hash=hashlib.sha256(token_seed.encode("utf-8")).hexdigest(),
                    ip=ip,
                    user_agent=user_agent,
                    created_at=now - dt.timedelta(hours=created_hours_ago),
                    last_seen_at=now - dt.timedelta(minutes=created_hours_ago * 3),
                    expires_at=now + dt.timedelta(minutes=ttl_minutes),
                    revoked_at=revoked_at,
                )
            )
        expanded_counts["user_sessions"] = len(session_specs)

        trusted_device_specs = [
            (admin_id, "Alicia-MacBook-Pro", "trusted-device-admin-1", 40, True),
            (partner_id, "Daniel-Lenovo-T14", "trusted-device-partner-1", 25, True),
            (associate_id, "Nandi-iPhone", "trusted-device-associate-1", 7, True),
            (paralegal_id, "Sipho-iPad", "trusted-device-paralegal-1", 10, False),
        ]
        for user_id, device_name, fingerprint_seed, created_days_ago, is_active in trusted_device_specs:
            db.session.add(
                TrustedDevice(
                    user_id=user_id,
                    device_name=device_name,
                    fingerprint_hash=hashlib.sha256(fingerprint_seed.encode("utf-8")).hexdigest(),
                    created_at=now - dt.timedelta(days=created_days_ago),
                    last_seen_at=now - dt.timedelta(hours=created_days_ago),
                    is_active=is_active,
                )
            )
        expanded_counts["trusted_devices"] = len(trusted_device_specs)

        backup_code_specs = [
            (admin_id, "ABC-123", False),
            (admin_id, "DEF-456", False),
            (admin_id, "GHI-789", True),
            (partner_id, "JKL-123", False),
            (partner_id, "MNO-456", False),
            (partner_id, "PQR-789", False),
        ]
        for idx, (user_id, backup_code, is_used) in enumerate(backup_code_specs):
            db.session.add(
                UserMFABackupCode(
                    user_id=user_id,
                    code_hash=hash_backup_code(backup_code),
                    created_at=now - dt.timedelta(days=idx + 1),
                    used_at=(now - dt.timedelta(days=1, hours=3)) if is_used else None,
                )
            )
        expanded_counts["mfa_backup_codes"] = len(backup_code_specs)

        sso_app = SSOApplication(
            name="Client Portal Companion",
            client_id="portal-companion-web",
            client_secret_hash=hash_backup_code("portal-client-secret-2026"),
            redirect_uri="https://portal.elf-ai-demo.co.za/sso/callback",
            is_active=True,
            created_at=now - dt.timedelta(days=18),
        )
        db.session.add(sso_app)
        db.session.flush()

        auth_code = SSOAuthorizationCode(
            app_id=sso_app.id,
            user_id=partner_id,
            code_hash=hashlib.sha256(b"sso-auth-code-1").hexdigest(),
            scope="openid profile matters.read",
            created_at=now - dt.timedelta(minutes=20),
            expires_at=now + dt.timedelta(minutes=10),
            consumed_at=now - dt.timedelta(minutes=15),
        )
        db.session.add(auth_code)

        sso_token = SSOToken(
            app_id=sso_app.id,
            user_id=partner_id,
            access_token_hash=hashlib.sha256(b"sso-access-token-1").hexdigest(),
            refresh_token_hash=hashlib.sha256(b"sso-refresh-token-1").hexdigest(),
            scope="openid profile matters.read",
            created_at=now - dt.timedelta(minutes=15),
            expires_at=now + dt.timedelta(hours=3),
            revoked_at=None,
        )
        db.session.add(sso_token)
        expanded_counts["sso_apps"] = 1
        expanded_counts["sso_authorization_codes"] = 1
        expanded_counts["sso_tokens"] = 1

        permission_specs = [
            ("admin", "matter", "manage", True),
            ("admin", "billing", "approve", True),
            ("admin", "trust", "post", True),
            ("lawyer", "matter", "read", True),
            ("lawyer", "matter", "update", True),
            ("lawyer", "billing", "approve", True),
            ("lawyer", "trust", "approve", True),
            ("paralegal", "matter", "read", True),
            ("paralegal", "matter", "update", True),
            ("paralegal", "billing", "approve", False),
            ("staff", "matter", "read", True),
            ("staff", "trust", "post", False),
        ]
        for role, resource, action, is_allowed in permission_specs:
            db.session.add(
                PermissionGrant(
                    role=role,
                    resource=resource,
                    action=action,
                    is_allowed=is_allowed,
                    created_at=now - dt.timedelta(days=20),
                )
            )
        expanded_counts["permission_grants"] = len(permission_specs)

        wall = EthicalWall(
            name="Acme Counterparty Isolation",
            description="Restrict staff from both sides of adverse matters and conflict-sensitive exports.",
            is_active=True,
            created_by=admin_id,
            created_at=now - dt.timedelta(days=12),
        )
        db.session.add(wall)
        db.session.flush()
        wall_rules = [
            EthicalWallRule(
                wall_id=wall.id,
                user_id=staff_id,
                is_deny=True,
                is_active=True,
                created_at=now - dt.timedelta(days=12),
            ),
            EthicalWallRule(
                wall_id=wall.id,
                user_id=paralegal_id,
                is_deny=False,
                is_active=True,
                created_at=now - dt.timedelta(days=10),
            ),
        ]
        db.session.add_all(wall_rules)
        wall_matter_links = [
            EthicalWallMatter(wall_id=wall.id, matter_id=matter_map["2026-LIT-0142"].id, created_at=now - dt.timedelta(days=12)),
            EthicalWallMatter(wall_id=wall.id, matter_id=matter_map["2026-COM-0055"].id, created_at=now - dt.timedelta(days=9)),
        ]
        db.session.add_all(wall_matter_links)
        expanded_counts["ethical_walls"] = 1
        expanded_counts["ethical_wall_rules"] = len(wall_rules)
        expanded_counts["ethical_wall_matter_links"] = len(wall_matter_links)

        legal_hold_rows = [
            LegalHold(
                matter_id=matter_map["2026-LIT-0142"].id,
                reason="Pending discovery obligations and hold notice from client GC.",
                is_active=True,
                created_by=partner_id,
                created_at=now - dt.timedelta(days=6),
                released_at=None,
            ),
            LegalHold(
                matter_id=matter_map["2025-PROB-0119"].id,
                reason="Retention pending tax authority post-close review.",
                is_active=False,
                created_by=admin_id,
                created_at=now - dt.timedelta(days=40),
                released_at=now - dt.timedelta(days=8),
            ),
        ]
        db.session.add_all(legal_hold_rows)
        expanded_counts["legal_holds"] = len(legal_hold_rows)

        retention_rows = [
            RetentionPolicy(
                name="Commercial Litigation ZA",
                matter_type="Commercial Litigation",
                jurisdiction="ZA-GP",
                retain_days=3650,
                archive_after_days=730,
                is_active=True,
                created_at=now - dt.timedelta(days=45),
            ),
            RetentionPolicy(
                name="Employment Matters ZA",
                matter_type="Employment Law",
                jurisdiction="ZA-GP",
                retain_days=2555,
                archive_after_days=365,
                is_active=True,
                created_at=now - dt.timedelta(days=45),
            ),
            RetentionPolicy(
                name="Estate Administration ZA",
                matter_type="Estates",
                jurisdiction="ZA-WC",
                retain_days=4380,
                archive_after_days=1095,
                is_active=True,
                created_at=now - dt.timedelta(days=30),
            ),
        ]
        db.session.add_all(retention_rows)
        expanded_counts["retention_policies"] = len(retention_rows)

        residency_rows = [
            DataResidencyPolicy(
                name="Primary ZA Data",
                data_class="primary_storage",
                region_code="za-central-1",
                is_active=True,
                created_at=now - dt.timedelta(days=60),
            ),
            DataResidencyPolicy(
                name="Export Controls",
                data_class="exports",
                region_code="za-central-1",
                is_active=True,
                created_at=now - dt.timedelta(days=60),
            ),
            DataResidencyPolicy(
                name="Backups Residency",
                data_class="backups",
                region_code="za-central-1",
                is_active=True,
                created_at=now - dt.timedelta(days=60),
            ),
        ]
        db.session.add_all(residency_rows)
        expanded_counts["data_residency_policies"] = len(residency_rows)

        suspicious_alerts = [
            SuspiciousActivityAlert(
                alert_type="mass_export_attempt",
                severity="high",
                status="open",
                details_json=json.dumps({"matter_id": matter_map["2026-LIT-0142"].id, "attempt_count": 5}),
                created_at=now - dt.timedelta(hours=6),
                resolved_at=None,
                resolved_by=None,
            ),
            SuspiciousActivityAlert(
                alert_type="repeated_denied_access",
                severity="medium",
                status="resolved",
                details_json=json.dumps({"user_id": staff_id, "denied_count": 8}),
                created_at=now - dt.timedelta(days=2),
                resolved_at=now - dt.timedelta(days=1, hours=2),
                resolved_by=admin_id,
            ),
        ]
        db.session.add_all(suspicious_alerts)
        expanded_counts["suspicious_alerts"] = len(suspicious_alerts)

        notifications = [
            Notification(
                event_type="deadline_digest",
                actor_user_id=partner_id,
                subject_ref=f"matter:{matter_map['2026-LIT-0142'].id}",
                channel="in_app",
                status="delivered",
                created_at=now - dt.timedelta(hours=4),
                delivered_at=now - dt.timedelta(hours=4) + dt.timedelta(minutes=1),
            ),
            Notification(
                event_type="task_escalation",
                actor_user_id=associate_id,
                subject_ref=f"task:{task_map[('2026-LIT-0142', 'Court filing QC')].id}",
                channel="in_app",
                status="queued",
                created_at=now - dt.timedelta(hours=2),
                delivered_at=None,
            ),
            Notification(
                event_type="invoice_created",
                actor_user_id=partner_id,
                subject_ref=f"matter:{matter_map['2026-COM-0055'].id}",
                channel="email",
                status="delivered",
                created_at=now - dt.timedelta(days=1),
                delivered_at=now - dt.timedelta(days=1) + dt.timedelta(minutes=4),
            ),
        ]
        db.session.add_all(notifications)
        expanded_counts["notifications"] = len(notifications)

        # -------------------------------------------------------------------
        # Admin settings and templates
        # -------------------------------------------------------------------
        firm_settings = [
            ("firm_profile", {"name": "Elf AI Demo Attorneys", "jurisdiction_default": "ZA-GP", "timezone": "Africa/Johannesburg"}),
            ("default_tax", {"code": "VAT", "rate_percent": 15.0}),
            ("deadline_policy", {"default_calendar": "South Africa Court Calendar", "business_day_adjust": True}),
            (
                "office365_integration",
                {
                    "enabled": True,
                    "tenant_id": "elf-ai-demo-tenant",
                    "client_id": "elf-ai-demo-office365-client",
                    "domain_hint": "elf-ai-demo.co.za",
                    "sync_notes": "Pilot enabled for Outlook calendar and Excel exports.",
                },
            ),
            (
                "third_party_integration_defaults",
                {
                    "cost_recovery_enabled": True,
                    "conveyancing_enabled": True,
                    "last_sync_note": "Demo profile with import/export templates preconfigured.",
                },
            ),
        ]
        for key, value in firm_settings:
            db.session.add(
                FirmSetting(
                    setting_key=key,
                    setting_value_json=json.dumps(value),
                    updated_at=now - dt.timedelta(days=3),
                    updated_by=admin_id,
                )
            )
        expanded_counts["firm_settings"] = len(firm_settings)

        office_specs = [
            ("Johannesburg", "ZA-GP", True),
            ("Cape Town", "ZA-WC", True),
            ("Durban", "ZA-KZN", True),
        ]
        office_rows: dict[str, Office] = {}
        for name, jurisdiction, is_active in office_specs:
            row = Office(
                name=name,
                jurisdiction=jurisdiction,
                is_active=is_active,
                created_at=now - dt.timedelta(days=200),
            )
            db.session.add(row)
            office_rows[name] = row
        expanded_counts["offices"] = len(office_specs)

        practice_area_rows = []
        for name in ["Commercial Litigation", "Employment Law", "Corporate and M&A", "Regulatory", "Estates"]:
            row = PracticeArea(name=name, is_active=True, created_at=now - dt.timedelta(days=180))
            db.session.add(row)
            practice_area_rows.append(row)
        expanded_counts["practice_areas"] = len(practice_area_rows)

        timekeeper_role_rows: dict[str, TimekeeperRole] = {}
        for name in ["Partner", "Associate", "Paralegal", "Support Staff"]:
            row = TimekeeperRole(name=name, is_active=True)
            db.session.add(row)
            timekeeper_role_rows[name] = row
        expanded_counts["timekeeper_roles"] = len(timekeeper_role_rows)

        matter_templates = [
            MatterTemplate(
                name="Commercial Dispute Standard",
                practice_area="Commercial Litigation",
                default_stage="Intake",
                default_risk_level="Medium",
                checklist_json=json.dumps(["Conflict check", "Engagement letter", "Initial strategy memo"]),
                created_by=admin_id,
                created_at=now - dt.timedelta(days=20),
            ),
            MatterTemplate(
                name="Regulatory Remediation",
                practice_area="Regulatory",
                default_stage="Assessment",
                default_risk_level="High",
                checklist_json=json.dumps(["Regulator notice review", "Filing checklist", "Executive briefing"]),
                created_by=admin_id,
                created_at=now - dt.timedelta(days=20),
            ),
        ]
        db.session.add_all(matter_templates)
        expanded_counts["matter_templates"] = len(matter_templates)

        task_template = TaskTemplate(
            name="Litigation Hearing Sprint",
            matter_type="Commercial Litigation",
            priority="High",
            sla_hours=24,
            recurrence_rule=None,
            created_by=admin_id,
            created_at=now - dt.timedelta(days=18),
        )
        db.session.add(task_template)
        db.session.flush()
        task_template_items = [
            TaskTemplateItem(task_template_id=task_template.id, title="Bundle QA", description="Confirm indexed exhibits", position=1),
            TaskTemplateItem(task_template_id=task_template.id, title="Witness confirmations", description="Finalize witness readiness", position=2),
            TaskTemplateItem(task_template_id=task_template.id, title="Counsel prep memo", description="Summarize hearing objectives", position=3),
        ]
        db.session.add_all(task_template_items)
        expanded_counts["task_templates"] = 1
        expanded_counts["task_template_items"] = len(task_template_items)

        document_templates = [
            DocumentTemplate(
                name="Engagement Letter ZA",
                template_type="engagement_letter",
                body="This engagement letter confirms scope, fees, and obligations under South African law.",
                requires_signature=True,
                created_by=admin_id,
                created_at=now - dt.timedelta(days=15),
            ),
            DocumentTemplate(
                name="Without Prejudice Offer",
                template_type="settlement_offer",
                body="This communication is made without prejudice and for settlement purposes only.",
                requires_signature=False,
                created_by=partner_id,
                created_at=now - dt.timedelta(days=10),
            ),
        ]
        db.session.add_all(document_templates)
        expanded_counts["document_templates"] = len(document_templates)

        db.session.flush()

        # -------------------------------------------------------------------
        # Matter graph, notes, stage history, closing checklist
        # -------------------------------------------------------------------
        entity_specs = [
            ("Acme Holdings (Pty) Ltd", "organization", "legal@acme.co.za", "+27 11 555 0100", {"classification": "client"}),
            ("Mkhize Engineering", "organization", "legal@mkhizeeng.co.za", "+27 11 555 0130", {"classification": "counterparty"}),
            ("Zanele Dube", "person", "zanele.dube@acme.co.za", "+27 11 555 0101", {"role": "General Counsel"}),
            ("Ethan Ross", "person", "ethan.ross@mkhizeeng.co.za", "+27 11 555 0122", {"role": "Opposing Counsel"}),
            ("Silverstream Capital", "organization", "legal@silverstream.vc", "+27 21 555 0150", {"classification": "client"}),
            ("Blue Dune Energy", "organization", "compliance@bluedune.energy", "+27 11 555 0199", {"classification": "client"}),
            ("NERSA", "organization", "licensing@nersa.org.za", "+27 12 555 0001", {"classification": "regulator"}),
        ]
        entities: dict[str, Entity] = {}
        for name, entity_type, email, phone, metadata in entity_specs:
            row = Entity(
                name=name,
                entity_type=entity_type,
                email=email,
                phone=phone,
                metadata_json=json.dumps(metadata),
                created_at=now - dt.timedelta(days=40),
            )
            db.session.add(row)
            entities[name] = row
        db.session.flush()
        expanded_counts["entities"] = len(entity_specs)

        relationship_rows = [
            EntityRelationship(
                src_entity_id=entities["Zanele Dube"].id,
                dst_entity_id=entities["Acme Holdings (Pty) Ltd"].id,
                relationship_type="represents",
                created_at=now - dt.timedelta(days=38),
            ),
            EntityRelationship(
                src_entity_id=entities["Ethan Ross"].id,
                dst_entity_id=entities["Mkhize Engineering"].id,
                relationship_type="represents",
                created_at=now - dt.timedelta(days=38),
            ),
            EntityRelationship(
                src_entity_id=entities["Blue Dune Energy"].id,
                dst_entity_id=entities["NERSA"].id,
                relationship_type="regulated_by",
                created_at=now - dt.timedelta(days=20),
            ),
        ]
        db.session.add_all(relationship_rows)
        expanded_counts["entity_relationships"] = len(relationship_rows)

        matter_party_rows = [
            MatterParty(
                matter_id=matter_map["2026-LIT-0142"].id,
                entity_id=entities["Acme Holdings (Pty) Ltd"].id,
                party_role="client",
                is_primary=True,
                created_at=now - dt.timedelta(days=20),
            ),
            MatterParty(
                matter_id=matter_map["2026-LIT-0142"].id,
                entity_id=entities["Mkhize Engineering"].id,
                party_role="counterparty",
                is_primary=False,
                created_at=now - dt.timedelta(days=20),
            ),
            MatterParty(
                matter_id=matter_map["2026-CORP-0033"].id,
                entity_id=entities["Silverstream Capital"].id,
                party_role="client",
                is_primary=True,
                created_at=now - dt.timedelta(days=9),
            ),
            MatterParty(
                matter_id=matter_map["2026-REG-0021"].id,
                entity_id=entities["Blue Dune Energy"].id,
                party_role="client",
                is_primary=True,
                created_at=now - dt.timedelta(days=12),
            ),
            MatterParty(
                matter_id=matter_map["2026-REG-0021"].id,
                entity_id=entities["NERSA"].id,
                party_role="regulator",
                is_primary=False,
                created_at=now - dt.timedelta(days=12),
            ),
        ]
        db.session.add_all(matter_party_rows)
        expanded_counts["matter_parties"] = len(matter_party_rows)

        note_rows = [
            MatterNote(
                matter_id=matter_map["2026-LIT-0142"].id,
                body="Client approved conditional settlement range subject to board notification.",
                tags="settlement,client,strategy",
                privilege_label="Attorney-Client",
                created_by=partner_id,
                created_at=now - dt.timedelta(days=2),
                updated_at=now - dt.timedelta(days=1),
            ),
            MatterNote(
                matter_id=matter_map["2026-CORP-0033"].id,
                body="Beneficial ownership checks flagged two unresolved disclosures for escalation.",
                tags="due-diligence,ownership,escalation",
                privilege_label="Work Product",
                created_by=associate_id,
                created_at=now - dt.timedelta(days=3),
                updated_at=now - dt.timedelta(days=2),
            ),
            MatterNote(
                matter_id=matter_map["2026-REG-0021"].id,
                body="Regulator requested additional environmental compliance annexure by next week.",
                tags="regulator,follow-up",
                privilege_label="Internal",
                created_by=staff_id,
                created_at=now - dt.timedelta(days=1),
                updated_at=now - dt.timedelta(hours=12),
            ),
        ]
        db.session.add_all(note_rows)
        db.session.flush()
        note_acl_rows = [
            MatterNoteACL(note_id=note_rows[0].id, user_id=partner_id, can_read=True, can_edit=True),
            MatterNoteACL(note_id=note_rows[0].id, user_id=associate_id, can_read=True, can_edit=False),
            MatterNoteACL(note_id=note_rows[1].id, user_id=associate_id, can_read=True, can_edit=True),
            MatterNoteACL(note_id=note_rows[2].id, user_id=staff_id, can_read=True, can_edit=True),
            MatterNoteACL(note_id=note_rows[2].id, user_id=partner_id, can_read=True, can_edit=False),
        ]
        db.session.add_all(note_acl_rows)
        expanded_counts["matter_notes"] = len(note_rows)
        expanded_counts["matter_note_acl"] = len(note_acl_rows)

        stage_history_rows = [
            MatterStageHistory(
                matter_id=matter_map["2026-LIT-0142"].id,
                from_stage="Intake",
                to_stage="Discovery",
                reason="Pleadings and document requests exchanged.",
                changed_by=partner_id,
                changed_at=now - dt.timedelta(days=8),
            ),
            MatterStageHistory(
                matter_id=matter_map["2026-CORP-0033"].id,
                from_stage="Intake",
                to_stage="Due Diligence",
                reason="Transaction scope and workstreams approved.",
                changed_by=partner_id,
                changed_at=now - dt.timedelta(days=7),
            ),
            MatterStageHistory(
                matter_id=matter_map["2026-REG-0021"].id,
                from_stage="Assessment",
                to_stage="Regulatory Response",
                reason="Supplementary filing request received.",
                changed_by=partner_id,
                changed_at=now - dt.timedelta(days=6),
            ),
        ]
        db.session.add_all(stage_history_rows)
        expanded_counts["matter_stage_history"] = len(stage_history_rows)

        closing_items: list[MatterClosingChecklistItem] = []
        for matter_no, metadata in matter_metadata.items():
            matter_id = matter_map[matter_no].id
            for idx, item_text in enumerate(metadata["closing_checklist"], start=1):
                is_done = matter_no == "2025-PROB-0119" or idx == 1
                closing_items.append(
                    MatterClosingChecklistItem(
                        matter_id=matter_id,
                        item_text=item_text,
                        is_done=is_done,
                        done_at=(now - dt.timedelta(days=4 + idx)) if is_done else None,
                        done_by=partner_id if is_done else None,
                    )
                )
        db.session.add_all(closing_items)
        expanded_counts["matter_closing_items"] = len(closing_items)

        # -------------------------------------------------------------------
        # Calendaring, deadlines, task workflow extensions
        # -------------------------------------------------------------------
        holiday_rows = [
            HolidayCalendar(
                name="South Africa Court Calendar",
                jurisdiction="ZA-GP",
                office_id=office_rows["Johannesburg"].id,
                holiday_date=dt.date(now.year, 3, 21),
                label="Human Rights Day",
            ),
            HolidayCalendar(
                name="South Africa Court Calendar",
                jurisdiction="ZA-WC",
                office_id=office_rows["Cape Town"].id,
                holiday_date=dt.date(now.year, 4, 27),
                label="Freedom Day",
            ),
            HolidayCalendar(
                name="South Africa Court Calendar",
                jurisdiction="ZA-KZN",
                office_id=office_rows["Durban"].id,
                holiday_date=dt.date(now.year, 12, 16),
                label="Day of Reconciliation",
            ),
        ]
        db.session.add_all(holiday_rows)
        expanded_counts["holiday_calendar_rows"] = len(holiday_rows)

        deadline_rules = [
            DeadlineRule(
                name="High Court Filing Cutoff",
                matter_id=matter_map["2026-LIT-0142"].id,
                jurisdiction="ZA-GP",
                office_id=office_rows["Johannesburg"].id,
                trigger_type="hearing_date",
                offset_days=-5,
                business_day_adjust=True,
                is_active=True,
                created_by=partner_id,
                created_at=now - dt.timedelta(days=10),
            ),
            DeadlineRule(
                name="Regulator Response SLA",
                matter_id=matter_map["2026-REG-0021"].id,
                jurisdiction="ZA-GP",
                office_id=office_rows["Johannesburg"].id,
                trigger_type="regulator_query",
                offset_days=7,
                business_day_adjust=True,
                is_active=True,
                created_by=partner_id,
                created_at=now - dt.timedelta(days=8),
            ),
            DeadlineRule(
                name="Mediation Readiness Reminder",
                matter_id=matter_map["2026-COM-0055"].id,
                jurisdiction="ZA-KZN",
                office_id=office_rows["Durban"].id,
                trigger_type="mediation_notice",
                offset_days=3,
                business_day_adjust=False,
                is_active=True,
                created_by=associate_id,
                created_at=now - dt.timedelta(days=6),
            ),
        ]
        db.session.add_all(deadline_rules)
        db.session.flush()
        expanded_counts["deadline_rules"] = len(deadline_rules)

        deadline_rows = [
            Deadline(
                matter_id=matter_map["2026-LIT-0142"].id,
                task_id=task_map[("2026-LIT-0142", "Court filing QC")].id,
                title="Court filing package due",
                due_at=now.date() + dt.timedelta(days=2),
                is_critical=True,
                source_rule_id=deadline_rules[0].id,
                calculation_trace=json.dumps({"trigger": "hearing_date", "offset_days": -5, "business_day_adjusted": True}),
                status="open",
                acknowledged_by=partner_id,
                acknowledged_at=now - dt.timedelta(hours=5),
                override_reason=None,
                overridden_by=None,
                overridden_at=None,
                created_by=partner_id,
                created_at=now - dt.timedelta(days=1),
            ),
            Deadline(
                matter_id=matter_map["2026-REG-0021"].id,
                task_id=task_map[("2026-REG-0021", "Regulator response tracker")].id,
                title="Submit regulator clarification annexure",
                due_at=now.date() + dt.timedelta(days=5),
                is_critical=True,
                source_rule_id=deadline_rules[1].id,
                calculation_trace=json.dumps({"trigger": "regulator_query", "offset_days": 7}),
                status="open",
                acknowledged_by=None,
                acknowledged_at=None,
                override_reason="Extended by authority after interim submission",
                overridden_by=partner_id,
                overridden_at=now - dt.timedelta(hours=8),
                created_by=partner_id,
                created_at=now - dt.timedelta(days=1),
            ),
            Deadline(
                matter_id=matter_map["2026-COM-0055"].id,
                task_id=task_map[("2026-COM-0055", "Mediation prep binder")].id,
                title="Deliver mediation bundle to counsel",
                due_at=now.date() + dt.timedelta(days=1),
                is_critical=False,
                source_rule_id=deadline_rules[2].id,
                calculation_trace=json.dumps({"trigger": "mediation_notice", "offset_days": 3}),
                status="open",
                acknowledged_by=associate_id,
                acknowledged_at=now - dt.timedelta(hours=3),
                override_reason=None,
                overridden_by=None,
                overridden_at=None,
                created_by=associate_id,
                created_at=now - dt.timedelta(days=2),
            ),
        ]
        db.session.add_all(deadline_rows)
        expanded_counts["deadlines"] = len(deadline_rows)

        dependency_rows = [
            TaskDependency(
                task_id=task_map[("2026-LIT-0142", "Court filing QC")].id,
                depends_on_task_id=task_map[("2026-LIT-0142", "Prepare witness bundle")].id,
                created_at=now - dt.timedelta(days=2),
            ),
            TaskDependency(
                task_id=task_map[("2026-LIT-0142", "Court filing QC")].id,
                depends_on_task_id=task_map[("2026-LIT-0142", "Draft settlement position")].id,
                created_at=now - dt.timedelta(days=2),
            ),
            TaskDependency(
                task_id=task_map[("2026-COM-0055", "Without-prejudice offer draft")].id,
                depends_on_task_id=task_map[("2026-COM-0055", "Client operations workshop")].id,
                created_at=now - dt.timedelta(days=1),
            ),
        ]
        db.session.add_all(dependency_rows)
        expanded_counts["task_dependencies"] = len(dependency_rows)

        checklist_rows = [
            TaskChecklistItem(
                task_id=task_map[("2026-LIT-0142", "Prepare witness bundle")].id,
                item_text="Confirm exhibit pagination",
                is_done=True,
                position=1,
            ),
            TaskChecklistItem(
                task_id=task_map[("2026-LIT-0142", "Prepare witness bundle")].id,
                item_text="Validate affidavit signatures",
                is_done=False,
                position=2,
            ),
            TaskChecklistItem(
                task_id=task_map[("2026-CORP-0033", "Draft risk matrix")].id,
                item_text="Map risks by severity and probability",
                is_done=True,
                position=1,
            ),
            TaskChecklistItem(
                task_id=task_map[("2026-REG-0021", "Risk committee briefing")].id,
                item_text="Attach regulator correspondence bundle",
                is_done=False,
                position=1,
            ),
        ]
        db.session.add_all(checklist_rows)
        expanded_counts["task_checklists"] = len(checklist_rows)

        task_approvals = [
            TaskApproval(
                task_id=task_map[("2026-LIT-0142", "Court filing QC")].id,
                requested_by=associate_id,
                approver_user_id=partner_id,
                state="approved",
                notes="Approved subject to final hearing list confirmation.",
                created_at=now - dt.timedelta(hours=20),
                decided_at=now - dt.timedelta(hours=18),
            ),
            TaskApproval(
                task_id=task_map[("2026-CORP-0033", "Supplier concentration note")].id,
                requested_by=associate_id,
                approver_user_id=partner_id,
                state="pending",
                notes="Waiting for final supplier concentration data.",
                created_at=now - dt.timedelta(hours=8),
                decided_at=None,
            ),
            TaskApproval(
                task_id=task_map[("2026-REG-0021", "Risk committee briefing")].id,
                requested_by=staff_id,
                approver_user_id=partner_id,
                state="rejected",
                notes="Need updated regulator timeline before briefing.",
                created_at=now - dt.timedelta(hours=10),
                decided_at=now - dt.timedelta(hours=7),
            ),
        ]
        db.session.add_all(task_approvals)
        expanded_counts["task_approvals"] = len(task_approvals)

        # -------------------------------------------------------------------
        # DMS normalized containers, versions, OCR, productions, email capture
        # -------------------------------------------------------------------
        def _chain_hash(prev_hash: str | None, file_sha256: str) -> str:
            seed = f"{prev_hash or 'GENESIS'}:{file_sha256}"
            return hashlib.sha256(seed.encode("utf-8")).hexdigest()

        document_state_map = {
            "Draft": "draft",
            "For Review": "reviewed",
            "Final": "final",
        }
        document_records: dict[str, DocumentRecord] = {}
        for spec in doc_specs:
            row = DocumentRecord(
                matter_id=matter_map[spec["matter_no"]].id,
                title=Path(spec["original_filename"]).stem.replace("-", " ").title(),
                document_type=spec["category"],
                confidentiality="Confidential" if spec["is_privileged"] else "Internal",
                privilege_label="Attorney-Client" if spec["is_privileged"] else None,
                retention_category="litigation_record" if spec["category"] == "Court Filing" else "advisory",
                legal_hold=(spec["matter_no"] == "2026-LIT-0142"),
                created_by=paralegal_id,
                created_at=now - dt.timedelta(days=2),
            )
            db.session.add(row)
            document_records[spec["original_filename"]] = row
        db.session.flush()

        document_latest_version: dict[int, DocumentVersion] = {}
        all_document_versions: list[DocumentVersion] = []
        for spec in doc_specs:
            legacy = doc_file_map[spec["original_filename"]]
            container = document_records[spec["original_filename"]]
            state = document_state_map.get(spec["lifecycle_stage"], "draft")
            base_version = DocumentVersion(
                document_id=container.id,
                document_file_id=legacy.id,
                version_no=1,
                original_filename=legacy.original_filename,
                stored_filename=legacy.stored_filename,
                sha256=legacy.sha256,
                hash_chain_prev=None,
                hash_chain_current=_chain_hash(None, legacy.sha256),
                state=state,
                notes=f"Initial migration from legacy DocumentFile ({legacy.doc_version}).",
                filed_reference=None,
                is_immutable=False,
                uploaded_by=legacy.uploaded_by,
                uploaded_at=legacy.uploaded_at,
            )
            db.session.add(base_version)
            db.session.flush()
            db.session.add(
                DocumentOCRText(
                    document_version_id=base_version.id,
                    extracted_text="\n".join(spec["lines"]),
                    extracted_at=now - dt.timedelta(days=1),
                )
            )
            current_version = base_version

            if spec["original_filename"] in {"acme-hearing-pack.pdf", "silverstream-dd-brief.docx"}:
                revised_lines = spec["lines"] + ["Revision note: Counsel comments incorporated."]
                revised_payload = "\n".join(revised_lines).encode("utf-8")
                revised_filename = f"demo_dms_{container.id}_v2.txt"
                revised_path = upload_dir / revised_filename
                revised_path.write_bytes(revised_payload)
                revised_sha = hashlib.sha256(revised_payload).hexdigest()

                revised_version = DocumentVersion(
                    document_id=container.id,
                    document_file_id=None,
                    version_no=2,
                    original_filename=f"{Path(spec['original_filename']).stem}-v2.txt",
                    stored_filename=revised_filename,
                    sha256=revised_sha,
                    hash_chain_prev=base_version.hash_chain_current,
                    hash_chain_current=_chain_hash(base_version.hash_chain_current, revised_sha),
                    state="filed" if spec["original_filename"] == "acme-hearing-pack.pdf" else "final",
                    notes="Revision prepared after partner QA.",
                    filed_reference="PTA-HC-2026-2198" if spec["original_filename"] == "acme-hearing-pack.pdf" else None,
                    is_immutable=(spec["original_filename"] == "acme-hearing-pack.pdf"),
                    uploaded_by=partner_id if spec["original_filename"] == "acme-hearing-pack.pdf" else associate_id,
                    uploaded_at=now - dt.timedelta(hours=30),
                )
                db.session.add(revised_version)
                db.session.flush()
                db.session.add(
                    DocumentOCRText(
                        document_version_id=revised_version.id,
                        extracted_text="\n".join(revised_lines),
                        extracted_at=now - dt.timedelta(hours=28),
                    )
                )
                current_version = revised_version
                all_document_versions.append(revised_version)

            document_latest_version[container.id] = current_version
            all_document_versions.append(base_version)

        db.session.flush()

        lock_row = DocumentLock(
            document_id=document_records["silverstream-dd-brief.docx"].id,
            locked_by=associate_id,
            lock_reason="Transaction memo update in progress.",
            locked_at=now - dt.timedelta(hours=5),
            expires_at=now + dt.timedelta(hours=3),
            released_at=None,
        )
        db.session.add(lock_row)

        saved_searches = [
            SavedSearch(
                user_id=admin_id,
                name="Privileged court filings",
                query_json=json.dumps({"document_type": "Court Filing", "privilege_label": "Attorney-Client"}),
                matter_id=None,
                created_at=now - dt.timedelta(days=2),
            ),
            SavedSearch(
                user_id=partner_id,
                name="Mediation package",
                query_json=json.dumps({"matter_no": "2026-COM-0055", "q": "mediation"}),
                matter_id=matter_map["2026-COM-0055"].id,
                created_at=now - dt.timedelta(days=1),
            ),
        ]
        db.session.add_all(saved_searches)

        production_set = ProductionSet(
            matter_id=matter_map["2026-LIT-0142"].id,
            name="Acme Production Round 1",
            confidentiality_designation="Confidential",
            watermark_text="Produced - Confidential",
            bates_prefix="ACME",
            bates_start=1001,
            bates_end=1002,
            created_by=partner_id,
            created_at=now - dt.timedelta(hours=14),
        )
        db.session.add(production_set)
        db.session.flush()
        bates_range = BatesRange(
            production_set_id=production_set.id,
            prefix="ACME",
            start_no=1001,
            end_no=1002,
            created_at=now - dt.timedelta(hours=14),
        )
        db.session.add(bates_range)

        production_versions = [
            document_latest_version[document_records["acme-hearing-pack.pdf"].id],
            document_latest_version[document_records["board-risk-brief.txt"].id],
        ]
        production_items = []
        for offset, ver in enumerate(production_versions):
            production_items.append(
                ProductionItem(
                    production_set_id=production_set.id,
                    document_version_id=ver.id,
                    bates_number=f"ACME{1001 + offset:06d}",
                )
            )
        db.session.add_all(production_items)

        email_capture_rows = [
            EmailCapture(
                matter_id=matter_map["2026-LIT-0142"].id,
                message_id_hash=hashlib.sha256(b"<acme-lit-1@demo.mail>").hexdigest(),
                dedup_key=hashlib.sha256(b"acme-lit-1").hexdigest(),
                subject="Witness schedule confirmation",
                sender="gc@acme.co.za",
                received_at=now - dt.timedelta(hours=19),
                stored_filename="demo_email_lit_1.eml",
                attachment_hash=hashlib.sha256(b"witness-pack-attachment").hexdigest(),
                captured_by=paralegal_id,
                captured_at=now - dt.timedelta(hours=18),
            ),
            EmailCapture(
                matter_id=matter_map["2026-REG-0021"].id,
                message_id_hash=hashlib.sha256(b"<blue-dune-reg-1@demo.mail>").hexdigest(),
                dedup_key=hashlib.sha256(b"blue-dune-reg-1").hexdigest(),
                subject="Supplementary annexure request",
                sender="licensing@nersa.org.za",
                received_at=now - dt.timedelta(hours=9),
                stored_filename="demo_email_reg_1.eml",
                attachment_hash=hashlib.sha256(b"annexure-checklist").hexdigest(),
                captured_by=staff_id,
                captured_at=now - dt.timedelta(hours=8),
            ),
        ]
        db.session.add_all(email_capture_rows)
        expanded_counts["document_records"] = len(document_records)
        expanded_counts["document_versions"] = len(all_document_versions)
        expanded_counts["document_locks"] = 1
        expanded_counts["saved_searches"] = len(saved_searches)
        expanded_counts["production_sets"] = 1
        expanded_counts["production_items"] = len(production_items)
        expanded_counts["bates_ranges"] = 1
        expanded_counts["email_captures"] = len(email_capture_rows)

        # -------------------------------------------------------------------
        # Timekeeping, expenses, billing and receivables
        # -------------------------------------------------------------------
        rounding_rows = [
            TimeRoundingPolicy(
                client_name="Acme Holdings (Pty) Ltd",
                matter_id=matter_map["2026-LIT-0142"].id,
                increment_hours=0.1,
                min_narrative_length=25,
                require_activity_code=True,
                daily_hour_cap=10.0,
                is_active=True,
            ),
            TimeRoundingPolicy(
                client_name=None,
                matter_id=None,
                increment_hours=0.25,
                min_narrative_length=15,
                require_activity_code=False,
                daily_hour_cap=12.0,
                is_active=True,
            ),
        ]
        db.session.add_all(rounding_rows)
        expanded_counts["time_rounding_policies"] = len(rounding_rows)

        timer_rows = [
            TimeTimer(
                user_id=associate_id,
                matter_id=matter_map["2026-LIT-0142"].id,
                task_id=task_map[("2026-LIT-0142", "Draft settlement position")].id,
                label="Settlement drafting block",
                started_at=now - dt.timedelta(minutes=55),
                paused_at=None,
                elapsed_seconds=2400,
                status="running",
                created_at=now - dt.timedelta(hours=2),
                updated_at=now - dt.timedelta(minutes=1),
            ),
            TimeTimer(
                user_id=paralegal_id,
                matter_id=matter_map["2026-COM-0055"].id,
                task_id=task_map[("2026-COM-0055", "Mediation prep binder")].id,
                label="Mediation bundle indexing",
                started_at=now - dt.timedelta(hours=5),
                paused_at=now - dt.timedelta(hours=3),
                elapsed_seconds=3900,
                status="paused",
                created_at=now - dt.timedelta(days=1),
                updated_at=now - dt.timedelta(hours=3),
            ),
        ]
        db.session.add_all(timer_rows)
        expanded_counts["time_timers"] = len(timer_rows)

        time_entries = [
            TimeEntry(
                user_id=partner_id,
                matter_id=matter_map["2026-LIT-0142"].id,
                task_id=task_map[("2026-LIT-0142", "Client strategy call")].id,
                start_at=now - dt.timedelta(days=5, hours=4),
                end_at=now - dt.timedelta(days=5, hours=1),
                hours=3.0,
                rounded_hours=3.0,
                narrative="Client strategy call and exposure review with CFO.",
                task_code="LIT-STRAT",
                activity_code="A101",
                is_billable=True,
                status="approved",
                approved_by=partner_id,
                approved_at=now - dt.timedelta(days=4, hours=20),
                locked_at=now - dt.timedelta(days=4, hours=18),
                created_at=now - dt.timedelta(days=5),
                updated_at=now - dt.timedelta(days=4, hours=18),
            ),
            TimeEntry(
                user_id=associate_id,
                matter_id=matter_map["2026-LIT-0142"].id,
                task_id=task_map[("2026-LIT-0142", "Draft settlement position")].id,
                start_at=now - dt.timedelta(days=4, hours=6),
                end_at=now - dt.timedelta(days=4, hours=3, minutes=30),
                hours=2.5,
                rounded_hours=2.6,
                narrative="Drafted settlement framework and fallback options.",
                task_code="LIT-SETTLE",
                activity_code="A103",
                is_billable=True,
                status="approved",
                approved_by=partner_id,
                approved_at=now - dt.timedelta(days=3, hours=23),
                locked_at=None,
                created_at=now - dt.timedelta(days=4),
                updated_at=now - dt.timedelta(days=3, hours=23),
            ),
            TimeEntry(
                user_id=paralegal_id,
                matter_id=matter_map["2026-CORP-0033"].id,
                task_id=task_map[("2026-CORP-0033", "Corporate registry search")].id,
                start_at=now - dt.timedelta(days=3, hours=8),
                end_at=now - dt.timedelta(days=3, hours=4),
                hours=4.0,
                rounded_hours=4.0,
                narrative="Registry extracts and cross-jurisdiction verification.",
                task_code="DD-REG",
                activity_code="B201",
                is_billable=True,
                status="approved",
                approved_by=partner_id,
                approved_at=now - dt.timedelta(days=3, hours=2),
                locked_at=None,
                created_at=now - dt.timedelta(days=3),
                updated_at=now - dt.timedelta(days=3, hours=2),
            ),
            TimeEntry(
                user_id=staff_id,
                matter_id=matter_map["2026-REG-0021"].id,
                task_id=task_map[("2026-REG-0021", "Regulator response tracker")].id,
                start_at=now - dt.timedelta(days=2, hours=5),
                end_at=now - dt.timedelta(days=2, hours=3, minutes=45),
                hours=1.25,
                rounded_hours=1.3,
                narrative="Regulator correspondence log updates and action assignment.",
                task_code="REG-TRACK",
                activity_code="C302",
                is_billable=False,
                status="approved",
                approved_by=partner_id,
                approved_at=now - dt.timedelta(days=2, hours=2),
                locked_at=None,
                created_at=now - dt.timedelta(days=2),
                updated_at=now - dt.timedelta(days=2, hours=2),
            ),
            TimeEntry(
                user_id=associate_id,
                matter_id=matter_map["2026-COM-0055"].id,
                task_id=task_map[("2026-COM-0055", "Without-prejudice offer draft")].id,
                start_at=now - dt.timedelta(days=1, hours=4),
                end_at=now - dt.timedelta(days=1, hours=2, minutes=30),
                hours=1.5,
                rounded_hours=1.5,
                narrative="Initial offer draft pending partner review.",
                task_code="COM-OFFER",
                activity_code="A110",
                is_billable=True,
                status="draft",
                approved_by=None,
                approved_at=None,
                locked_at=None,
                created_at=now - dt.timedelta(days=1),
                updated_at=now - dt.timedelta(days=1, hours=1),
            ),
        ]
        db.session.add_all(time_entries)
        db.session.flush()
        expanded_counts["time_entries"] = len(time_entries)

        validation_rows = [
            TimeValidationEvent(
                time_entry_id=time_entries[0].id,
                event_type="validated",
                message="Narrative and activity code meet policy minimum.",
                created_at=now - dt.timedelta(days=4, hours=20),
            ),
            TimeValidationEvent(
                time_entry_id=time_entries[1].id,
                event_type="rounded",
                message="Rounded from 2.50h to 2.60h under client policy.",
                created_at=now - dt.timedelta(days=3, hours=23),
            ),
            TimeValidationEvent(
                time_entry_id=time_entries[4].id,
                event_type="warning",
                message="Draft entry pending approval.",
                created_at=now - dt.timedelta(days=1, hours=1),
            ),
        ]
        db.session.add_all(validation_rows)
        expanded_counts["time_validation_events"] = len(validation_rows)

        expense_specs = [
            {
                "matter_no": "2026-LIT-0142",
                "user_id": partner_id,
                "amount": 1800.0,
                "category": "Travel",
                "description": "Court filing courier and travel disbursements.",
                "incurred_on": now.date() - dt.timedelta(days=6),
                "status": "approved",
                "approved_by": partner_id,
                "approved_at": now - dt.timedelta(days=5),
                "filename": "demo_receipt_lit_1.txt",
                "receipt_text": "Receipt: Court courier and travel expenses, ZAR 1,800.00",
            },
            {
                "matter_no": "2026-CORP-0033",
                "user_id": staff_id,
                "amount": 750.0,
                "category": "Search Fees",
                "description": "Cross-border registry filing fees.",
                "incurred_on": now.date() - dt.timedelta(days=4),
                "status": "draft",
                "approved_by": None,
                "approved_at": None,
                "filename": "demo_receipt_corp_1.txt",
                "receipt_text": "Receipt: Corporate registry search fees, ZAR 750.00",
            },
            {
                "matter_no": "2026-REG-0021",
                "user_id": paralegal_id,
                "amount": 520.0,
                "category": "Filing",
                "description": "Supplementary filing packet print and certification.",
                "incurred_on": now.date() - dt.timedelta(days=3),
                "status": "approved",
                "approved_by": partner_id,
                "approved_at": now - dt.timedelta(days=2, hours=10),
                "filename": "demo_receipt_reg_1.txt",
                "receipt_text": "Receipt: Regulatory filing packet and certification, ZAR 520.00",
            },
        ]
        expense_rows: list[ExpenseEntry] = []
        for spec in expense_specs:
            receipt_path = upload_dir / spec["filename"]
            receipt_path.write_text(spec["receipt_text"], encoding="utf-8")
            receipt_sha = hashlib.sha256(spec["receipt_text"].encode("utf-8")).hexdigest()
            row = ExpenseEntry(
                matter_id=matter_map[spec["matter_no"]].id,
                user_id=spec["user_id"],
                amount=spec["amount"],
                currency="ZAR",
                category=spec["category"],
                description=spec["description"],
                incurred_on=spec["incurred_on"],
                is_reimbursable=True,
                status=spec["status"],
                approved_by=spec["approved_by"],
                approved_at=spec["approved_at"],
                receipt_filename=spec["filename"],
                receipt_sha256=receipt_sha,
                receipt_ocr_text=spec["receipt_text"],
                invoice_id=None,
                created_at=now - dt.timedelta(days=2),
            )
            db.session.add(row)
            expense_rows.append(row)
        db.session.flush()
        expanded_counts["expenses"] = len(expense_rows)

        rate_rows = [
            RateCard(
                name="Partner Standard",
                client_name="Acme Holdings (Pty) Ltd",
                matter_id=matter_map["2026-LIT-0142"].id,
                timekeeper_role_id=timekeeper_role_rows["Partner"].id,
                user_id=partner_id,
                currency="ZAR",
                rate_per_hour=3500.0,
                effective_from=now.date() - dt.timedelta(days=180),
                effective_to=None,
                is_active=True,
            ),
            RateCard(
                name="Associate Standard",
                client_name="Acme Holdings (Pty) Ltd",
                matter_id=matter_map["2026-LIT-0142"].id,
                timekeeper_role_id=timekeeper_role_rows["Associate"].id,
                user_id=associate_id,
                currency="ZAR",
                rate_per_hour=2200.0,
                effective_from=now.date() - dt.timedelta(days=180),
                effective_to=None,
                is_active=True,
            ),
            RateCard(
                name="Paralegal Diligence",
                client_name="Silverstream Capital",
                matter_id=matter_map["2026-CORP-0033"].id,
                timekeeper_role_id=timekeeper_role_rows["Paralegal"].id,
                user_id=paralegal_id,
                currency="ZAR",
                rate_per_hour=1400.0,
                effective_from=now.date() - dt.timedelta(days=120),
                effective_to=None,
                is_active=True,
            ),
        ]
        db.session.add_all(rate_rows)
        expanded_counts["rate_cards"] = len(rate_rows)

        fee_arrangements = [
            FeeArrangement(
                matter_id=matter_map["2026-LIT-0142"].id,
                arrangement_type="capped",
                fixed_amount=None,
                cap_amount=350000.0,
                blended_rate=None,
                notes="Cap approval required beyond threshold.",
            ),
            FeeArrangement(
                matter_id=matter_map["2026-CORP-0033"].id,
                arrangement_type="blended",
                fixed_amount=None,
                cap_amount=None,
                blended_rate=2100.0,
                notes="Blend applies across associate/paralegal diligence work.",
            ),
        ]
        db.session.add_all(fee_arrangements)
        expanded_counts["fee_arrangements"] = len(fee_arrangements)

        tax_rules = [
            TaxRule(jurisdiction="ZA-GP", name="VAT", rate_percent=15.0, is_active=True),
            TaxRule(jurisdiction="ZA-WC", name="VAT", rate_percent=15.0, is_active=True),
        ]
        db.session.add_all(tax_rules)
        expanded_counts["tax_rules"] = len(tax_rules)

        invoice_1_subtotal = 19110.0
        invoice_1_tax = 2866.5
        invoice_2_subtotal = 5600.0
        invoice_2_tax = 840.0
        invoice_rows = [
            Invoice(
                matter_id=matter_map["2026-LIT-0142"].id,
                client_name=matter_map["2026-LIT-0142"].client_name,
                period_start=now.date() - dt.timedelta(days=30),
                period_end=now.date() - dt.timedelta(days=1),
                status="approved",
                subtotal=invoice_1_subtotal,
                tax_total=invoice_1_tax,
                total=invoice_1_subtotal + invoice_1_tax,
                approved_by=partner_id,
                approved_at=now - dt.timedelta(hours=30),
                pdf_path=None,
                created_by=partner_id,
                created_at=now - dt.timedelta(hours=40),
            ),
            Invoice(
                matter_id=matter_map["2026-CORP-0033"].id,
                client_name=matter_map["2026-CORP-0033"].client_name,
                period_start=now.date() - dt.timedelta(days=28),
                period_end=now.date() - dt.timedelta(days=2),
                status="draft",
                subtotal=invoice_2_subtotal,
                tax_total=invoice_2_tax,
                total=invoice_2_subtotal + invoice_2_tax,
                approved_by=None,
                approved_at=None,
                pdf_path=None,
                created_by=associate_id,
                created_at=now - dt.timedelta(hours=14),
            ),
        ]
        db.session.add_all(invoice_rows)
        db.session.flush()
        expanded_counts["invoices"] = len(invoice_rows)

        lit_invoice = invoice_rows[0]
        corp_invoice = invoice_rows[1]
        invoice_line_rows = [
            InvoiceLine(
                invoice_id=lit_invoice.id,
                time_entry_id=time_entries[0].id,
                expense_id=None,
                description="Partner strategy and hearing prep",
                hours=time_entries[0].rounded_hours,
                rate=3500.0,
                amount=10500.0,
                tax_amount=1575.0,
                task_code=time_entries[0].task_code,
                activity_code=time_entries[0].activity_code,
            ),
            InvoiceLine(
                invoice_id=lit_invoice.id,
                time_entry_id=time_entries[1].id,
                expense_id=None,
                description="Associate settlement drafting",
                hours=time_entries[1].rounded_hours,
                rate=2200.0,
                amount=5720.0,
                tax_amount=858.0,
                task_code=time_entries[1].task_code,
                activity_code=time_entries[1].activity_code,
            ),
            InvoiceLine(
                invoice_id=lit_invoice.id,
                time_entry_id=None,
                expense_id=expense_rows[0].id,
                description="Travel and courier disbursement",
                hours=0.0,
                rate=0.0,
                amount=1800.0,
                tax_amount=270.0,
                task_code=None,
                activity_code=None,
            ),
            InvoiceLine(
                invoice_id=corp_invoice.id,
                time_entry_id=time_entries[2].id,
                expense_id=None,
                description="Registry and diligence verification",
                hours=time_entries[2].rounded_hours,
                rate=1400.0,
                amount=5600.0,
                tax_amount=840.0,
                task_code=time_entries[2].task_code,
                activity_code=time_entries[2].activity_code,
            ),
        ]
        db.session.add_all(invoice_line_rows)
        expanded_counts["invoice_lines"] = len(invoice_line_rows)

        expense_rows[0].invoice_id = lit_invoice.id

        invoice_adjustments = [
            InvoiceAdjustment(
                invoice_id=corp_invoice.id,
                adjustment_type="write_down",
                reason="Scope reduction approved by partner.",
                amount=-640.0,
                created_by=partner_id,
                created_at=now - dt.timedelta(hours=4),
            )
        ]
        db.session.add_all(invoice_adjustments)
        expanded_counts["invoice_adjustments"] = len(invoice_adjustments)

        ledes_dir = upload_dir / "ledes"
        ledes_dir.mkdir(parents=True, exist_ok=True)
        ledes_path = ledes_dir / f"demo_invoice_{lit_invoice.id}_1998b.csv"
        ledes_payload = "INVOICE_DATE|INVOICE_NUMBER|LINE_ITEM_NUMBER|LINE_ITEM_TOTAL\n"
        ledes_payload += f"{lit_invoice.period_end:%Y%m%d}|{lit_invoice.id}|1|{lit_invoice.total:.2f}\n"
        ledes_path.write_text(ledes_payload, encoding="utf-8")
        ledes_export = LEDESExport(
            invoice_id=lit_invoice.id,
            variant="1998B",
            file_path=str(ledes_path),
            created_by=partner_id,
            created_at=now - dt.timedelta(hours=3),
        )
        db.session.add(ledes_export)
        expanded_counts["ledes_exports"] = 1

        payment_rows = [
            PaymentAllocation(
                invoice_id=lit_invoice.id,
                amount=12000.0,
                method="EFT",
                reference="EFT-ACME-2026-02-11",
                status="settled",
                settled_at=now - dt.timedelta(hours=2),
                settled_by=staff_id,
                external_txn_id="ACQ-TRX-2026-0001",
                processor_note="Settled via corporate treasury transfer.",
                allocated_at=now - dt.timedelta(hours=2),
                created_by=staff_id,
            ),
            PaymentAllocation(
                invoice_id=corp_invoice.id,
                amount=1500.0,
                method="Card",
                reference="PENDING-CORP-2026-02-12",
                status="pending",
                settled_at=None,
                settled_by=None,
                external_txn_id="ACQ-TRX-2026-0002",
                processor_note="Pending settlement confirmation from gateway.",
                allocated_at=now - dt.timedelta(hours=1, minutes=20),
                created_by=staff_id,
            ),
        ]
        db.session.add_all(payment_rows)
        expanded_counts["payment_allocations"] = len(payment_rows)

        settled_total_by_invoice: dict[int, float] = {}
        for row in payment_rows:
            if (row.status or "").strip().lower() != "settled":
                continue
            invoice_id = int(row.invoice_id)
            settled_total_by_invoice[invoice_id] = round(
                float(settled_total_by_invoice.get(invoice_id, 0.0)) + float(row.amount or 0.0),
                2,
            )

        ar_rows = [
            ARSnapshot(
                as_of_date=now.date(),
                invoice_id=lit_invoice.id,
                outstanding_amount=round(float(lit_invoice.total or 0.0) - float(settled_total_by_invoice.get(lit_invoice.id, 0.0)), 2),
                aging_bucket="0-30",
                collection_notes="Payment plan acknowledged by client finance.",
                created_at=now - dt.timedelta(hours=1),
            ),
            ARSnapshot(
                as_of_date=now.date(),
                invoice_id=corp_invoice.id,
                outstanding_amount=round(float(corp_invoice.total or 0.0) - float(settled_total_by_invoice.get(corp_invoice.id, 0.0)), 2),
                aging_bucket="0-30",
                collection_notes="Draft invoice under internal review.",
                created_at=now - dt.timedelta(hours=1),
            ),
        ]
        db.session.add_all(ar_rows)
        expanded_counts["ar_snapshots"] = len(ar_rows)

        # -------------------------------------------------------------------
        # Trust accounting and reconciliation
        # -------------------------------------------------------------------
        trust_account = TrustAccount(
            name="Main Client Trust Account",
            bank_name="Standard Bank",
            account_no_last4="4419",
            jurisdiction="ZA-GP",
            currency="ZAR",
            is_active=True,
            created_at=now - dt.timedelta(days=100),
        )
        db.session.add(trust_account)
        db.session.flush()

        trust_ledger_lit = TrustClientLedger(
            trust_account_id=trust_account.id,
            client_name=matter_map["2026-LIT-0142"].client_name,
            matter_id=matter_map["2026-LIT-0142"].id,
            current_balance=110000.0,
            created_at=now - dt.timedelta(days=90),
        )
        trust_ledger_reg = TrustClientLedger(
            trust_account_id=trust_account.id,
            client_name=matter_map["2026-REG-0021"].client_name,
            matter_id=matter_map["2026-REG-0021"].id,
            current_balance=10000.0,
            created_at=now - dt.timedelta(days=60),
        )
        db.session.add_all([trust_ledger_lit, trust_ledger_reg])
        db.session.flush()

        trust_entry_1 = TrustLedgerEntry(
            trust_account_id=trust_account.id,
            client_ledger_id=trust_ledger_lit.id,
            entry_type="deposit",
            amount=120000.0,
            currency="ZAR",
            description="Initial litigation trust funding.",
            supporting_document_id=document_latest_version[document_records["acme-hearing-pack.pdf"].id].id,
            reversal_of_entry_id=None,
            immutable_ref="TRUST-2026-0001",
            created_by=partner_id,
            created_at=now - dt.timedelta(days=20),
        )
        db.session.add(trust_entry_1)
        db.session.flush()
        trust_entry_2 = TrustLedgerEntry(
            trust_account_id=trust_account.id,
            client_ledger_id=trust_ledger_lit.id,
            entry_type="disbursement",
            amount=25000.0,
            currency="ZAR",
            description="Counsel briefing and filing disbursement.",
            supporting_document_id=None,
            reversal_of_entry_id=None,
            immutable_ref="TRUST-2026-0002",
            created_by=partner_id,
            created_at=now - dt.timedelta(days=12),
        )
        db.session.add(trust_entry_2)
        db.session.flush()
        trust_entry_3 = TrustLedgerEntry(
            trust_account_id=trust_account.id,
            client_ledger_id=trust_ledger_lit.id,
            entry_type="transfer",
            amount=10000.0,
            currency="ZAR",
            description="Transfer to regulatory matter reserve.",
            supporting_document_id=None,
            reversal_of_entry_id=None,
            immutable_ref="TRUST-2026-0003",
            created_by=partner_id,
            created_at=now - dt.timedelta(days=8),
        )
        trust_entry_4 = TrustLedgerEntry(
            trust_account_id=trust_account.id,
            client_ledger_id=trust_ledger_reg.id,
            entry_type="transfer",
            amount=10000.0,
            currency="ZAR",
            description="Transfer in from litigation reserve.",
            supporting_document_id=None,
            reversal_of_entry_id=None,
            immutable_ref="TRUST-2026-0004",
            created_by=partner_id,
            created_at=now - dt.timedelta(days=8),
        )
        db.session.add_all([trust_entry_3, trust_entry_4])
        db.session.flush()
        trust_entry_5 = TrustLedgerEntry(
            trust_account_id=trust_account.id,
            client_ledger_id=trust_ledger_lit.id,
            entry_type="reversal",
            amount=25000.0,
            currency="ZAR",
            description="Reversal of duplicate counsel disbursement posting.",
            supporting_document_id=None,
            reversal_of_entry_id=trust_entry_2.id,
            immutable_ref="TRUST-2026-0005",
            created_by=admin_id,
            created_at=now - dt.timedelta(days=7),
        )
        db.session.add(trust_entry_5)
        db.session.flush()

        section86_investment_rows = [
            Section86Investment(
                trust_account_id=trust_account.id,
                client_ledger_id=trust_ledger_lit.id,
                matter_id=matter_map["2026-LIT-0142"].id,
                investment_ref="S86-LIT-2026-001",
                institution="Standard Bank Trust Desk",
                principal_amount=60000.0,
                annual_rate_percent=7.5,
                opened_on=now.date() - dt.timedelta(days=32),
                maturity_on=now.date() + dt.timedelta(days=180),
                status="active",
                source="import",
                notes="Imported from monthly Section 86 investment register.",
                created_by=partner_id,
                created_at=now - dt.timedelta(days=31),
                closed_on=None,
            ),
            Section86Investment(
                trust_account_id=trust_account.id,
                client_ledger_id=trust_ledger_reg.id,
                matter_id=matter_map["2026-REG-0021"].id,
                investment_ref="S86-REG-2026-004",
                institution="Nedbank Investment Services",
                principal_amount=25000.0,
                annual_rate_percent=6.8,
                opened_on=now.date() - dt.timedelta(days=20),
                maturity_on=now.date() + dt.timedelta(days=120),
                status="active",
                source="manual",
                notes="Opened for regulatory reserve optimization.",
                created_by=partner_id,
                created_at=now - dt.timedelta(days=20),
                closed_on=None,
            ),
        ]
        db.session.add_all(section86_investment_rows)
        db.session.flush()

        trust_entry_6 = TrustLedgerEntry(
            trust_account_id=trust_account.id,
            client_ledger_id=trust_ledger_reg.id,
            entry_type="deposit",
            amount=3.96,
            currency="ZAR",
            description="Section 86 net interest accrual 2026-02-12 [S86-REG-2026-004]",
            supporting_document_id=None,
            reversal_of_entry_id=None,
            immutable_ref="TRUST-2026-0006",
            created_by=partner_id,
            created_at=now - dt.timedelta(days=1),
        )
        db.session.add(trust_entry_6)
        db.session.flush()
        trust_ledger_reg.current_balance = round(float(trust_ledger_reg.current_balance or 0.0) + 3.96, 2)

        section86_accrual_rows = [
            Section86Accrual(
                investment_id=section86_investment_rows[0].id,
                accrual_date=now.date() - dt.timedelta(days=2),
                interest_amount=12.33,
                withholding_tax_amount=1.85,
                net_interest_amount=10.48,
                posted_entry_id=None,
                created_by=partner_id,
                created_at=now - dt.timedelta(days=2),
            ),
            Section86Accrual(
                investment_id=section86_investment_rows[1].id,
                accrual_date=now.date() - dt.timedelta(days=1),
                interest_amount=4.66,
                withholding_tax_amount=0.70,
                net_interest_amount=3.96,
                posted_entry_id=trust_entry_6.id,
                created_by=partner_id,
                created_at=now - dt.timedelta(days=1),
            ),
        ]
        db.session.add_all(section86_accrual_rows)

        statement_filename = "demo_trust_statement_main_2026_02.csv"
        statement_payload = "\n".join(
            [
                "posted_on,description,reference,debit,credit,signed_amount,running_balance",
                f"{(now.date() - dt.timedelta(days=20)).isoformat()},Initial litigation trust funding,TRUST-2026-0001,0,120000.00,120000.00,120000.00",
                f"{(now.date() - dt.timedelta(days=12)).isoformat()},Counsel disbursement,TRUST-2026-0002,25000.00,0,-25000.00,95000.00",
                f"{(now.date() - dt.timedelta(days=8)).isoformat()},Transfer out to regulatory reserve,TRUST-2026-0003,10000.00,0,-10000.00,85000.00",
                f"{(now.date() - dt.timedelta(days=8)).isoformat()},Transfer in from litigation reserve,TRUST-2026-0004,0,10000.00,10000.00,95000.00",
                f"{(now.date() - dt.timedelta(days=7)).isoformat()},Reversal of duplicate disbursement,TRUST-2026-0005,0,25000.00,25000.00,120000.00",
                f"{(now.date() - dt.timedelta(days=1)).isoformat()},Section 86 net interest accrual,TRUST-2026-0006,0,3.96,3.96,120003.96",
            ]
        )
        (upload_dir / statement_filename).write_text(statement_payload + "\n", encoding="utf-8")

        statement_import = TrustBankStatementImport(
            trust_account_id=trust_account.id,
            statement_label="Main Trust Statement - Current Month",
            source_filename=statement_filename,
            period_start=now.date() - dt.timedelta(days=30),
            period_end=now.date(),
            opening_balance=0.0,
            closing_balance=120003.96,
            currency="ZAR",
            row_count=6,
            imported_by=admin_id,
            imported_at=now - dt.timedelta(hours=7),
            notes="Demo statement import aligned to trust ledger and reconciliation.",
        )
        db.session.add(statement_import)
        db.session.flush()

        statement_line_rows = [
            TrustBankStatementLine(
                import_id=statement_import.id,
                posted_on=now.date() - dt.timedelta(days=20),
                description="Initial litigation trust funding",
                reference="TRUST-2026-0001",
                debit=0.0,
                credit=120000.0,
                signed_amount=120000.0,
                running_balance=120000.0,
                raw_json=json.dumps({"source": "seed", "type": "deposit"}),
            ),
            TrustBankStatementLine(
                import_id=statement_import.id,
                posted_on=now.date() - dt.timedelta(days=12),
                description="Counsel disbursement",
                reference="TRUST-2026-0002",
                debit=25000.0,
                credit=0.0,
                signed_amount=-25000.0,
                running_balance=95000.0,
                raw_json=json.dumps({"source": "seed", "type": "disbursement"}),
            ),
            TrustBankStatementLine(
                import_id=statement_import.id,
                posted_on=now.date() - dt.timedelta(days=8),
                description="Transfer out to regulatory reserve",
                reference="TRUST-2026-0003",
                debit=10000.0,
                credit=0.0,
                signed_amount=-10000.0,
                running_balance=85000.0,
                raw_json=json.dumps({"source": "seed", "type": "transfer_out"}),
            ),
            TrustBankStatementLine(
                import_id=statement_import.id,
                posted_on=now.date() - dt.timedelta(days=8),
                description="Transfer in from litigation reserve",
                reference="TRUST-2026-0004",
                debit=0.0,
                credit=10000.0,
                signed_amount=10000.0,
                running_balance=95000.0,
                raw_json=json.dumps({"source": "seed", "type": "transfer_in"}),
            ),
            TrustBankStatementLine(
                import_id=statement_import.id,
                posted_on=now.date() - dt.timedelta(days=7),
                description="Reversal of duplicate disbursement",
                reference="TRUST-2026-0005",
                debit=0.0,
                credit=25000.0,
                signed_amount=25000.0,
                running_balance=120000.0,
                raw_json=json.dumps({"source": "seed", "type": "reversal"}),
            ),
            TrustBankStatementLine(
                import_id=statement_import.id,
                posted_on=now.date() - dt.timedelta(days=1),
                description="Section 86 net interest accrual",
                reference="TRUST-2026-0006",
                debit=0.0,
                credit=3.96,
                signed_amount=3.96,
                running_balance=120003.96,
                raw_json=json.dumps({"source": "seed", "type": "section86_interest"}),
            ),
        ]
        db.session.add_all(statement_line_rows)

        trust_recon = TrustReconciliationRun(
            trust_account_id=trust_account.id,
            bank_statement_import_id=statement_import.id,
            period_start=now - dt.timedelta(days=30),
            period_end=now,
            bank_closing_balance=120003.96,
            ledger_closing_balance=120003.96,
            client_subledger_total=120003.96,
            status="balanced",
            exception_notes=None,
            created_by=admin_id,
            created_at=now - dt.timedelta(hours=6),
        )
        db.session.add(trust_recon)

        trust_alert = TrustThresholdAlert(
            client_ledger_id=trust_ledger_reg.id,
            threshold_amount=15000.0,
            current_balance=float(trust_ledger_reg.current_balance or 0.0),
            status="open",
            created_at=now - dt.timedelta(hours=5),
            resolved_at=None,
            resolved_by=None,
        )
        db.session.add(trust_alert)

        trust_approval = TrustApprovalRequest(
            action_type="transfer",
            payload_json=json.dumps(
                {
                    "trust_account_id": trust_account.id,
                    "source_ledger_id": trust_ledger_lit.id,
                    "target_ledger_id": trust_ledger_reg.id,
                    "amount": 10000.0,
                }
            ),
            status="approved",
            requested_by=associate_id,
            approved_by=partner_id,
            requested_at=now - dt.timedelta(days=8, hours=1),
            approved_at=now - dt.timedelta(days=8),
            executed_at=now - dt.timedelta(days=8),
            executed_entry_id=trust_entry_3.id,
            notes="Maker-checker policy satisfied.",
        )
        db.session.add(trust_approval)
        expanded_counts["trust_accounts"] = 1
        expanded_counts["trust_client_ledgers"] = 2
        expanded_counts["trust_ledger_entries"] = 6
        expanded_counts["trust_bank_statement_imports"] = 1
        expanded_counts["trust_bank_statement_lines"] = len(statement_line_rows)
        expanded_counts["section86_investments"] = len(section86_investment_rows)
        expanded_counts["section86_accruals"] = len(section86_accrual_rows)
        expanded_counts["trust_reconciliations"] = 1
        expanded_counts["trust_threshold_alerts"] = 1
        expanded_counts["trust_approvals"] = 1

        # -------------------------------------------------------------------
        # CRM, intake, conflicts, engagement
        # -------------------------------------------------------------------
        lead_rows = [
            CRMLead(
                full_name="Themba Nkosi",
                organization="Nkosi Manufacturing",
                email="themba.nkosi@nkosimfg.co.za",
                phone="+27 11 555 0201",
                source="Referral",
                stage="qualified",
                notes="Potential commercial recovery matter.",
                assigned_to=associate_id,
                created_by=admin_id,
                created_at=now - dt.timedelta(days=9),
                updated_at=now - dt.timedelta(days=2),
            ),
            CRMLead(
                full_name="Aisha Peters",
                organization="Peters Renewables",
                email="aisha@petersrenewables.co.za",
                phone="+27 21 555 0202",
                source="Website",
                stage="proposal",
                notes="Needs regulatory response support.",
                assigned_to=partner_id,
                created_by=admin_id,
                created_at=now - dt.timedelta(days=6),
                updated_at=now - dt.timedelta(days=1),
            ),
            CRMLead(
                full_name="Marius van Heerden",
                organization="Van Heerden Estates",
                email="marius@vhestates.co.za",
                phone="+27 11 555 0203",
                source="Conference",
                stage="new",
                notes="Preliminary probate advisory request.",
                assigned_to=staff_id,
                created_by=admin_id,
                created_at=now - dt.timedelta(days=2),
                updated_at=now - dt.timedelta(days=1),
            ),
        ]
        db.session.add_all(lead_rows)
        db.session.flush()
        expanded_counts["crm_leads"] = len(lead_rows)

        follow_ups = [
            CRMFollowUp(
                lead_id=lead_rows[0].id,
                due_at=now + dt.timedelta(days=1),
                note="Schedule conflict-screening call with finance head.",
                status="open",
                created_by=associate_id,
                created_at=now - dt.timedelta(days=1),
            ),
            CRMFollowUp(
                lead_id=lead_rows[1].id,
                due_at=now + dt.timedelta(days=2),
                note="Deliver regulatory roadmap and pricing proposal.",
                status="open",
                created_by=partner_id,
                created_at=now - dt.timedelta(hours=20),
            ),
        ]
        db.session.add_all(follow_ups)
        expanded_counts["crm_followups"] = len(follow_ups)

        intake_rows = [
            IntakeForm(
                lead_id=lead_rows[0].id,
                matter_id=matter_map["2026-LIT-0142"].id,
                data_json=json.dumps(
                    {
                        "client_name": lead_rows[0].organization,
                        "entities": [lead_rows[0].organization, "Acme Holdings (Pty) Ltd"],
                        "notes": "Potential overlap with existing counterparty list.",
                    }
                ),
                created_by=associate_id,
                created_at=now - dt.timedelta(days=1),
            ),
            IntakeForm(
                lead_id=lead_rows[1].id,
                matter_id=matter_map["2026-REG-0021"].id,
                data_json=json.dumps(
                    {
                        "client_name": lead_rows[1].organization,
                        "entities": [lead_rows[1].organization, "NERSA"],
                        "notes": "Urgent regulator response work.",
                    }
                ),
                created_by=partner_id,
                created_at=now - dt.timedelta(hours=18),
            ),
        ]
        db.session.add_all(intake_rows)
        db.session.flush()
        expanded_counts["intake_forms"] = len(intake_rows)

        conflict_rows = [
            ConflictCheck(
                intake_form_id=intake_rows[0].id,
                status="hit",
                result_json=json.dumps(
                    {
                        "matched_matter_no": "2026-LIT-0142",
                        "match_reason": "entity_name_overlap",
                        "score": 0.94,
                    }
                ),
                override_required=True,
                overridden_by=partner_id,
                override_reason="Client consent obtained and ethical wall in place.",
                created_at=now - dt.timedelta(hours=14),
            ),
            ConflictCheck(
                intake_form_id=intake_rows[1].id,
                status="clear",
                result_json=json.dumps({"score": 0.08, "matched_entities": []}),
                override_required=False,
                overridden_by=None,
                override_reason=None,
                created_at=now - dt.timedelta(hours=12),
            ),
        ]
        db.session.add_all(conflict_rows)
        expanded_counts["conflict_checks"] = len(conflict_rows)

        engagement_rows = [
            EngagementLetter(
                matter_id=matter_map["2026-LIT-0142"].id,
                template_name="Engagement Letter ZA",
                content="Engagement approved for Acme dispute resolution and hearing representation.",
                status="signed",
                signed_by="Zanele Dube",
                signed_at=now - dt.timedelta(days=5),
                signed_ip="154.0.23.11",
                created_by=partner_id,
                created_at=now - dt.timedelta(days=6),
            ),
            EngagementLetter(
                matter_id=matter_map["2026-REG-0021"].id,
                template_name="Engagement Letter ZA",
                content="Regulatory intervention scope and fee schedule.",
                status="sent",
                signed_by=None,
                signed_at=None,
                signed_ip=None,
                created_by=partner_id,
                created_at=now - dt.timedelta(days=2),
            ),
        ]
        db.session.add_all(engagement_rows)
        expanded_counts["engagement_letters"] = len(engagement_rows)

        # -------------------------------------------------------------------
        # Client portal
        # -------------------------------------------------------------------
        portal_users = [
            PortalUser(
                email="client.zanele@acme.co.za",
                full_name="Zanele Dube",
                password_hash="x",
                mfa_enabled=True,
                mfa_secret="NB2W45DFOIZSIYLUMVZXI2LOM5XW6YTB",
                is_active=True,
                created_at=now - dt.timedelta(days=12),
                last_login_at=now - dt.timedelta(hours=9),
            ),
            PortalUser(
                email="ops.bongani@ntulilogistics.co.za",
                full_name="Bongani Ndlovu",
                password_hash="x",
                mfa_enabled=False,
                mfa_secret=None,
                is_active=True,
                created_at=now - dt.timedelta(days=9),
                last_login_at=now - dt.timedelta(hours=16),
            ),
        ]
        portal_users[0].set_password(password)
        portal_users[1].set_password(password)
        db.session.add_all(portal_users)
        db.session.flush()
        expanded_counts["portal_users"] = len(portal_users)

        portal_access_rows = [
            PortalMatterAccess(
                portal_user_id=portal_users[0].id,
                matter_id=matter_map["2026-LIT-0142"].id,
                visibility_level="full_curated",
                granted_by=admin_id,
                granted_at=now - dt.timedelta(days=11),
                revoked_at=None,
            ),
            PortalMatterAccess(
                portal_user_id=portal_users[0].id,
                matter_id=matter_map["2026-COM-0055"].id,
                visibility_level="shared_docs",
                granted_by=admin_id,
                granted_at=now - dt.timedelta(days=9),
                revoked_at=None,
            ),
            PortalMatterAccess(
                portal_user_id=portal_users[1].id,
                matter_id=matter_map["2026-COM-0055"].id,
                visibility_level="summary_only",
                granted_by=admin_id,
                granted_at=now - dt.timedelta(days=8),
                revoked_at=None,
            ),
        ]
        db.session.add_all(portal_access_rows)
        expanded_counts["portal_matter_access"] = len(portal_access_rows)

        thread = PortalMessageThread(
            matter_id=matter_map["2026-LIT-0142"].id,
            subject="Discovery packet status",
            created_by_user_id=associate_id,
            created_by_portal_user_id=None,
            created_at=now - dt.timedelta(days=1, hours=8),
        )
        db.session.add(thread)
        db.session.flush()
        portal_messages = [
            PortalMessage(
                thread_id=thread.id,
                body="We have uploaded the latest discovery packet for your review.",
                from_user_id=associate_id,
                from_portal_user_id=None,
                created_at=now - dt.timedelta(days=1, hours=7),
            ),
            PortalMessage(
                thread_id=thread.id,
                body="Received, please confirm filing deadline for court bundle.",
                from_user_id=None,
                from_portal_user_id=portal_users[0].id,
                created_at=now - dt.timedelta(days=1, hours=6),
            ),
        ]
        db.session.add_all(portal_messages)
        expanded_counts["portal_threads"] = 1
        expanded_counts["portal_messages"] = len(portal_messages)

        portal_upload_payload = "invoice_ref,description,amount\nLIT-EXP-1,Courier advance,300.00\n"
        portal_upload_name = "demo_portal_upload_1.csv"
        (upload_dir / portal_upload_name).write_text(portal_upload_payload, encoding="utf-8")
        portal_upload_row = PortalUpload(
            matter_id=matter_map["2026-LIT-0142"].id,
            portal_user_id=portal_users[0].id,
            filename="client-courier-advance.csv",
            stored_filename=portal_upload_name,
            sha256=hashlib.sha256(portal_upload_payload.encode("utf-8")).hexdigest(),
            uploaded_at=now - dt.timedelta(hours=10),
        )
        db.session.add(portal_upload_row)
        expanded_counts["portal_uploads"] = 1

        portal_invoice_view = PortalInvoiceView(
            portal_user_id=portal_users[0].id,
            invoice_id=lit_invoice.id,
            last_viewed_at=now - dt.timedelta(hours=6),
        )
        db.session.add(portal_invoice_view)
        expanded_counts["portal_invoice_views"] = 1

        portal_receipt = PortalPaymentReceipt(
            invoice_id=lit_invoice.id,
            portal_user_id=portal_users[0].id,
            amount=12000.0,
            currency="ZAR",
            status="settled",
            reference="EFT-ACME-2026-02-11",
            created_at=now - dt.timedelta(hours=2),
        )
        db.session.add(portal_receipt)
        expanded_counts["portal_payment_receipts"] = 1

        link_tokens = [
            PortalLinkToken(
                portal_user_id=portal_users[0].id,
                matter_id=matter_map["2026-LIT-0142"].id,
                document_version_id=None,
                token_hash=hashlib.sha256(b"portal-link-matter-1").hexdigest(),
                expires_at=now + dt.timedelta(days=2),
                created_at=now - dt.timedelta(hours=4),
                used_at=None,
            ),
            PortalLinkToken(
                portal_user_id=portal_users[0].id,
                matter_id=None,
                document_version_id=document_latest_version[document_records["acme-hearing-pack.pdf"].id].id,
                token_hash=hashlib.sha256(b"portal-link-document-1").hexdigest(),
                expires_at=now + dt.timedelta(hours=36),
                created_at=now - dt.timedelta(hours=3),
                used_at=None,
            ),
        ]
        db.session.add_all(link_tokens)
        expanded_counts["portal_link_tokens"] = len(link_tokens)

        # -------------------------------------------------------------------
        # Analytics, worker queue, and operations controls
        # -------------------------------------------------------------------
        analytics_rows = [
            AnalyticsMetricSnapshot(
                as_of_date=now.date(),
                metric_key="utilization",
                scope_type="firm",
                scope_id=None,
                value_num=0.73,
                value_text=None,
                created_at=now - dt.timedelta(hours=1),
            ),
            AnalyticsMetricSnapshot(
                as_of_date=now.date(),
                metric_key="realization",
                scope_type="firm",
                scope_id=None,
                value_num=0.88,
                value_text=None,
                created_at=now - dt.timedelta(hours=1),
            ),
            AnalyticsMetricSnapshot(
                as_of_date=now.date(),
                metric_key="ehr",
                scope_type="firm",
                scope_id=None,
                value_num=2650.0,
                value_text="ZAR/hr",
                created_at=now - dt.timedelta(hours=1),
            ),
            AnalyticsMetricSnapshot(
                as_of_date=now.date(),
                metric_key="workload_open_tasks",
                scope_type="user",
                scope_id=associate_id,
                value_num=6,
                value_text=None,
                created_at=now - dt.timedelta(hours=1),
            ),
            AnalyticsMetricSnapshot(
                as_of_date=now.date(),
                metric_key="profitability_index",
                scope_type="matter",
                scope_id=matter_map["2026-LIT-0142"].id,
                value_num=1.24,
                value_text=None,
                created_at=now - dt.timedelta(hours=1),
            ),
        ]
        db.session.add_all(analytics_rows)
        expanded_counts["analytics_snapshots"] = len(analytics_rows)

        forecast_rows = [
            WorkloadForecast(
                as_of_date=now.date(),
                user_id=partner_id,
                predicted_hours=34.0,
                confidence=0.82,
                features_json=json.dumps({"open_tasks": 5, "active_matters": 4}),
                created_at=now - dt.timedelta(hours=1),
            ),
            WorkloadForecast(
                as_of_date=now.date(),
                user_id=associate_id,
                predicted_hours=42.5,
                confidence=0.78,
                features_json=json.dumps({"open_tasks": 8, "active_matters": 5}),
                created_at=now - dt.timedelta(hours=1),
            ),
            WorkloadForecast(
                as_of_date=now.date(),
                user_id=paralegal_id,
                predicted_hours=30.0,
                confidence=0.75,
                features_json=json.dumps({"open_tasks": 6, "active_matters": 4}),
                created_at=now - dt.timedelta(hours=1),
            ),
        ]
        db.session.add_all(forecast_rows)
        expanded_counts["workload_forecasts"] = len(forecast_rows)

        burnout_rows = [
            BurnoutSignal(
                user_id=associate_id,
                as_of_date=now.date(),
                score=0.68,
                reason="High concentration of critical deadlines",
                status="open",
                created_at=now - dt.timedelta(hours=1),
            ),
            BurnoutSignal(
                user_id=staff_id,
                as_of_date=now.date(),
                score=0.32,
                reason="Balanced workload after reassignment",
                status="monitoring",
                created_at=now - dt.timedelta(hours=1),
            ),
        ]
        db.session.add_all(burnout_rows)
        expanded_counts["burnout_signals"] = len(burnout_rows)

        job_rows = [
            JobQueue(
                job_type="deadline_sweep",
                payload_json=json.dumps({"scope": "all_matters"}),
                status="queued",
                worker_id=None,
                lease_until=None,
                attempts=0,
                max_attempts=5,
                last_error=None,
                run_after=now + dt.timedelta(minutes=5),
                created_at=now - dt.timedelta(minutes=15),
                started_at=None,
                finished_at=None,
            ),
            JobQueue(
                job_type="ocr_extract",
                payload_json=json.dumps({"document_version_id": document_latest_version[document_records["acme-hearing-pack.pdf"].id].id}),
                status="succeeded",
                worker_id="worker-1",
                lease_until=None,
                attempts=1,
                max_attempts=5,
                last_error=None,
                run_after=now - dt.timedelta(hours=6),
                created_at=now - dt.timedelta(hours=6, minutes=5),
                started_at=now - dt.timedelta(hours=6),
                finished_at=now - dt.timedelta(hours=5, minutes=58),
            ),
            JobQueue(
                job_type="suspicious_activity_scan",
                payload_json=json.dumps({"window_minutes": 60}),
                status="failed",
                worker_id="worker-2",
                lease_until=None,
                attempts=2,
                max_attempts=5,
                last_error="Transient DB timeout during scan.",
                run_after=now - dt.timedelta(minutes=20),
                created_at=now - dt.timedelta(hours=1),
                started_at=now - dt.timedelta(minutes=25),
                finished_at=now - dt.timedelta(minutes=23),
            ),
        ]
        db.session.add_all(job_rows)
        db.session.flush()
        expanded_counts["job_queue"] = len(job_rows)

        job_history_rows = [
            JobHistory(job_id=job_rows[0].id, status="queued", message="Waiting for lease claim.", created_at=now - dt.timedelta(minutes=15)),
            JobHistory(job_id=job_rows[1].id, status="running", message="OCR extraction started.", created_at=now - dt.timedelta(hours=6)),
            JobHistory(job_id=job_rows[1].id, status="succeeded", message="OCR extraction completed.", created_at=now - dt.timedelta(hours=5, minutes=58)),
            JobHistory(job_id=job_rows[2].id, status="running", message="Suspicious scan started.", created_at=now - dt.timedelta(minutes=25)),
            JobHistory(job_id=job_rows[2].id, status="failed", message="Transient DB timeout during scan.", created_at=now - dt.timedelta(minutes=23)),
        ]
        db.session.add_all(job_history_rows)
        expanded_counts["job_history"] = len(job_history_rows)

        scheduled_jobs = [
            ScheduledJob(
                job_type="deadline_sweep",
                default_payload={"scope": "all_matters"},
                interval_minutes=15,
                next_run_at=now + dt.timedelta(minutes=5),
                last_run_at=now - dt.timedelta(minutes=10),
                is_active=True,
            ),
            ScheduledJob(
                job_type="retention_sweep",
                default_payload={"mode": "archive_candidates"},
                interval_minutes=1440,
                next_run_at=now + dt.timedelta(hours=4),
                last_run_at=now - dt.timedelta(hours=20),
                is_active=True,
            ),
            ScheduledJob(
                job_type="analytics_snapshot",
                default_payload={"scope": "firm"},
                interval_minutes=60,
                next_run_at=now + dt.timedelta(minutes=30),
                last_run_at=now - dt.timedelta(minutes=35),
                is_active=True,
            ),
        ]
        db.session.add_all(scheduled_jobs)
        expanded_counts["scheduled_jobs"] = len(scheduled_jobs)

        backup_dir = upload_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_manifest_path = backup_dir / "demo_backup_manifest.json"
        backup_manifest_payload = {
            "backup_run_id": "demo-seed",
            "db_dump_path": "uploads/backups/demo_seed.dbdump",
            "uploads_archive_path": "uploads/backups/demo_seed_uploads.tar.gz",
            "db_dump_sha256": hashlib.sha256(b"demo-db-dump").hexdigest(),
            "uploads_archive_sha256": hashlib.sha256(b"demo-uploads-archive").hexdigest(),
            "encryption": {"enabled": True, "algorithm": "aes-256-gcm"},
        }
        backup_manifest_path.write_text(json.dumps(backup_manifest_payload, indent=2), encoding="utf-8")

        backup_run = BackupRun(
            started_at=now - dt.timedelta(hours=5),
            finished_at=now - dt.timedelta(hours=5) + dt.timedelta(minutes=2),
            status="succeeded",
            location=str(backup_manifest_path),
            details_json=json.dumps(backup_manifest_payload),
            triggered_by=admin_id,
        )
        db.session.add(backup_run)
        db.session.flush()

        restore_verification = RestoreVerification(
            backup_run_id=backup_run.id,
            verified_at=now - dt.timedelta(hours=4),
            status="passed",
            notes="Checksum and decryption checks validated.",
            verified_by=admin_id,
        )
        db.session.add(restore_verification)

        dr_targets = [
            DRTarget(
                name="Primary Region",
                rpo_minutes_target=30,
                rto_minutes_target=120,
                last_actual_rpo_minutes=18,
                last_actual_rto_minutes=92,
                updated_at=now - dt.timedelta(hours=2),
            ),
            DRTarget(
                name="Secondary Region",
                rpo_minutes_target=60,
                rto_minutes_target=240,
                last_actual_rpo_minutes=44,
                last_actual_rto_minutes=210,
                updated_at=now - dt.timedelta(hours=2),
            ),
        ]
        db.session.add_all(dr_targets)
        expanded_counts["backup_runs"] = 1
        expanded_counts["restore_verifications"] = 1
        expanded_counts["dr_targets"] = len(dr_targets)

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
            ("demo_seed", "System", None, admin_id, {"seeded": True, "version": 5}),
            ("login", "User", admin_id, admin_id, {"channel": "web"}),
            ("matter_summary_update", "Matter", matter_map["2026-LIT-0142"].id, partner_id, {"risk_level": "High"}),
            ("document_upload", "DocumentFile", doc_file_map["acme-hearing-pack.pdf"].id, paralegal_id, {"filename": "acme-hearing-pack.pdf"}),
            (
                "dms_state_change",
                "DocumentVersion",
                document_latest_version[document_records["acme-hearing-pack.pdf"].id].id,
                partner_id,
                {"state": document_latest_version[document_records["acme-hearing-pack.pdf"].id].state},
            ),
            ("invoice_approve", "Invoice", lit_invoice.id, partner_id, {"total": lit_invoice.total}),
            ("trust_post", "TrustLedgerEntry", trust_entry_5.id, admin_id, {"entry_type": "reversal"}),
            ("conflict_override", "ConflictCheck", conflict_rows[0].id, partner_id, {"override": True}),
            ("backup_run", "BackupRun", backup_run.id, admin_id, {"status": backup_run.status}),
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

        existing_task_assignee_pairs = {
            (int(task_id), int(user_id))
            for task_id, user_id in db.session.query(TaskAssignee.task_id, TaskAssignee.user_id).all()
            if task_id is not None and user_id is not None
        }
        seeded_tasks = Task.query.filter(Task.assigned_to.isnot(None)).all()
        for seeded_task in seeded_tasks:
            pair = (int(seeded_task.id), int(seeded_task.assigned_to))
            if pair in existing_task_assignee_pairs:
                continue
            db.session.add(
                TaskAssignee(
                    task_id=seeded_task.id,
                    user_id=seeded_task.assigned_to,
                    assigned_by=seeded_task.created_by,
                    assigned_at=seeded_task.created_at or now,
                )
            )
            existing_task_assignee_pairs.add(pair)

        multi_assignee_specs = [
            (task_map[("2026-LIT-0142", "Prepare witness bundle")].id, associate_id, partner_id),
            (task_map[("2026-COM-0055", "Client operations workshop")].id, partner_id, associate_id),
            (task_map[("2026-REG-0021", "Regulator response tracker")].id, paralegal_id, partner_id),
        ]
        for task_id, user_id, assigned_by in multi_assignee_specs:
            pair = (int(task_id), int(user_id))
            if pair in existing_task_assignee_pairs:
                continue
            db.session.add(
                TaskAssignee(
                    task_id=task_id,
                    user_id=user_id,
                    assigned_by=assigned_by,
                    assigned_at=now - dt.timedelta(hours=6),
                )
            )
            existing_task_assignee_pairs.add(pair)
        expanded_counts["task_assignees"] = len(existing_task_assignee_pairs)

        db.session.commit()
        summary = {
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
        }
        summary.update(expanded_counts)
        summary["password"] = password
        return summary


def _reset_demo_dataset(app):
    all_models = [
        ARSnapshot,
        Announcement,
        AnalyticsMetricSnapshot,
        AuditLog,
        BackupRun,
        BatesRange,
        BurnoutSignal,
        CRMFollowUp,
        CRMLead,
        ConflictCheck,
        ConflictSemanticHit,
        Contact,
        DRTarget,
        DataResidencyPolicy,
        Deadline,
        DeadlineRule,
        DocumentFile,
        DocumentLock,
        DocumentOCRText,
        DocumentRecord,
        DocumentTemplate,
        DocumentVersion,
        EmailCapture,
        EngagementLetter,
        Entity,
        EntityRelationship,
        EthicalWall,
        EthicalWallMatter,
        EthicalWallRule,
        ExpenseEntry,
        FeeArrangement,
        FirmSetting,
        GovernanceIncident,
        HolidayCalendar,
        IntakeForm,
        Invoice,
        InvoiceAdjustment,
        InvoiceLine,
        JobHistory,
        JobQueue,
        KnowledgeBase,
        LEDESExport,
        LegalHold,
        Matter,
        MatterActivity,
        MatterClosingChecklistItem,
        MatterMember,
        MatterNote,
        MatterNoteACL,
        MatterParty,
        MatterStageHistory,
        MatterTemplate,
        MatterTimelineEvent,
        Notification,
        Office,
        PaymentAllocation,
        PermissionGrant,
        PortalInvoiceView,
        PortalLinkToken,
        PortalMatterAccess,
        PortalMessage,
        PortalMessageThread,
        PortalPaymentReceipt,
        PortalUpload,
        PortalUser,
        PracticeArea,
        ProductionItem,
        ProductionSet,
        RateCard,
        RestoreVerification,
        RetentionPolicy,
        SSOApplication,
        SSOAuthorizationCode,
        SSOToken,
        SavedSearch,
        ScheduledJob,
        SuspiciousActivityAlert,
        Task,
        TaskApproval,
        TaskAssignee,
        TaskChecklistItem,
        TaskDependency,
        TaskTemplate,
        TaskTemplateItem,
        TaxRule,
        TimeEntry,
        TimeRoundingPolicy,
        TimeTimer,
        TimeValidationEvent,
        TimekeeperRole,
        TrustAccount,
        TrustApprovalRequest,
        TrustBankStatementImport,
        TrustBankStatementLine,
        TrustClientLedger,
        TrustLedgerEntry,
        TrustReconciliationRun,
        Section86Investment,
        Section86Accrual,
        TrustThresholdAlert,
        TrustedDevice,
        User,
        UserMFABackupCode,
        UserSession,
        WorkloadForecast,
    ]

    bind = db.session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        preparer = bind.dialect.identifier_preparer
        table_names = ", ".join(
            preparer.quote(name)
            for name in sorted({model.__table__.name for model in all_models})
        )
        try:
            # Avoid hanging indefinitely when other app sessions hold locks.
            db.session.execute(sa.text("SET LOCAL lock_timeout = '8s'"))
            db.session.execute(sa.text("SET LOCAL statement_timeout = '120s'"))
            db.session.execute(sa.text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            raise SystemExit(
                "Database reset timed out waiting for locks. Stop app/worker/scheduler services and retry."
            ) from exc
    else:
        if bind is not None and bind.dialect.name == "sqlite":
            # Clear hold flags so legal-hold delete guards permit demo reset paths.
            db.session.query(LegalHold).update({LegalHold.is_active: False}, synchronize_session=False)
            db.session.query(DocumentRecord).update({DocumentRecord.legal_hold: False}, synchronize_session=False)
            db.session.commit()

            # Demo reset must bypass immutable trigger guards before bulk deletes.
            sqlite_triggers = db.session.execute(
                sa.text("SELECT name FROM sqlite_master WHERE type='trigger'")
            ).scalars().all()
            for trigger_name in sqlite_triggers:
                lowered = str(trigger_name).lower()
                if "audit_log_no_" in lowered or "trust_ledger_no_" in lowered:
                    db.session.execute(sa.text(f'DROP TRIGGER IF EXISTS "{trigger_name}"'))
            db.session.commit()

        delete_order = [
            AuditLog,
            SuspiciousActivityAlert,
            Notification,
            RestoreVerification,
            BackupRun,
            DRTarget,
            JobHistory,
            JobQueue,
            PortalLinkToken,
            PortalPaymentReceipt,
            PortalInvoiceView,
            PortalUpload,
            PortalMessage,
            PortalMessageThread,
            PortalMatterAccess,
            PortalUser,
            EngagementLetter,
            ConflictSemanticHit,
            ConflictCheck,
            IntakeForm,
            CRMFollowUp,
            CRMLead,
            TrustApprovalRequest,
            TrustThresholdAlert,
            Section86Accrual,
            TrustReconciliationRun,
            TrustBankStatementLine,
            TrustBankStatementImport,
            TrustLedgerEntry,
            Section86Investment,
            TrustClientLedger,
            TrustAccount,
            ARSnapshot,
            PaymentAllocation,
            LEDESExport,
            InvoiceAdjustment,
            InvoiceLine,
            ExpenseEntry,
            Invoice,
            TaxRule,
            FeeArrangement,
            RateCard,
            TimeValidationEvent,
            TimeEntry,
            TimeTimer,
            TimeRoundingPolicy,
            EmailCapture,
            ProductionItem,
            BatesRange,
            ProductionSet,
            SavedSearch,
            LegalHold,
            DocumentOCRText,
            DocumentLock,
            DocumentVersion,
            DocumentRecord,
            TaskApproval,
            TaskAssignee,
            TaskChecklistItem,
            TaskDependency,
            Task,
            Deadline,
            DeadlineRule,
            HolidayCalendar,
            MatterClosingChecklistItem,
            MatterStageHistory,
            MatterNoteACL,
            MatterNote,
            MatterParty,
            EntityRelationship,
            Entity,
            MatterTimelineEvent,
            MatterActivity,
            MatterMember,
            DocumentFile,
            EthicalWallMatter,
            EthicalWallRule,
            EthicalWall,
            MatterTemplate,
            TaskTemplateItem,
            TaskTemplate,
            DocumentTemplate,
            FirmSetting,
            PracticeArea,
            Office,
            TimekeeperRole,
            PermissionGrant,
            RetentionPolicy,
            DataResidencyPolicy,
            GovernanceIncident,
            KnowledgeBase,
            Contact,
            Announcement,
            WorkloadForecast,
            BurnoutSignal,
            AnalyticsMetricSnapshot,
            SSOToken,
            SSOAuthorizationCode,
            SSOApplication,
            UserMFABackupCode,
            TrustedDevice,
            UserSession,
            ScheduledJob,
            Matter,
            User,
        ]
        for model in delete_order:
            db.session.query(model).delete(synchronize_session=False)
        db.session.commit()

    upload_dir = Path(app.config["UPLOAD_DIR"])
    if upload_dir.exists():
        for path in upload_dir.rglob("demo_*"):
            if path.is_file():
                path.unlink()
