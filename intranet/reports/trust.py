from __future__ import annotations

import json

from ..models import TrustClientLedger, TrustLedgerEntry, TrustReconciliationRun


def generate_trust_reconciliation_report(run_id: int) -> str:
    run = TrustReconciliationRun.query.get(run_id)
    if run is None:
        return json.dumps({"error": "run not found"})

    entries = TrustLedgerEntry.query.filter(
        TrustLedgerEntry.trust_account_id == run.trust_account_id,
        TrustLedgerEntry.created_at >= run.period_start,
        TrustLedgerEntry.created_at <= run.period_end,
    ).all()
    ledgers = TrustClientLedger.query.filter_by(trust_account_id=run.trust_account_id).all()

    payload = {
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
    }
    return json.dumps(payload, indent=2)
