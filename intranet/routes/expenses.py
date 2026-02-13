from __future__ import annotations

import datetime as dt
import io
import os
import uuid

from flask import abort, flash, redirect, request, send_file, send_from_directory, url_for
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from ..extensions import db
from ..helpers import allowed_doc, audit, can_access_matter, sha256_file
from ..models import ExpenseEntry, Matter
from ..templates import page


def _extract_receipt_text(path: str) -> str | None:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".txt":
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()[:5000]
        except Exception:
            return None
    return None


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


def register_expenses_routes(app):
    @app.route("/expenses", methods=["GET", "POST"])
    @login_required
    def expenses():
        if request.method == "POST":
            matter_id = request.form.get("matter_id", type=int)
            if not matter_id or not can_access_matter(matter_id):
                abort(403)

            amount = request.form.get("amount", type=float)
            incurred_raw = (request.form.get("incurred_on") or "").strip()
            if amount is None:
                flash("Amount required.", "warning")
                return redirect(url_for("expenses"))
            try:
                incurred_on = dt.date.fromisoformat(incurred_raw)
            except ValueError:
                flash("Invalid incurred date.", "warning")
                return redirect(url_for("expenses"))

            receipt_filename = None
            receipt_sha = None
            receipt_ocr = None
            file_obj = request.files.get("receipt")
            if file_obj and file_obj.filename:
                if not allowed_doc(file_obj.filename):
                    flash("Unsupported receipt type.", "warning")
                    return redirect(url_for("expenses"))
                safe = secure_filename(file_obj.filename)
                stored = f"expense_{matter_id}_{uuid.uuid4().hex}_{safe}"
                path = os.path.join(app.config["UPLOAD_DIR"], stored)
                file_obj.save(path)
                receipt_filename = stored
                receipt_sha = sha256_file(path)
                receipt_ocr = _extract_receipt_text(path)

            row = ExpenseEntry(
                matter_id=matter_id,
                user_id=current_user.id,
                amount=amount,
                currency=(request.form.get("currency") or "ZAR").strip().upper(),
                category=(request.form.get("category") or "General").strip() or "General",
                description=(request.form.get("description") or "").strip() or None,
                incurred_on=incurred_on,
                is_reimbursable=(request.form.get("is_reimbursable") or "").lower() in {"1", "true", "yes", "on"},
                status="submitted",
                receipt_filename=receipt_filename,
                receipt_sha256=receipt_sha,
                receipt_ocr_text=receipt_ocr,
            )
            db.session.add(row)
            db.session.commit()
            audit("expense_create", "ExpenseEntry", row.id, {"matter_id": matter_id, "amount": amount})
            flash("Expense submitted.", "info")
            return redirect(url_for("expenses"))

        rows = ExpenseEntry.query.order_by(ExpenseEntry.created_at.desc()).limit(300).all()
        rows = [r for r in rows if can_access_matter(r.matter_id)]
        matters = Matter.query.order_by(Matter.opened_at.desc()).limit(200).all()
        return page("Expenses", "expenses/list.html", expenses=rows, matters=matters)

    @app.post("/expenses/<int:expense_id>/approve")
    @login_required
    def expense_approve(expense_id: int):
        row = db.session.get(ExpenseEntry, expense_id)
        if not row:
            abort(404)
        if not can_access_matter(row.matter_id):
            abort(403)
        if current_user.role not in {"admin", "lawyer"}:
            abort(403)

        row.status = "approved"
        row.approved_by = current_user.id
        row.approved_at = dt.datetime.utcnow()
        db.session.commit()
        audit("expense_approve", "ExpenseEntry", row.id)
        flash("Expense approved.", "info")
        return redirect(url_for("expenses"))

    @app.get("/expenses/<int:expense_id>/receipt")
    @login_required
    def expense_receipt(expense_id: int):
        row = db.session.get(ExpenseEntry, expense_id)
        if not row:
            abort(404)
        if not can_access_matter(row.matter_id):
            abort(403)
        if not row.receipt_filename:
            abort(404)

        path = os.path.join(app.config["UPLOAD_DIR"], row.receipt_filename)
        if not os.path.isfile(path):
            abort(404)
        audit("expense_receipt_download", "ExpenseEntry", row.id)
        return send_from_directory(app.config["UPLOAD_DIR"], row.receipt_filename, as_attachment=True)

    @app.get("/expenses/<int:expense_id>/receipt/pdf")
    @login_required
    def expense_receipt_pdf(expense_id: int):
        row = db.session.get(ExpenseEntry, expense_id)
        if not row:
            abort(404)
        if not can_access_matter(row.matter_id):
            abort(403)

        matter = db.session.get(Matter, row.matter_id)
        text_lines = [
            "Expense Receipt Summary",
            f"Expense ID: {row.id}",
            f"Matter: {(matter.matter_no + ' - ' + matter.title) if matter else row.matter_id}",
            f"Amount: {float(row.amount or 0.0):.2f} {row.currency}",
            f"Category: {row.category}",
            f"Incurred On: {row.incurred_on}",
            f"Status: {row.status}",
            f"Description: {(row.description or '').strip() or '-'}",
            f"Receipt File: {row.receipt_filename or 'none'}",
            f"Receipt SHA256: {row.receipt_sha256 or '-'}",
        ]
        if row.receipt_ocr_text:
            text_lines.append("OCR Snippet:")
            text_lines.append((row.receipt_ocr_text or "")[:300].replace("\n", " "))

        payload = _build_lines_pdf(text_lines)
        buffer = io.BytesIO(payload)
        buffer.seek(0)
        audit("expense_receipt_pdf_generate", "ExpenseEntry", row.id)
        return send_file(
            buffer,
            as_attachment=True,
            download_name=f"expense_receipt_{row.id}.pdf",
            mimetype="application/pdf",
        )
