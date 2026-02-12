from __future__ import annotations

import datetime as dt
import json

from flask import Response, abort, flash, redirect, request, session, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from ..extensions import db
from ..helpers import audit
from ..models import (
    TrustAccount,
    TrustClientLedger,
    TrustLedgerEntry,
    TrustReconciliationRun,
    TrustApprovalRequest,
    TrustThresholdAlert,
)
from ..reports.trust import generate_trust_reconciliation_report
from ..services.trust_engine import TrustEngine
from ..templates import page


def _trust_admin_required() -> None:
    if current_user.role not in {"admin", "lawyer"}:
        abort(403)


def _create_threshold_alerts(ledger: TrustClientLedger) -> None:
    # Baseline replenishment alert at <= 20% of last reconciliation nominal value.
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

    @app.route("/trust/reconciliations", methods=["GET", "POST"])
    @login_required
    def trust_reconciliations():
        _trust_admin_required()
        if request.method == "POST":
            account_id = request.form.get("trust_account_id", type=int)
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
            if bank_balance is None:
                flash("Bank closing balance required.", "warning")
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
            audit("trust_reconciliation_run", "TrustReconciliationRun", run.id, {"status": status})
            flash("Reconciliation run created.", "info")
            return redirect(url_for("trust_reconciliations"))

        runs = TrustReconciliationRun.query.order_by(TrustReconciliationRun.created_at.desc()).limit(100).all()
        accounts = TrustAccount.query.order_by(TrustAccount.id.desc()).all()
        return page("Trust Reconciliations", "trust_accounting/reconciliations.html", runs=runs, accounts=accounts)

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
