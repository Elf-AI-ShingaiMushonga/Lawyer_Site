from __future__ import annotations

import datetime as dt
from ..timeutils import utc_now

from sqlalchemy import or_

from flask import abort, flash, redirect, request, url_for
from flask_login import current_user, login_required

from ..config import is_valid_email
from ..extensions import db
from ..helpers import audit, can_access_matter, normalize_query
from ..models import Matter, MatterMember, TenderChecklistItem, TenderOpportunity, User
from ..policies import enforce_permission, has_permission
from ..templates import page

SA_PROVINCES = (
    "National",
    "Eastern Cape",
    "Free State",
    "Gauteng",
    "KwaZulu-Natal",
    "Limpopo",
    "Mpumalanga",
    "Northern Cape",
    "North West",
    "Western Cape",
)

TENDER_STATUS_OPTIONS = (
    "Sourced",
    "Go / No-Go Review",
    "Awaiting Briefing",
    "Preparing Response",
    "Submitted",
    "Clarification",
    "Awarded",
    "Unsuccessful",
    "Withdrawn",
    "Cancelled",
)

TENDER_TYPE_OPTIONS = (
    "Tender",
    "RFP",
    "RFQ",
    "Panel",
    "Framework",
    "Expression of Interest",
)

PORTAL_SOURCE_OPTIONS = (
    "SA eTender Portal",
    "Municipal Portal",
    "CIDB i-Tender",
    "Department Website",
    "Manual Circulation",
)

PREFERENCE_SYSTEM_OPTIONS = (
    "Not specified",
    "80/20",
    "90/10",
    "Functionality only",
)

SUBMISSION_CHANNEL_OPTIONS = (
    "SA eTender Portal",
    "Municipal Portal",
    "Physical bid box",
    "Courier",
    "Email",
)

CHECKLIST_STATUS_OPTIONS = (
    ("pending", "Pending"),
    ("ready", "Ready"),
    ("blocked", "Blocked"),
    ("not_applicable", "Not Applicable"),
)

CHECKLIST_STATUS_VALUES = {value for value, _label in CHECKLIST_STATUS_OPTIONS}
TERMINAL_TENDER_STATUSES = {"Awarded", "Unsuccessful", "Withdrawn", "Cancelled"}

SA_TENDER_CHECKLIST = (
    ("csd_profile", "CSD supplier profile / report"),
    ("sars_tcs_pin", "SARS Tax Compliance Status (TCS) PIN"),
    ("bbbee_proof", "B-BBEE certificate or sworn affidavit"),
    ("sbd_1", "SBD 1 invitation to bid"),
    ("sbd_4", "SBD 4 declaration of interest"),
    ("sbd_6_1", "SBD 6.1 preference points claim form"),
    ("sbd_8", "SBD 8 declaration of past SCM practices"),
    ("sbd_9", "SBD 9 independent bid determination"),
    ("pricing_schedule", "Pricing schedule / BOQ"),
    ("technical_response", "Technical proposal / methodology"),
    ("authority_to_sign", "Resolution / authority to sign"),
    ("briefing_proof", "Compulsory briefing / site inspection proof"),
    ("cidb_grading", "CIDB registration and grading"),
    ("local_content", "Local content schedules and declarations"),
)


def _tender_users() -> list[User]:
    return User.query.filter(User.is_active.is_(True)).order_by(User.full_name.asc()).limit(500).all()


def _as_bool(raw: str | None, *, default: bool = False) -> bool:
    if raw is None:
        return default
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_datetime_local(raw: str | None, *, label: str, required: bool = False) -> dt.datetime | None:
    value = str(raw or "").strip()
    if not value:
        if required:
            raise ValueError(f"{label} is required.")
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must use the date/time picker value.") from exc


def _parse_date(raw: str | None, *, label: str) -> dt.date | None:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must use the date picker value.") from exc


def _tender_detail_redirect(tender_id: int):
    return redirect(url_for("tender_detail", tender_id=tender_id))


def _find_duplicate_tender(
    reference_no: str,
    issuing_authority: str,
    *,
    exclude_tender_id: int | None = None,
) -> TenderOpportunity | None:
    if not reference_no or not issuing_authority:
        return None
    query = TenderOpportunity.query.filter_by(
        reference_no=reference_no,
        issuing_authority=issuing_authority,
    )
    if exclude_tender_id:
        query = query.filter(TenderOpportunity.id != exclude_tender_id)
    return query.order_by(TenderOpportunity.id.asc()).first()


def _validate_tender_dates(
    *,
    closing_at: dt.datetime,
    briefing_required: bool,
    briefing_date: dt.datetime | None,
    validity_end_date: dt.date | None,
) -> None:
    if briefing_required and briefing_date and briefing_date > closing_at:
        raise ValueError("Briefing date cannot be after the closing date and time.")
    if validity_end_date and validity_end_date < closing_at.date():
        raise ValueError("Validity end date cannot be before the closing date.")


def _checklist_summary(items: list[TenderChecklistItem]) -> dict[str, int | str]:
    applicable = [item for item in items if item.is_required and item.status != "not_applicable"]
    ready = sum(1 for item in applicable if item.status == "ready")
    blocked = sum(1 for item in applicable if item.status == "blocked")
    pending = max(0, len(applicable) - ready - blocked)
    percent_ready = int(round((ready / len(applicable)) * 100)) if applicable else 100
    state = "Ready"
    if blocked:
        state = "Blocked"
    elif pending:
        state = "In Progress"
    return {
        "applicable": len(applicable),
        "ready": ready,
        "blocked": blocked,
        "pending": pending,
        "percent_ready": percent_ready,
        "state": state,
    }


def _ensure_sa_checklist(tender_id: int, *, actor_user_id: int | None) -> None:
    existing = {
        str(row.item_key or "").strip()
        for row in TenderChecklistItem.query.filter_by(tender_id=tender_id).all()
    }
    created = False
    for item_key, label in SA_TENDER_CHECKLIST:
        if item_key in existing:
            continue
        db.session.add(
            TenderChecklistItem(
                tender_id=tender_id,
                item_key=item_key,
                label=label,
                is_required=True,
                status="pending",
                updated_by=actor_user_id,
                updated_at=utc_now(),
            )
        )
        created = True
    if created:
        db.session.flush()


def _align_conditional_checklist(tender: TenderOpportunity) -> None:
    required_flags = {
        "briefing_proof": bool(tender.briefing_required),
        "cidb_grading": bool(tender.cidb_required),
        "local_content": bool(tender.local_content_required),
    }
    rows = TenderChecklistItem.query.filter_by(tender_id=tender.id).all()
    for row in rows:
        item_key = str(row.item_key or "").strip()
        if item_key not in required_flags:
            continue
        should_require = required_flags[item_key]
        row.is_required = should_require
        if should_require and row.status == "not_applicable":
            row.status = "pending"
        elif not should_require:
            row.status = "not_applicable"
        row.updated_at = utc_now()


def _matter_no_for_tender() -> str:
    year = dt.date.today().year
    prefix = f"{year}-TDR-"
    rows = Matter.query.filter(Matter.matter_no.like(f"{prefix}%")).all()
    max_sequence = 0
    for row in rows:
        raw = str(row.matter_no or "")
        tail = raw[len(prefix) :]
        if tail.isdigit():
            max_sequence = max(max_sequence, int(tail))
    return f"{prefix}{max_sequence + 1:04d}"


def _tender_ops_board(
    tender: TenderOpportunity,
    checklist_items: list[TenderChecklistItem],
    checklist_summary: dict[str, int | str],
    *,
    days_to_close: int | None,
    bid_manager: User | None,
    linked_matter: Matter | None,
) -> dict[str, object]:
    focus_items: list[dict[str, str]] = []
    status = str(tender.status or "").strip()
    blocked_items = [item for item in checklist_items if item.is_required and item.status == "blocked"]
    pending_items = [item for item in checklist_items if item.is_required and item.status == "pending"]

    if status in TERMINAL_TENDER_STATUSES:
        if status == "Awarded" and linked_matter:
            focus_items.append(
                {
                    "title": "Delivery workspace linked",
                    "summary": f"{linked_matter.matter_no} is already carrying the awarded work.",
                    "tone": "positive",
                }
            )
        elif status == "Awarded":
            focus_items.append(
                {
                    "title": "Ready for matter conversion",
                    "summary": "This bid is awarded. Convert it into a live matter when the delivery team is ready.",
                    "tone": "warning",
                }
            )
        else:
            focus_items.append(
                {
                    "title": "Tender is closed out",
                    "summary": f"Status is {status}. Keep the record for reference, audit history, and lessons learned.",
                    "tone": "neutral",
                }
            )
    else:
        if days_to_close is not None:
            if days_to_close < 0:
                focus_items.append(
                    {
                        "title": "Closing date has passed",
                        "summary": "Confirm whether the status should move to Submitted, Withdrawn, or Unsuccessful.",
                        "tone": "danger",
                    }
                )
            elif days_to_close <= 3:
                focus_items.append(
                    {
                        "title": "Submission window is tight",
                        "summary": f"{days_to_close} day(s) remain before closing. Final pack control needs immediate attention.",
                        "tone": "danger",
                    }
                )
            elif days_to_close <= 7:
                focus_items.append(
                    {
                        "title": "Closing date is approaching",
                        "summary": f"{days_to_close} day(s) remain. Confirm final checks, signatures, and submission routing.",
                        "tone": "warning",
                    }
                )
        if blocked_items:
            blocked_labels = ", ".join(item.label for item in blocked_items[:2])
            blocked_extra = max(0, len(blocked_items) - 2)
            blocked_suffix = f", and {blocked_extra} more" if blocked_extra else ""
            focus_items.append(
                {
                    "title": "Blocked compliance items",
                    "summary": f"{blocked_labels}{blocked_suffix} still prevent submission readiness.",
                    "tone": "danger",
                }
            )
        elif pending_items:
            focus_items.append(
                {
                    "title": "Compliance pack still open",
                    "summary": f"{len(pending_items)} required item(s) are still pending before the bid is fully submission-ready.",
                    "tone": "warning",
                }
            )
        if tender.briefing_required and not tender.briefing_date:
            focus_items.append(
                {
                    "title": "Briefing date missing",
                    "summary": "Capture the compulsory briefing or site inspection date so attendance can be planned and evidenced.",
                    "tone": "warning",
                }
            )
        if not bid_manager:
            focus_items.append(
                {
                    "title": "Bid manager not assigned",
                    "summary": "Assign an owner so submission control, clarifications, and pack completion have clear accountability.",
                    "tone": "warning",
                }
            )
        elif tender.next_action:
            focus_items.append(
                {
                    "title": "Current next action",
                    "summary": tender.next_action,
                    "tone": "accent",
                }
            )

    if not focus_items:
        focus_items.append(
            {
                "title": "Tender record is under control",
                "summary": "The ownership, dates, and compliance pack are in place with no immediate workflow gaps surfaced.",
                "tone": "positive",
            }
        )

    tone_rank = {"danger": 4, "warning": 3, "accent": 2, "positive": 1, "neutral": 0}
    board_tone = max((item.get("tone", "neutral") for item in focus_items), key=lambda value: tone_rank.get(value, 0))
    board_title = {
        "danger": "Action required",
        "warning": "Keep this bid moving",
        "accent": "Operational focus",
        "positive": "Tender is in shape",
        "neutral": "Reference state",
    }.get(board_tone, "Tender overview")
    board_summary = {
        "danger": "Resolve the highlighted blockers before the submission window tightens further.",
        "warning": "The bid is progressing, but there are still control items that need active attention.",
        "accent": "The tender is moving normally. Keep the next step visible and ownership clear.",
        "positive": "Core control signals look healthy right now.",
        "neutral": "No live action is expected from this tender state.",
    }.get(board_tone, "Tender operations summary.")

    return {
        "tone": board_tone,
        "title": board_title,
        "summary": board_summary,
        "items": focus_items[:4],
        "blocked_items": len(blocked_items),
        "pending_items": len(pending_items),
        "percent_ready": int(checklist_summary.get("percent_ready", 0) or 0),
    }


def register_tender_routes(app):
    @app.route("/tenders", methods=["GET", "POST"])
    @login_required
    def tender_list():
        if request.method == "POST":
            enforce_permission("tenders", "write")
            reference_no = normalize_query(request.form.get("reference_no", ""))
            title = normalize_query(request.form.get("title", ""))
            issuing_authority = normalize_query(request.form.get("issuing_authority", ""))
            province = normalize_query(request.form.get("province", "")) or "National"
            closing_raw = request.form.get("closing_at")
            briefing_required = _as_bool(request.form.get("briefing_required"))
            bid_manager_user_id = request.form.get("bid_manager_user_id", type=int)

            if not reference_no or not title or not issuing_authority:
                flash("Reference number, title, and issuing authority are required.", "warning")
                return redirect(url_for("tender_list"))
            if province not in SA_PROVINCES:
                flash("Select a valid South African province scope.", "warning")
                return redirect(url_for("tender_list"))
            if _find_duplicate_tender(reference_no, issuing_authority):
                flash("A tender with this reference number and issuing authority already exists.", "warning")
                return redirect(url_for("tender_list"))

            try:
                closing_at = _parse_datetime_local(closing_raw, label="Closing date and time", required=True)
            except ValueError as exc:
                flash(str(exc), "warning")
                return redirect(url_for("tender_list"))

            if bid_manager_user_id and db.session.get(User, bid_manager_user_id) is None:
                flash("Selected bid manager could not be found.", "warning")
                return redirect(url_for("tender_list"))

            status = "Awaiting Briefing" if briefing_required else "Sourced"
            tender = TenderOpportunity(
                reference_no=reference_no,
                title=title,
                issuing_authority=issuing_authority,
                province=province,
                tender_type="Tender",
                portal_source="SA eTender Portal",
                status=status,
                briefing_required=briefing_required,
                closing_at=closing_at,
                bid_manager_user_id=bid_manager_user_id or current_user.id,
                next_action=(
                    "Confirm briefing attendance and collect proof."
                    if briefing_required
                    else "Complete the bid compliance pack and prepare the initial response."
                ),
                created_by=current_user.id,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            db.session.add(tender)
            db.session.flush()
            _ensure_sa_checklist(tender.id, actor_user_id=current_user.id)
            _align_conditional_checklist(tender)
            db.session.commit()
            audit(
                "tender_create",
                "TenderOpportunity",
                tender.id,
                {"reference_no": tender.reference_no, "issuing_authority": tender.issuing_authority},
            )
            flash("Tender added to the South African tender desk.", "info")
            return redirect(url_for("tender_detail", tender_id=tender.id))

        enforce_permission("tenders", "read")
        q = normalize_query(request.args.get("q", ""))
        base = TenderOpportunity.query
        if q:
            like = f"%{q}%"
            base = base.filter(
                or_(
                    TenderOpportunity.reference_no.ilike(like),
                    TenderOpportunity.title.ilike(like),
                    TenderOpportunity.issuing_authority.ilike(like),
                    TenderOpportunity.sector.ilike(like),
                    TenderOpportunity.status.ilike(like),
                )
            )
        tenders = (
            base.order_by(TenderOpportunity.closing_at.asc(), TenderOpportunity.updated_at.desc())
            .limit(250)
            .all()
        )
        tender_ids = [int(row.id) for row in tenders]
        checklist_items = (
            TenderChecklistItem.query.filter(TenderChecklistItem.tender_id.in_(tender_ids)).all()
            if tender_ids
            else []
        )
        checklist_by_tender: dict[int, list[TenderChecklistItem]] = {tender_id: [] for tender_id in tender_ids}
        for row in checklist_items:
            checklist_by_tender.setdefault(int(row.tender_id), []).append(row)
        checklist_summary_map = {
            tender_id: _checklist_summary(rows)
            for tender_id, rows in checklist_by_tender.items()
        }

        user_ids = {int(row.bid_manager_user_id) for row in tenders if row.bid_manager_user_id}
        user_lookup = {row.id: row for row in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}

        matter_ids = {int(row.matter_id) for row in tenders if row.matter_id}
        matter_lookup = {}
        if matter_ids:
            for row in Matter.query.filter(Matter.id.in_(matter_ids)).all():
                if can_access_matter(int(row.id)):
                    matter_lookup[int(row.id)] = row

        today = dt.date.today()
        active_rows = [row for row in tenders if (row.status or "") not in TERMINAL_TENDER_STATUSES]
        due_soon_count = sum(
            1
            for row in active_rows
            if row.closing_at and today <= row.closing_at.date() <= (today + dt.timedelta(days=7))
        )
        blocked_count = sum(
            1
            for row in tenders
            if int(checklist_summary_map.get(int(row.id), {}).get("blocked", 0) or 0) > 0
        )
        stats = {
            "total": len(tenders),
            "active": len(active_rows),
            "due_soon": due_soon_count,
            "submitted": sum(1 for row in tenders if (row.status or "") == "Submitted"),
            "awarded": sum(1 for row in tenders if (row.status or "") == "Awarded"),
            "blocked": blocked_count,
        }

        return page(
            "Tender Desk",
            "tenders/list.html",
            tenders=tenders,
            checklist_summary_map=checklist_summary_map,
            stats=stats,
            user_lookup=user_lookup,
            matter_lookup=matter_lookup,
            today=today,
            q=q,
            can_write=has_permission("tenders", "write"),
            user_choices=_tender_users(),
            provinces=SA_PROVINCES,
        )

    @app.route("/tenders/<int:tender_id>", methods=["GET", "POST"])
    @login_required
    def tender_detail(tender_id: int):
        tender = db.session.get(TenderOpportunity, tender_id)
        if tender is None:
            abort(404)

        if request.method == "POST":
            enforce_permission("tenders", "write")
            reference_no = normalize_query(request.form.get("reference_no", ""))
            title = normalize_query(request.form.get("title", ""))
            issuing_authority = normalize_query(request.form.get("issuing_authority", ""))
            province = normalize_query(request.form.get("province", "")) or "National"
            status = normalize_query(request.form.get("status", "")) or "Sourced"
            tender_type = normalize_query(request.form.get("tender_type", "")) or "Tender"
            portal_source = normalize_query(request.form.get("portal_source", "")) or "SA eTender Portal"
            sector = normalize_query(request.form.get("sector", ""))
            etender_url = normalize_query(request.form.get("etender_url", ""))
            preference_system = normalize_query(request.form.get("preference_system", ""))
            submission_channel = normalize_query(request.form.get("submission_channel", ""))
            contact_person = normalize_query(request.form.get("contact_person", ""))
            contact_email = normalize_query(request.form.get("contact_email", "")).lower()
            contact_phone = normalize_query(request.form.get("contact_phone", ""))
            csd_supplier_number = normalize_query(request.form.get("csd_supplier_number", ""))
            tcs_pin = normalize_query(request.form.get("tcs_pin", ""))
            bbbee_level = normalize_query(request.form.get("bbbee_level", ""))
            cidb_grading = normalize_query(request.form.get("cidb_grading", ""))
            scope_summary = (request.form.get("scope_summary") or "").strip()
            next_action = (request.form.get("next_action") or "").strip()
            internal_notes = (request.form.get("internal_notes") or "").strip()
            submission_address = (request.form.get("submission_address") or "").strip()
            briefing_required = _as_bool(request.form.get("briefing_required"))
            cidb_required = _as_bool(request.form.get("cidb_required"))
            local_content_required = _as_bool(request.form.get("local_content_required"))
            bid_manager_user_id = request.form.get("bid_manager_user_id", type=int)

            if not reference_no or not title or not issuing_authority:
                flash("Reference number, title, and issuing authority are required.", "warning")
                return _tender_detail_redirect(tender.id)
            if province not in SA_PROVINCES:
                flash("Select a valid South African province scope.", "warning")
                return _tender_detail_redirect(tender.id)
            if _find_duplicate_tender(reference_no, issuing_authority, exclude_tender_id=tender.id):
                flash("A tender with this reference number and issuing authority already exists.", "warning")
                return _tender_detail_redirect(tender.id)
            if status not in TENDER_STATUS_OPTIONS:
                flash("Select a valid tender status.", "warning")
                return _tender_detail_redirect(tender.id)
            if tender_type not in TENDER_TYPE_OPTIONS:
                flash("Select a valid tender type.", "warning")
                return _tender_detail_redirect(tender.id)
            if portal_source not in PORTAL_SOURCE_OPTIONS:
                flash("Select a valid tender source.", "warning")
                return _tender_detail_redirect(tender.id)
            if preference_system and preference_system not in PREFERENCE_SYSTEM_OPTIONS:
                flash("Select a valid preference system.", "warning")
                return _tender_detail_redirect(tender.id)
            if submission_channel and submission_channel not in SUBMISSION_CHANNEL_OPTIONS:
                flash("Select a valid submission channel.", "warning")
                return _tender_detail_redirect(tender.id)
            if contact_email and not is_valid_email(contact_email):
                flash("Contact email format is invalid.", "warning")
                return _tender_detail_redirect(tender.id)
            if bid_manager_user_id and db.session.get(User, bid_manager_user_id) is None:
                flash("Selected bid manager could not be found.", "warning")
                return _tender_detail_redirect(tender.id)

            try:
                closing_at = _parse_datetime_local(
                    request.form.get("closing_at"),
                    label="Closing date and time",
                    required=True,
                )
                briefing_date = _parse_datetime_local(
                    request.form.get("briefing_date"),
                    label="Briefing date and time",
                    required=False,
                )
                validity_end_date = _parse_date(
                    request.form.get("validity_end_date"),
                    label="Validity end date",
                )
            except ValueError as exc:
                flash(str(exc), "warning")
                return _tender_detail_redirect(tender.id)

            try:
                _validate_tender_dates(
                    closing_at=closing_at,
                    briefing_required=briefing_required,
                    briefing_date=briefing_date,
                    validity_end_date=validity_end_date,
                )
            except ValueError as exc:
                flash(str(exc), "warning")
                return _tender_detail_redirect(tender.id)

            estimated_value_raw = normalize_query(request.form.get("estimated_value", ""))
            estimated_value = None
            if estimated_value_raw:
                try:
                    estimated_value = float(estimated_value_raw.replace(",", ""))
                except ValueError:
                    flash("Estimated value must be numeric.", "warning")
                    return _tender_detail_redirect(tender.id)

            if not briefing_required:
                briefing_date = None
            if not cidb_required:
                cidb_grading = ""

            tender.reference_no = reference_no
            tender.title = title
            tender.issuing_authority = issuing_authority
            tender.province = province
            tender.status = status
            tender.tender_type = tender_type
            tender.portal_source = portal_source
            tender.sector = sector or None
            tender.etender_url = etender_url or None
            tender.briefing_required = briefing_required
            tender.briefing_date = briefing_date
            tender.closing_at = closing_at
            tender.validity_end_date = validity_end_date
            tender.estimated_value = estimated_value
            tender.preference_system = preference_system or None
            tender.cidb_required = cidb_required
            tender.cidb_grading = cidb_grading or None
            tender.local_content_required = local_content_required
            tender.submission_channel = submission_channel or None
            tender.submission_address = submission_address or None
            tender.contact_person = contact_person or None
            tender.contact_email = contact_email or None
            tender.contact_phone = contact_phone or None
            tender.csd_supplier_number = csd_supplier_number or None
            tender.tcs_pin = tcs_pin or None
            tender.bbbee_level = bbbee_level or None
            tender.bid_manager_user_id = bid_manager_user_id or None
            tender.scope_summary = scope_summary or None
            tender.next_action = next_action or None
            tender.internal_notes = internal_notes or None
            tender.updated_at = utc_now()

            _ensure_sa_checklist(tender.id, actor_user_id=current_user.id)
            _align_conditional_checklist(tender)
            db.session.commit()
            audit(
                "tender_update",
                "TenderOpportunity",
                tender.id,
                {"status": tender.status, "province": tender.province},
            )
            flash("Tender record updated.", "info")
            return _tender_detail_redirect(tender.id)

        enforce_permission("tenders", "read")
        _ensure_sa_checklist(tender.id, actor_user_id=current_user.id)
        _align_conditional_checklist(tender)
        db.session.commit()

        checklist_items = (
            TenderChecklistItem.query.filter_by(tender_id=tender.id)
            .order_by(TenderChecklistItem.id.asc())
            .all()
        )
        checklist_summary = _checklist_summary(checklist_items)
        bid_manager = db.session.get(User, tender.bid_manager_user_id) if tender.bid_manager_user_id else None
        linked_matter = (
            db.session.get(Matter, tender.matter_id)
            if tender.matter_id and can_access_matter(int(tender.matter_id))
            else None
        )
        today = dt.date.today()
        days_to_close = None
        if tender.closing_at:
            days_to_close = (tender.closing_at.date() - today).days
        ops_board = _tender_ops_board(
            tender,
            checklist_items,
            checklist_summary,
            days_to_close=days_to_close,
            bid_manager=bid_manager,
            linked_matter=linked_matter,
        )

        return page(
            f"Tender {tender.reference_no}",
            "tenders/detail.html",
            tender=tender,
            checklist_items=checklist_items,
            checklist_summary=checklist_summary,
            checklist_status_options=CHECKLIST_STATUS_OPTIONS,
            status_options=TENDER_STATUS_OPTIONS,
            type_options=TENDER_TYPE_OPTIONS,
            portal_source_options=PORTAL_SOURCE_OPTIONS,
            preference_system_options=PREFERENCE_SYSTEM_OPTIONS,
            submission_channel_options=SUBMISSION_CHANNEL_OPTIONS,
            provinces=SA_PROVINCES,
            user_choices=_tender_users(),
            bid_manager=bid_manager,
            linked_matter=linked_matter,
            can_write=has_permission("tenders", "write"),
            can_convert=has_permission("tenders", "convert"),
            days_to_close=days_to_close,
            today=today,
            ops_board=ops_board,
        )

    @app.post("/tenders/<int:tender_id>/checklist/<int:item_id>")
    @login_required
    def tender_checklist_update(tender_id: int, item_id: int):
        enforce_permission("tenders", "write")
        tender = db.session.get(TenderOpportunity, tender_id)
        item = db.session.get(TenderChecklistItem, item_id)
        if tender is None or item is None or int(item.tender_id) != int(tender.id):
            abort(404)

        status = normalize_query(request.form.get("status", "")).replace(" ", "_").lower()
        if status not in CHECKLIST_STATUS_VALUES:
            flash("Select a valid checklist status.", "warning")
            return _tender_detail_redirect(tender.id)

        item.is_required = _as_bool(request.form.get("is_required"), default=False)
        item.status = status
        item.notes = (request.form.get("notes") or "").strip() or None
        item.updated_by = current_user.id
        item.updated_at = utc_now()
        tender.updated_at = utc_now()
        db.session.commit()
        audit(
            "tender_checklist_update",
            "TenderChecklistItem",
            item.id,
            {"tender_id": tender.id, "status": item.status, "is_required": bool(item.is_required)},
        )
        flash("Compliance item updated.", "info")
        return _tender_detail_redirect(tender.id)

    @app.post("/tenders/<int:tender_id>/matter")
    @login_required
    def tender_create_matter(tender_id: int):
        enforce_permission("tenders", "convert")
        tender = db.session.get(TenderOpportunity, tender_id)
        if tender is None:
            abort(404)
        if tender.matter_id:
            flash("This tender is already linked to a matter.", "info")
            return redirect(url_for("matter_workspace", matter_id=tender.matter_id))
        if (tender.status or "") != "Awarded":
            flash("Mark the tender as Awarded before converting it into a live matter.", "warning")
            return _tender_detail_redirect(tender.id)

        now = utc_now()
        matter = Matter(
            matter_no=_matter_no_for_tender(),
            title=f"{tender.reference_no} - {tender.title}",
            client_name=tender.issuing_authority,
            status="Open",
            description=tender.internal_notes or tender.scope_summary,
            objective=tender.next_action or tender.scope_summary,
            legal_category="Public Procurement",
            practice_area="Public Procurement",
            case_type="Tender Delivery",
            created_by=current_user.id,
            opened_at=now,
            last_updated_at=now,
        )
        db.session.add(matter)
        db.session.flush()

        member_ids = {int(current_user.id)}
        if tender.bid_manager_user_id:
            member_ids.add(int(tender.bid_manager_user_id))
        for user_id in sorted(member_ids):
            db.session.add(
                MatterMember(
                    matter_id=matter.id,
                    user_id=user_id,
                    role_in_matter="Lead" if user_id == int(tender.bid_manager_user_id or current_user.id) else "Team",
                )
            )

        tender.matter_id = matter.id
        tender.updated_at = now
        db.session.commit()
        audit(
            "tender_convert_to_matter",
            "TenderOpportunity",
            tender.id,
            {"matter_id": matter.id, "matter_no": matter.matter_no},
        )
        flash("Awarded tender converted into a live matter workspace.", "info")
        return redirect(url_for("matter_workspace", matter_id=matter.id))
