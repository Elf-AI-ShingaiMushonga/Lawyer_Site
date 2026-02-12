from __future__ import annotations

import datetime as dt
import io
import os

from flask import Response, abort, flash, redirect, request, send_file, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from ..extensions import db
from ..helpers import audit, can_access_matter, is_admin
from ..models import ARSnapshot, FeeArrangement, Invoice, InvoiceAdjustment, InvoiceLine, LEDESExport, Matter, PaymentAllocation, RateCard
from ..policies import enforce_data_residency, visible_matter_ids
from ..reports.ledes import generate_ledes_1998b
from ..services.billing_engine import BillingEngine
from ..services.notification_engine import NotificationEngine
from ..templates import page


def _build_invoice_pdf(invoice: Invoice, lines: list[InvoiceLine]) -> bytes:
    rows = [
        f"Invoice #{invoice.id}",
        f"Client: {invoice.client_name}",
        f"Matter: {invoice.matter_id}",
        f"Period: {invoice.period_start} to {invoice.period_end}",
        f"Status: {invoice.status}",
        "",
        "Lines:",
    ]
    for idx, line in enumerate(lines, start=1):
        rows.append(
            f"{idx}. {line.description} | hours={line.hours:.2f} rate={line.rate:.2f} amount={line.amount:.2f}"
        )
    rows += ["", f"Subtotal: {invoice.subtotal:.2f}", f"Tax: {invoice.tax_total:.2f}", f"Total: {invoice.total:.2f}"]

    content = "\n".join(rows)
    escaped = content.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 10 Tf 50 760 Td 12 TL ({escaped.replace(chr(10), ') Tj T* (')}) Tj ET".encode("utf-8")

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
        return page("Billing Rates", "billing/rates.html", rates=rates, arrangements=arrangements, matters=matters)

    @app.route("/billing/invoices", methods=["GET", "POST"])
    @login_required
    def billing_invoices():
        if request.method == "POST":
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
            if result.invoice_id is None:
                flash("No approved time/expenses for that period.", "warning")
            else:
                audit("invoice_generate", "Invoice", result.invoice_id, {"line_count": result.line_count})
                NotificationEngine.enqueue("invoice_created", current_user.id, f"invoice:{result.invoice_id}")
                flash(f"Invoice {result.invoice_id} generated.", "info")
            return redirect(url_for("billing_invoices"))

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
        rows = invoice_query.order_by(Invoice.created_at.desc()).limit(300).all()
        matters = matter_query.order_by(Matter.opened_at.desc()).limit(200).all()
        return page("Invoices", "billing/invoices.html", invoices=rows, matters=matters)

    @app.get("/billing/invoices/<int:invoice_id>")
    @login_required
    def billing_invoice_detail(invoice_id: int):
        inv = db.session.get(Invoice, invoice_id)
        if not inv:
            abort(404)
        if not can_access_matter(inv.matter_id):
            abort(403)

        lines = InvoiceLine.query.filter_by(invoice_id=invoice_id).order_by(InvoiceLine.id.asc()).all()
        adjustments = InvoiceAdjustment.query.filter_by(invoice_id=invoice_id).order_by(InvoiceAdjustment.created_at.desc()).all()
        payments = PaymentAllocation.query.filter_by(invoice_id=invoice_id).order_by(PaymentAllocation.allocated_at.desc()).all()
        outstanding = round(float(inv.total or 0.0) - sum(float(p.amount or 0.0) for p in payments), 2)
        return page(
            "Invoice Detail",
            "billing/invoice_detail.html",
            inv=inv,
            lines=lines,
            adjustments=adjustments,
            payments=payments,
            outstanding=outstanding,
        )

    @app.post("/billing/invoices/<int:invoice_id>/approve")
    @login_required
    def billing_invoice_approve(invoice_id: int):
        inv = db.session.get(Invoice, invoice_id)
        if not inv:
            abort(404)
        if not can_access_matter(inv.matter_id):
            abort(403)
        if current_user.role not in {"admin", "lawyer"}:
            abort(403)

        inv.status = "approved"
        inv.approved_by = current_user.id
        inv.approved_at = dt.datetime.utcnow()
        db.session.commit()
        NotificationEngine.enqueue("invoice_approved", current_user.id, f"invoice:{inv.id}")
        audit("invoice_approve", "Invoice", inv.id)
        flash("Invoice approved.", "info")
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
        pdf_bytes = _build_invoice_pdf(inv, lines)
        invoice_dir = os.path.join(app.config["UPLOAD_DIR"], "invoices")
        os.makedirs(invoice_dir, exist_ok=True)
        filename = f"invoice_{invoice_id}.pdf"
        path = os.path.join(invoice_dir, filename)
        with open(path, "wb") as f:
            f.write(pdf_bytes)
        inv.pdf_path = path
        db.session.commit()
        audit("invoice_pdf_generate", "Invoice", inv.id)

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
        inv = db.session.get(Invoice, invoice_id)
        if not inv:
            abort(404)
        if not can_access_matter(inv.matter_id):
            abort(403)
        if current_user.role not in {"admin", "lawyer"}:
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
        if current_user.role not in {"admin", "lawyer"}:
            abort(403)

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

        rows = []
        for inv in invoices:
            paid = float(
                db.session.query(func.coalesce(func.sum(PaymentAllocation.amount), 0.0))
                .filter(PaymentAllocation.invoice_id == inv.id, PaymentAllocation.allocated_at <= dt.datetime.combine(as_of, dt.time.max))
                .scalar()
                or 0.0
            )
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

            snapshot = ARSnapshot.query.filter_by(as_of_date=as_of, invoice_id=inv.id).first()
            if snapshot is None:
                snapshot = ARSnapshot(as_of_date=as_of, invoice_id=inv.id)
                db.session.add(snapshot)
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
