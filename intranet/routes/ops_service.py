from __future__ import annotations

import datetime as dt
from ..timeutils import utc_now

import sqlalchemy as sa
from flask import abort, flash, redirect, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..helpers import audit
from ..models import HelpdeskTicket, HelpdeskTicketComment, ITAsset, User
from ..roles import role_is_admin, role_is_support
from ..templates import page

ASSET_TYPES = [
    "laptop",
    "desktop",
    "monitor",
    "phone",
    "printer",
    "network",
    "peripheral",
    "other",
]
ASSET_STATUSES = ["in_stock", "assigned", "repair", "retired", "disposed"]
HELPDESK_CATEGORIES = ["hardware", "software", "access", "network", "printer", "procurement", "other"]
HELPDESK_PRIORITIES = ["low", "medium", "high", "critical"]
HELPDESK_STATUSES = ["new", "triaged", "in_progress", "waiting_user", "resolved", "closed"]
OPEN_TICKET_STATUSES = {"new", "triaged", "in_progress", "waiting_user"}


def _ops_it_operator() -> bool:
    role = getattr(current_user, "role", None)
    return role_is_admin(role) or role_is_support(role)


def _ops_it_required() -> None:
    if not _ops_it_operator():
        abort(403)


def _active_internal_users() -> list[User]:
    return User.query.filter(User.is_active.is_(True)).order_by(User.full_name.asc(), User.email.asc()).all()


def _choice_or_default(raw_value: str | None, allowed_values: list[str], default: str) -> str:
    value = str(raw_value or "").strip().lower()
    return value if value in allowed_values else default


def _parse_optional_date(field_name: str) -> dt.date | None:
    raw = (request.form.get(field_name) or "").strip()
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid {field_name.replace('_', ' ')}.") from exc


def _helpdesk_asset_choices() -> list[ITAsset]:
    query = ITAsset.query.order_by(ITAsset.asset_tag.asc(), ITAsset.name.asc())
    if _ops_it_operator():
        return query.all()
    return query.filter(ITAsset.assigned_user_id == current_user.id).all()


def _ticket_is_visible(ticket: HelpdeskTicket) -> bool:
    if _ops_it_operator():
        return True
    return ticket.reporter_user_id == current_user.id or ticket.assigned_to == current_user.id


def _ticket_queue_query():
    query = HelpdeskTicket.query
    if not _ops_it_operator():
        query = query.filter(
            sa.or_(
                HelpdeskTicket.reporter_user_id == current_user.id,
                HelpdeskTicket.assigned_to == current_user.id,
            )
        )
    return query


def _helpdesk_users_by_id(user_ids: set[int]) -> dict[int, User]:
    clean_ids = sorted({int(user_id) for user_id in user_ids if user_id})
    if not clean_ids:
        return {}
    return {row.id: row for row in User.query.filter(User.id.in_(clean_ids)).all()}


def register_ops_service_routes(app):
    @app.route("/ops/assets", methods=["GET", "POST"])
    @login_required
    def ops_assets():
        _ops_it_required()

        users = _active_internal_users()
        selected_asset_id = request.args.get("asset_id", type=int)
        selected_asset = db.session.get(ITAsset, selected_asset_id) if selected_asset_id else None
        if selected_asset_id and selected_asset is None:
            abort(404)

        if request.method == "POST":
            asset_id = request.form.get("asset_id", type=int)
            row = db.session.get(ITAsset, asset_id) if asset_id else None
            if asset_id and row is None:
                abort(404)

            asset_tag = (request.form.get("asset_tag") or "").strip().upper()
            name = (request.form.get("name") or "").strip()
            if not asset_tag or not name:
                flash("Asset tag and asset name are required.", "warning")
                return redirect(url_for("ops_assets", asset_id=asset_id) if asset_id else url_for("ops_assets"))

            duplicate_query = ITAsset.query.filter(ITAsset.asset_tag == asset_tag)
            if row is not None:
                duplicate_query = duplicate_query.filter(ITAsset.id != row.id)
            if duplicate_query.first():
                flash("Asset tag already exists.", "warning")
                return redirect(url_for("ops_assets", asset_id=asset_id) if asset_id else url_for("ops_assets"))

            assigned_user_id = request.form.get("assigned_user_id", type=int) or None
            if assigned_user_id and not any(user.id == assigned_user_id for user in users):
                flash("Assigned user was not found.", "warning")
                return redirect(url_for("ops_assets", asset_id=asset_id) if asset_id else url_for("ops_assets"))

            try:
                purchase_date = _parse_optional_date("purchase_date")
                warranty_expires_on = _parse_optional_date("warranty_expires_on")
            except ValueError as exc:
                flash(str(exc), "warning")
                return redirect(url_for("ops_assets", asset_id=asset_id) if asset_id else url_for("ops_assets"))

            now = utc_now()
            created = row is None
            if row is None:
                row = ITAsset(created_by=current_user.id, created_at=now)
                db.session.add(row)

            row.asset_tag = asset_tag
            row.name = name
            row.asset_type = _choice_or_default(request.form.get("asset_type"), ASSET_TYPES, "other")
            row.status = _choice_or_default(
                request.form.get("status"),
                ASSET_STATUSES,
                "assigned" if assigned_user_id else "in_stock",
            )
            row.serial_number = (request.form.get("serial_number") or "").strip() or None
            row.vendor = (request.form.get("vendor") or "").strip() or None
            row.location = (request.form.get("location") or "").strip() or None
            row.assigned_user_id = assigned_user_id
            row.purchase_date = purchase_date
            row.warranty_expires_on = warranty_expires_on
            row.notes = (request.form.get("notes") or "").strip() or None
            row.updated_at = now

            db.session.commit()
            audit(
                "it_asset_create" if created else "it_asset_update",
                "ITAsset",
                row.id,
                {"status": row.status, "assigned_user_id": row.assigned_user_id},
            )
            flash("Asset saved." if created else "Asset updated.", "info")
            return redirect(url_for("ops_assets", asset_id=row.id))

        assets = (
            ITAsset.query.order_by(
                sa.case((ITAsset.status == "repair", 0), else_=1),
                ITAsset.updated_at.desc(),
                ITAsset.id.desc(),
            )
            .all()
        )
        open_ticket_counts = {
            int(asset_id): int(count)
            for asset_id, count in (
                db.session.query(HelpdeskTicket.asset_id, sa.func.count(HelpdeskTicket.id))
                .filter(
                    HelpdeskTicket.asset_id.isnot(None),
                    HelpdeskTicket.status.in_(sorted(OPEN_TICKET_STATUSES)),
                )
                .group_by(HelpdeskTicket.asset_id)
                .all()
            )
            if asset_id is not None
        }
        assigned_user_ids = {asset.assigned_user_id for asset in assets if asset.assigned_user_id}
        users_by_id = _helpdesk_users_by_id(assigned_user_ids)
        asset_summary = {
            "tracked_count": len(assets),
            "assigned_count": sum(1 for asset in assets if asset.assigned_user_id is not None),
            "repair_count": sum(1 for asset in assets if asset.status == "repair"),
            "open_ticket_total": sum(open_ticket_counts.values()),
        }
        today = dt.date.today()

        return page(
            "IT Assets",
            "ops_plus/assets.html",
            assets=assets,
            selected_asset=selected_asset,
            users=users,
            users_by_id=users_by_id,
            asset_types=ASSET_TYPES,
            asset_statuses=ASSET_STATUSES,
            asset_summary=asset_summary,
            open_ticket_counts=open_ticket_counts,
            today=today,
            warranty_warning_date=today + dt.timedelta(days=60),
        )

    @app.route("/ops/helpdesk", methods=["GET", "POST"])
    @login_required
    def ops_helpdesk():
        asset_choices = _helpdesk_asset_choices()
        assignee_choices = _active_internal_users() if _ops_it_operator() else []

        if request.method == "POST":
            subject = (request.form.get("subject") or "").strip()
            description = (request.form.get("description") or "").strip()
            if not subject or not description:
                flash("Subject and description are required.", "warning")
                return redirect(url_for("ops_helpdesk"))

            asset_id = request.form.get("asset_id", type=int) or None
            asset = db.session.get(ITAsset, asset_id) if asset_id else None
            if asset_id and asset is None:
                flash("Selected asset was not found.", "warning")
                return redirect(url_for("ops_helpdesk"))
            if asset is not None and not _ops_it_operator() and asset.assigned_user_id != current_user.id:
                flash("You can only link assets assigned to your account.", "warning")
                return redirect(url_for("ops_helpdesk"))

            now = utc_now()
            ticket = HelpdeskTicket(
                ticket_no=f"DRAFT-{now:%Y%m%d%H%M%S%f}",
                subject=subject,
                description=description,
                category=_choice_or_default(request.form.get("category"), HELPDESK_CATEGORIES, "other"),
                priority=_choice_or_default(request.form.get("priority"), HELPDESK_PRIORITIES, "medium"),
                status="new",
                reporter_user_id=current_user.id,
                asset_id=asset_id,
                created_at=now,
                updated_at=now,
            )

            if _ops_it_operator():
                ticket.status = _choice_or_default(request.form.get("status"), HELPDESK_STATUSES, "new")
                assigned_to = request.form.get("assigned_to", type=int) or None
                if assigned_to and not any(user.id == assigned_to for user in assignee_choices):
                    flash("Assigned user was not found.", "warning")
                    return redirect(url_for("ops_helpdesk"))
                ticket.assigned_to = assigned_to
                ticket.resolution_summary = (request.form.get("resolution_summary") or "").strip() or None
                if ticket.assigned_to and ticket.assigned_to != ticket.reporter_user_id:
                    ticket.first_response_at = now
                if ticket.status in {"resolved", "closed"}:
                    ticket.resolved_at = now

            db.session.add(ticket)
            db.session.flush()
            ticket.ticket_no = f"HD-{now:%Y%m%d}-{ticket.id:04d}"
            db.session.commit()

            audit(
                "helpdesk_ticket_create",
                "HelpdeskTicket",
                ticket.id,
                {"ticket_no": ticket.ticket_no, "priority": ticket.priority, "status": ticket.status},
            )
            flash("Helpdesk ticket submitted.", "info")
            return redirect(url_for("ops_helpdesk_ticket", ticket_id=ticket.id))

        tickets = (
            _ticket_queue_query()
            .order_by(
                sa.case((HelpdeskTicket.status.in_(sorted(OPEN_TICKET_STATUSES)), 0), else_=1),
                HelpdeskTicket.updated_at.desc(),
                HelpdeskTicket.id.desc(),
            )
            .limit(200)
            .all()
        )
        user_ids = {
            user_id
            for ticket in tickets
            for user_id in (ticket.reporter_user_id, ticket.assigned_to)
            if user_id is not None
        }
        users_by_id = _helpdesk_users_by_id(user_ids)
        asset_ids = {ticket.asset_id for ticket in tickets if ticket.asset_id}
        assets_by_id = (
            {row.id: row for row in ITAsset.query.filter(ITAsset.id.in_(sorted(asset_ids))).all()}
            if asset_ids
            else {}
        )
        helpdesk_summary = {
            "visible_count": len(tickets),
            "open_count": sum(1 for ticket in tickets if ticket.status in OPEN_TICKET_STATUSES),
            "critical_count": sum(1 for ticket in tickets if ticket.priority == "critical"),
            "unassigned_count": sum(
                1 for ticket in tickets if ticket.assigned_to is None and ticket.status in OPEN_TICKET_STATUSES
            ),
            "my_reported_count": sum(1 for ticket in tickets if ticket.reporter_user_id == current_user.id),
        }

        return page(
            "Helpdesk",
            "ops_plus/helpdesk.html",
            tickets=tickets,
            users_by_id=users_by_id,
            assets_by_id=assets_by_id,
            asset_choices=asset_choices,
            assignee_choices=assignee_choices,
            helpdesk_summary=helpdesk_summary,
            helpdesk_categories=HELPDESK_CATEGORIES,
            helpdesk_priorities=HELPDESK_PRIORITIES,
            helpdesk_statuses=HELPDESK_STATUSES,
            open_ticket_statuses=sorted(OPEN_TICKET_STATUSES),
            is_helpdesk_agent=_ops_it_operator(),
        )

    @app.route("/ops/helpdesk/<int:ticket_id>", methods=["GET", "POST"])
    @login_required
    def ops_helpdesk_ticket(ticket_id: int):
        ticket = db.session.get(HelpdeskTicket, ticket_id)
        if ticket is None:
            abort(404)
        if not _ticket_is_visible(ticket):
            abort(403)

        if request.method == "POST":
            action = (request.form.get("action") or "comment").strip().lower()
            now = utc_now()

            if action == "comment":
                body = (request.form.get("body") or "").strip()
                if not body:
                    flash("Comment text is required.", "warning")
                    return redirect(url_for("ops_helpdesk_ticket", ticket_id=ticket.id))

                comment = HelpdeskTicketComment(
                    ticket_id=ticket.id,
                    author_user_id=current_user.id,
                    body=body,
                    created_at=now,
                )
                db.session.add(comment)
                ticket.updated_at = now
                if _ops_it_operator() and current_user.id != ticket.reporter_user_id and ticket.first_response_at is None:
                    ticket.first_response_at = now
                db.session.commit()
                audit(
                    "helpdesk_ticket_comment",
                    "HelpdeskTicket",
                    ticket.id,
                    {"comment_id": comment.id},
                )
                flash("Comment added.", "info")
                return redirect(url_for("ops_helpdesk_ticket", ticket_id=ticket.id))

            if action != "update":
                flash("Unsupported helpdesk action.", "warning")
                return redirect(url_for("ops_helpdesk_ticket", ticket_id=ticket.id))

            _ops_it_required()
            assigned_to = request.form.get("assigned_to", type=int) or None
            if assigned_to and db.session.get(User, assigned_to) is None:
                flash("Assigned user was not found.", "warning")
                return redirect(url_for("ops_helpdesk_ticket", ticket_id=ticket.id))

            asset_id = request.form.get("asset_id", type=int) or None
            if asset_id and db.session.get(ITAsset, asset_id) is None:
                flash("Selected asset was not found.", "warning")
                return redirect(url_for("ops_helpdesk_ticket", ticket_id=ticket.id))

            ticket.status = _choice_or_default(request.form.get("status"), HELPDESK_STATUSES, ticket.status)
            ticket.priority = _choice_or_default(request.form.get("priority"), HELPDESK_PRIORITIES, ticket.priority)
            ticket.category = _choice_or_default(request.form.get("category"), HELPDESK_CATEGORIES, ticket.category)
            ticket.assigned_to = assigned_to
            ticket.asset_id = asset_id
            ticket.resolution_summary = (request.form.get("resolution_summary") or "").strip() or None
            ticket.updated_at = now
            if current_user.id != ticket.reporter_user_id and ticket.first_response_at is None:
                ticket.first_response_at = now
            if ticket.status in {"resolved", "closed"}:
                ticket.resolved_at = ticket.resolved_at or now
            else:
                ticket.resolved_at = None

            db.session.commit()
            audit(
                "helpdesk_ticket_update",
                "HelpdeskTicket",
                ticket.id,
                {"status": ticket.status, "assigned_to": ticket.assigned_to, "asset_id": ticket.asset_id},
            )
            flash("Ticket updated.", "info")
            return redirect(url_for("ops_helpdesk_ticket", ticket_id=ticket.id))

        comments = (
            HelpdeskTicketComment.query.filter(HelpdeskTicketComment.ticket_id == ticket.id)
            .order_by(HelpdeskTicketComment.created_at.asc(), HelpdeskTicketComment.id.asc())
            .all()
        )
        user_ids = {ticket.reporter_user_id}
        if ticket.assigned_to:
            user_ids.add(ticket.assigned_to)
        for comment in comments:
            user_ids.add(comment.author_user_id)
        users_by_id = _helpdesk_users_by_id(user_ids)
        linked_asset = db.session.get(ITAsset, ticket.asset_id) if ticket.asset_id else None
        assignee_choices = _active_internal_users() if _ops_it_operator() else []
        asset_choices = (
            ITAsset.query.order_by(ITAsset.asset_tag.asc(), ITAsset.name.asc()).all()
            if _ops_it_operator()
            else _helpdesk_asset_choices()
        )

        return page(
            "Helpdesk Ticket",
            "ops_plus/helpdesk_ticket.html",
            ticket=ticket,
            comments=comments,
            users_by_id=users_by_id,
            linked_asset=linked_asset,
            assignee_choices=assignee_choices,
            asset_choices=asset_choices,
            helpdesk_categories=HELPDESK_CATEGORIES,
            helpdesk_priorities=HELPDESK_PRIORITIES,
            helpdesk_statuses=HELPDESK_STATUSES,
            open_ticket_statuses=sorted(OPEN_TICKET_STATUSES),
            is_helpdesk_agent=_ops_it_operator(),
        )
