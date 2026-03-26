from __future__ import annotations

import datetime as dt
from ..timeutils import utc_now
import hashlib
import os
import secrets
import uuid
from functools import wraps

from flask import abort, flash, redirect, request, send_from_directory, session, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, or_
from werkzeug.utils import secure_filename

from ..extensions import db, limiter
from ..helpers import allowed_doc, audit, get_active_matter_id, set_active_matter_context, sha256_file
from ..mfa import build_otpauth_uri, generate_totp_secret, verify_totp
from ..models import (
    DocumentFile,
    DocumentRecord,
    DocumentVersion,
    Invoice,
    Matter,
    PaymentAllocation,
    PortalInvoiceView,
    PortalLinkToken,
    PortalMatterAccess,
    PortalMessage,
    PortalMessageThread,
    PortalPaymentReceipt,
    PortalUpload,
    PortalUser,
    User,
)
from ..policies import enforce_data_residency, visible_matter_ids
from ..roles import role_is_admin
from ..services.notification_engine import NotificationEngine
from ..services.storage_paths import harden_private_file, resolve_upload_path
from ..services.workflow_automation import create_portal_upload_review_task
from ..templates import page


PORTAL_SESSION_KEY = "portal_user_id"
PORTAL_ACTIVE_MATTER_SESSION_KEY = "portal_active_matter_id"
VISIBILITY_LEVEL_RANK = {
    "hidden": 0,
    "summary_only": 1,
    "shared_docs": 2,
    "full_curated": 3,
}


def _portal_current_user() -> PortalUser | None:
    user_id = session.get(PORTAL_SESSION_KEY)
    if not user_id:
        return None
    try:
        return db.session.get(PortalUser, int(user_id))
    except (TypeError, ValueError):
        session.pop(PORTAL_SESSION_KEY, None)
        return None


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _visibility_rank(level: str | None) -> int:
    normalized = (level or "summary_only").strip().lower()
    return VISIBILITY_LEVEL_RANK.get(normalized, VISIBILITY_LEVEL_RANK["summary_only"])


def _portal_message_excerpt(body: str | None, *, limit: int = 180) -> str:
    text = " ".join((body or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _portal_message_author_label(
    message: PortalMessage,
    *,
    user_map: dict[int, User],
    portal_user_map: dict[int, PortalUser],
) -> str:
    if message.from_user_id:
        user = user_map.get(int(message.from_user_id))
        return (user.full_name or "").strip() or "Internal user"
    if message.from_portal_user_id:
        portal_user = portal_user_map.get(int(message.from_portal_user_id))
        return (portal_user.full_name or "").strip() or "Client contact"
    return "Unknown sender"


def portal_login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        portal_user = _portal_current_user()
        if portal_user is None:
            return redirect(url_for("portal_login"))
        if not portal_user.is_active:
            session.pop(PORTAL_SESSION_KEY, None)
            session.pop(PORTAL_ACTIVE_MATTER_SESSION_KEY, None)
            flash("Portal access is inactive. Contact your law firm administrator.", "warning")
            return redirect(url_for("portal_login"))
        return view(*args, **kwargs)

    return wrapped


def _portal_accessible_matter_ids(portal_user_id: int, *, min_level: str = "summary_only") -> list[int]:
    required_rank = _visibility_rank(min_level)
    allowed_levels = [level for level, rank in VISIBILITY_LEVEL_RANK.items() if rank >= required_rank]
    rows = (
        db.session.query(PortalMatterAccess.matter_id)
        .filter(
            PortalMatterAccess.portal_user_id == portal_user_id,
            PortalMatterAccess.revoked_at.is_(None),
            PortalMatterAccess.visibility_level.in_(allowed_levels),
        )
        .all()
    )
    return [int(row[0]) for row in rows]


def _portal_matter_access(portal_user_id: int, matter_id: int) -> PortalMatterAccess | None:
    return (
        PortalMatterAccess.query.filter_by(
            portal_user_id=portal_user_id,
            matter_id=matter_id,
            revoked_at=None,
        ).first()
    )


def _portal_has_matter_access(portal_user_id: int, matter_id: int, *, min_level: str = "summary_only") -> bool:
    row = (
        db.session.query(PortalMatterAccess.visibility_level)
        .filter(
            PortalMatterAccess.portal_user_id == portal_user_id,
            PortalMatterAccess.matter_id == matter_id,
            PortalMatterAccess.revoked_at.is_(None),
        )
        .first()
    )
    if not row:
        return False
    return _visibility_rank(row[0]) >= _visibility_rank(min_level)


def _portal_set_active_matter(
    portal_user_id: int,
    matter_id: int | None,
    *,
    min_level: str = "summary_only",
) -> int | None:
    try:
        parsed = int(matter_id) if matter_id else None
    except (TypeError, ValueError):
        return None
    if not parsed or parsed <= 0:
        return None
    if not _portal_has_matter_access(portal_user_id, parsed, min_level=min_level):
        return None
    session[PORTAL_ACTIVE_MATTER_SESSION_KEY] = parsed
    return parsed


def _portal_get_active_matter_id(
    portal_user_id: int,
    *,
    min_level: str = "summary_only",
) -> int | None:
    raw_id = session.get(PORTAL_ACTIVE_MATTER_SESSION_KEY)
    try:
        parsed = int(raw_id) if raw_id else None
    except (TypeError, ValueError):
        parsed = None
    if not parsed or parsed <= 0:
        session.pop(PORTAL_ACTIVE_MATTER_SESSION_KEY, None)
        return None
    if not _portal_has_matter_access(portal_user_id, parsed, min_level=min_level):
        session.pop(PORTAL_ACTIVE_MATTER_SESSION_KEY, None)
        return None
    return parsed


def register_portal_routes(app):
    @app.route("/portal/login", methods=["GET", "POST"])
    @limiter.limit(lambda: app.config.get("PORTAL_LOGIN_RATE_LIMIT", "10/minute"), methods=["POST"])
    def portal_login():
        if request.method == "POST":
            email = (request.form.get("email") or "").strip().lower()
            password = request.form.get("password") or ""
            mfa_code = (request.form.get("mfa_code") or "").strip()
            user = PortalUser.query.filter_by(email=email, is_active=True).first()
            if not user or not user.check_password(password):
                flash("Invalid portal credentials.", "warning")
                return redirect(url_for("portal_login"))
            if user.mfa_enabled:
                if not (user.mfa_secret or "").strip():
                    audit("portal_login_mfa_misconfigured", "PortalUser", user.id)
                    flash("Portal MFA is enabled but not configured. Please contact support.", "warning")
                    return redirect(url_for("portal_login"))
                if not user.mfa_secret or not verify_totp(user.mfa_secret, mfa_code):
                    flash("Invalid portal MFA code.", "warning")
                    return redirect(url_for("portal_login"))
            session[PORTAL_SESSION_KEY] = user.id
            session.pop(PORTAL_ACTIVE_MATTER_SESSION_KEY, None)
            user.last_login_at = utc_now()
            db.session.commit()
            audit("portal_login", "PortalUser", user.id)
            return redirect(url_for("portal_matters"))

        return page("Portal Login", "portal/login.html")

    @app.post("/portal/logout")
    @portal_login_required
    def portal_logout():
        user = _portal_current_user()
        session.pop(PORTAL_SESSION_KEY, None)
        session.pop(PORTAL_ACTIVE_MATTER_SESSION_KEY, None)
        if user:
            audit("portal_logout", "PortalUser", user.id)
        flash("Logged out of client portal.", "info")
        return redirect(url_for("portal_login"))

    @app.get("/portal/matters")
    @portal_login_required
    def portal_matters():
        portal_user = _portal_current_user()
        assert portal_user is not None
        ids = _portal_accessible_matter_ids(portal_user.id)
        matters = Matter.query.filter(Matter.id.in_(ids)).order_by(Matter.opened_at.desc()).all() if ids else []
        return page("Client Matters", "portal/matters.html", portal_user=portal_user, matters=matters)

    @app.get("/portal/matters/<int:matter_id>")
    @portal_login_required
    def portal_matter_detail(matter_id: int):
        portal_user = _portal_current_user()
        assert portal_user is not None
        access = _portal_matter_access(portal_user.id, matter_id)
        if not access or _visibility_rank(access.visibility_level) < _visibility_rank("summary_only"):
            abort(403)
        _portal_set_active_matter(portal_user.id, matter_id, min_level="summary_only")

        matter = db.session.get(Matter, matter_id)
        if not matter:
            abort(404)

        visibility_rank = _visibility_rank(access.visibility_level)
        threads = []
        uploads = []
        invoices = []
        if visibility_rank >= _visibility_rank("shared_docs"):
            uploads = (
                PortalUpload.query.filter_by(matter_id=matter_id, portal_user_id=portal_user.id)
                .order_by(PortalUpload.uploaded_at.desc())
                .all()
            )
        if visibility_rank >= _visibility_rank("full_curated"):
            threads = PortalMessageThread.query.filter_by(matter_id=matter_id).order_by(PortalMessageThread.created_at.desc()).all()
            invoices = Invoice.query.filter_by(matter_id=matter_id).order_by(Invoice.created_at.desc()).all()

        return page(
            "Portal Matter",
            "portal/matter_detail.html",
            portal_user=portal_user,
            matter=matter,
            access=access,
            threads=threads,
            uploads=uploads,
            invoices=invoices,
        )

    @app.route("/portal/messages/workbench", methods=["GET", "POST"])
    @login_required
    def portal_message_center():
        matter_scope = [int(item) for item in visible_matter_ids() if int(item) > 0]
        matters = Matter.query.filter(Matter.id.in_(matter_scope)).order_by(Matter.opened_at.desc()).all() if matter_scope else []
        matter_map = {matter.id: matter for matter in matters}
        threads = (
            PortalMessageThread.query.filter(PortalMessageThread.matter_id.in_(matter_scope))
            .order_by(PortalMessageThread.created_at.desc())
            .limit(200)
            .all()
            if matter_scope
            else []
        )
        threads_by_id = {thread.id: thread for thread in threads}

        if request.method == "POST":
            thread_id = request.form.get("thread_id", type=int)
            matter_id = request.form.get("matter_id", type=int)
            body = (request.form.get("body") or "").strip()

            if not body:
                flash("Message body required.", "warning")
                return redirect(
                    url_for(
                        "portal_message_center",
                        matter_id=matter_id or None,
                        thread_id=thread_id or None,
                    )
                )

            thread = db.session.get(PortalMessageThread, thread_id) if thread_id else None
            if thread_id and thread is None:
                flash("Selected thread was not found.", "warning")
                return redirect(url_for("portal_message_center"))
            if thread is not None:
                if int(thread.matter_id) not in matter_map:
                    abort(403)
                if matter_id and int(matter_id) != int(thread.matter_id):
                    flash("Selected thread belongs to a different matter.", "warning")
                    return redirect(
                        url_for(
                            "portal_message_center",
                            matter_id=thread.matter_id,
                            thread_id=thread.id,
                        )
                    )
                matter_id = int(thread.matter_id)

            if not thread_id:
                if not matter_id or matter_id not in matter_map:
                    abort(403)
                thread = PortalMessageThread(
                    matter_id=matter_id,
                    subject=(request.form.get("subject") or "General update").strip() or "General update",
                    created_by_user_id=current_user.id,
                    created_by_portal_user_id=None,
                )
                db.session.add(thread)
                db.session.flush()
                thread_id = thread.id

            message = PortalMessage(
                thread_id=thread_id,
                body=body,
                from_user_id=current_user.id,
                from_portal_user_id=None,
            )
            db.session.add(message)
            db.session.commit()
            set_active_matter_context(matter_id)
            NotificationEngine.enqueue("portal_message_created", None, f"portal_message:{message.id}")
            audit("portal_message_create", "PortalMessage", message.id)
            flash("Portal message sent.", "info")
            return redirect(url_for("portal_message_center", matter_id=matter_id, thread_id=thread_id))

        prefill_thread_id = request.args.get("thread_id", type=int)
        if prefill_thread_id and prefill_thread_id not in threads_by_id:
            abort(403)

        prefill_matter_id = request.args.get("matter_id", type=int)
        active_matter_id = get_active_matter_id()
        if prefill_matter_id and prefill_matter_id not in matter_map:
            abort(403)
        if not prefill_matter_id and active_matter_id and active_matter_id in matter_map:
            prefill_matter_id = active_matter_id

        selected_thread = threads_by_id.get(prefill_thread_id) if prefill_thread_id else None
        if selected_thread is not None:
            prefill_matter_id = int(selected_thread.matter_id)
        if not prefill_matter_id and matters:
            prefill_matter_id = matters[0].id
        if prefill_matter_id:
            set_active_matter_context(prefill_matter_id)

        if selected_thread is None and prefill_matter_id:
            selected_thread = next((thread for thread in threads if int(thread.matter_id) == int(prefill_matter_id)), None)
        if selected_thread is None and threads:
            selected_thread = threads[0]
            prefill_matter_id = int(selected_thread.matter_id)
            set_active_matter_context(prefill_matter_id)

        thread_ids = [thread.id for thread in threads]
        recent_messages = (
            PortalMessage.query.filter(PortalMessage.thread_id.in_(thread_ids))
            .order_by(PortalMessage.created_at.desc())
            .limit(800)
            .all()
            if thread_ids
            else []
        )
        selected_thread_messages = (
            PortalMessage.query.filter_by(thread_id=selected_thread.id).order_by(PortalMessage.created_at.asc()).all()
            if selected_thread is not None
            else []
        )

        user_ids = {int(message.from_user_id) for message in recent_messages if message.from_user_id}
        user_ids.update(int(message.from_user_id) for message in selected_thread_messages if message.from_user_id)
        portal_user_ids = {int(message.from_portal_user_id) for message in recent_messages if message.from_portal_user_id}
        portal_user_ids.update(int(message.from_portal_user_id) for message in selected_thread_messages if message.from_portal_user_id)
        user_map = {row.id: row for row in User.query.filter(User.id.in_(sorted(user_ids))).all()} if user_ids else {}
        portal_user_map = (
            {row.id: row for row in PortalUser.query.filter(PortalUser.id.in_(sorted(portal_user_ids))).all()}
            if portal_user_ids
            else {}
        )

        latest_message_by_thread: dict[int, PortalMessage] = {}
        for message in recent_messages:
            latest_message_by_thread.setdefault(int(message.thread_id), message)

        thread_cards = []
        for thread in threads:
            matter = matter_map.get(int(thread.matter_id))
            latest_message = latest_message_by_thread.get(int(thread.id))
            latest_message_at = latest_message.created_at if latest_message is not None else thread.created_at
            thread_cards.append(
                {
                    "id": thread.id,
                    "matter_id": thread.matter_id,
                    "subject": thread.subject,
                    "matter_no": matter.matter_no if matter else f"Matter #{thread.matter_id}",
                    "matter_title": matter.title if matter else "Unavailable matter",
                    "latest_message_at": latest_message_at,
                    "latest_message_excerpt": _portal_message_excerpt(latest_message.body if latest_message is not None else ""),
                    "latest_author_label": (
                        _portal_message_author_label(
                            latest_message,
                            user_map=user_map,
                            portal_user_map=portal_user_map,
                        )
                        if latest_message is not None
                        else "No messages yet"
                    ),
                    "waiting_on_internal_response": bool(latest_message and latest_message.from_portal_user_id),
                }
            )
        thread_cards.sort(key=lambda row: row["latest_message_at"] or dt.datetime.min, reverse=True)

        conversation_messages = [
            {
                "id": message.id,
                "body": message.body,
                "created_at": message.created_at,
                "author_label": _portal_message_author_label(
                    message,
                    user_map=user_map,
                    portal_user_map=portal_user_map,
                ),
                "origin_label": "Client" if message.from_portal_user_id else "Internal",
                "is_from_portal": bool(message.from_portal_user_id),
            }
            for message in selected_thread_messages
        ]
        waiting_on_internal_count = sum(1 for row in thread_cards if row["waiting_on_internal_response"])
        selected_thread_matter = matter_map.get(selected_thread.matter_id) if selected_thread is not None else None
        return page(
            "Client Message Center",
            "portal/message_center.html",
            matters=matters,
            matter_map=matter_map,
            thread_cards=thread_cards,
            prefill_matter_id=prefill_matter_id,
            prefill_thread_id=selected_thread.id if selected_thread is not None else None,
            selected_thread=selected_thread,
            selected_thread_matter=selected_thread_matter,
            conversation_messages=conversation_messages,
            waiting_on_internal_count=waiting_on_internal_count,
        )

    @app.route("/portal/messages", methods=["GET", "POST"])
    @portal_login_required
    def portal_messages():
        portal_user = _portal_current_user()
        assert portal_user is not None

        if request.method == "POST":
            thread_id = request.form.get("thread_id", type=int)
            matter_id = request.form.get("matter_id", type=int)
            body = (request.form.get("body") or "").strip()
            if not body:
                flash("Message body required.", "warning")
                return redirect(url_for("portal_messages"))

            thread = db.session.get(PortalMessageThread, thread_id) if thread_id else None
            if thread_id and thread is None:
                flash("Selected thread was not found.", "warning")
                return redirect(url_for("portal_messages"))
            if thread is not None:
                if not _portal_has_matter_access(portal_user.id, thread.matter_id, min_level="full_curated"):
                    abort(403)
                if matter_id and int(matter_id) != int(thread.matter_id):
                    flash("Selected thread belongs to a different matter.", "warning")
                    return redirect(url_for("portal_messages"))
                matter_id = int(thread.matter_id)

            if not thread_id:
                if not matter_id or not _portal_has_matter_access(portal_user.id, matter_id, min_level="full_curated"):
                    abort(403)
                thread = PortalMessageThread(
                    matter_id=matter_id,
                    subject=(request.form.get("subject") or "General update").strip() or "General update",
                    created_by_portal_user_id=portal_user.id,
                )
                db.session.add(thread)
                db.session.flush()
                thread_id = thread.id

            message = PortalMessage(thread_id=thread_id, body=body, from_portal_user_id=portal_user.id)
            db.session.add(message)
            db.session.commit()
            _portal_set_active_matter(portal_user.id, matter_id, min_level="full_curated")
            NotificationEngine.enqueue("portal_message_created", None, f"portal_message:{message.id}")
            audit("portal_message_create", "PortalMessage", message.id)
            flash("Message sent.", "info")
            return redirect(url_for("portal_messages"))

        accessible_ids = _portal_accessible_matter_ids(portal_user.id, min_level="full_curated")
        threads = (
            PortalMessageThread.query.filter(PortalMessageThread.matter_id.in_(accessible_ids))
            .order_by(PortalMessageThread.created_at.desc())
            .limit(200)
            .all()
            if accessible_ids
            else []
        )
        messages = (
            PortalMessage.query.filter(PortalMessage.thread_id.in_([t.id for t in threads]))
            .order_by(PortalMessage.created_at.desc())
            .limit(500)
            .all()
            if threads
            else []
        )
        matters = Matter.query.filter(Matter.id.in_(accessible_ids)).order_by(Matter.opened_at.desc()).all() if accessible_ids else []
        matter_map = {matter.id: matter for matter in matters}
        prefill_matter_id = request.args.get("matter_id", type=int)
        if prefill_matter_id and not _portal_has_matter_access(portal_user.id, prefill_matter_id, min_level="full_curated"):
            prefill_matter_id = None
        if not prefill_matter_id:
            prefill_matter_id = _portal_get_active_matter_id(portal_user.id, min_level="full_curated")
        if not prefill_matter_id and matters:
            prefill_matter_id = matters[0].id
        prefill_thread_id = request.args.get("thread_id", type=int)
        if prefill_thread_id and prefill_thread_id not in {thread.id for thread in threads}:
            prefill_thread_id = None
        return page(
            "Portal Messages",
            "portal/messages.html",
            portal_user=portal_user,
            threads=threads,
            messages=messages,
            matters=matters,
            matter_map=matter_map,
            prefill_matter_id=prefill_matter_id,
            prefill_thread_id=prefill_thread_id,
        )

    @app.route("/portal/uploads", methods=["GET", "POST"])
    @portal_login_required
    def portal_uploads():
        portal_user = _portal_current_user()
        assert portal_user is not None

        if request.method == "POST":
            matter_id = request.form.get("matter_id", type=int)
            if not matter_id or not _portal_has_matter_access(portal_user.id, matter_id, min_level="shared_docs"):
                abort(403)

            f = request.files.get("file")
            if not f or not f.filename:
                flash("File required.", "warning")
                return redirect(url_for("portal_uploads"))
            if not allowed_doc(f.filename):
                flash("Unsupported file type.", "warning")
                return redirect(url_for("portal_uploads"))
            enforce_data_residency("primary_storage")

            safe = secure_filename(f.filename)
            stored = f"portal_{matter_id}_{uuid.uuid4().hex}_{safe}"
            try:
                stored, path = resolve_upload_path(app.config["UPLOAD_DIR"], stored, create_parent=True)
            except ValueError:
                flash("Storage path validation failed for upload.", "warning")
                return redirect(url_for("portal_uploads"))
            f.save(path)
            harden_private_file(path)

            row = PortalUpload(
                matter_id=matter_id,
                portal_user_id=portal_user.id,
                filename=safe,
                stored_filename=stored,
                sha256=sha256_file(path),
            )
            db.session.add(row)
            db.session.flush()

            matter = db.session.get(Matter, matter_id)
            if matter is None:
                abort(404)
            dms_row = DocumentFile(
                matter_id=matter_id,
                original_filename=safe,
                stored_filename=stored,
                sha256=row.sha256,
                content_type=(f.mimetype or "").strip() or None,
                category="Correspondence",
                doc_version="v1",
                lifecycle_stage="For Review",
                owner_name=f"portal_upload:{row.id}",
                is_privileged=False,
                uploaded_by=int(matter.created_by),
            )
            db.session.add(dms_row)
            review_task_id = create_portal_upload_review_task(
                row.id,
                actor_user_id=int(matter.created_by),
            )
            db.session.commit()
            _portal_set_active_matter(portal_user.id, matter_id, min_level="shared_docs")
            NotificationEngine.enqueue("portal_upload_created", None, f"portal_upload:{row.id}")
            audit(
                "portal_upload",
                "PortalUpload",
                row.id,
                {"document_file_id": dms_row.id, "review_task_id": review_task_id},
            )
            if review_task_id:
                flash(f"Upload complete. Filed to DMS and queued review task #{review_task_id}.", "info")
            else:
                flash("Upload complete. Filed to DMS.", "info")
            return redirect(url_for("portal_uploads"))

        ids = _portal_accessible_matter_ids(portal_user.id, min_level="shared_docs")
        full_curated_matter_ids = set(_portal_accessible_matter_ids(portal_user.id, min_level="full_curated"))
        uploads = PortalUpload.query.filter(PortalUpload.matter_id.in_(ids)).order_by(PortalUpload.uploaded_at.desc()).all() if ids else []
        matters = Matter.query.filter(Matter.id.in_(ids)).order_by(Matter.opened_at.desc()).all() if ids else []
        prefill_matter_id = request.args.get("matter_id", type=int)
        if prefill_matter_id and not _portal_has_matter_access(portal_user.id, prefill_matter_id, min_level="shared_docs"):
            prefill_matter_id = None
        if not prefill_matter_id:
            prefill_matter_id = _portal_get_active_matter_id(portal_user.id, min_level="shared_docs")
        if not prefill_matter_id and matters:
            prefill_matter_id = matters[0].id
        return page(
            "Portal Uploads",
            "portal/uploads.html",
            portal_user=portal_user,
            uploads=uploads,
            matters=matters,
            full_curated_matter_ids=full_curated_matter_ids,
            prefill_matter_id=prefill_matter_id,
        )

    @app.get("/portal/invoices")
    @portal_login_required
    def portal_invoices():
        portal_user = _portal_current_user()
        assert portal_user is not None
        ids = _portal_accessible_matter_ids(portal_user.id, min_level="full_curated")
        invoices = Invoice.query.filter(Invoice.matter_id.in_(ids)).order_by(Invoice.created_at.desc()).all() if ids else []
        matter_ids = {inv.matter_id for inv in invoices}
        matter_map = {m.id: m for m in Matter.query.filter(Matter.id.in_(matter_ids)).all()} if matter_ids else {}

        invoice_ids = [inv.id for inv in invoices]
        if invoice_ids:
            now = utc_now()
            existing_rows = (
                PortalInvoiceView.query.filter(
                    PortalInvoiceView.portal_user_id == portal_user.id,
                    PortalInvoiceView.invoice_id.in_(invoice_ids),
                ).all()
            )
            existing_by_invoice_id = {row.invoice_id: row for row in existing_rows}
            for invoice_id in invoice_ids:
                row = existing_by_invoice_id.get(invoice_id)
                if row is None:
                    db.session.add(
                        PortalInvoiceView(
                            portal_user_id=portal_user.id,
                            invoice_id=invoice_id,
                            last_viewed_at=now,
                        )
                    )
                else:
                    row.last_viewed_at = now
            db.session.commit()

        return page("Portal Invoices", "portal/invoices.html", portal_user=portal_user, invoices=invoices, matter_map=matter_map)

    @app.route("/portal/payments/<int:invoice_id>", methods=["GET", "POST"])
    @portal_login_required
    def portal_payments(invoice_id: int):
        portal_user = _portal_current_user()
        assert portal_user is not None

        invoice = db.session.get(Invoice, invoice_id)
        if not invoice:
            abort(404)
        if not _portal_has_matter_access(portal_user.id, invoice.matter_id, min_level="full_curated"):
            abort(403)
        _portal_set_active_matter(portal_user.id, invoice.matter_id, min_level="full_curated")

        if request.method == "POST":
            amount = request.form.get("amount", type=float)
            if amount is None or amount <= 0:
                flash("Valid amount required.", "warning")
                return redirect(url_for("portal_payments", invoice_id=invoice_id))

            receipt = PortalPaymentReceipt(
                invoice_id=invoice.id,
                portal_user_id=portal_user.id,
                amount=amount,
                currency=(request.form.get("currency") or "ZAR").strip().upper(),
                status="pending_settlement",
                reference=(request.form.get("reference") or "").strip() or None,
            )
            db.session.add(receipt)

            # Internal native payment recording (no external provider).
            db.session.add(
                PaymentAllocation(
                    invoice_id=invoice.id,
                    amount=amount,
                    method="portal_manual",
                    reference=receipt.reference,
                    status="pending",
                    processor_note="Captured via client portal; awaiting settlement confirmation.",
                    created_by=None,
                )
            )
            db.session.commit()
            _portal_set_active_matter(portal_user.id, invoice.matter_id, min_level="full_curated")
            NotificationEngine.enqueue("portal_payment_recorded", None, f"portal_payment:{receipt.id}")
            audit("portal_payment_record", "PortalPaymentReceipt", receipt.id)
            flash("Payment receipt recorded.", "info")
            return redirect(url_for("portal_payments", invoice_id=invoice_id))

        receipts = PortalPaymentReceipt.query.filter_by(invoice_id=invoice.id, portal_user_id=portal_user.id).order_by(
            PortalPaymentReceipt.created_at.desc()
        ).all()
        settled_total = (
            db.session.query(func.coalesce(func.sum(PaymentAllocation.amount), 0.0))
            .filter(
                PaymentAllocation.invoice_id == invoice.id,
                or_(PaymentAllocation.status == "settled", PaymentAllocation.status.is_(None)),
            )
            .scalar()
            or 0.0
        )
        outstanding_amount = max(0.0, round(float(invoice.total or 0.0) - float(settled_total), 2))
        default_amount = outstanding_amount if outstanding_amount > 0 else round(float(invoice.total or 0.0), 2)
        latest_currency = receipts[0].currency if receipts else None
        prefill_currency = (latest_currency or "ZAR").strip().upper()
        return page(
            "Portal Payment",
            "portal/payment.html",
            portal_user=portal_user,
            invoice=invoice,
            receipts=receipts,
            outstanding_amount=outstanding_amount,
            prefill_amount=default_amount,
            prefill_currency=prefill_currency,
        )

    @app.route("/portal/links", methods=["GET", "POST"])
    @portal_login_required
    def portal_links():
        portal_user = _portal_current_user()
        assert portal_user is not None

        if request.method == "POST":
            matter_id = request.form.get("matter_id", type=int)
            if not matter_id or not _portal_has_matter_access(portal_user.id, matter_id, min_level="shared_docs"):
                abort(403)

            document_version_id = request.form.get("document_version_id", type=int)
            if document_version_id:
                row = (
                    db.session.query(DocumentVersion, DocumentRecord)
                    .join(DocumentRecord, DocumentRecord.id == DocumentVersion.document_id)
                    .filter(DocumentVersion.id == document_version_id)
                    .first()
                )
                if row is None or int(row[1].matter_id) != int(matter_id):
                    flash("Document version is not linked to this matter.", "warning")
                    return redirect(url_for("portal_links"))

            expires_minutes = max(1, min(7 * 24 * 60, int(request.form.get("expires_minutes", type=int) or 60)))
            token_raw = secrets.token_urlsafe(24)
            token = PortalLinkToken(
                portal_user_id=portal_user.id,
                matter_id=matter_id,
                document_version_id=document_version_id,
                token_hash=_hash_token(token_raw),
                expires_at=utc_now() + dt.timedelta(minutes=expires_minutes),
            )
            db.session.add(token)
            db.session.commit()
            _portal_set_active_matter(portal_user.id, matter_id, min_level="shared_docs")
            audit("portal_link_create", "PortalLinkToken", token.id)
            session["portal_last_link_url"] = url_for("portal_link_access", token=token_raw, _external=True)
            flash("Time-limited link created.", "info")
            return redirect(url_for("portal_links"))

        matter_ids = _portal_accessible_matter_ids(portal_user.id, min_level="shared_docs")
        matters = Matter.query.filter(Matter.id.in_(matter_ids)).order_by(Matter.opened_at.desc()).all() if matter_ids else []
        versions = (
            db.session.query(DocumentVersion, DocumentRecord)
            .join(DocumentRecord, DocumentRecord.id == DocumentVersion.document_id)
            .filter(DocumentRecord.matter_id.in_(matter_ids))
            .order_by(DocumentVersion.uploaded_at.desc())
            .limit(200)
            .all()
            if matter_ids
            else []
        )
        matter_map = {m.id: m for m in matters}
        version_map = {pair[0].id: pair for pair in versions}
        tokens = (
            PortalLinkToken.query.filter_by(portal_user_id=portal_user.id)
            .order_by(PortalLinkToken.created_at.desc())
            .limit(200)
            .all()
        )
        last_link_url = session.pop("portal_last_link_url", None)
        prefill_matter_id = request.args.get("matter_id", type=int)
        if prefill_matter_id and not _portal_has_matter_access(portal_user.id, prefill_matter_id, min_level="shared_docs"):
            prefill_matter_id = None
        if not prefill_matter_id:
            prefill_matter_id = _portal_get_active_matter_id(portal_user.id, min_level="shared_docs")
        if not prefill_matter_id and matters:
            prefill_matter_id = matters[0].id
        return page(
            "Portal Links",
            "portal/links.html",
            portal_user=portal_user,
            matters=matters,
            matter_map=matter_map,
            versions=versions,
            version_map=version_map,
            tokens=tokens,
            last_link_url=last_link_url,
            prefill_matter_id=prefill_matter_id,
        )

    @app.get("/portal/link/<token>")
    def portal_link_access(token: str):
        token_hash = _hash_token(token)
        row = PortalLinkToken.query.filter_by(token_hash=token_hash).first()
        if row is None:
            abort(404)
        if row.expires_at < utc_now():
            abort(410)
        if row.used_at is not None:
            audit("portal_link_reuse_blocked", "PortalLinkToken", row.id)
            abort(410)

        portal_user = db.session.get(PortalUser, row.portal_user_id)
        if not portal_user or not portal_user.is_active:
            abort(403)

        if row.document_version_id:
            joined = (
                db.session.query(DocumentVersion, DocumentRecord)
                .join(DocumentRecord, DocumentRecord.id == DocumentVersion.document_id)
                .filter(DocumentVersion.id == row.document_version_id)
                .first()
            )
            if joined is None:
                abort(404)
            version, record = joined
            if row.matter_id and int(record.matter_id) != int(row.matter_id):
                abort(403)
            if not _portal_has_matter_access(portal_user.id, record.matter_id, min_level="shared_docs"):
                abort(403)
            enforce_data_residency("exports")
            try:
                stored_filename, path = resolve_upload_path(app.config["UPLOAD_DIR"], version.stored_filename)
            except ValueError:
                abort(404)
            if not os.path.isfile(path):
                abort(404)
            row.used_at = utc_now()
            db.session.commit()
            _portal_set_active_matter(portal_user.id, record.matter_id, min_level="shared_docs")
            audit(
                "portal_link_document_access",
                "DocumentVersion",
                version.id,
                {"portal_user_id": portal_user.id, "matter_id": record.matter_id},
            )
            return send_from_directory(
                app.config["UPLOAD_DIR"],
                stored_filename,
                as_attachment=True,
                download_name=version.original_filename,
            )

        if not row.matter_id:
            abort(404)
        if not _portal_has_matter_access(portal_user.id, row.matter_id, min_level="summary_only"):
            abort(403)
        row.used_at = utc_now()
        db.session.commit()
        _portal_set_active_matter(portal_user.id, row.matter_id, min_level="summary_only")
        audit("portal_link_matter_access", "Matter", row.matter_id, {"portal_user_id": portal_user.id})
        return redirect(url_for("portal_matter_detail", matter_id=row.matter_id))

    @app.route("/admin/portal/users", methods=["GET", "POST"])
    @login_required
    def admin_portal_users():
        if not role_is_admin(getattr(current_user, "role", None)):
            abort(403)

        if request.method == "POST":
            action = (request.form.get("action") or "create_user").strip()
            if action == "create_user":
                email = (request.form.get("email") or "").strip().lower()
                full_name = (request.form.get("full_name") or "").strip() or "Portal User"
                password = request.form.get("password") or ""
                enable_mfa = (request.form.get("enable_mfa") or "").strip().lower() in {"1", "true", "yes", "on"}
                if not email or len(password) < 12:
                    flash("Email and strong password required.", "warning")
                    return redirect(url_for("admin_portal_users"))
                if PortalUser.query.filter_by(email=email).first():
                    flash("Portal user already exists.", "warning")
                    return redirect(url_for("admin_portal_users"))
                user = PortalUser(email=email, full_name=full_name, password_hash="x", is_active=True)
                user.set_password(password)
                if enable_mfa:
                    user.mfa_enabled = True
                    user.mfa_secret = generate_totp_secret()
                db.session.add(user)
                db.session.commit()
                audit("portal_user_create", "PortalUser", user.id)
                if enable_mfa:
                    provisioning_uri = build_otpauth_uri(user.mfa_secret or "", user.email, issuer="LawFirmOS Portal")
                    flash(f"MFA enabled for {user.email}. Secret: {user.mfa_secret}", "info")
                    flash(f"Provisioning URI: {provisioning_uri}", "info")
                flash("Portal user created.", "info")

            elif action == "grant_access":
                portal_user_id = request.form.get("portal_user_id", type=int)
                matter_id = request.form.get("matter_id", type=int)
                if not portal_user_id or not matter_id:
                    flash("Portal user and matter are required.", "warning")
                    return redirect(url_for("admin_portal_users"))
                visibility_level = (request.form.get("visibility_level") or "summary_only").strip().lower()
                if visibility_level not in VISIBILITY_LEVEL_RANK:
                    flash("Invalid portal visibility level.", "warning")
                    return redirect(url_for("admin_portal_users"))
                access = PortalMatterAccess.query.filter_by(portal_user_id=portal_user_id, matter_id=matter_id).first()
                if access is None:
                    access = PortalMatterAccess(
                        portal_user_id=portal_user_id,
                        matter_id=matter_id,
                        visibility_level=visibility_level,
                        granted_by=current_user.id,
                    )
                    db.session.add(access)
                else:
                    access.revoked_at = None
                    access.visibility_level = visibility_level
                db.session.commit()
                audit("portal_access_grant", "PortalMatterAccess", access.id)
                flash("Portal access updated.", "info")

            elif action == "revoke_access":
                access_id = request.form.get("access_id", type=int)
                access = db.session.get(PortalMatterAccess, access_id) if access_id else None
                if access:
                    access.revoked_at = utc_now()
                    db.session.commit()
                    audit("portal_access_revoke", "PortalMatterAccess", access.id)
                    flash("Portal access revoked.", "info")

            elif action == "toggle_mfa":
                portal_user_id = request.form.get("portal_user_id", type=int)
                mode = (request.form.get("mode") or "").strip().lower()
                user = db.session.get(PortalUser, portal_user_id) if portal_user_id else None
                if user is None:
                    flash("Portal user not found.", "warning")
                    return redirect(url_for("admin_portal_users"))
                if mode == "disable":
                    user.mfa_enabled = False
                    user.mfa_secret = None
                    db.session.commit()
                    audit("portal_user_mfa_disabled", "PortalUser", user.id)
                    flash("Portal MFA disabled.", "info")
                elif mode in {"enable", "rotate"}:
                    user.mfa_enabled = True
                    user.mfa_secret = generate_totp_secret()
                    db.session.commit()
                    audit("portal_user_mfa_rotated", "PortalUser", user.id, {"mode": mode})
                    provisioning_uri = build_otpauth_uri(user.mfa_secret or "", user.email, issuer="LawFirmOS Portal")
                    flash(f"Portal MFA secret for {user.email}: {user.mfa_secret}", "info")
                    flash(f"Provisioning URI: {provisioning_uri}", "info")
                else:
                    flash("Unsupported MFA action.", "warning")
                    return redirect(url_for("admin_portal_users"))

            return redirect(url_for("admin_portal_users"))

        user_page = request.args.get("user_page", default=1, type=int) or 1
        access_page = request.args.get("access_page", default=1, type=int) or 1
        if user_page < 1:
            user_page = 1
        if access_page < 1:
            access_page = 1

        user_pagination = PortalUser.query.order_by(PortalUser.created_at.desc()).paginate(
            page=user_page,
            per_page=50,
            error_out=False,
        )
        access_pagination = PortalMatterAccess.query.order_by(PortalMatterAccess.granted_at.desc()).paginate(
            page=access_page,
            per_page=75,
            error_out=False,
        )
        users = user_pagination.items
        accesses = access_pagination.items
        grant_users = PortalUser.query.order_by(PortalUser.created_at.desc()).limit(300).all()
        matters = Matter.query.order_by(Matter.opened_at.desc()).limit(300).all()

        user_map = {u.id: u for u in grant_users}
        for user in users:
            user_map[user.id] = user
        missing_user_ids = {int(row.portal_user_id) for row in accesses if row.portal_user_id not in user_map}
        if missing_user_ids:
            for user in PortalUser.query.filter(PortalUser.id.in_(missing_user_ids)).all():
                user_map[user.id] = user

        matter_map = {m.id: m for m in matters}
        missing_matter_ids = {int(row.matter_id) for row in accesses if row.matter_id not in matter_map}
        if missing_matter_ids:
            for matter in Matter.query.filter(Matter.id.in_(missing_matter_ids)).all():
                matter_map[matter.id] = matter
        return page(
            "Portal Administration",
            "portal/admin_users.html",
            users=users,
            grant_users=grant_users,
            accesses=accesses,
            matters=matters,
            user_map=user_map,
            matter_map=matter_map,
            user_pagination=user_pagination,
            access_pagination=access_pagination,
        )
