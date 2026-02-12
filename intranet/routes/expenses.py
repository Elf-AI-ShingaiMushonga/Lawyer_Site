from __future__ import annotations

import datetime as dt
import os
import uuid

from flask import abort, flash, redirect, request, send_from_directory, url_for
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
