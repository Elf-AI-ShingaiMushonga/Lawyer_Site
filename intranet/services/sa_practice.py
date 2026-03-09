from __future__ import annotations

import json

from ..models import ContractTemplate, DocumentTemplate, Matter, MatterTemplate, PracticeArea, TaskTemplate, TaskTemplateItem


DEFAULT_SA_PRACTICE_AREAS: tuple[str, ...] = (
    "Commercial Litigation",
    "Conveyancing",
    "Corporate and Commercial",
    "Criminal Law",
    "Debt Recovery",
    "Deceased Estates",
    "Family Law",
    "General Litigation",
    "Insolvency and Business Rescue",
    "Labour and Employment",
    "Personal Injury and RAF",
    "Property Law",
    "Tax and SARS Disputes",
    "Trusts and Estates Planning",
)


SOUTH_AFRICA_PORTALS: tuple[dict[str, object], ...] = (
    {
        "slug": "court_online",
        "title": "Court Online",
        "url": "https://www.courtonline.judiciary.org.za/",
        "summary": "Official OCJ portal for civil case filing and digital court workflows.",
        "lane": "Litigation",
        "keywords": ("litigation", "court", "high court", "commercial", "family", "raf", "personal injury"),
    },
    {
        "slug": "caselines",
        "title": "CaseLines",
        "url": "https://sajustice.caselines.com/",
        "summary": "Digital bundles and hearing management for motion and trial courts.",
        "lane": "Litigation",
        "keywords": ("litigation", "motion", "trial", "bundle", "family", "commercial"),
    },
    {
        "slug": "cipc",
        "title": "CIPC eServices",
        "url": "https://eservices.cipc.co.za/",
        "summary": "Company registrations, amendments, annual returns, and corporate records.",
        "lane": "Corporate",
        "keywords": ("corporate", "commercial", "company", "cipc", "director", "share", "secretary"),
    },
    {
        "slug": "sars",
        "title": "SARS eFiling",
        "url": "https://secure.sarsefiling.co.za/landing",
        "summary": "Tax, transfer duty, VAT, and trust-related filing workflows.",
        "lane": "Tax",
        "keywords": ("tax", "sars", "vat", "transfer", "convey", "estate", "trust", "duty"),
    },
    {
        "slug": "master",
        "title": "Master of the High Court",
        "url": "https://www.justice.gov.za/master/m_about.html",
        "summary": "Official Master information for deceased estates, insolvency, and trusts.",
        "lane": "Estates",
        "keywords": ("estate", "deceased", "insolvency", "trust", "curator", "liquidation"),
    },
    {
        "slug": "ccma",
        "title": "CCMA Case Management",
        "url": "https://cmsonline.ccma.org.za/",
        "summary": "Conciliation, arbitration, and labour dispute filing and tracking.",
        "lane": "Labour",
        "keywords": ("labour", "employment", "ccma", "dismissal", "disciplinary", "conciliation"),
    },
    {
        "slug": "fic",
        "title": "Financial Intelligence Centre",
        "url": "https://www.fic.gov.za/",
        "summary": "FICA guidance, accountable institution obligations, and compliance resources.",
        "lane": "Compliance",
        "keywords": ("fica", "fic", "compliance", "kyc", "aml", "client due diligence"),
    },
    {
        "slug": "lpc",
        "title": "Legal Practice Council",
        "url": "https://lpc.org.za/",
        "summary": "Professional regulation, practice management, and compliance notices for practitioners.",
        "lane": "Regulatory",
        "keywords": ("lpc", "legal practice council", "compliance", "regulatory", "practitioner"),
    },
    {
        "slug": "saflii",
        "title": "SAFLII",
        "url": "https://www.saflii.org/",
        "summary": "Research current and historical South African case law and legislation.",
        "lane": "Research",
        "keywords": ("research", "case law", "precedent", "appeal", "judgment", "authority"),
    },
)


SOUTH_AFRICA_PLAYBOOKS: tuple[dict[str, object], ...] = (
    {
        "archetype": {
            "name": "SA Conveyancing - Property Transfer",
            "legal_category": "Property Law",
            "practice_area": "Conveyancing",
            "default_stage": "Instruction Received",
            "default_risk_level": "Medium",
            "checklist": [
                "Confirm FICA pack is complete for all principals.",
                "Verify title deed, rates clearance, and transfer duty inputs.",
                "Track lodgement, prep, and registration target dates.",
                "Confirm trust receipt and disbursement controls before registration.",
                "Archive signed transfer pack and proof of registration.",
            ],
            "required_fields": [
                {"key": "property_description", "label": "Property Description", "help": "Erf, unit, or sectional title description."},
                {"key": "seller_name", "label": "Seller Name", "help": "Primary transferor."},
                {"key": "purchaser_name", "label": "Purchaser Name", "help": "Primary transferee."},
                {"key": "transfer_duty_reference", "label": "Transfer Duty Reference", "help": "SARS transfer duty or exemption reference."},
            ],
            "boilerplate_template": (
                "Matter {{ matter_no }} concerns the transfer of {{ property_description }} for {{ client_name }}. "
                "Seller: {{ seller_name }}. Purchaser: {{ purchaser_name }}. "
                "Transfer duty reference: {{ transfer_duty_reference }}."
            ),
        },
        "document_templates": [
            {
                "name": "SA Conveyancing Opening Pack",
                "template_type": "Checklist",
                "body": (
                    "Open conveyancing matter {{ matter_no }} for {{ client_name }}.\n"
                    "Property: {{ property_description }}\n"
                    "Seller: {{ seller_name }}\n"
                    "Purchaser: {{ purchaser_name }}\n"
                    "Transfer duty reference: {{ transfer_duty_reference }}\n"
                    "Stage: {{ stage }}"
                ),
            }
        ],
        "contract_templates": [
            {
                "name": "SA Conveyancing Engagement Letter",
                "contract_type": "Engagement Letter",
                "body": (
                    "We record our mandate to attend to the transfer of {{ property_description }} for {{ client_name }}. "
                    "The transaction parties are {{ seller_name }} and {{ purchaser_name }}. "
                    "Professional fees, disbursements, and trust requirements apply in accordance with South African law."
                ),
            }
        ],
        "task_template": {
            "name": "SA Conveyancing Transfer Checklist",
            "matter_type": "Conveyancing",
            "priority": "High",
            "items": [
                "Open matter and verify FICA pack",
                "Request and review transfer documents",
                "Confirm transfer duty position",
                "Prepare for lodgement",
                "Monitor registration and report to client",
            ],
        },
    },
    {
        "archetype": {
            "name": "SA Deceased Estate Administration",
            "legal_category": "Compliance",
            "practice_area": "Trusts and Estates Planning",
            "default_stage": "Letters of Executorship Pending",
            "default_risk_level": "High",
            "checklist": [
                "Capture Master reference and executor appointment status.",
                "Validate next-of-kin, asset schedule, and reporting documents.",
                "Track notices, advertisements, and claims timetable.",
                "Monitor liquidation and distribution account milestones.",
                "Retain proof of final distribution and estate close-out.",
            ],
            "required_fields": [
                {"key": "deceased_name", "label": "Deceased Name", "help": "Full legal name of the deceased."},
                {"key": "master_reference", "label": "Master Reference", "help": "Master of the High Court estate reference."},
                {"key": "executor_name", "label": "Executor Name", "help": "Appointed executor or nominee."},
                {"key": "date_of_death", "label": "Date of Death", "help": "ISO date preferred."},
            ],
            "boilerplate_template": (
                "Matter {{ matter_no }} concerns the administration of the estate of {{ deceased_name }}. "
                "Master reference: {{ master_reference }}. Executor: {{ executor_name }}. "
                "Date of death: {{ date_of_death }}."
            ),
        },
        "document_templates": [
            {
                "name": "SA Deceased Estate Master Pack",
                "template_type": "Memo",
                "body": (
                    "Estate administration summary for {{ deceased_name }}.\n"
                    "Master reference: {{ master_reference }}\n"
                    "Executor: {{ executor_name }}\n"
                    "Date of death: {{ date_of_death }}\n"
                    "Matter: {{ matter_no }}"
                ),
            }
        ],
        "contract_templates": [
            {
                "name": "SA Estate Administration Mandate",
                "contract_type": "Mandate",
                "body": (
                    "We confirm our appointment to assist {{ client_name }} with the administration of the estate of {{ deceased_name }} "
                    "under Master reference {{ master_reference }}. Our mandate covers reporting, asset collection, and estate administration support."
                ),
            }
        ],
        "task_template": {
            "name": "SA Estate Administration Checklist",
            "matter_type": "Trusts and Estates Planning",
            "priority": "High",
            "items": [
                "Open Master file and confirm reporting pack",
                "Obtain executor documents and asset schedule",
                "Prepare estate notices and advertisements",
                "Draft liquidation and distribution account",
                "Finalize distribution and close estate file",
            ],
        },
    },
    {
        "archetype": {
            "name": "SA CCMA Unfair Dismissal",
            "legal_category": "Labour Law",
            "practice_area": "Labour and Employment",
            "default_stage": "Referral Drafting",
            "default_risk_level": "Medium",
            "checklist": [
                "Confirm referral time bar and condonation position.",
                "Capture dismissal date, forum, and referral reference.",
                "Prepare bundle, witness list, and settlement posture.",
                "Track conciliation and arbitration settings.",
                "Close out award, settlement, or review escalation steps.",
            ],
            "required_fields": [
                {"key": "employee_name", "label": "Employee Name", "help": "Applicant or affected employee."},
                {"key": "employer_name", "label": "Employer Name", "help": "Respondent employer."},
                {"key": "dismissal_date", "label": "Dismissal Date", "help": "Date of dismissal or challenged act."},
                {"key": "ccma_reference", "label": "CCMA Reference", "help": "Referral number when allocated."},
            ],
            "boilerplate_template": (
                "Matter {{ matter_no }} concerns an unfair dismissal dispute for {{ employee_name }} against {{ employer_name }}. "
                "Dismissal date: {{ dismissal_date }}. CCMA reference: {{ ccma_reference }}."
            ),
        },
        "document_templates": [
            {
                "name": "SA CCMA Referral Pack",
                "template_type": "Referral",
                "body": (
                    "CCMA intake summary\n"
                    "Employee: {{ employee_name }}\n"
                    "Employer: {{ employer_name }}\n"
                    "Dismissal date: {{ dismissal_date }}\n"
                    "CCMA reference: {{ ccma_reference }}\n"
                    "Matter: {{ matter_no }}"
                ),
            }
        ],
        "contract_templates": [
            {
                "name": "SA Labour Dispute Engagement Letter",
                "contract_type": "Engagement Letter",
                "body": (
                    "We confirm our engagement in the labour dispute between {{ employee_name }} and {{ employer_name }}. "
                    "The current forum is the CCMA under reference {{ ccma_reference }}."
                ),
            }
        ],
        "task_template": {
            "name": "SA CCMA Matter Checklist",
            "matter_type": "Labour and Employment",
            "priority": "High",
            "items": [
                "Confirm referral deadline and condonation position",
                "Prepare CCMA referral and supporting pack",
                "Settle witness and bundle strategy",
                "Track conciliation outcome",
                "Prepare arbitration or Labour Court escalation",
            ],
        },
    },
    {
        "archetype": {
            "name": "SA High Court Civil Litigation",
            "legal_category": "Litigation",
            "practice_area": "General Litigation",
            "default_stage": "Pleadings",
            "default_risk_level": "High",
            "checklist": [
                "Capture court division, judge, and case number references.",
                "Confirm service, filing, and bundle obligations.",
                "Track interlocutory applications and hearing dates.",
                "Maintain signed pleadings and counsel brief in DMS.",
                "Close the matter with order, costs, and archive controls.",
            ],
            "required_fields": [
                {"key": "court_division", "label": "Court Division", "help": "Example: Gauteng Division, Pretoria."},
                {"key": "case_number", "label": "Case Number", "help": "Allocated High Court case number."},
                {"key": "plaintiff_name", "label": "Plaintiff / Applicant", "help": "Primary initiating party."},
                {"key": "defendant_name", "label": "Defendant / Respondent", "help": "Primary opposing party."},
            ],
            "boilerplate_template": (
                "Matter {{ matter_no }} is a High Court civil litigation brief in the {{ court_division }} under case number {{ case_number }}. "
                "Parties: {{ plaintiff_name }} and {{ defendant_name }}."
            ),
        },
        "document_templates": [
            {
                "name": "SA High Court Filing Pack",
                "template_type": "Pleading",
                "body": (
                    "High Court matter summary\n"
                    "Division: {{ court_division }}\n"
                    "Case number: {{ case_number }}\n"
                    "Applicant / Plaintiff: {{ plaintiff_name }}\n"
                    "Respondent / Defendant: {{ defendant_name }}\n"
                    "Matter: {{ matter_no }}"
                ),
            }
        ],
        "contract_templates": [
            {
                "name": "SA Litigation Engagement Letter",
                "contract_type": "Engagement Letter",
                "body": (
                    "We confirm our appointment in the High Court matter between {{ plaintiff_name }} and {{ defendant_name }} "
                    "in the {{ court_division }} under case number {{ case_number }}."
                ),
            }
        ],
        "task_template": {
            "name": "SA High Court Litigation Checklist",
            "matter_type": "General Litigation",
            "priority": "High",
            "items": [
                "Confirm court division and case number",
                "Prepare and file core pleadings",
                "Monitor service and opposition timelines",
                "Assemble hearing bundle and counsel brief",
                "Capture order and post-hearing actions",
            ],
        },
    },
)


def practice_blob(matter: Matter | None) -> str:
    if matter is None:
        return ""
    values = [
        matter.title,
        matter.client_name,
        matter.description,
        matter.objective,
        matter.last_update_note,
        matter.legal_category,
        matter.practice_area,
        matter.case_type,
        matter.court_name,
        matter.jurisdiction,
        matter.stage,
    ]
    return " ".join(str(value or "").strip().lower() for value in values if value)


def south_africa_portal_recommendations(matter: Matter | None) -> list[dict[str, object]]:
    blob = practice_blob(matter)
    cards: list[dict[str, object]] = []
    for portal in SOUTH_AFRICA_PORTALS:
        score = sum(1 for keyword in portal["keywords"] if str(keyword).lower() in blob)
        is_recommended = bool(score) or str(portal["slug"]) in {"lpc", "saflii"}
        row = dict(portal)
        row["is_recommended"] = is_recommended
        row["relevance_score"] = score
        cards.append(row)
    cards.sort(
        key=lambda item: (
            not bool(item["is_recommended"]),
            -int(item["relevance_score"]),
            str(item["title"]).lower(),
        )
    )
    return cards


def south_africa_matter_reference(
    matter: Matter,
    *,
    team_names: list[str],
    deadline_count: int,
    open_task_count: int,
) -> str:
    lines = [
        f"Matter No: {matter.matter_no}",
        f"Title: {matter.title}",
        f"Client: {matter.client_name}",
        f"Practice Area: {matter.practice_area or '-'}",
        f"Case Type: {matter.case_type or '-'}",
        f"Legal Category: {matter.legal_category or '-'}",
        f"Court / Forum: {matter.court_name or '-'}",
        f"Jurisdiction: {matter.jurisdiction or '-'}",
        f"Stage: {matter.stage or '-'}",
        f"Status: {matter.status or '-'}",
        f"Upcoming Deadlines (30d): {deadline_count}",
        f"Open Tasks: {open_task_count}",
        f"Team: {', '.join(team_names) if team_names else '-'}",
    ]
    return "\n".join(lines)


def seed_south_africa_practice_areas(session) -> int:
    existing = {
        str(name).strip().casefold()
        for (name,) in session.query(PracticeArea.name).all()
        if isinstance(name, str) and str(name).strip()
    }
    created = 0
    for name in DEFAULT_SA_PRACTICE_AREAS:
        key = name.strip().casefold()
        if key in existing:
            continue
        session.add(PracticeArea(name=name, is_active=True))
        existing.add(key)
        created += 1
    return created


def seed_south_africa_playbooks(session, *, created_by: int) -> dict[str, int]:
    counts = {
        "practice_areas": seed_south_africa_practice_areas(session),
        "matter_archetypes": 0,
        "document_templates": 0,
        "contract_templates": 0,
        "task_templates": 0,
    }

    for pack in SOUTH_AFRICA_PLAYBOOKS:
        archetype_spec = dict(pack["archetype"])
        archetype = session.query(MatterTemplate).filter_by(name=archetype_spec["name"]).first()
        if archetype is None:
            archetype = MatterTemplate(
                name=str(archetype_spec["name"]),
                legal_category=str(archetype_spec["legal_category"]),
                practice_area=str(archetype_spec["practice_area"]),
                default_stage=str(archetype_spec["default_stage"]),
                default_risk_level=str(archetype_spec["default_risk_level"]),
                checklist_json=json.dumps(archetype_spec["checklist"], ensure_ascii=True),
                required_fields_json=json.dumps(archetype_spec["required_fields"], ensure_ascii=True),
                boilerplate_template=str(archetype_spec["boilerplate_template"]),
                created_by=created_by,
            )
            session.add(archetype)
            session.flush()
            counts["matter_archetypes"] += 1

        for document_spec in pack["document_templates"]:
            if session.query(DocumentTemplate).filter_by(name=str(document_spec["name"])).first() is not None:
                continue
            session.add(
                DocumentTemplate(
                    name=str(document_spec["name"]),
                    archetype_id=archetype.id,
                    template_type=str(document_spec["template_type"]),
                    body=str(document_spec["body"]),
                    requires_signature=bool(document_spec.get("requires_signature", False)),
                    created_by=created_by,
                )
            )
            counts["document_templates"] += 1

        for contract_spec in pack["contract_templates"]:
            if session.query(ContractTemplate).filter_by(name=str(contract_spec["name"])).first() is not None:
                continue
            session.add(
                ContractTemplate(
                    name=str(contract_spec["name"]),
                    legal_category=str(archetype_spec["legal_category"]),
                    archetype_id=archetype.id,
                    contract_type=str(contract_spec["contract_type"]),
                    required_fields_json=json.dumps(archetype_spec["required_fields"], ensure_ascii=True),
                    body=str(contract_spec["body"]),
                    requires_signature=bool(contract_spec.get("requires_signature", True)),
                    auto_create_on_matter_open=bool(contract_spec.get("auto_create_on_matter_open", True)),
                    is_active=True,
                    created_by=created_by,
                )
            )
            counts["contract_templates"] += 1

        task_spec = dict(pack["task_template"])
        task_template = session.query(TaskTemplate).filter_by(name=str(task_spec["name"])).first()
        if task_template is None:
            task_template = TaskTemplate(
                name=str(task_spec["name"]),
                matter_type=str(task_spec["matter_type"]),
                priority=str(task_spec.get("priority") or "Medium"),
                sla_hours=task_spec.get("sla_hours"),
                created_by=created_by,
            )
            session.add(task_template)
            session.flush()
            for index, item in enumerate(task_spec["items"], start=1):
                session.add(TaskTemplateItem(task_template_id=task_template.id, title=str(item), position=index))
            counts["task_templates"] += 1

    return counts
