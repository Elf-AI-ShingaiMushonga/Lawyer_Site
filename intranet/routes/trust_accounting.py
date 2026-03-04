from __future__ import annotations

import csv
import datetime as dt
import io
import json
from typing import Any

from flask import Response, abort, flash, redirect, request, session, url_for
from flask_login import current_user, login_required
from sqlalchemy import func
from werkzeug.utils import secure_filename

from ..extensions import db
from ..helpers import audit
from ..models import (
    Section86Accrual,
    Section86Investment,
    TrustAccount,
    TrustApprovalRequest,
    TrustBankStatementImport,
    TrustBankStatementLine,
    TrustClientLedger,
    TrustLedgerEntry,
    TrustReconciliationRun,
    TrustThresholdAlert,
)
from ..reports.trust import (
    generate_trust_auditor_summary,
    generate_trust_reconciliation_report,
    generate_trust_trial_balance,
)
from ..roles import role_is_lawyer
from ..services.trust_engine import TrustEngine
from ..templates import page


def _trust_admin_required() -> None:
    if not role_is_lawyer(getattr(current_user, "role", None)):
        abort(403)


def _create_threshold_alerts(ledger: TrustClientLedger) -> None:
    threshold = 5000.0
    if float(ledger.current_balance or 0) <= threshold:
        if not TrustThresholdAlert.query.filter_by(client_ledger_id=ledger.id, status="open").first():
            db.session.add(
                TrustThresholdAlert(
                    client_ledger_id=ledger.id,
                    threshold_amount=threshold,
                    current_balance=float(ledger.current_balance or 0),
                    status="open",
                )
            )


MAKER_CHECKER_THRESHOLD = 10000.0


def _maker_checker_required(action_type: str, amount: float) -> bool:
    return action_type in {"disbursement", "transfer"} and float(amount or 0.0) >= MAKER_CHECKER_THRESHOLD


def _queue_trust_approval(action_type: str, payload: dict) -> TrustApprovalRequest:
    row = TrustApprovalRequest(
        action_type=action_type,
        payload_json=json.dumps(payload, sort_keys=True),
        status="pending",
        requested_by=current_user.id,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _execute_transfer_payload(payload: dict) -> tuple[bool, str, int | None]:
    trust_account_id = payload.get("trust_account_id")
    source_ledger_id = payload.get("source_ledger_id")
    target_ledger_id = payload.get("target_ledger_id")
    amount = payload.get("amount")
    currency = payload.get("currency") or "ZAR"
    created_by = int(payload.get("created_by") or current_user.id)

    out_result = TrustEngine.post_transaction(
        {
            "trust_account_id": trust_account_id,
            "client_ledger_id": source_ledger_id,
            "entry_type": "transfer",
            "amount": amount,
            "currency": currency,
            "description": f"Transfer out to ledger {target_ledger_id}",
            "created_by": created_by,
        }
    )
    if not out_result.posted:
        return False, out_result.message, None

    in_result = TrustEngine.post_transaction(
        {
            "trust_account_id": trust_account_id,
            "client_ledger_id": target_ledger_id,
            "entry_type": "deposit",
            "amount": amount,
            "currency": currency,
            "description": f"Transfer in from ledger {source_ledger_id}",
            "created_by": created_by,
        }
    )
    if not in_result.posted:
        return False, "Transfer-out posted but transfer-in failed. Reverse required.", out_result.entry_id

    for ledger_id in [source_ledger_id, target_ledger_id]:
        ledger = db.session.get(TrustClientLedger, ledger_id)
        if ledger:
            _create_threshold_alerts(ledger)
    db.session.commit()
    return True, "Transfer posted.", out_result.entry_id


def _signed_trust_entry_amount(entry: TrustLedgerEntry, cache: dict[int, TrustLedgerEntry]) -> float:
    amount = float(entry.amount or 0.0)
    entry_type = (entry.entry_type or "").strip().lower()
    if entry_type == "deposit":
        return amount
    if entry_type in {"disbursement", "transfer"}:
        return -amount
    if entry_type == "reversal":
        original = cache.get(int(entry.reversal_of_entry_id or 0))
        if original is None and entry.reversal_of_entry_id:
            original = db.session.get(TrustLedgerEntry, int(entry.reversal_of_entry_id))
        if original and (original.entry_type or "").strip().lower() == "deposit":
            return -amount
        return amount
    return 0.0


def _parse_any_date(raw: str | None) -> dt.date | None:
    value = (raw or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    try:
        return dt.datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _parse_amount(raw: str | None) -> float | None:
    value = (raw or "").strip()
    if not value:
        return None
    normalized = value.replace(",", "")
    if normalized.startswith("(") and normalized.endswith(")"):
        normalized = f"-{normalized[1:-1]}"
    try:
        return float(normalized)
    except ValueError:
        return None


def _first_present(row: dict[str, str], keys: list[str]) -> str | None:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _build_csv_response(filename: str, headers: list[str], rows: list[list[Any]]) -> Response:
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


def _parse_statement_csv_rows(file_bytes: bytes) -> tuple[list[dict[str, str]], str | None]:
    try:
        decoded = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            decoded = file_bytes.decode("latin-1")
        except UnicodeDecodeError:
            return [], "Unable to decode bank statement file."
    reader = csv.DictReader(io.StringIO(decoded))
    if not reader.fieldnames:
        return [], "Bank statement CSV is missing headers."
    rows: list[dict[str, str]] = []
    for row in reader:
        parsed = {str(k).strip().lower(): (str(v).strip() if v is not None else "") for k, v in row.items() if k}
        if any(parsed.values()):
            rows.append(parsed)
    if not rows:
        return [], "Bank statement CSV has no data rows."
    return rows, None


def _statement_line_from_row(row: dict[str, str]) -> tuple[dt.date, str | None, str | None, float, float, float, float | None] | None:
    posted_raw = _first_present(row, ["posted_on", "date", "transaction_date", "value_date"])
    posted_on = _parse_any_date(posted_raw)
    if posted_on is None:
        return None

    description = _first_present(row, ["description", "memo", "narrative", "details"])
    reference = _first_present(row, ["reference", "ref", "transaction_ref", "transaction_id"])

    debit = _parse_amount(_first_present(row, ["debit", "withdrawal", "outflow"])) or 0.0
    credit = _parse_amount(_first_present(row, ["credit", "deposit", "inflow"])) or 0.0
    signed_amount = _parse_amount(_first_present(row, ["signed_amount", "amount", "transaction_amount"]))
    if signed_amount is None:
        signed_amount = round(credit - debit, 2)
    if credit == 0.0 and debit == 0.0:
        if signed_amount >= 0:
            credit = abs(signed_amount)
        else:
            debit = abs(signed_amount)
    running_balance = _parse_amount(_first_present(row, ["running_balance", "balance", "closing_balance"]))
    return posted_on, description, reference, round(debit, 2), round(credit, 2), round(signed_amount, 2), running_balance


def _accrue_section86(as_of_date: dt.date, withholding_percent: float, post_to_ledger: bool) -> tuple[int, int, float, int]:
    investments = (
        Section86Investment.query.filter(Section86Investment.opened_on <= as_of_date)
        .order_by(Section86Investment.id.asc())
        .all()
    )
    created = 0
    posted = 0
    skipped = 0
    net_total = 0.0

    for inv in investments:
        if (inv.status or "").lower() not in {"active", "matured"}:
            skipped += 1
            continue
        if inv.closed_on and inv.closed_on < as_of_date:
            skipped += 1
            continue
        if inv.maturity_on and inv.maturity_on < as_of_date:
            skipped += 1
            continue
        existing = Section86Accrual.query.filter_by(investment_id=inv.id, accrual_date=as_of_date).first()
        if existing:
            skipped += 1
            continue

        interest = round(float(inv.principal_amount or 0.0) * (float(inv.annual_rate_percent or 0.0) / 100.0) / 365.0, 2)
        withholding_tax = round(max(0.0, interest * (max(0.0, withholding_percent) / 100.0)), 2)
        net_interest = round(interest - withholding_tax, 2)
        posted_entry_id = None
        if post_to_ledger and net_interest > 0:
            result = TrustEngine.post_transaction(
                {
                    "trust_account_id": inv.trust_account_id,
                    "client_ledger_id": inv.client_ledger_id,
                    "entry_type": "deposit",
                    "amount": net_interest,
                    "currency": "ZAR",
                    "description": f"Section 86 interest accrual {as_of_date.isoformat()} [{inv.investment_ref}]",
                    "created_by": current_user.id,
                }
            )
            if result.posted:
                posted += 1
                posted_entry_id = result.entry_id

        row = Section86Accrual(
            investment_id=inv.id,
            accrual_date=as_of_date,
            interest_amount=interest,
            withholding_tax_amount=withholding_tax,
            net_interest_amount=net_interest,
            posted_entry_id=posted_entry_id,
            created_by=current_user.id,
        )
        db.session.add(row)
        db.session.commit()
        created += 1
        net_total += net_interest
    return created, posted, round(net_total, 2), skipped


def register_trust_accounting_routes(app):
    @app.get("/trust/ledger")
    @login_required
    def trust_ledger():
        _trust_admin_required()
        accounts = TrustAccount.query.order_by(TrustAccount.id.desc()).all()
        ledgers = TrustClientLedger.query.order_by(TrustClientLedger.id.desc()).all()
        entries = TrustLedgerEntry.query.order_by(TrustLedgerEntry.created_at.desc()).limit(200).all()
        alerts = TrustThresholdAlert.query.filter_by(status="open").order_by(TrustThresholdAlert.created_at.desc()).all()
        approvals = (
            TrustApprovalRequest.query.filter(TrustApprovalRequest.status.in_(["pending", "approved"]))
            .order_by(TrustApprovalRequest.requested_at.desc())
            .limit(200)
            .all()
        )
        return page(
            "Trust Ledger",
            "trust_accounting/ledger.html",
            accounts=accounts,
            ledgers=ledgers,
            entries=entries,
            alerts=alerts,
            approvals=approvals,
            maker_checker_threshold=MAKER_CHECKER_THRESHOLD,
        )

    @app.post("/trust/deposits")
    @login_required
    def trust_deposit():
        _trust_admin_required()
        result = TrustEngine.post_transaction(
            {
                "trust_account_id": request.form.get("trust_account_id", type=int),
                "client_ledger_id": request.form.get("client_ledger_id", type=int),
                "entry_type": "deposit",
                "amount": request.form.get("amount", type=float),
                "currency": (request.form.get("currency") or "ZAR").strip().upper(),
                "description": (request.form.get("description") or "").strip() or None,
                "created_by": current_user.id,
            }
        )
        if not result.posted:
            flash(result.message, "warning")
            return redirect(url_for("trust_ledger"))

        ledger = db.session.get(TrustClientLedger, request.form.get("client_ledger_id", type=int))
        if ledger:
            _create_threshold_alerts(ledger)
            db.session.commit()
        audit("trust_deposit", "TrustLedgerEntry", result.entry_id)
        flash("Deposit posted.", "info")
        return redirect(url_for("trust_ledger"))

    @app.post("/trust/disbursements")
    @login_required
    def trust_disbursement():
        _trust_admin_required()
        trust_account_id = request.form.get("trust_account_id", type=int)
        client_ledger_id = request.form.get("client_ledger_id", type=int)
        amount = request.form.get("amount", type=float)
        currency = (request.form.get("currency") or "ZAR").strip().upper()
        description = (request.form.get("description") or "").strip() or None
        if not trust_account_id or not client_ledger_id or amount is None or amount <= 0:
            flash("Trust account, ledger, and positive amount are required.", "warning")
            return redirect(url_for("trust_ledger"))

        if _maker_checker_required("disbursement", amount):
            approval = _queue_trust_approval(
                "disbursement",
                {
                    "trust_account_id": trust_account_id,
                    "client_ledger_id": client_ledger_id,
                    "amount": amount,
                    "currency": currency,
                    "description": description,
                    "created_by": current_user.id,
                },
            )
            audit("trust_disbursement_approval_requested", "TrustApprovalRequest", approval.id)
            flash(
                f"Disbursement queued for maker-checker approval (threshold {MAKER_CHECKER_THRESHOLD:.2f}).",
                "warning",
            )
            return redirect(url_for("trust_ledger"))

        result = TrustEngine.post_transaction(
            {
                "trust_account_id": trust_account_id,
                "client_ledger_id": client_ledger_id,
                "entry_type": "disbursement",
                "amount": amount,
                "currency": currency,
                "description": description,
                "created_by": current_user.id,
            }
        )
        if not result.posted:
            flash(result.message, "warning")
            return redirect(url_for("trust_ledger"))

        ledger = db.session.get(TrustClientLedger, client_ledger_id)
        if ledger:
            _create_threshold_alerts(ledger)
            db.session.commit()
        audit("trust_disbursement", "TrustLedgerEntry", result.entry_id)
        flash("Disbursement posted.", "info")
        return redirect(url_for("trust_ledger"))

    @app.post("/trust/transfers")
    @login_required
    def trust_transfer():
        _trust_admin_required()
        source_ledger_id = request.form.get("source_ledger_id", type=int)
        target_ledger_id = request.form.get("target_ledger_id", type=int)
        amount = request.form.get("amount", type=float)
        trust_account_id = request.form.get("trust_account_id", type=int)

        if not source_ledger_id or not target_ledger_id or not amount:
            flash("Source, target, and amount are required.", "warning")
            return redirect(url_for("trust_ledger"))
        currency = (request.form.get("currency") or "ZAR").strip().upper()
        payload = {
            "trust_account_id": trust_account_id,
            "source_ledger_id": source_ledger_id,
            "target_ledger_id": target_ledger_id,
            "amount": amount,
            "currency": currency,
            "created_by": current_user.id,
        }
        if _maker_checker_required("transfer", amount):
            approval = _queue_trust_approval("transfer", payload)
            audit("trust_transfer_approval_requested", "TrustApprovalRequest", approval.id)
            flash(
                f"Transfer queued for maker-checker approval (threshold {MAKER_CHECKER_THRESHOLD:.2f}).",
                "warning",
            )
            return redirect(url_for("trust_ledger"))

        ok, message, source_entry_id = _execute_transfer_payload(payload)
        if not ok:
            flash(message, "warning")
            return redirect(url_for("trust_ledger"))

        audit("trust_transfer", "TrustLedgerEntry", source_entry_id)
        flash(message, "info")
        return redirect(url_for("trust_ledger"))

    @app.post("/trust/approvals/<int:approval_id>/decision")
    @login_required
    def trust_approval_decision(approval_id: int):
        _trust_admin_required()
        row = db.session.get(TrustApprovalRequest, approval_id)
        if row is None:
            abort(404)
        actor_user_id = int(session.get("_user_id") or getattr(current_user, "id", 0) or 0)

        decision = (request.form.get("decision") or "approve").strip().lower()
        if decision not in {"approve", "reject"}:
            flash("Invalid approval decision.", "warning")
            return redirect(url_for("trust_ledger"))
        if row.status not in {"pending", "approved"}:
            flash("Approval request is already finalized.", "warning")
            return redirect(url_for("trust_ledger"))
        if row.requested_by == actor_user_id:
            flash("Maker-checker requires a different approver.", "warning")
            return redirect(url_for("trust_ledger"))

        row.approved_by = actor_user_id or current_user.id
        row.approved_at = dt.datetime.utcnow()
        note = (request.form.get("note") or "").strip() or None

        if decision == "reject":
            row.status = "rejected"
            row.notes = note
            db.session.commit()
            audit("trust_approval_rejected", "TrustApprovalRequest", row.id)
            flash("Trust action rejected.", "info")
            return redirect(url_for("trust_ledger"))

        try:
            payload = json.loads(row.payload_json or "{}")
        except json.JSONDecodeError:
            payload = {}

        payload["created_by"] = int(payload.get("created_by") or row.requested_by)
        action_type = (row.action_type or "").strip().lower()
        if action_type == "disbursement":
            result = TrustEngine.post_transaction(
                {
                    "trust_account_id": payload.get("trust_account_id"),
                    "client_ledger_id": payload.get("client_ledger_id"),
                    "entry_type": "disbursement",
                    "amount": payload.get("amount"),
                    "currency": payload.get("currency") or "ZAR",
                    "description": payload.get("description"),
                    "created_by": payload["created_by"],
                }
            )
            if not result.posted:
                row.status = "approved"
                row.notes = f"Approved but execution failed: {result.message}"
                db.session.commit()
                flash(result.message, "warning")
                return redirect(url_for("trust_ledger"))
            ledger = db.session.get(TrustClientLedger, payload.get("client_ledger_id"))
            if ledger:
                _create_threshold_alerts(ledger)
            db.session.commit()
            row.status = "executed"
            row.executed_at = dt.datetime.utcnow()
            row.executed_entry_id = result.entry_id
            row.notes = note
            db.session.commit()
            audit("trust_approval_executed", "TrustApprovalRequest", row.id, {"entry_id": result.entry_id})
            flash("Approved and executed.", "info")
            return redirect(url_for("trust_ledger"))

        if action_type == "transfer":
            ok, message, source_entry_id = _execute_transfer_payload(payload)
            if not ok:
                row.status = "approved"
                row.notes = f"Approved but execution failed: {message}"
                db.session.commit()
                flash(message, "warning")
                return redirect(url_for("trust_ledger"))
            row.status = "executed"
            row.executed_at = dt.datetime.utcnow()
            row.executed_entry_id = source_entry_id
            row.notes = note
            db.session.commit()
            audit("trust_approval_executed", "TrustApprovalRequest", row.id, {"entry_id": source_entry_id})
            flash("Approved and executed.", "info")
            return redirect(url_for("trust_ledger"))

        row.status = "rejected"
        row.notes = f"Unsupported action_type: {action_type}"
        db.session.commit()
        flash("Unsupported trust approval action.", "warning")
        return redirect(url_for("trust_ledger"))

    @app.post("/trust/statements/import")
    @login_required
    def trust_statement_import():
        _trust_admin_required()
        account_id = request.form.get("trust_account_id", type=int)
        if not account_id:
            flash("Trust account is required for bank statement import.", "warning")
            return redirect(url_for("trust_reconciliations"))
        if db.session.get(TrustAccount, account_id) is None:
            flash("Selected trust account does not exist.", "warning")
            return redirect(url_for("trust_reconciliations"))

        file_obj = request.files.get("statement_file")
        if file_obj is None or not file_obj.filename:
            flash("Bank statement CSV file is required.", "warning")
            return redirect(url_for("trust_reconciliations"))
        if not file_obj.filename.lower().endswith(".csv"):
            flash("Only CSV bank statements are supported.", "warning")
            return redirect(url_for("trust_reconciliations"))

        file_bytes = file_obj.read()
        parsed_rows, error = _parse_statement_csv_rows(file_bytes)
        if error:
            flash(error, "warning")
            return redirect(url_for("trust_reconciliations"))

        lines: list[TrustBankStatementLine] = []
        parsed_dates: list[dt.date] = []
        running_balances: list[float] = []
        for row in parsed_rows:
            parsed = _statement_line_from_row(row)
            if parsed is None:
                continue
            posted_on, description, reference, debit, credit, signed_amount, running_balance = parsed
            parsed_dates.append(posted_on)
            if running_balance is not None:
                running_balances.append(float(running_balance))
            lines.append(
                TrustBankStatementLine(
                    import_id=0,
                    posted_on=posted_on,
                    description=description,
                    reference=reference,
                    debit=debit,
                    credit=credit,
                    signed_amount=signed_amount,
                    running_balance=running_balance,
                    raw_json=json.dumps(row, sort_keys=True),
                )
            )
        if not lines:
            flash("No usable rows found in the statement CSV.", "warning")
            return redirect(url_for("trust_reconciliations"))

        label = (request.form.get("statement_label") or "").strip() or None
        notes = (request.form.get("notes") or "").strip() or None
        safe_name = secure_filename(file_obj.filename or "statement.csv")
        import_row = TrustBankStatementImport(
            trust_account_id=account_id,
            statement_label=label,
            source_filename=safe_name,
            period_start=min(parsed_dates) if parsed_dates else None,
            period_end=max(parsed_dates) if parsed_dates else None,
            opening_balance=running_balances[0] if running_balances else None,
            closing_balance=running_balances[-1] if running_balances else None,
            currency=(request.form.get("currency") or "ZAR").strip().upper(),
            row_count=len(lines),
            imported_by=current_user.id,
            notes=notes,
        )
        db.session.add(import_row)
        db.session.flush()
        for line in lines:
            line.import_id = import_row.id
            db.session.add(line)
        db.session.commit()
        audit("trust_bank_statement_import", "TrustBankStatementImport", import_row.id, {"rows": len(lines)})
        flash(f"Bank statement imported with {len(lines)} rows.", "info")
        return redirect(url_for("trust_reconciliations"))

    @app.route("/trust/reconciliations", methods=["GET", "POST"])
    @login_required
    def trust_reconciliations():
        _trust_admin_required()
        if request.method == "POST":
            account_id = request.form.get("trust_account_id", type=int)
            statement_import_id = request.form.get("bank_statement_import_id", type=int)
            statement_import = None
            if statement_import_id:
                statement_import = db.session.get(TrustBankStatementImport, statement_import_id)
                if statement_import is None:
                    flash("Selected bank statement import was not found.", "warning")
                    return redirect(url_for("trust_reconciliations"))
                if account_id and account_id != statement_import.trust_account_id:
                    flash("Bank statement import does not belong to selected trust account.", "warning")
                    return redirect(url_for("trust_reconciliations"))
                account_id = statement_import.trust_account_id
            if not account_id:
                flash("Trust account required.", "warning")
                return redirect(url_for("trust_reconciliations"))

            try:
                start = dt.datetime.fromisoformat((request.form.get("period_start") or "").strip())
                end = dt.datetime.fromisoformat((request.form.get("period_end") or "").strip())
            except ValueError:
                flash("Invalid reconciliation period datetimes.", "warning")
                return redirect(url_for("trust_reconciliations"))

            bank_balance = request.form.get("bank_closing_balance", type=float)
            if bank_balance is None and statement_import and statement_import.closing_balance is not None:
                bank_balance = float(statement_import.closing_balance)
            if bank_balance is None:
                flash("Bank closing balance required (or select a statement import with closing balance).", "warning")
                return redirect(url_for("trust_reconciliations"))

            entries = (
                TrustLedgerEntry.query.filter(
                    TrustLedgerEntry.trust_account_id == account_id,
                    TrustLedgerEntry.created_at <= end,
                )
                .order_by(TrustLedgerEntry.created_at.asc(), TrustLedgerEntry.id.asc())
                .all()
            )
            entry_cache = {entry.id: entry for entry in entries}
            ledger_total = round(sum(_signed_trust_entry_amount(entry, entry_cache) for entry in entries), 2)
            client_total = float(
                db.session.query(func.coalesce(func.sum(TrustClientLedger.current_balance), 0.0))
                .filter(TrustClientLedger.trust_account_id == account_id)
                .scalar()
                or 0.0
            )

            status = "balanced"
            notes = []
            if round(bank_balance, 2) != round(ledger_total, 2):
                status = "exception"
                notes.append("Bank vs ledger mismatch")
            if round(ledger_total, 2) != round(client_total, 2):
                status = "exception"
                notes.append("Ledger vs sub-ledgers mismatch")

            run = TrustReconciliationRun(
                trust_account_id=account_id,
                bank_statement_import_id=statement_import_id,
                period_start=start,
                period_end=end,
                bank_closing_balance=bank_balance,
                ledger_closing_balance=ledger_total,
                client_subledger_total=client_total,
                status=status,
                exception_notes="; ".join(notes) if notes else None,
                created_by=current_user.id,
            )
            db.session.add(run)
            db.session.commit()
            audit(
                "trust_reconciliation_run",
                "TrustReconciliationRun",
                run.id,
                {"status": status, "statement_import_id": statement_import_id},
            )
            flash("Reconciliation run created.", "info")
            return redirect(url_for("trust_reconciliations"))

        runs = TrustReconciliationRun.query.order_by(TrustReconciliationRun.created_at.desc()).limit(100).all()
        accounts = TrustAccount.query.order_by(TrustAccount.id.desc()).all()
        statement_imports = (
            TrustBankStatementImport.query.order_by(TrustBankStatementImport.imported_at.desc())
            .limit(200)
            .all()
        )
        return page(
            "Trust Reconciliations",
            "trust_accounting/reconciliations.html",
            runs=runs,
            accounts=accounts,
            statement_imports=statement_imports,
        )

    @app.get("/trust/cashbook")
    @login_required
    def trust_cashbook():
        _trust_admin_required()
        start = _parse_any_date(request.args.get("start"))
        end = _parse_any_date(request.args.get("end"))
        account_filter = request.args.get("trust_account_id", type=int)
        fmt = (request.args.get("format") or "html").strip().lower()

        account_query = TrustAccount.query
        if account_filter:
            account_query = account_query.filter(TrustAccount.id == account_filter)
        accounts = account_query.order_by(TrustAccount.id.asc()).all()
        rows: list[dict[str, Any]] = []

        for account in accounts:
            entries_query = TrustLedgerEntry.query.filter(TrustLedgerEntry.trust_account_id == account.id)
            if start:
                entries_query = entries_query.filter(TrustLedgerEntry.created_at >= dt.datetime.combine(start, dt.time.min))
            if end:
                entries_query = entries_query.filter(TrustLedgerEntry.created_at <= dt.datetime.combine(end, dt.time.max))
            entries = entries_query.order_by(TrustLedgerEntry.created_at.asc(), TrustLedgerEntry.id.asc()).all()
            entry_cache = {entry.id: entry for entry in entries}
            signed_total = round(sum(_signed_trust_entry_amount(entry, entry_cache) for entry in entries), 2)
            deposit_total = round(
                sum(float(entry.amount or 0.0) for entry in entries if (entry.entry_type or "").lower() == "deposit"),
                2,
            )
            disbursement_total = round(
                sum(float(entry.amount or 0.0) for entry in entries if (entry.entry_type or "").lower() == "disbursement"),
                2,
            )
            transfer_total = round(
                sum(float(entry.amount or 0.0) for entry in entries if (entry.entry_type or "").lower() == "transfer"),
                2,
            )
            latest_import = (
                TrustBankStatementImport.query.filter_by(trust_account_id=account.id)
                .order_by(TrustBankStatementImport.imported_at.desc(), TrustBankStatementImport.id.desc())
                .first()
            )
            rows.append(
                {
                    "trust_account_id": account.id,
                    "trust_account_name": account.name,
                    "currency": account.currency,
                    "deposit_total": deposit_total,
                    "disbursement_total": disbursement_total,
                    "transfer_total": transfer_total,
                    "net_total": signed_total,
                    "entry_count": len(entries),
                    "latest_statement_closing": None if latest_import is None else latest_import.closing_balance,
                    "statement_delta": None
                    if latest_import is None or latest_import.closing_balance is None
                    else round(float(latest_import.closing_balance) - signed_total, 2),
                }
            )

        if fmt == "csv":
            csv_rows = [
                [
                    row["trust_account_id"],
                    row["trust_account_name"],
                    row["currency"],
                    row["deposit_total"],
                    row["disbursement_total"],
                    row["transfer_total"],
                    row["net_total"],
                    row["entry_count"],
                    row["latest_statement_closing"],
                    row["statement_delta"],
                ]
                for row in rows
            ]
            return _build_csv_response(
                f"trust_cashbook_{dt.date.today().isoformat()}.csv",
                [
                    "trust_account_id",
                    "trust_account_name",
                    "currency",
                    "deposit_total",
                    "disbursement_total",
                    "transfer_total",
                    "net_total",
                    "entry_count",
                    "latest_statement_closing",
                    "statement_delta",
                ],
                csv_rows,
            )
        return page(
            "Trust Cash Books",
            "trust_accounting/cashbook.html",
            rows=rows,
            accounts=TrustAccount.query.order_by(TrustAccount.id.asc()).all(),
            selected_account_id=account_filter,
            start=start,
            end=end,
        )

    @app.route("/trust/section86", methods=["GET", "POST"])
    @login_required
    def trust_section86():
        _trust_admin_required()
        if request.method == "POST":
            action = (request.form.get("action") or "create").strip().lower()
            if action == "create":
                trust_account_id = request.form.get("trust_account_id", type=int)
                client_ledger_id = request.form.get("client_ledger_id", type=int)
                if not trust_account_id or not client_ledger_id:
                    flash("Trust account and client ledger are required.", "warning")
                    return redirect(url_for("trust_section86"))
                investment_ref = (request.form.get("investment_ref") or "").strip()
                if not investment_ref:
                    flash("Investment reference is required.", "warning")
                    return redirect(url_for("trust_section86"))
                principal = request.form.get("principal_amount", type=float)
                if principal is None or principal <= 0:
                    flash("Principal amount must be positive.", "warning")
                    return redirect(url_for("trust_section86"))
                annual_rate = request.form.get("annual_rate_percent", type=float)
                if annual_rate is None or annual_rate < 0:
                    flash("Annual rate must be zero or positive.", "warning")
                    return redirect(url_for("trust_section86"))
                opened_on = _parse_any_date(request.form.get("opened_on"))
                if opened_on is None:
                    flash("Opened date is invalid.", "warning")
                    return redirect(url_for("trust_section86"))
                maturity_on = _parse_any_date(request.form.get("maturity_on"))
                if Section86Investment.query.filter_by(investment_ref=investment_ref).first():
                    flash("Investment reference already exists.", "warning")
                    return redirect(url_for("trust_section86"))

                row = Section86Investment(
                    trust_account_id=trust_account_id,
                    client_ledger_id=client_ledger_id,
                    matter_id=request.form.get("matter_id", type=int),
                    investment_ref=investment_ref,
                    institution=(request.form.get("institution") or "").strip() or None,
                    principal_amount=principal,
                    annual_rate_percent=annual_rate,
                    opened_on=opened_on,
                    maturity_on=maturity_on,
                    status=(request.form.get("status") or "active").strip().lower(),
                    source=(request.form.get("source") or "manual").strip().lower(),
                    notes=(request.form.get("notes") or "").strip() or None,
                    created_by=current_user.id,
                )
                db.session.add(row)
                db.session.commit()
                audit("section86_investment_create", "Section86Investment", row.id)
                flash("Section 86 investment created.", "info")
                return redirect(url_for("trust_section86"))

            if action == "automate":
                as_of = _parse_any_date(request.form.get("as_of_date")) or dt.date.today()
                withholding_percent = request.form.get("withholding_percent", type=float)
                if withholding_percent is None:
                    withholding_percent = 15.0
                post_to_ledger = (request.form.get("post_to_ledger") or "").lower() in {"1", "true", "yes", "on"}
                created, posted, net_total, skipped = _accrue_section86(as_of, withholding_percent, post_to_ledger)
                audit(
                    "section86_accrual_run",
                    "Section86Accrual",
                    None,
                    {
                        "as_of": as_of.isoformat(),
                        "created": created,
                        "posted": posted,
                        "skipped": skipped,
                        "net_total": net_total,
                    },
                )
                flash(
                    f"Section 86 automation complete: created={created}, posted={posted}, skipped={skipped}, net={net_total:.2f}.",
                    "info",
                )
                return redirect(url_for("trust_section86"))

            flash("Unknown Section 86 action.", "warning")
            return redirect(url_for("trust_section86"))

        investments = Section86Investment.query.order_by(Section86Investment.created_at.desc()).limit(300).all()
        accruals = Section86Accrual.query.order_by(Section86Accrual.created_at.desc()).limit(300).all()
        return page(
            "Section 86 Investments",
            "trust_accounting/section86.html",
            investments=investments,
            accruals=accruals,
            accounts=TrustAccount.query.order_by(TrustAccount.id.asc()).all(),
            ledgers=TrustClientLedger.query.order_by(TrustClientLedger.id.asc()).all(),
        )

    @app.post("/trust/section86/import")
    @login_required
    def trust_section86_import():
        _trust_admin_required()
        file_obj = request.files.get("import_file")
        if file_obj is None or not file_obj.filename:
            flash("Section 86 import CSV is required.", "warning")
            return redirect(url_for("trust_section86"))
        if not file_obj.filename.lower().endswith(".csv"):
            flash("Only CSV imports are supported.", "warning")
            return redirect(url_for("trust_section86"))

        parsed_rows, error = _parse_statement_csv_rows(file_obj.read())
        if error:
            flash(error, "warning")
            return redirect(url_for("trust_section86"))

        created = 0
        skipped = 0
        for row in parsed_rows:
            investment_ref = (row.get("investment_ref") or "").strip()
            if not investment_ref:
                skipped += 1
                continue
            if Section86Investment.query.filter_by(investment_ref=investment_ref).first():
                skipped += 1
                continue
            trust_account_id = _parse_amount(row.get("trust_account_id"))
            client_ledger_id = _parse_amount(row.get("client_ledger_id"))
            opened_on = _parse_any_date(row.get("opened_on"))
            principal = _parse_amount(row.get("principal_amount"))
            annual_rate = _parse_amount(row.get("annual_rate_percent"))
            if (
                trust_account_id is None
                or client_ledger_id is None
                or opened_on is None
                or principal is None
                or principal <= 0
                or annual_rate is None
            ):
                skipped += 1
                continue
            row_obj = Section86Investment(
                trust_account_id=int(trust_account_id),
                client_ledger_id=int(client_ledger_id),
                matter_id=int(_parse_amount(row.get("matter_id")) or 0) or None,
                investment_ref=investment_ref,
                institution=(row.get("institution") or "").strip() or None,
                principal_amount=float(principal),
                annual_rate_percent=float(annual_rate),
                opened_on=opened_on,
                maturity_on=_parse_any_date(row.get("maturity_on")),
                status=(row.get("status") or "active").strip().lower(),
                source=(row.get("source") or "import").strip().lower() or "import",
                notes=(row.get("notes") or "").strip() or None,
                created_by=current_user.id,
            )
            db.session.add(row_obj)
            created += 1
        db.session.commit()
        audit("section86_import", "Section86Investment", None, {"created": created, "skipped": skipped})
        flash(f"Section 86 import complete: created={created}, skipped={skipped}.", "info")
        return redirect(url_for("trust_section86"))

    @app.get("/trust/section86/report")
    @login_required
    def trust_section86_report():
        _trust_admin_required()
        fmt = (request.args.get("format") or "json").strip().lower()
        investments = Section86Investment.query.order_by(Section86Investment.id.asc()).all()
        rows = []
        for inv in investments:
            totals = (
                db.session.query(
                    func.coalesce(func.sum(Section86Accrual.interest_amount), 0.0),
                    func.coalesce(func.sum(Section86Accrual.withholding_tax_amount), 0.0),
                    func.coalesce(func.sum(Section86Accrual.net_interest_amount), 0.0),
                )
                .filter(Section86Accrual.investment_id == inv.id)
                .first()
            )
            rows.append(
                {
                    "investment_id": inv.id,
                    "investment_ref": inv.investment_ref,
                    "trust_account_id": inv.trust_account_id,
                    "client_ledger_id": inv.client_ledger_id,
                    "principal_amount": float(inv.principal_amount or 0.0),
                    "annual_rate_percent": float(inv.annual_rate_percent or 0.0),
                    "status": inv.status,
                    "interest_total": round(float(totals[0] or 0.0), 2),
                    "withholding_total": round(float(totals[1] or 0.0), 2),
                    "net_interest_total": round(float(totals[2] or 0.0), 2),
                }
            )
        if fmt == "csv":
            csv_rows = [
                [
                    row["investment_id"],
                    row["investment_ref"],
                    row["trust_account_id"],
                    row["client_ledger_id"],
                    row["principal_amount"],
                    row["annual_rate_percent"],
                    row["status"],
                    row["interest_total"],
                    row["withholding_total"],
                    row["net_interest_total"],
                ]
                for row in rows
            ]
            return _build_csv_response(
                f"section86_report_{dt.date.today().isoformat()}.csv",
                [
                    "investment_id",
                    "investment_ref",
                    "trust_account_id",
                    "client_ledger_id",
                    "principal_amount",
                    "annual_rate_percent",
                    "status",
                    "interest_total",
                    "withholding_total",
                    "net_interest_total",
                ],
                csv_rows,
            )
        return Response(json.dumps({"generated_at": dt.datetime.utcnow().isoformat(), "rows": rows}, indent=2), mimetype="application/json")

    @app.get("/trust/reports/monthly")
    @login_required
    def trust_reports_monthly():
        _trust_admin_required()
        run_id = request.args.get("run_id", type=int)
        if not run_id:
            runs = TrustReconciliationRun.query.order_by(TrustReconciliationRun.created_at.desc()).limit(100).all()
            return page("Trust Monthly Reports", "trust_accounting/reports.html", runs=runs)

        payload = generate_trust_reconciliation_report(run_id)
        return Response(payload, mimetype="application/json")

    @app.get("/trust/reports/trial-balance")
    @login_required
    def trust_trial_balance_report():
        _trust_admin_required()
        as_of = _parse_any_date(request.args.get("as_of")) or dt.date.today()
        fmt = (request.args.get("format") or "html").strip().lower()
        payload = generate_trust_trial_balance(as_of)
        if fmt == "json":
            return Response(json.dumps(payload, indent=2), mimetype="application/json")
        if fmt == "csv":
            csv_rows = [
                [
                    row.get("trust_account_id"),
                    row.get("trust_account_name"),
                    row.get("currency"),
                    row.get("cashbook_total"),
                    row.get("client_subledger_total"),
                    row.get("cashbook_vs_subledger_delta"),
                    row.get("latest_bank_closing"),
                    row.get("bank_vs_cashbook_delta"),
                    row.get("entry_count"),
                    row.get("client_ledger_count"),
                ]
                for row in payload.get("rows", [])
            ]
            return _build_csv_response(
                f"trust_trial_balance_{as_of.isoformat()}.csv",
                [
                    "trust_account_id",
                    "trust_account_name",
                    "currency",
                    "cashbook_total",
                    "client_subledger_total",
                    "cashbook_vs_subledger_delta",
                    "latest_bank_closing",
                    "bank_vs_cashbook_delta",
                    "entry_count",
                    "client_ledger_count",
                ],
                csv_rows,
            )
        return page(
            "Trust Trial Balance",
            "trust_accounting/trial_balance.html",
            payload=payload,
            as_of=as_of,
        )

    @app.get("/trust/reports/auditor")
    @login_required
    def trust_auditor_report():
        _trust_admin_required()
        as_of = _parse_any_date(request.args.get("as_of")) or dt.date.today()
        fmt = (request.args.get("format") or "html").strip().lower()
        payload = generate_trust_auditor_summary(as_of)
        if fmt == "json":
            return Response(json.dumps(payload, indent=2), mimetype="application/json")
        if fmt == "csv":
            return _build_csv_response(
                f"trust_auditor_report_{as_of.isoformat()}.csv",
                ["metric", "value"],
                [[k, v] for k, v in payload.items()],
            )
        return page(
            "Trust Auditor Report",
            "trust_accounting/auditor_report.html",
            payload=payload,
            as_of=as_of,
        )
