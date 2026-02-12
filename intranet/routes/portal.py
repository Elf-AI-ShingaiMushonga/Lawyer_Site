from __future__ import annotations

import datetime as dt
import hashlib
import os
import secrets
import uuid
from functools import wraps

from flask import abort, flash, redirect, request, send_from_directory, session, url_for
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from ..extensions import db
from ..helpers import allowed_doc, audit, sha256_file
from ..mfa import build_otpauth_uri, generate_totp_secret, verify_totp
from ..models import (
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
)
from ..policies import enforce_data_residency
from ..services.notification_engine import NotificationEngine
from ..templates import page


PORTAL_SESSION_KEY = "portal_user_id"
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
    return db.session.get(PortalUser, int(user_id))


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _visibility_rank(level: str | None) -> int:
    normalized = (level or "summary_only").strip().lower()
    return VISIBILITY_LEVEL_RANK.get(normalized, VISIBILITY_LEVEL_RANK["summary_only"])


def portal_login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if _portal_current_user() is None:
            return redirect(url_for("portal_login"))
        return view(*args, **kwargs)

    return wrapped


def _portal_accessible_matter_ids(portal_user_id: int, *, min_level: str = "summary_only") -> list[int]:
    required_rank = _visibility_rank(min_level)
    return [
        matter_id
        for matter_id, level in db.session.query(PortalMatterAccess.matter_id, PortalMatterAccess.visibility_level)
        .filter(PortalMatterAccess.portal_user_id == portal_user_id, PortalMatterAccess.revoked_at.is_(None))
        .all()
        if _visibility_rank(level) >= required_rank
    ]


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


def register_portal_routes(app):
    @app.route("/portal/login", methods=["GET", "POST"])
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
                if not user.mfa_secret or not verify_totp(user.mfa_secret, mfa_code):
                    flash("Invalid portal MFA code.", "warning")
                    return redirect(url_for("portal_login"))
            session[PORTAL_SESSION_KEY] = user.id
            user.last_login_at = dt.datetime.utcnow()
            db.session.commit()
            audit("portal_login", "PortalUser", user.id)
            return redirect(url_for("portal_matters"))

        return page("Portal Login", "portal/login.html")

    @app.post("/portal/logout")
    @portal_login_required
    def portal_logout():
        user = _portal_current_user()
        session.pop(PORTAL_SESSION_KEY, None)
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

    @app.route("/portal/messages", methods=["GET", "POST"])
    @portal_login_required
    def portal_messages():
        portal_user = _portal_current_user()
        assert portal_user is not None

        if request.method == "POST":
            matter_id = request.form.get("matter_id", type=int)
            if not matter_id or not _portal_has_matter_access(portal_user.id, matter_id, min_level="full_curated"):
                abort(403)

            thread_id = request.form.get("thread_id", type=int)
            body = (request.form.get("body") or "").strip()
            if not body:
                flash("Message body required.", "warning")
                return redirect(url_for("portal_messages"))

            if not thread_id:
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
        return page(
            "Portal Messages",
            "portal/messages.html",
            portal_user=portal_user,
            threads=threads,
            messages=messages,
            matters=matters,
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
            path = os.path.join(app.config["UPLOAD_DIR"], stored)
            f.save(path)

            row = PortalUpload(
                matter_id=matter_id,
                portal_user_id=portal_user.id,
                filename=safe,
                stored_filename=stored,
                sha256=sha256_file(path),
            )
            db.session.add(row)
            db.session.commit()
            NotificationEngine.enqueue("portal_upload_created", None, f"portal_upload:{row.id}")
            audit("portal_upload", "PortalUpload", row.id)
            flash("Upload complete.", "info")
            return redirect(url_for("portal_uploads"))

        ids = _portal_accessible_matter_ids(portal_user.id, min_level="shared_docs")
        uploads = PortalUpload.query.filter(PortalUpload.matter_id.in_(ids)).order_by(PortalUpload.uploaded_at.desc()).all() if ids else []
        matters = Matter.query.filter(Matter.id.in_(ids)).order_by(Matter.opened_at.desc()).all() if ids else []
        return page("Portal Uploads", "portal/uploads.html", portal_user=portal_user, uploads=uploads, matters=matters)

    @app.get("/portal/invoices")
    @portal_login_required
    def portal_invoices():
        portal_user = _portal_current_user()
        assert portal_user is not None
        ids = _portal_accessible_matter_ids(portal_user.id, min_level="full_curated")
        invoices = Invoice.query.filter(Invoice.matter_id.in_(ids)).order_by(Invoice.created_at.desc()).all() if ids else []
        matter_map = {m.id: m for m in Matter.query.filter(Matter.id.in_({inv.matter_id for inv in invoices})).all()} if invoices else {}

        for inv in invoices:
            viewed = PortalInvoiceView.query.filter_by(portal_user_id=portal_user.id, invoice_id=inv.id).first()
            if viewed is None:
                db.session.add(PortalInvoiceView(portal_user_id=portal_user.id, invoice_id=inv.id, last_viewed_at=dt.datetime.utcnow()))
            else:
                viewed.last_viewed_at = dt.datetime.utcnow()
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
                status="recorded",
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
                    created_by=None,
                )
            )
            db.session.commit()
            NotificationEngine.enqueue("portal_payment_recorded", None, f"portal_payment:{receipt.id}")
            audit("portal_payment_record", "PortalPaymentReceipt", receipt.id)
            flash("Payment receipt recorded.", "info")
            return redirect(url_for("portal_payments", invoice_id=invoice_id))

        receipts = PortalPaymentReceipt.query.filter_by(invoice_id=invoice.id, portal_user_id=portal_user.id).order_by(
            PortalPaymentReceipt.created_at.desc()
        ).all()
        return page("Portal Payment", "portal/payment.html", portal_user=portal_user, invoice=invoice, receipts=receipts)

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
                expires_at=dt.datetime.utcnow() + dt.timedelta(minutes=expires_minutes),
            )
            db.session.add(token)
            db.session.commit()
            audit("portal_link_create", "PortalLinkToken", token.id)
            session["portal_last_link_url"] = url_for("portal_link_access", token=token_raw, _external=False)
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
        )

    @app.get("/portal/link/<token>")
    def portal_link_access(token: str):
        token_hash = _hash_token(token)
        row = PortalLinkToken.query.filter_by(token_hash=token_hash).first()
        if row is None:
            abort(404)
        if row.expires_at < dt.datetime.utcnow():
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
            path = os.path.join(app.config["UPLOAD_DIR"], version.stored_filename)
            if not os.path.isfile(path):
                abort(404)
            row.used_at = dt.datetime.utcnow()
            db.session.commit()
            audit(
                "portal_link_document_access",
                "DocumentVersion",
                version.id,
                {"portal_user_id": portal_user.id, "matter_id": record.matter_id},
            )
            return send_from_directory(
                app.config["UPLOAD_DIR"],
                version.stored_filename,
                as_attachment=True,
                download_name=version.original_filename,
            )

        if not row.matter_id:
            abort(404)
        if not _portal_has_matter_access(portal_user.id, row.matter_id, min_level="summary_only"):
            abort(403)
        row.used_at = dt.datetime.utcnow()
        db.session.commit()
        audit("portal_link_matter_access", "Matter", row.matter_id, {"portal_user_id": portal_user.id})
        return redirect(url_for("portal_matter_detail", matter_id=row.matter_id))

    @app.route("/admin/portal/users", methods=["GET", "POST"])
    @login_required
    def admin_portal_users():
        if current_user.role != "admin":
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
                    access.revoked_at = dt.datetime.utcnow()
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

        users = PortalUser.query.order_by(PortalUser.created_at.desc()).all()
        accesses = PortalMatterAccess.query.order_by(PortalMatterAccess.granted_at.desc()).all()
        matters = Matter.query.order_by(Matter.opened_at.desc()).limit(300).all()
        user_map = {u.id: u for u in users}
        matter_map = {m.id: m for m in matters}
        return page(
            "Portal Administration",
            "portal/admin_users.html",
            users=users,
            accesses=accesses,
            matters=matters,
            user_map=user_map,
            matter_map=matter_map,
        )
