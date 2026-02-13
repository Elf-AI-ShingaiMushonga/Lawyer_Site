from __future__ import annotations

import datetime as dt
import json

from sqlalchemy import func

from ..extensions import db
from ..models import (
    Section86Accrual,
    Section86Investment,
    TrustAccount,
    TrustApprovalRequest,
    TrustBankStatementImport,
    TrustClientLedger,
    TrustLedgerEntry,
    TrustReconciliationRun,
    TrustThresholdAlert,
)


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


def generate_trust_reconciliation_report(run_id: int) -> str:
    run = db.session.get(TrustReconciliationRun, run_id)
    if run is None:
        return json.dumps({"error": "run not found"})

    entries = TrustLedgerEntry.query.filter(
        TrustLedgerEntry.trust_account_id == run.trust_account_id,
        TrustLedgerEntry.created_at >= run.period_start,
        TrustLedgerEntry.created_at <= run.period_end,
    ).all()
    ledgers = TrustClientLedger.query.filter_by(trust_account_id=run.trust_account_id).all()

    statement_import = None
    if run.bank_statement_import_id:
        statement_import = db.session.get(TrustBankStatementImport, run.bank_statement_import_id)

    section86_count = (
        Section86Investment.query.filter(
            Section86Investment.trust_account_id == run.trust_account_id,
            Section86Investment.opened_on <= run.period_end.date(),
        ).count()
    )
    section86_interest = float(
        db.session.query(func.coalesce(func.sum(Section86Accrual.net_interest_amount), 0.0))
        .join(Section86Investment, Section86Investment.id == Section86Accrual.investment_id)
        .filter(
            Section86Investment.trust_account_id == run.trust_account_id,
            Section86Accrual.accrual_date >= run.period_start.date(),
            Section86Accrual.accrual_date <= run.period_end.date(),
        )
        .scalar()
        or 0.0
    )

    payload: dict[str, object] = {
        "run_id": run.id,
        "period_start": run.period_start.isoformat(),
        "period_end": run.period_end.isoformat(),
        "bank_closing_balance": float(run.bank_closing_balance or 0),
        "ledger_closing_balance": float(run.ledger_closing_balance or 0),
        "client_subledger_total": float(run.client_subledger_total or 0),
        "entry_count": len(entries),
        "client_ledgers": [
            {
                "id": ledger.id,
                "client_name": ledger.client_name,
                "balance": float(ledger.current_balance or 0),
            }
            for ledger in ledgers
        ],
        "section86": {
            "active_investments_in_scope": int(section86_count),
            "net_interest_in_period": round(float(section86_interest), 2),
        },
    }
    if statement_import:
        payload["bank_statement_import"] = {
            "id": statement_import.id,
            "label": statement_import.statement_label,
            "filename": statement_import.source_filename,
            "period_start": statement_import.period_start.isoformat() if statement_import.period_start else None,
            "period_end": statement_import.period_end.isoformat() if statement_import.period_end else None,
            "opening_balance": float(statement_import.opening_balance or 0.0)
            if statement_import.opening_balance is not None
            else None,
            "closing_balance": float(statement_import.closing_balance or 0.0)
            if statement_import.closing_balance is not None
            else None,
        }
    return json.dumps(payload, indent=2)


def generate_trust_trial_balance(as_of_date: dt.date | None = None) -> dict[str, object]:
    as_of = as_of_date or dt.date.today()
    cutoff = dt.datetime.combine(as_of, dt.time.max)
    rows: list[dict[str, object]] = []
    accounts = TrustAccount.query.order_by(TrustAccount.id.asc()).all()

    for account in accounts:
        ledgers = TrustClientLedger.query.filter_by(trust_account_id=account.id).all()
        client_total = round(sum(float(ledger.current_balance or 0.0) for ledger in ledgers), 2)
        entries = (
            TrustLedgerEntry.query.filter(
                TrustLedgerEntry.trust_account_id == account.id,
                TrustLedgerEntry.created_at <= cutoff,
            )
            .order_by(TrustLedgerEntry.created_at.asc(), TrustLedgerEntry.id.asc())
            .all()
        )
        entry_cache = {entry.id: entry for entry in entries}
        cashbook_total = round(sum(_signed_trust_entry_amount(entry, entry_cache) for entry in entries), 2)
        latest_bank = (
            TrustBankStatementImport.query.filter(
                TrustBankStatementImport.trust_account_id == account.id,
                TrustBankStatementImport.imported_at <= cutoff,
            )
            .order_by(TrustBankStatementImport.imported_at.desc(), TrustBankStatementImport.id.desc())
            .first()
        )
        bank_closing = None if latest_bank is None else latest_bank.closing_balance
        bank_delta = None
        if bank_closing is not None:
            bank_delta = round(float(bank_closing or 0.0) - cashbook_total, 2)

        rows.append(
            {
                "trust_account_id": int(account.id),
                "trust_account_name": account.name,
                "currency": account.currency,
                "cashbook_total": cashbook_total,
                "client_subledger_total": client_total,
                "cashbook_vs_subledger_delta": round(cashbook_total - client_total, 2),
                "latest_bank_closing": None if bank_closing is None else round(float(bank_closing or 0.0), 2),
                "bank_vs_cashbook_delta": bank_delta,
                "entry_count": len(entries),
                "client_ledger_count": len(ledgers),
            }
        )

    return {
        "as_of_date": as_of.isoformat(),
        "rows": rows,
        "row_count": len(rows),
    }


def generate_trust_auditor_summary(as_of_date: dt.date | None = None) -> dict[str, object]:
    as_of = as_of_date or dt.date.today()
    cutoff = dt.datetime.combine(as_of, dt.time.max)

    reconciliation_runs = (
        TrustReconciliationRun.query.filter(TrustReconciliationRun.created_at <= cutoff)
        .order_by(TrustReconciliationRun.created_at.desc())
        .all()
    )
    exceptions = sum(1 for run in reconciliation_runs if (run.status or "").lower() == "exception")
    balances = db.session.query(func.coalesce(func.sum(TrustClientLedger.current_balance), 0.0)).scalar() or 0.0
    entries = (
        TrustLedgerEntry.query.filter(TrustLedgerEntry.created_at <= cutoff)
        .order_by(TrustLedgerEntry.created_at.asc(), TrustLedgerEntry.id.asc())
        .all()
    )
    entry_cache = {entry.id: entry for entry in entries}
    cashbook_total = round(sum(_signed_trust_entry_amount(entry, entry_cache) for entry in entries), 2)
    open_alerts = TrustThresholdAlert.query.filter_by(status="open").count()
    pending_approvals = TrustApprovalRequest.query.filter_by(status="pending").count()
    statement_imports = TrustBankStatementImport.query.filter(TrustBankStatementImport.imported_at <= cutoff).count()
    active_investments = Section86Investment.query.filter(
        Section86Investment.opened_on <= as_of,
        Section86Investment.status.in_(["active", "matured"]),
    ).count()
    total_net_interest = float(
        db.session.query(func.coalesce(func.sum(Section86Accrual.net_interest_amount), 0.0))
        .filter(Section86Accrual.created_at <= cutoff)
        .scalar()
        or 0.0
    )

    return {
        "as_of_date": as_of.isoformat(),
        "reconciliation_runs": len(reconciliation_runs),
        "reconciliation_exceptions": exceptions,
        "open_threshold_alerts": open_alerts,
        "pending_maker_checker_requests": pending_approvals,
        "bank_statement_imports": statement_imports,
        "client_subledger_total": round(float(balances), 2),
        "cashbook_total": round(float(cashbook_total), 2),
        "cashbook_vs_subledger_delta": round(float(cashbook_total) - float(balances), 2),
        "section86_active_investments": int(active_investments),
        "section86_net_interest_total": round(total_net_interest, 2),
    }
