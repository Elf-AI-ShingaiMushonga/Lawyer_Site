from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os

from flask import Response, abort, flash, redirect, request, send_file, url_for
from flask_login import current_user, login_required
from sqlalchemy import and_, func, or_

from ..extensions import db
from ..helpers import audit, can_access_matter, get_active_matter_id, is_admin, set_active_matter_context
from ..models import (
    ARSnapshot,
    AuditLog,
    FeeArrangement,
    Invoice,
    InvoiceAdjustment,
    InvoiceLine,
    LEDESExport,
    Matter,
    PaymentAllocation,
    PortalPaymentReceipt,
    RateCard,
    User,
)
from ..policies import enforce_data_residency, enforce_permission, visible_matter_ids
from ..reports.ledes import generate_ledes_1998b
from ..services.billing_engine import BillingEngine
from ..services.notification_engine import NotificationEngine
from ..services.workflow_automation import (
    reconcile_invoice_payment_status,
    schedule_invoice_collection_followups,
)
from ..templates import page


def _build_lines_pdf(text_lines: list[str]) -> bytes:
    escaped = [
        (line or "").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        for line in (text_lines or [""])
    ]
    stream = f"BT /F1 10 Tf 50 760 Td 12 TL ({') Tj T* ('.join(escaped)}) Tj ET".encode("utf-8")

    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n",
        b"4 0 obj\n<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream\nendobj\n",
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
    ]

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(out.tell())
        out.write(obj)
    xref_pos = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    out.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.write(f"{off:010d} 00000 n \n".encode("ascii"))
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii"))
    return out.getvalue()


def _build_invoice_pdf(invoice: Invoice, lines: list[InvoiceLine], *, heading: str = "Invoice", as_of: dt.date | None = None) -> bytes:
    rows = [
        heading,
        f"Invoice #{invoice.id}",
        f"Client: {invoice.client_name}",
        f"Matter: {invoice.matter_id}",
        f"Period: {invoice.period_start} to {invoice.period_end}",
        f"Status: {invoice.status}",
    ]
    if as_of is not None:
        rows.append(f"Statement As Of: {as_of.isoformat()}")
    rows += ["", "Lines:"]
    for idx, line in enumerate(lines, start=1):
        rows.append(
            f"{idx}. {line.description} | hours={line.hours:.2f} rate={line.rate:.2f} amount={line.amount:.2f}"
        )
    rows += ["", f"Subtotal: {invoice.subtotal:.2f}", f"Tax: {invoice.tax_total:.2f}", f"Total: {invoice.total:.2f}"]
    return _build_lines_pdf(rows)


def _persist_invoice_pdf(upload_dir: str, filename: str, payload: bytes) -> str:
    invoice_dir = os.path.join(upload_dir, "invoices")
    os.makedirs(invoice_dir, exist_ok=True)
    path = os.path.join(invoice_dir, filename)
    with open(path, "wb") as f:
        f.write(payload)
    return path


def _parse_as_of(raw: str | None, fallback: dt.date | None = None) -> dt.date:
    if not raw:
        return fallback or dt.date.today()
    try:
        return dt.date.fromisoformat(raw.strip())
    except ValueError:
        return fallback or dt.date.today()


def _csv_response(filename: str, headers: list[str], rows: list[list[object]]) -> Response:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _settled_payment_clause():
    return or_(PaymentAllocation.status == "settled", PaymentAllocation.status.is_(None))


def _is_settled_payment_row(payment: PaymentAllocation) -> bool:
    status = (payment.status or "settled").strip().lower()
    return status == "settled"


def _settled_paid_total_for_invoice(invoice_id: int) -> float:
    total = (
        db.session.query(func.coalesce(func.sum(PaymentAllocation.amount), 0.0))
        .filter(PaymentAllocation.invoice_id == invoice_id)
        .filter(_settled_payment_clause())
        .scalar()
        or 0.0
    )
    return round(float(total), 2)


def _parse_optional_datetime(raw: str | None) -> dt.datetime | None:
    value = (raw or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = dt.datetime.strptime(value, fmt)
            if fmt == "%Y-%m-%d":
                return dt.datetime.combine(parsed.date(), dt.time(12, 0))
            return parsed
        except ValueError:
            continue
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def _billing_audit_scope_filter(scope_ids: list[int]):
    own_activity = AuditLog.actor_user_id == current_user.id
    if not scope_ids:
        return own_activity

    invoice_scope = db.session.query(Invoice.id).filter(Invoice.matter_id.in_(scope_ids))
    adjustment_scope = (
        db.session.query(InvoiceAdjustment.id)
        .join(Invoice, Invoice.id == InvoiceAdjustment.invoice_id)
        .filter(Invoice.matter_id.in_(scope_ids))
    )
    payment_scope = (
        db.session.query(PaymentAllocation.id)
        .join(Invoice, Invoice.id == PaymentAllocation.invoice_id)
        .filter(Invoice.matter_id.in_(scope_ids))
    )
    ledes_scope = (
        db.session.query(LEDESExport.id)
        .join(Invoice, Invoice.id == LEDESExport.invoice_id)
        .filter(Invoice.matter_id.in_(scope_ids))
    )

    return or_(
        own_activity,
        and_(AuditLog.entity_type == "Matter", AuditLog.entity_id.in_(scope_ids)),
        and_(AuditLog.entity_type == "Invoice", AuditLog.entity_id.in_(invoice_scope)),
        and_(AuditLog.entity_type == "InvoiceAdjustment", AuditLog.entity_id.in_(adjustment_scope)),
        and_(AuditLog.entity_type == "PaymentAllocation", AuditLog.entity_id.in_(payment_scope)),
        and_(AuditLog.entity_type == "LEDESExport", AuditLog.entity_id.in_(ledes_scope)),
    )


def register_billing_routes(app):
    @app.route("/billing/rates", methods=["GET", "POST"])
    @login_required
    def billing_rates():
        if request.method == "POST":
            if current_user.role != "admin":
                abort(403)
            action = (request.form.get("action") or "rate").strip().lower()
            if action == "fee_arrangement":
                matter_id = request.form.get("matter_id", type=int)
                arrangement_type = (request.form.get("arrangement_type") or "hourly").strip().lower()
                if not matter_id:
                    flash("Matter is required for fee arrangements.", "warning")
                    return redirect(url_for("billing_rates"))
                if db.session.get(Matter, matter_id) is None:
                    flash("Matter not found.", "warning")
                    return redirect(url_for("billing_rates"))
                if arrangement_type not in {"hourly", "fixed", "capped", "blended"}:
                    flash("Invalid arrangement type.", "warning")
                    return redirect(url_for("billing_rates"))

                fixed_amount = request.form.get("fixed_amount", type=float)
                cap_amount = request.form.get("cap_amount", type=float)
                blended_rate = request.form.get("blended_rate", type=float)
                if arrangement_type == "fixed" and (fixed_amount is None or fixed_amount <= 0):
                    flash("Fixed arrangements require a positive fixed amount.", "warning")
                    return redirect(url_for("billing_rates"))
                if arrangement_type == "capped" and (cap_amount is None or cap_amount <= 0):
                    flash("Capped arrangements require a positive cap amount.", "warning")
                    return redirect(url_for("billing_rates"))
                if arrangement_type == "blended" and (blended_rate is None or blended_rate <= 0):
                    flash("Blended arrangements require a positive blended rate.", "warning")
                    return redirect(url_for("billing_rates"))

                row = FeeArrangement(
                    matter_id=matter_id,
                    arrangement_type=arrangement_type,
                    fixed_amount=fixed_amount,
                    cap_amount=cap_amount,
                    blended_rate=blended_rate,
                    notes=(request.form.get("notes") or "").strip() or None,
                )
                db.session.add(row)
                db.session.commit()
                audit("fee_arrangement_create", "FeeArrangement", row.id)
                flash("Fee arrangement saved.", "info")
                return redirect(url_for("billing_rates"))

            rate = request.form.get("rate_per_hour", type=float)
            if rate is None:
                flash("Rate required.", "warning")
                return redirect(url_for("billing_rates"))
            row = RateCard(
                name=(request.form.get("name") or "").strip() or None,
                client_name=(request.form.get("client_name") or "").strip() or None,
                matter_id=request.form.get("matter_id", type=int),
                user_id=request.form.get("user_id", type=int),
                rate_per_hour=rate,
                currency=(request.form.get("currency") or "ZAR").strip().upper(),
                is_active=True,
            )
            db.session.add(row)
            db.session.commit()
            audit("billing_rate_create", "RateCard", row.id)
            flash("Rate saved.", "info")
            return redirect(url_for("billing_rates"))

        rate_query = RateCard.query
        arrangement_query = FeeArrangement.query
        matter_query = Matter.query
        if not is_admin():
            scoped_ids = visible_matter_ids()
            if scoped_ids:
                rate_query = rate_query.filter((RateCard.matter_id.is_(None)) | (RateCard.matter_id.in_(scoped_ids)))
                arrangement_query = arrangement_query.filter(FeeArrangement.matter_id.in_(scoped_ids))
                matter_query = matter_query.filter(Matter.id.in_(scoped_ids))
            else:
                rate_query = rate_query.filter(RateCard.id == -1)
                arrangement_query = arrangement_query.filter(FeeArrangement.id == -1)
                matter_query = matter_query.filter(Matter.id == -1)
        rates = rate_query.order_by(RateCard.id.desc()).limit(300).all()
        arrangements = arrangement_query.order_by(FeeArrangement.id.desc()).limit(300).all()
        matters = matter_query.order_by(Matter.opened_at.desc()).limit(200).all()
        matter_lookup = {matter.id: matter for matter in matters}
        matter_ids_from_rows = {
            int(rate.matter_id)
            for rate in rates
            if rate.matter_id is not None and int(rate.matter_id) not in matter_lookup
        }
        matter_ids_from_rows.update(
            int(arrangement.matter_id)
            for arrangement in arrangements
            if arrangement.matter_id is not None and int(arrangement.matter_id) not in matter_lookup
        )
        if matter_ids_from_rows:
            for row in Matter.query.filter(Matter.id.in_(sorted(matter_ids_from_rows))).all():
                matter_lookup[row.id] = row

        assignable_users = (
            User.query.filter(User.is_active.is_(True)).order_by(User.full_name.asc(), User.email.asc()).limit(300).all()
            if is_admin()
            else []
        )
        user_lookup = {user.id: user for user in assignable_users}
        missing_user_ids = {
            int(rate.user_id)
            for rate in rates
            if rate.user_id is not None and int(rate.user_id) not in user_lookup
        }
        if missing_user_ids:
            for row in User.query.filter(User.id.in_(sorted(missing_user_ids))).all():
                user_lookup[row.id] = row

        return page(
            "Billing Rates",
            "billing/rates.html",
            rates=rates,
            arrangements=arrangements,
            matters=matters,
            assignable_users=assignable_users,
            matter_lookup=matter_lookup,
            user_lookup=user_lookup,
        )

    @app.route("/billing/invoices", methods=["GET", "POST"])
    @login_required
    def billing_invoices():
        if request.method == "POST":
            enforce_permission("billing", "generate")
            matter_id = request.form.get("matter_id", type=int)
            if not matter_id or not can_access_matter(matter_id):
                abort(403)
            try:
                period_start = dt.date.fromisoformat((request.form.get("period_start") or "").strip())
                period_end = dt.date.fromisoformat((request.form.get("period_end") or "").strip())
            except ValueError:
                flash("Invalid period dates.", "warning")
                return redirect(url_for("billing_invoices"))

            result = BillingEngine.generate_invoice(
                matter_id,
                (period_start, period_end),
                created_by=current_user.id,
            )
            set_active_matter_context(matter_id)
            if result.invoice_id is None:
                flash("No approved time/expenses for that period.", "warning")
            else:
                audit("invoice_generate", "Invoice", result.invoice_id, {"line_count": result.line_count})
                NotificationEngine.enqueue("invoice_created", current_user.id, f"invoice:{result.invoice_id}")
                flash(f"Invoice {result.invoice_id} generated.", "info")
            return redirect(url_for("billing_invoices"))

        page_number = request.args.get("page", default=1, type=int) or 1
        if page_number < 1:
            page_number = 1
        selected_matter_id = request.args.get("matter_id", type=int)
        if not selected_matter_id:
            selected_matter_id = get_active_matter_id()
        invoice_query = Invoice.query
        matter_query = Matter.query
        if not is_admin():
            scoped_ids = visible_matter_ids()
            if scoped_ids:
                invoice_query = invoice_query.filter(Invoice.matter_id.in_(scoped_ids))
                matter_query = matter_query.filter(Matter.id.in_(scoped_ids))
            else:
                invoice_query = invoice_query.filter(Invoice.id == -1)
                matter_query = matter_query.filter(Matter.id == -1)
        pagination = invoice_query.order_by(Invoice.created_at.desc()).paginate(page=page_number, per_page=50, error_out=False)
        rows = pagination.items
        matters = matter_query.order_by(Matter.opened_at.desc()).limit(200).all()
        selectable_matter_ids = {matter.id for matter in matters}
        if selected_matter_id and selected_matter_id not in selectable_matter_ids:
            selected_matter = db.session.get(Matter, selected_matter_id)
            if selected_matter and can_access_matter(selected_matter.id):
                matters = [selected_matter] + matters
                selectable_matter_ids.add(selected_matter.id)
            else:
                selected_matter_id = None

        matter_ids_for_display = {int(row.matter_id) for row in rows if row.matter_id}
        missing_matter_ids = matter_ids_for_display - selectable_matter_ids
        display_matters = list(matters)
        if missing_matter_ids:
            display_matters.extend(Matter.query.filter(Matter.id.in_(missing_matter_ids)).all())
        matter_lookup = {matter.id: matter for matter in display_matters}

        today = dt.date.today()
        default_period_start = request.args.get("period_start", type=str) or today.replace(day=1).isoformat()
        default_period_end = request.args.get("period_end", type=str) or today.isoformat()
        total_billed = float(invoice_query.with_entities(func.coalesce(func.sum(Invoice.total), 0.0)).scalar() or 0.0)
        approved_count = invoice_query.filter(Invoice.status.in_(["approved", "part_paid", "paid"])).count()
        draft_count = invoice_query.filter(Invoice.status == "draft").count()
        return page(
            "Invoices",
            "billing/invoices.html",
            invoices=rows,
            matters=matters,
            matter_lookup=matter_lookup,
            pagination=pagination,
            total_invoices=pagination.total,
            approved_count=approved_count,
            draft_count=draft_count,
            total_billed=total_billed,
            selected_matter_id=selected_matter_id,
            default_period_start=default_period_start,
            default_period_end=default_period_end,
        )

    @app.get("/billing/invoices/<int:invoice_id>")
    @login_required
    def billing_invoice_detail(invoice_id: int):
        inv = db.session.get(Invoice, invoice_id)
        if not inv:
            abort(404)
        if not can_access_matter(inv.matter_id):
            abort(403)
        set_active_matter_context(inv.matter_id)

        lines = InvoiceLine.query.filter_by(invoice_id=invoice_id).order_by(InvoiceLine.id.asc()).all()
        adjustments = InvoiceAdjustment.query.filter_by(invoice_id=invoice_id).order_by(InvoiceAdjustment.created_at.desc()).all()
        payments = PaymentAllocation.query.filter_by(invoice_id=invoice_id).order_by(PaymentAllocation.allocated_at.desc()).all()
        settled_total = round(sum(float(p.amount or 0.0) for p in payments if _is_settled_payment_row(p)), 2)
        outstanding = round(float(inv.total or 0.0) - settled_total, 2)
        outstanding = max(0.0, outstanding)
        return page(
            "Invoice Detail",
            "billing/invoice_detail.html",
            inv=inv,
            lines=lines,
            adjustments=adjustments,
            payments=payments,
            outstanding=outstanding,
            settled_total=settled_total,
        )

    @app.post("/billing/invoices/<int:invoice_id>/approve")
    @login_required
    def billing_invoice_approve(invoice_id: int):
        enforce_permission("billing", "approve")
        inv = db.session.get(Invoice, invoice_id)
        if not inv:
            abort(404)
        if not can_access_matter(inv.matter_id):
            abort(403)

        inv.status = "approved"
        inv.approved_by = current_user.id
        inv.approved_at = dt.datetime.utcnow()
        followup_task_ids = schedule_invoice_collection_followups(
            inv.id,
            actor_user_id=current_user.id,
        )
        db.session.commit()
        NotificationEngine.enqueue("invoice_approved", current_user.id, f"invoice:{inv.id}")
        NotificationEngine.enqueue("invoice_sent", current_user.id, f"invoice:{inv.id}")
        audit("invoice_approve", "Invoice", inv.id, {"followup_task_ids": followup_task_ids})
        if followup_task_ids:
            flash(
                f"Invoice approved. Collection follow-up task(s) created: {', '.join(f'#{task_id}' for task_id in followup_task_ids)}.",
                "info",
            )
        else:
            flash("Invoice approved.", "info")
        return redirect(url_for("billing_invoice_detail", invoice_id=inv.id))

    @app.post("/billing/invoices/<int:invoice_id>/payments")
    @login_required
    def billing_invoice_payment_capture(invoice_id: int):
        enforce_permission("billing", "capture_payment")
        inv = db.session.get(Invoice, invoice_id)
        if not inv:
            abort(404)
        if not can_access_matter(inv.matter_id):
            abort(403)

        amount = request.form.get("amount", type=float)
        if amount is None or amount <= 0:
            flash("Payment amount must be positive.", "warning")
            return redirect(url_for("billing_invoice_detail", invoice_id=invoice_id))

        status = (request.form.get("status") or "settled").strip().lower()
        if status not in {"settled", "pending", "failed"}:
            flash("Payment status must be settled, pending, or failed.", "warning")
            return redirect(url_for("billing_invoice_detail", invoice_id=invoice_id))

        settled_before = _settled_paid_total_for_invoice(inv.id)
        outstanding_before = max(0.0, round(float(inv.total or 0.0) - settled_before, 2))
        if status == "settled" and amount > outstanding_before + 0.01:
            flash("Settled payment exceeds outstanding invoice balance.", "warning")
            return redirect(url_for("billing_invoice_detail", invoice_id=invoice_id))

        settled_at = _parse_optional_datetime(request.form.get("settled_at"))
        allocated_at = settled_at or dt.datetime.utcnow()
        if status == "settled" and settled_at is None:
            settled_at = allocated_at

        row = PaymentAllocation(
            invoice_id=inv.id,
            amount=round(float(amount), 2),
            method=(request.form.get("method") or "").strip() or None,
            reference=(request.form.get("reference") or "").strip() or None,
            status=status,
            allocated_at=allocated_at,
            settled_at=settled_at if status == "settled" else None,
            settled_by=current_user.id if status == "settled" else None,
            external_txn_id=(request.form.get("external_txn_id") or "").strip() or None,
            processor_note=(request.form.get("processor_note") or "").strip() or None,
            created_by=current_user.id,
        )
        db.session.add(row)
        if status == "settled" and row.reference:
            PortalPaymentReceipt.query.filter_by(invoice_id=inv.id, reference=row.reference).update(
                {"status": "settled"},
                synchronize_session=False,
            )
        reconciled_status, outstanding_after = reconcile_invoice_payment_status(inv.id)
        db.session.commit()

        audit(
            "payment_capture",
            "PaymentAllocation",
            row.id,
            {
                "invoice_id": inv.id,
                "matter_id": inv.matter_id,
                "status": status,
                "amount": float(row.amount or 0.0),
                "method": row.method,
                "reference": row.reference,
                "external_txn_id": row.external_txn_id,
                "outstanding_before": outstanding_before,
                "outstanding_after": outstanding_after,
                "invoice_status_after": reconciled_status,
            },
        )
        NotificationEngine.enqueue("payment_recorded", current_user.id, f"payment:{row.id}")
        if reconciled_status == "paid":
            flash("Payment captured. Invoice is now fully settled.", "info")
        else:
            flash("Payment captured.", "info")
        return redirect(url_for("billing_invoice_detail", invoice_id=invoice_id))

    @app.post("/billing/payments/<int:payment_id>/settle")
    @login_required
    def billing_payment_settle(payment_id: int):
        enforce_permission("billing", "settle_payment")
        payment = db.session.get(PaymentAllocation, payment_id)
        if not payment:
            abort(404)
        inv = db.session.get(Invoice, payment.invoice_id)
        if not inv:
            abort(404)
        if not can_access_matter(inv.matter_id):
            abort(403)

        if _is_settled_payment_row(payment):
            flash("Payment is already settled.", "warning")
            return redirect(url_for("billing_invoice_detail", invoice_id=inv.id))

        settled_before = _settled_paid_total_for_invoice(inv.id)
        outstanding_before = max(0.0, round(float(inv.total or 0.0) - settled_before, 2))
        amount = float(payment.amount or 0.0)
        if amount > outstanding_before + 0.01:
            flash("Cannot settle payment because it exceeds outstanding balance.", "warning")
            return redirect(url_for("billing_invoice_detail", invoice_id=inv.id))

        settled_at = _parse_optional_datetime(request.form.get("settled_at")) or dt.datetime.utcnow()
        payment.status = "settled"
        payment.settled_at = settled_at
        payment.settled_by = current_user.id
        external_txn_id = (request.form.get("external_txn_id") or "").strip()
        if external_txn_id:
            payment.external_txn_id = external_txn_id
        processor_note = (request.form.get("processor_note") or "").strip()
        if processor_note:
            payment.processor_note = processor_note
        if payment.reference:
            PortalPaymentReceipt.query.filter_by(invoice_id=inv.id, reference=payment.reference).update(
                {"status": "settled"},
                synchronize_session=False,
            )
        reconciled_status, outstanding_after = reconcile_invoice_payment_status(inv.id)
        db.session.commit()

        audit(
            "payment_settle",
            "PaymentAllocation",
            payment.id,
            {
                "invoice_id": inv.id,
                "matter_id": inv.matter_id,
                "amount": amount,
                "method": payment.method,
                "reference": payment.reference,
                "external_txn_id": payment.external_txn_id,
                "outstanding_before": outstanding_before,
                "outstanding_after": outstanding_after,
                "invoice_status_after": reconciled_status,
            },
        )
        NotificationEngine.enqueue("payment_settled", current_user.id, f"payment:{payment.id}")
        if reconciled_status == "paid":
            flash("Payment marked as settled. Invoice is now fully paid.", "info")
        else:
            flash("Payment marked as settled.", "info")
        return redirect(url_for("billing_invoice_detail", invoice_id=inv.id))

    @app.get("/billing/invoices/<int:invoice_id>/pdf")
    @login_required
    def billing_invoice_pdf(invoice_id: int):
        inv = db.session.get(Invoice, invoice_id)
        if not inv:
            abort(404)
        if not can_access_matter(inv.matter_id):
            abort(403)
        enforce_data_residency("exports")

        lines = InvoiceLine.query.filter_by(invoice_id=invoice_id).order_by(InvoiceLine.id.asc()).all()
        filename = f"invoice_{invoice_id}.pdf"
        path = _persist_invoice_pdf(app.config["UPLOAD_DIR"], filename, _build_invoice_pdf(inv, lines, heading="Invoice"))
        inv.pdf_path = path
        db.session.commit()
        audit("invoice_pdf_generate", "Invoice", inv.id)

        return send_file(path, as_attachment=True, download_name=filename, mimetype="application/pdf")

    @app.get("/billing/invoices/<int:invoice_id>/tax-invoice")
    @login_required
    def billing_tax_invoice_pdf(invoice_id: int):
        inv = db.session.get(Invoice, invoice_id)
        if not inv:
            abort(404)
        if not can_access_matter(inv.matter_id):
            abort(403)
        enforce_data_residency("exports")

        lines = InvoiceLine.query.filter_by(invoice_id=invoice_id).order_by(InvoiceLine.id.asc()).all()
        filename = f"tax_invoice_{invoice_id}.pdf"
        payload = _build_invoice_pdf(inv, lines, heading="Tax Invoice")
        path = _persist_invoice_pdf(app.config["UPLOAD_DIR"], filename, payload)
        audit("tax_invoice_pdf_generate", "Invoice", inv.id)
        return send_file(path, as_attachment=True, download_name=filename, mimetype="application/pdf")

    @app.get("/billing/invoices/<int:invoice_id>/ledes")
    @login_required
    def billing_invoice_ledes(invoice_id: int):
        inv = db.session.get(Invoice, invoice_id)
        if not inv:
            abort(404)
        if not can_access_matter(inv.matter_id):
            abort(403)
        enforce_data_residency("exports")

        data = generate_ledes_1998b(inv)
        export_dir = os.path.join(app.config["UPLOAD_DIR"], "ledes")
        os.makedirs(export_dir, exist_ok=True)
        filename = f"invoice_{invoice_id}_ledes_1998b.csv"
        path = os.path.join(export_dir, filename)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(data)

        export = LEDESExport(invoice_id=inv.id, variant="1998B", file_path=path, created_by=current_user.id)
        db.session.add(export)
        db.session.commit()
        audit("invoice_ledes_export", "LEDESExport", export.id)

        return Response(
            data,
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/billing/invoices/<int:invoice_id>/adjust")
    @login_required
    def billing_invoice_adjust(invoice_id: int):
        enforce_permission("billing", "adjust")
        inv = db.session.get(Invoice, invoice_id)
        if not inv:
            abort(404)
        if not can_access_matter(inv.matter_id):
            abort(403)

        adjustment_type = (request.form.get("adjustment_type") or "").strip().lower()
        if adjustment_type not in {"write_down", "write_off"}:
            flash("Adjustment type must be write_down or write_off.", "warning")
            return redirect(url_for("billing_invoice_detail", invoice_id=invoice_id))

        amount = request.form.get("amount", type=float)
        reason = (request.form.get("reason") or "").strip()
        if amount is None or amount <= 0:
            flash("Adjustment amount must be positive.", "warning")
            return redirect(url_for("billing_invoice_detail", invoice_id=invoice_id))
        if not reason:
            flash("Adjustment reason is required.", "warning")
            return redirect(url_for("billing_invoice_detail", invoice_id=invoice_id))

        current_total = float(inv.total or 0.0)
        applied_amount = min(current_total, float(amount))
        if applied_amount <= 0:
            flash("Invoice total is already zero.", "warning")
            return redirect(url_for("billing_invoice_detail", invoice_id=invoice_id))

        adjustment = InvoiceAdjustment(
            invoice_id=inv.id,
            adjustment_type=adjustment_type,
            reason=reason,
            amount=-round(applied_amount, 2),
            created_by=current_user.id,
        )
        db.session.add(adjustment)

        current_subtotal = float(inv.subtotal or 0.0)
        current_tax = float(inv.tax_total or 0.0)
        reduce_subtotal = min(current_subtotal, applied_amount)
        reduce_tax = min(current_tax, max(0.0, applied_amount - reduce_subtotal))
        inv.subtotal = round(current_subtotal - reduce_subtotal, 2)
        inv.tax_total = round(current_tax - reduce_tax, 2)
        inv.total = round(inv.subtotal + inv.tax_total, 2)
        db.session.commit()

        audit(
            "invoice_adjustment",
            "InvoiceAdjustment",
            adjustment.id,
            {"invoice_id": inv.id, "type": adjustment_type, "amount": applied_amount},
        )
        flash("Invoice adjustment applied.", "info")
        return redirect(url_for("billing_invoice_detail", invoice_id=invoice_id))

    @app.route("/billing/ar-aging", methods=["GET", "POST"])
    @login_required
    def billing_ar_aging():
        enforce_permission("billing", "report")

        as_of = dt.date.today()
        if request.method == "POST":
            raw = (request.form.get("as_of_date") or "").strip()
            try:
                as_of = dt.date.fromisoformat(raw)
            except ValueError:
                flash("Invalid as-of date.", "warning")
                return redirect(url_for("billing_ar_aging"))
        else:
            raw = (request.args.get("as_of_date") or "").strip()
            if raw:
                try:
                    as_of = dt.date.fromisoformat(raw)
                except ValueError:
                    pass

        scope_ids = None if is_admin() else visible_matter_ids()
        invoice_query = Invoice.query
        if scope_ids is not None:
            if scope_ids:
                invoice_query = invoice_query.filter(Invoice.matter_id.in_(scope_ids))
            else:
                invoice_query = invoice_query.filter(Invoice.id == -1)
        invoices = invoice_query.order_by(Invoice.created_at.asc()).all()
        invoice_ids = [inv.id for inv in invoices]

        paid_by_invoice: dict[int, float] = {}
        snapshots_by_invoice: dict[int, ARSnapshot] = {}
        if invoice_ids:
            cutoff = dt.datetime.combine(as_of, dt.time.max)
            paid_by_invoice = {
                int(invoice_id): float(amount or 0.0)
                for invoice_id, amount in (
                    db.session.query(PaymentAllocation.invoice_id, func.coalesce(func.sum(PaymentAllocation.amount), 0.0))
                    .filter(
                        PaymentAllocation.invoice_id.in_(invoice_ids),
                        PaymentAllocation.allocated_at <= cutoff,
                        _settled_payment_clause(),
                    )
                    .group_by(PaymentAllocation.invoice_id)
                    .all()
                )
            }
            snapshots_by_invoice = {
                int(row.invoice_id): row
                for row in ARSnapshot.query.filter(
                    ARSnapshot.as_of_date == as_of,
                    ARSnapshot.invoice_id.in_(invoice_ids),
                ).all()
            }

        rows = []
        for inv in invoices:
            paid = paid_by_invoice.get(inv.id, 0.0)
            outstanding = round(max(0.0, float(inv.total or 0.0) - paid), 2)
            if outstanding <= 0:
                continue

            age_days = max(0, (as_of - inv.period_end).days)
            if age_days <= 30:
                bucket = "0-30"
            elif age_days <= 60:
                bucket = "31-60"
            elif age_days <= 90:
                bucket = "61-90"
            else:
                bucket = "90+"

            snapshot = snapshots_by_invoice.get(inv.id)
            if snapshot is None:
                snapshot = ARSnapshot(as_of_date=as_of, invoice_id=inv.id)
                db.session.add(snapshot)
                snapshots_by_invoice[inv.id] = snapshot
            snapshot.outstanding_amount = outstanding
            snapshot.aging_bucket = bucket
            if snapshot.collection_notes is None:
                snapshot.collection_notes = "Pending outreach"

            rows.append(
                {
                    "invoice": inv,
                    "outstanding": outstanding,
                    "age_days": age_days,
                    "bucket": bucket,
                    "collection_notes": snapshot.collection_notes or "",
                }
            )

        db.session.commit()
        totals = {
            "0-30": round(sum(r["outstanding"] for r in rows if r["bucket"] == "0-30"), 2),
            "31-60": round(sum(r["outstanding"] for r in rows if r["bucket"] == "31-60"), 2),
            "61-90": round(sum(r["outstanding"] for r in rows if r["bucket"] == "61-90"), 2),
            "90+": round(sum(r["outstanding"] for r in rows if r["bucket"] == "90+"), 2),
        }

        return page("AR Aging", "billing/ar_aging.html", rows=rows, as_of=as_of, totals=totals)

    @app.get("/billing/accounts/<int:matter_id>/statement")
    @login_required
    def billing_account_statement(matter_id: int):
        matter = db.session.get(Matter, matter_id)
        if matter is None:
            abort(404)
        if not can_access_matter(matter_id):
            abort(403)
        as_of = _parse_as_of(request.args.get("as_of"), dt.date.today())
        fmt = (request.args.get("format") or "html").strip().lower()
        cutoff = dt.datetime.combine(as_of, dt.time.max)

        invoices = Invoice.query.filter_by(matter_id=matter_id).order_by(Invoice.created_at.asc(), Invoice.id.asc()).all()
        invoice_ids = [inv.id for inv in invoices]
        paid_by_invoice: dict[int, float] = {}
        if invoice_ids:
            paid_by_invoice = {
                int(invoice_id): float(amount or 0.0)
                for invoice_id, amount in (
                    db.session.query(PaymentAllocation.invoice_id, func.coalesce(func.sum(PaymentAllocation.amount), 0.0))
                    .filter(
                        PaymentAllocation.invoice_id.in_(invoice_ids),
                        PaymentAllocation.allocated_at <= cutoff,
                        _settled_payment_clause(),
                    )
                    .group_by(PaymentAllocation.invoice_id)
                    .all()
                )
            }

        rows: list[dict[str, object]] = []
        for inv in invoices:
            paid = paid_by_invoice.get(inv.id, 0.0)
            total = round(float(inv.total or 0.0), 2)
            outstanding = round(max(0.0, total - paid), 2)
            rows.append(
                {
                    "invoice_id": inv.id,
                    "status": inv.status,
                    "period_start": inv.period_start,
                    "period_end": inv.period_end,
                    "subtotal": round(float(inv.subtotal or 0.0), 2),
                    "tax_total": round(float(inv.tax_total or 0.0), 2),
                    "total": total,
                    "paid": round(float(paid), 2),
                    "outstanding": outstanding,
                }
            )

        totals = {
            "subtotal": round(sum(float(row["subtotal"]) for row in rows), 2),
            "tax_total": round(sum(float(row["tax_total"]) for row in rows), 2),
            "total": round(sum(float(row["total"]) for row in rows), 2),
            "paid": round(sum(float(row["paid"]) for row in rows), 2),
            "outstanding": round(sum(float(row["outstanding"]) for row in rows), 2),
        }

        if fmt == "csv":
            csv_rows = [
                [
                    row["invoice_id"],
                    row["status"],
                    row["period_start"],
                    row["period_end"],
                    row["subtotal"],
                    row["tax_total"],
                    row["total"],
                    row["paid"],
                    row["outstanding"],
                ]
                for row in rows
            ]
            csv_rows.append(["TOTAL", "", "", "", totals["subtotal"], totals["tax_total"], totals["total"], totals["paid"], totals["outstanding"]])
            audit(
                "account_statement_export",
                "Matter",
                matter.id,
                {
                    "as_of": as_of.isoformat(),
                    "invoice_count": len(rows),
                    "outstanding": totals["outstanding"],
                },
            )
            return _csv_response(
                f"account_statement_matter_{matter_id}_{as_of.isoformat()}.csv",
                ["invoice_id", "status", "period_start", "period_end", "subtotal", "tax_total", "total", "paid", "outstanding"],
                csv_rows,
            )
        audit(
            "account_statement_view",
            "Matter",
            matter.id,
            {
                "as_of": as_of.isoformat(),
                "invoice_count": len(rows),
                "outstanding": totals["outstanding"],
            },
        )
        return page(
            "Account Statement",
            "billing/account_statement.html",
            matter=matter,
            rows=rows,
            totals=totals,
            as_of=as_of,
        )

    @app.get("/billing/reports/trial-balance")
    @login_required
    def billing_trial_balance():
        enforce_permission("billing", "report")
        as_of = _parse_as_of(request.args.get("as_of"), dt.date.today())
        fmt = (request.args.get("format") or "html").strip().lower()
        cutoff = dt.datetime.combine(as_of, dt.time.max)

        scope_ids = None if is_admin() else visible_matter_ids()
        invoice_query = Invoice.query.filter(Invoice.created_at <= cutoff)
        matter_query = Matter.query
        if scope_ids is not None:
            if scope_ids:
                invoice_query = invoice_query.filter(Invoice.matter_id.in_(scope_ids))
                matter_query = matter_query.filter(Matter.id.in_(scope_ids))
            else:
                invoice_query = invoice_query.filter(Invoice.id == -1)
                matter_query = matter_query.filter(Matter.id == -1)
        invoices = invoice_query.order_by(Invoice.matter_id.asc(), Invoice.id.asc()).all()
        matters = {matter.id: matter for matter in matter_query.all()}

        invoice_ids = [inv.id for inv in invoices]
        paid_by_invoice: dict[int, float] = {}
        if invoice_ids:
            paid_by_invoice = {
                int(invoice_id): float(amount or 0.0)
                for invoice_id, amount in (
                    db.session.query(PaymentAllocation.invoice_id, func.coalesce(func.sum(PaymentAllocation.amount), 0.0))
                    .filter(
                        PaymentAllocation.invoice_id.in_(invoice_ids),
                        PaymentAllocation.allocated_at <= cutoff,
                        _settled_payment_clause(),
                    )
                    .group_by(PaymentAllocation.invoice_id)
                    .all()
                )
            }

        grouped: dict[int, dict[str, object]] = {}
        for inv in invoices:
            matter_id = int(inv.matter_id)
            bucket = grouped.setdefault(
                matter_id,
                {
                    "matter_id": matter_id,
                    "matter_no": (matters.get(matter_id).matter_no if matters.get(matter_id) else str(matter_id)),
                    "matter_title": (matters.get(matter_id).title if matters.get(matter_id) else "Unknown Matter"),
                    "invoice_count": 0,
                    "subtotal": 0.0,
                    "tax_total": 0.0,
                    "billed_total": 0.0,
                    "paid_total": 0.0,
                    "outstanding_total": 0.0,
                },
            )
            paid = float(paid_by_invoice.get(inv.id, 0.0))
            billed = float(inv.total or 0.0)
            outstanding = max(0.0, billed - paid)
            bucket["invoice_count"] = int(bucket["invoice_count"]) + 1
            bucket["subtotal"] = float(bucket["subtotal"]) + float(inv.subtotal or 0.0)
            bucket["tax_total"] = float(bucket["tax_total"]) + float(inv.tax_total or 0.0)
            bucket["billed_total"] = float(bucket["billed_total"]) + billed
            bucket["paid_total"] = float(bucket["paid_total"]) + paid
            bucket["outstanding_total"] = float(bucket["outstanding_total"]) + outstanding

        rows = []
        for row in grouped.values():
            rows.append(
                {
                    **row,
                    "subtotal": round(float(row["subtotal"]), 2),
                    "tax_total": round(float(row["tax_total"]), 2),
                    "billed_total": round(float(row["billed_total"]), 2),
                    "paid_total": round(float(row["paid_total"]), 2),
                    "outstanding_total": round(float(row["outstanding_total"]), 2),
                }
            )
        rows.sort(key=lambda r: (str(r["matter_no"]), int(r["matter_id"])))

        totals = {
            "invoice_count": sum(int(row["invoice_count"]) for row in rows),
            "subtotal": round(sum(float(row["subtotal"]) for row in rows), 2),
            "tax_total": round(sum(float(row["tax_total"]) for row in rows), 2),
            "billed_total": round(sum(float(row["billed_total"]) for row in rows), 2),
            "paid_total": round(sum(float(row["paid_total"]) for row in rows), 2),
            "outstanding_total": round(sum(float(row["outstanding_total"]) for row in rows), 2),
        }

        if fmt == "csv":
            csv_rows = [
                [
                    row["matter_id"],
                    row["matter_no"],
                    row["matter_title"],
                    row["invoice_count"],
                    row["subtotal"],
                    row["tax_total"],
                    row["billed_total"],
                    row["paid_total"],
                    row["outstanding_total"],
                ]
                for row in rows
            ]
            csv_rows.append(["TOTAL", "", "", totals["invoice_count"], totals["subtotal"], totals["tax_total"], totals["billed_total"], totals["paid_total"], totals["outstanding_total"]])
            audit(
                "billing_trial_balance_export",
                "Invoice",
                None,
                {"as_of": as_of.isoformat(), "rows": len(rows), "outstanding_total": totals["outstanding_total"]},
            )
            return _csv_response(
                f"business_trial_balance_{as_of.isoformat()}.csv",
                [
                    "matter_id",
                    "matter_no",
                    "matter_title",
                    "invoice_count",
                    "subtotal",
                    "tax_total",
                    "billed_total",
                    "paid_total",
                    "outstanding_total",
                ],
                csv_rows,
            )
        audit(
            "billing_trial_balance_view",
            "Invoice",
            None,
            {"as_of": as_of.isoformat(), "rows": len(rows), "outstanding_total": totals["outstanding_total"]},
        )
        return page(
            "Business Trial Balance",
            "billing/trial_balance.html",
            rows=rows,
            totals=totals,
            as_of=as_of,
        )

    @app.get("/billing/reports/auditor")
    @login_required
    def billing_auditor_report():
        enforce_permission("billing", "report")
        as_of = _parse_as_of(request.args.get("as_of"), dt.date.today())
        fmt = (request.args.get("format") or "html").strip().lower()
        cutoff = dt.datetime.combine(as_of, dt.time.max)

        scope_ids = None if is_admin() else visible_matter_ids()
        invoice_query = Invoice.query.filter(Invoice.created_at <= cutoff)
        if scope_ids is not None:
            if scope_ids:
                invoice_query = invoice_query.filter(Invoice.matter_id.in_(scope_ids))
            else:
                invoice_query = invoice_query.filter(Invoice.id == -1)
        invoices = invoice_query.all()
        invoice_ids = [inv.id for inv in invoices]

        payments_total = 0.0
        adjustments_total = 0.0
        overdue_90_plus = 0
        if invoice_ids:
            payments_total = float(
                db.session.query(func.coalesce(func.sum(PaymentAllocation.amount), 0.0))
                .filter(
                    PaymentAllocation.invoice_id.in_(invoice_ids),
                    PaymentAllocation.allocated_at <= cutoff,
                    _settled_payment_clause(),
                )
                .scalar()
                or 0.0
            )
            adjustments_total = float(
                db.session.query(func.coalesce(func.sum(InvoiceAdjustment.amount), 0.0))
                .filter(InvoiceAdjustment.invoice_id.in_(invoice_ids), InvoiceAdjustment.created_at <= cutoff)
                .scalar()
                or 0.0
            )
            overdue_90_plus = (
                ARSnapshot.query.filter(
                    ARSnapshot.invoice_id.in_(invoice_ids),
                    ARSnapshot.as_of_date == as_of,
                    ARSnapshot.aging_bucket == "90+",
                ).count()
            )

        subtotal_total = round(sum(float(inv.subtotal or 0.0) for inv in invoices), 2)
        tax_total = round(sum(float(inv.tax_total or 0.0) for inv in invoices), 2)
        billed_total = round(sum(float(inv.total or 0.0) for inv in invoices), 2)
        approved_count = sum(1 for inv in invoices if (inv.status or "").lower() in {"approved", "part_paid", "paid"})
        draft_count = sum(1 for inv in invoices if (inv.status or "").lower() == "draft")
        outstanding_total = round(max(0.0, billed_total - payments_total), 2)
        payload = {
            "as_of_date": as_of.isoformat(),
            "invoice_count": len(invoices),
            "approved_invoice_count": approved_count,
            "draft_invoice_count": draft_count,
            "subtotal_total": subtotal_total,
            "tax_total": tax_total,
            "billed_total": billed_total,
            "payments_total": round(payments_total, 2),
            "outstanding_total": outstanding_total,
            "adjustments_net_total": round(adjustments_total, 2),
            "overdue_90_plus_count": overdue_90_plus,
        }
        if fmt == "json":
            audit(
                "billing_auditor_report_export",
                "Invoice",
                None,
                {"as_of": as_of.isoformat(), "format": "json", "invoice_count": len(invoices)},
            )
            return Response(
                json.dumps(payload, indent=2),
                mimetype="application/json",
            )
        if fmt == "csv":
            audit(
                "billing_auditor_report_export",
                "Invoice",
                None,
                {"as_of": as_of.isoformat(), "format": "csv", "invoice_count": len(invoices)},
            )
            return _csv_response(
                f"business_auditor_report_{as_of.isoformat()}.csv",
                ["metric", "value"],
                [[k, v] for k, v in payload.items()],
            )
        audit(
            "billing_auditor_report_view",
            "Invoice",
            None,
            {"as_of": as_of.isoformat(), "invoice_count": len(invoices)},
        )
        return page(
            "Business Auditor Report",
            "billing/auditor_report.html",
            payload=payload,
            as_of=as_of,
        )

    @app.get("/billing/transactions")
    @login_required
    def billing_transactions():
        enforce_permission("billing", "report")

        scope_ids = None if is_admin() else visible_matter_ids()
        matter_filter = request.args.get("matter_id", type=int)
        invoice_filter = request.args.get("invoice_id", type=int)
        txn_type_filter = (request.args.get("txn_type") or "all").strip().lower() or "all"
        status_filter = (request.args.get("status") or "all").strip().lower() or "all"
        page_number = request.args.get("page", default=1, type=int) or 1
        if page_number < 1:
            page_number = 1
        per_page = 100
        start_date = None
        end_date = None
        start_raw = (request.args.get("start") or "").strip()
        end_raw = (request.args.get("end") or "").strip()
        try:
            if start_raw:
                start_date = dt.date.fromisoformat(start_raw)
            if end_raw:
                end_date = dt.date.fromisoformat(end_raw)
        except ValueError:
            flash("Invalid date filters. Use YYYY-MM-DD.", "warning")
            return redirect(url_for("billing_transactions"))
        fmt = (request.args.get("format") or "html").strip().lower()

        invoice_query = Invoice.query
        matter_query = Matter.query
        if scope_ids is not None:
            if scope_ids:
                invoice_query = invoice_query.filter(Invoice.matter_id.in_(scope_ids))
                matter_query = matter_query.filter(Matter.id.in_(scope_ids))
            else:
                invoice_query = invoice_query.filter(Invoice.id == -1)
                matter_query = matter_query.filter(Matter.id == -1)
        if matter_filter:
            invoice_query = invoice_query.filter(Invoice.matter_id == matter_filter)
        if invoice_filter:
            invoice_query = invoice_query.filter(Invoice.id == invoice_filter)
        if start_date:
            invoice_query = invoice_query.filter(Invoice.created_at >= dt.datetime.combine(start_date, dt.time.min))
        if end_date:
            invoice_query = invoice_query.filter(Invoice.created_at <= dt.datetime.combine(end_date, dt.time.max))

        invoices = invoice_query.order_by(Invoice.created_at.desc(), Invoice.id.desc()).limit(800).all()
        invoice_ids = [inv.id for inv in invoices]
        invoice_by_id = {inv.id: inv for inv in invoices}
        matter_ids = sorted({inv.matter_id for inv in invoices})
        matters = matter_query.order_by(Matter.opened_at.desc()).limit(250).all()
        matter_by_id = {m.id: m for m in matters if m.id in matter_ids}
        missing_matter_ids = [matter_id for matter_id in matter_ids if matter_id not in matter_by_id]
        if missing_matter_ids:
            for row in Matter.query.filter(Matter.id.in_(missing_matter_ids)).all():
                matter_by_id[row.id] = row

        rows: list[dict[str, object]] = []
        billed_total = 0.0
        adjustment_total = 0.0
        settled_total = 0.0
        pending_total = 0.0

        include_lines = txn_type_filter in {"all", "bill_line"}
        include_adjustments = txn_type_filter in {"all", "adjustment"}
        include_payments = txn_type_filter in {"all", "payment"}

        if invoice_ids:
            if include_lines:
                line_rows = (
                    InvoiceLine.query.filter(InvoiceLine.invoice_id.in_(invoice_ids))
                    .order_by(InvoiceLine.invoice_id.desc(), InvoiceLine.id.desc())
                    .all()
                )
                for line in line_rows:
                    inv = invoice_by_id.get(line.invoice_id)
                    if inv is None:
                        continue
                    matter = matter_by_id.get(inv.matter_id)
                    gross = round(float(line.amount or 0.0) + float(line.tax_amount or 0.0), 2)
                    billed_total += gross
                    rows.append(
                        {
                            "occurred_at": inv.created_at,
                            "transaction_type": "bill_line",
                            "transaction_id": f"line-{line.id}",
                            "invoice_id": inv.id,
                            "matter_id": inv.matter_id,
                            "matter_no": matter.matter_no if matter else str(inv.matter_id),
                            "client_name": inv.client_name,
                            "description": line.description,
                            "amount": gross,
                            "impact_amount": gross,
                            "status": inv.status,
                            "reference": line.task_code or line.activity_code or "-",
                        }
                    )

            if include_adjustments:
                adjustment_rows = (
                    InvoiceAdjustment.query.filter(InvoiceAdjustment.invoice_id.in_(invoice_ids))
                    .order_by(InvoiceAdjustment.created_at.desc(), InvoiceAdjustment.id.desc())
                    .all()
                )
                for adj in adjustment_rows:
                    inv = invoice_by_id.get(adj.invoice_id)
                    if inv is None:
                        continue
                    matter = matter_by_id.get(inv.matter_id)
                    amount = round(float(adj.amount or 0.0), 2)
                    adjustment_total += amount
                    rows.append(
                        {
                            "occurred_at": adj.created_at,
                            "transaction_type": "adjustment",
                            "transaction_id": f"adj-{adj.id}",
                            "invoice_id": inv.id,
                            "matter_id": inv.matter_id,
                            "matter_no": matter.matter_no if matter else str(inv.matter_id),
                            "client_name": inv.client_name,
                            "description": adj.reason,
                            "amount": amount,
                            "impact_amount": amount,
                            "status": adj.adjustment_type,
                            "reference": "-",
                        }
                    )

            payment_query = PaymentAllocation.query.filter(PaymentAllocation.invoice_id.in_(invoice_ids))
            if status_filter == "pending":
                payment_query = payment_query.filter(PaymentAllocation.status == "pending")
            elif status_filter == "failed":
                payment_query = payment_query.filter(PaymentAllocation.status == "failed")
            elif status_filter == "settled":
                payment_query = payment_query.filter(
                    or_(PaymentAllocation.status == "settled", PaymentAllocation.status.is_(None))
                )
            payment_rows = (
                payment_query.order_by(PaymentAllocation.allocated_at.desc(), PaymentAllocation.id.desc()).all()
                if include_payments
                else []
            )
            for pay in payment_rows:
                inv = invoice_by_id.get(pay.invoice_id)
                if inv is None:
                    continue
                matter = matter_by_id.get(inv.matter_id)
                status = (pay.status or "settled").strip().lower()
                amount = round(float(pay.amount or 0.0), 2)
                impact = -amount if status == "settled" else 0.0
                if status == "settled":
                    settled_total += amount
                elif status == "pending":
                    pending_total += amount
                rows.append(
                    {
                        "occurred_at": pay.settled_at or pay.allocated_at,
                        "transaction_type": "payment",
                        "transaction_id": f"pay-{pay.id}",
                        "invoice_id": inv.id,
                        "matter_id": inv.matter_id,
                        "matter_no": matter.matter_no if matter else str(inv.matter_id),
                        "client_name": inv.client_name,
                        "description": pay.processor_note or "Payment allocation",
                        "amount": amount,
                        "impact_amount": round(impact, 2),
                        "status": status,
                        "reference": pay.reference or pay.external_txn_id or "-",
                    }
                )

        rows.sort(key=lambda row: ((row["occurred_at"] or dt.datetime.min), str(row["transaction_id"])), reverse=True)
        if txn_type_filter != "all":
            rows = [row for row in rows if str(row.get("transaction_type") or "").strip().lower() == txn_type_filter]
        if status_filter != "all":
            rows = [row for row in rows if str(row.get("status") or "").strip().lower() == status_filter]

        payment_status_counts = {"pending": 0, "settled": 0, "failed": 0}
        if invoice_ids:
            payment_status_rows = (
                db.session.query(func.coalesce(PaymentAllocation.status, "settled").label("status"), func.count(PaymentAllocation.id))
                .filter(PaymentAllocation.invoice_id.in_(invoice_ids))
                .group_by(func.coalesce(PaymentAllocation.status, "settled"))
                .all()
            )
            for status, count in payment_status_rows:
                normalized = str(status or "").strip().lower()
                if normalized in payment_status_counts:
                    payment_status_counts[normalized] = int(count)
        pending_payment_rows = [
            row
            for row in rows
            if row.get("transaction_type") == "payment" and str(row.get("status") or "").strip().lower() == "pending"
        ][:10]

        total_rows = len(rows)
        total_pages = max(1, (total_rows + per_page - 1) // per_page)
        if page_number > total_pages:
            page_number = total_pages
        start_index = (page_number - 1) * per_page
        end_index = start_index + per_page
        rows_for_page = rows[start_index:end_index]

        summary = {
            "transaction_count": total_rows,
            "billed_total": round(billed_total, 2),
            "adjustment_total": round(adjustment_total, 2),
            "settled_collected_total": round(settled_total, 2),
            "pending_collected_total": round(pending_total, 2),
            "net_outstanding_position": round(billed_total + adjustment_total - settled_total, 2),
        }

        if fmt == "csv":
            audit(
                "billing_transactions_export",
                "Invoice",
                None,
                {
                    "matter_id": matter_filter,
                    "invoice_id": invoice_filter,
                    "txn_type": txn_type_filter,
                    "status": status_filter,
                    "start": start_raw or None,
                    "end": end_raw or None,
                    "row_count": total_rows,
                },
            )
            csv_rows = [
                [
                    row["occurred_at"],
                    row["transaction_type"],
                    row["transaction_id"],
                    row["invoice_id"],
                    row["matter_id"],
                    row["matter_no"],
                    row["client_name"],
                    row["description"],
                    row["amount"],
                    row["impact_amount"],
                    row["status"],
                    row["reference"],
                ]
                for row in rows
            ]
            return _csv_response(
                f"billing_transactions_{dt.date.today().isoformat()}.csv",
                [
                    "occurred_at",
                    "transaction_type",
                    "transaction_id",
                    "invoice_id",
                    "matter_id",
                    "matter_no",
                    "client_name",
                    "description",
                    "amount",
                    "impact_amount",
                    "status",
                    "reference",
                ],
                csv_rows,
            )

        audit(
            "billing_transactions_view",
            "Invoice",
            None,
            {
                "matter_id": matter_filter,
                "invoice_id": invoice_filter,
                "txn_type": txn_type_filter,
                "status": status_filter,
                "start": start_raw or None,
                "end": end_raw or None,
                "row_count": total_rows,
            },
        )
        return page(
            "Per-Transaction Billing",
            "billing/transactions.html",
            rows=rows_for_page,
            summary=summary,
            matters=matters,
            selected_matter_id=matter_filter,
            selected_invoice_id=invoice_filter,
            selected_txn_type=txn_type_filter,
            selected_status=status_filter,
            start=start_raw,
            end=end_raw,
            payment_status_counts=payment_status_counts,
            pending_payment_rows=pending_payment_rows,
            page_number=page_number,
            total_pages=total_pages,
            has_prev=(page_number > 1),
            has_next=(page_number < total_pages),
            prev_page=(page_number - 1),
            next_page=(page_number + 1),
            per_page=per_page,
        )

    @app.get("/billing/audit-log")
    @login_required
    def billing_audit_log():
        enforce_permission("billing", "audit")

        action_filter = (request.args.get("action") or "").strip()
        actor_filter = request.args.get("actor_id", type=int)
        fmt = (request.args.get("format") or "html").strip().lower()

        query = AuditLog.query.filter(
            or_(
                AuditLog.action.like("invoice_%"),
                AuditLog.action.like("billing_%"),
                AuditLog.action.like("payment_%"),
                AuditLog.action.like("tax_invoice_%"),
                AuditLog.action.like("account_statement_%"),
            )
        )
        if not is_admin():
            scope_ids = visible_matter_ids()
            query = query.filter(_billing_audit_scope_filter(scope_ids))
        if action_filter:
            query = query.filter(AuditLog.action == action_filter)
        actor_option_ids = [
            int(row[0])
            for row in query.with_entities(AuditLog.actor_user_id)
            .filter(AuditLog.actor_user_id.is_not(None))
            .distinct()
            .order_by(AuditLog.actor_user_id.asc())
            .limit(500)
            .all()
            if row[0] is not None
        ]
        actor_options = []
        if actor_option_ids:
            actor_options = User.query.filter(User.id.in_(actor_option_ids)).order_by(User.full_name.asc(), User.email.asc()).all()
        if actor_filter:
            query = query.filter(AuditLog.actor_user_id == actor_filter)
        logs = query.order_by(AuditLog.at.desc()).limit(1000).all()
        actor_ids = sorted({log.actor_user_id for log in logs if log.actor_user_id} | set(actor_option_ids))
        users_by_id = {u.id: u for u in User.query.filter(User.id.in_(actor_ids)).all()} if actor_ids else {}

        if fmt == "csv":
            csv_rows = [
                [
                    log.id,
                    log.at,
                    log.actor_user_id,
                    users_by_id[log.actor_user_id].email if log.actor_user_id in users_by_id else "-",
                    log.action,
                    log.entity_type,
                    log.entity_id,
                    (log.details_json or "")[:5000],
                    log.ip,
                ]
                for log in logs
            ]
            return _csv_response(
                f"billing_audit_log_{dt.date.today().isoformat()}.csv",
                ["id", "created_at", "actor_user_id", "actor_email", "action", "entity_type", "entity_id", "details_json", "ip"],
                csv_rows,
            )

        actions = sorted({log.action for log in logs})
        return page(
            "Billing Audit Log",
            "billing/audit_log.html",
            logs=logs,
            users_by_id=users_by_id,
            actions=actions,
            actor_options=actor_options,
            selected_action=action_filter,
            selected_actor_id=actor_filter,
        )
