from __future__ import annotations

import datetime as dt
import secrets

from sqlalchemy import select

from ..extensions import db
from ..types import LedgerPostingResult


class TrustEngine:
    """Trust posting engine with overdraft protection and immutable correction model."""

    @staticmethod
    def post_transaction(request: dict) -> LedgerPostingResult:
        from ..models import TrustClientLedger, TrustLedgerEntry

        required = ["trust_account_id", "client_ledger_id", "entry_type", "amount", "created_by"]
        for key in required:
            if key not in request:
                return LedgerPostingResult(posted=False, message=f"missing field: {key}")

        amount = float(request.get("amount") or 0)
        if amount <= 0:
            return LedgerPostingResult(posted=False, message="amount must be positive")

        trust_account_id = int(request["trust_account_id"])
        client_ledger_id = int(request["client_ledger_id"])
        entry_type = str(request["entry_type"]).lower()
        if entry_type not in {"deposit", "disbursement", "transfer", "reversal"}:
            return LedgerPostingResult(posted=False, message="invalid trust entry type")

        ledger = db.session.execute(
            select(TrustClientLedger).where(TrustClientLedger.id == client_ledger_id).with_for_update()
        ).scalar_one_or_none()
        if ledger is None:
            return LedgerPostingResult(posted=False, message="client ledger not found")
        if int(ledger.trust_account_id) != trust_account_id:
            return LedgerPostingResult(posted=False, message="ledger does not belong to trust account")

        delta = amount
        reversal_of_entry_id = request.get("reversal_of_entry_id")
        if entry_type == "reversal":
            if not reversal_of_entry_id:
                return LedgerPostingResult(posted=False, message="reversal requires reversal_of_entry_id")
            original = db.session.execute(
                select(TrustLedgerEntry).where(TrustLedgerEntry.id == int(reversal_of_entry_id)).with_for_update()
            ).scalar_one_or_none()
            if original is None:
                return LedgerPostingResult(posted=False, message="original entry for reversal not found")
            if int(original.client_ledger_id) != client_ledger_id or int(original.trust_account_id) != trust_account_id:
                return LedgerPostingResult(posted=False, message="reversal target does not match ledger/account")
            if str(original.entry_type).lower() == "reversal":
                return LedgerPostingResult(posted=False, message="cannot reverse a reversal entry")
            prior_reversal = TrustLedgerEntry.query.filter_by(reversal_of_entry_id=original.id).first()
            if prior_reversal is not None:
                return LedgerPostingResult(posted=False, message="entry already reversed")

            amount = float(original.amount)
            if str(original.entry_type).lower() == "deposit":
                delta = -amount
            else:
                delta = amount
        elif reversal_of_entry_id:
            return LedgerPostingResult(posted=False, message="reversal_of_entry_id only valid for reversal entries")
        elif entry_type in {"disbursement", "transfer"}:
            delta = -amount

        if (ledger.current_balance or 0) + delta < 0:
            db.session.rollback()
            return LedgerPostingResult(posted=False, message="overdraft lock: insufficient client trust balance")

        entry = TrustLedgerEntry(
            trust_account_id=trust_account_id,
            client_ledger_id=client_ledger_id,
            entry_type=entry_type,
            amount=amount,
            currency=request.get("currency") or "ZAR",
            description=request.get("description"),
            supporting_document_id=request.get("supporting_document_id"),
            created_by=int(request["created_by"]),
            reversal_of_entry_id=reversal_of_entry_id,
            immutable_ref=request.get("immutable_ref")
            or f"TL-{trust_account_id}-{dt.datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(4)}",
        )
        db.session.add(entry)

        ledger.current_balance = float(ledger.current_balance or 0) + delta
        db.session.commit()

        return LedgerPostingResult(
            posted=True,
            entry_id=entry.id,
            balance_after=float(ledger.current_balance or 0),
            message="posted",
        )
