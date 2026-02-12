from __future__ import annotations

import datetime as dt

from intranet.extensions import db
from intranet.jobs.worker import _handle_burnout_heuristics, _handle_workload_forecast
from intranet.models import (
    BurnoutSignal,
    Contact,
    DeadlineRule,
    FeeArrangement,
    IntakeForm,
    Invoice,
    InvoiceLine,
    Matter,
    PaymentAllocation,
    RateCard,
    TimeEntry,
    TrustAccount,
    TrustClientLedger,
    WorkloadForecast,
)
from intranet.services.analytics_engine import AnalyticsEngine
from intranet.services.billing_engine import BillingEngine
from intranet.services.conflict_engine import ConflictEngine
from intranet.services.deadline_engine import DeadlineEngine
from intranet.services.trust_engine import TrustEngine


def test_deadline_engine_calculation_trace(seed_user_matter):
    matter = seed_user_matter["matter"]
    user = seed_user_matter["user"]

    rule = DeadlineRule(
        name="Five-day rule",
        matter_id=matter.id,
        trigger_type="filing",
        offset_days=5,
        business_day_adjust=True,
        is_active=True,
        created_by=user.id,
    )
    db.session.add(rule)
    db.session.commit()

    trace = DeadlineEngine.calculate(matter.id, None, rule_id=rule.id, base_date=dt.date(2026, 2, 12))
    assert trace.matter_id == matter.id
    assert trace.offset_days == 5
    assert trace.rule_name == "Five-day rule"
    assert trace.result_due_at_iso >= "2026-02-17"


def test_trust_engine_enforces_overdraft_lock(seed_user_matter):
    account = seed_user_matter["account"]
    ledger = seed_user_matter["ledger"]
    user = seed_user_matter["user"]

    post_deposit = TrustEngine.post_transaction(
        {
            "trust_account_id": account.id,
            "client_ledger_id": ledger.id,
            "entry_type": "deposit",
            "amount": 1000.0,
            "currency": "ZAR",
            "created_by": user.id,
        }
    )
    assert post_deposit.posted is True

    overdraft = TrustEngine.post_transaction(
        {
            "trust_account_id": account.id,
            "client_ledger_id": ledger.id,
            "entry_type": "disbursement",
            "amount": 1500.0,
            "currency": "ZAR",
            "created_by": user.id,
        }
    )
    assert overdraft.posted is False
    assert "overdraft" in overdraft.message.lower()


def test_conflict_engine_detects_contact_match(seed_user_matter):
    user = seed_user_matter["user"]
    matter = seed_user_matter["matter"]

    db.session.add(Contact(name="Acme Holdings", created_by=user.id))
    db.session.flush()

    intake = IntakeForm(
        lead_id=None,
        matter_id=matter.id,
        data_json='{"entities": ["Acme Holdings"]}',
        created_by=user.id,
    )
    db.session.add(intake)
    db.session.commit()

    report = ConflictEngine.run_check(intake.id)
    assert report.conflict_check_id is not None
    assert report.status == "potential_conflict"
    assert any("acme" in item.lower() for item in report.matched_entities)


def test_trust_engine_rejects_ledger_account_mismatch(seed_user_matter):
    user = seed_user_matter["user"]
    account = seed_user_matter["account"]
    other_account = TrustAccount(name="Secondary Trust", currency="ZAR", is_active=True)
    db.session.add(other_account)
    db.session.flush()
    other_ledger = TrustClientLedger(
        trust_account_id=other_account.id,
        client_name="Other Client",
        matter_id=seed_user_matter["matter"].id,
        current_balance=0.0,
    )
    db.session.add(other_ledger)
    db.session.commit()

    result = TrustEngine.post_transaction(
        {
            "trust_account_id": account.id,
            "client_ledger_id": other_ledger.id,
            "entry_type": "deposit",
            "amount": 25.0,
            "currency": "ZAR",
            "created_by": user.id,
        }
    )
    assert result.posted is False
    assert "does not belong" in result.message.lower()


def test_analytics_engine_honors_matter_scope(seed_user_matter):
    user = seed_user_matter["user"]
    matter = seed_user_matter["matter"]

    other_matter = Matter(
        matter_no="2026-TEST-0002",
        title="Other Matter",
        client_name="Other Client",
        status="Open",
        created_by=user.id,
        opened_at=dt.datetime.utcnow(),
        last_updated_at=dt.datetime.utcnow(),
    )
    db.session.add(other_matter)
    db.session.flush()

    db.session.add(
        TimeEntry(
            user_id=user.id,
            matter_id=matter.id,
            start_at=dt.datetime.utcnow() - dt.timedelta(hours=2),
            end_at=dt.datetime.utcnow() - dt.timedelta(hours=1),
            hours=2.0,
            rounded_hours=2.0,
            narrative="Scoped work",
            status="approved",
        )
    )
    db.session.add(
        TimeEntry(
            user_id=user.id,
            matter_id=other_matter.id,
            start_at=dt.datetime.utcnow() - dt.timedelta(hours=3),
            end_at=dt.datetime.utcnow() - dt.timedelta(hours=2),
            hours=3.0,
            rounded_hours=3.0,
            narrative="Other work",
            status="approved",
        )
    )

    invoice_scoped = Invoice(
        matter_id=matter.id,
        client_name=matter.client_name,
        period_start=dt.date(2026, 1, 1),
        period_end=dt.date(2026, 1, 31),
        status="approved",
        subtotal=200.0,
        tax_total=30.0,
        total=230.0,
        created_by=user.id,
    )
    invoice_other = Invoice(
        matter_id=other_matter.id,
        client_name=other_matter.client_name,
        period_start=dt.date(2026, 1, 1),
        period_end=dt.date(2026, 1, 31),
        status="approved",
        subtotal=500.0,
        tax_total=75.0,
        total=575.0,
        created_by=user.id,
    )
    db.session.add_all([invoice_scoped, invoice_other])
    db.session.flush()
    db.session.add(PaymentAllocation(invoice_id=invoice_scoped.id, amount=230.0, created_by=user.id))
    db.session.add(PaymentAllocation(invoice_id=invoice_other.id, amount=575.0, created_by=user.id))
    db.session.commit()

    scoped = AnalyticsEngine.compute_snapshot(dt.date.today(), matter_scope_ids=[matter.id], persist=False)
    assert scoped.metrics["billable_hours"] == 2.0
    assert scoped.metrics["billed"] == 230.0
    assert scoped.metrics["collected"] == 230.0


def test_billing_engine_applies_fee_cap_adjustment(seed_user_matter):
    user = seed_user_matter["user"]
    matter = seed_user_matter["matter"]

    db.session.add(
        RateCard(
            name="Default rate",
            matter_id=matter.id,
            user_id=user.id,
            currency="ZAR",
            rate_per_hour=100.0,
            is_active=True,
        )
    )
    db.session.add(
        FeeArrangement(
            matter_id=matter.id,
            arrangement_type="capped",
            cap_amount=150.0,
            notes="Cap this matter at 150",
        )
    )
    db.session.add(
        TimeEntry(
            user_id=user.id,
            matter_id=matter.id,
            start_at=dt.datetime(2026, 2, 10, 9, 0, 0),
            end_at=dt.datetime(2026, 2, 10, 11, 0, 0),
            hours=2.0,
            rounded_hours=2.0,
            narrative="Cap-adjusted billing work",
            status="approved",
        )
    )
    db.session.commit()

    result = BillingEngine.generate_invoice(
        matter.id,
        (dt.date(2026, 2, 1), dt.date(2026, 2, 28)),
        created_by=user.id,
    )
    invoice = db.session.get(Invoice, result.invoice_id)
    assert invoice is not None
    assert float(invoice.subtotal or 0.0) == 150.0

    lines = InvoiceLine.query.filter_by(invoice_id=invoice.id).order_by(InvoiceLine.id.asc()).all()
    assert any("cap" in (line.description or "").lower() for line in lines)


def test_forecast_and_burnout_jobs_materialize_signals(seed_user_matter):
    user = seed_user_matter["user"]
    matter = seed_user_matter["matter"]
    today = dt.date.today()
    for day_offset, hours in enumerate([11.0, 10.5, 9.0, 8.5], start=1):
        start = dt.datetime.combine(today - dt.timedelta(days=day_offset), dt.time(hour=9))
        db.session.add(
            TimeEntry(
                user_id=user.id,
                matter_id=matter.id,
                start_at=start,
                end_at=start + dt.timedelta(hours=int(hours)),
                hours=hours,
                rounded_hours=hours,
                narrative=f"Long day {day_offset}",
                status="approved",
            )
        )
    db.session.commit()

    forecast_msg = _handle_workload_forecast({"as_of_date": today.isoformat(), "lookback_days": 30})
    burnout_msg = _handle_burnout_heuristics({"as_of_date": today.isoformat(), "window_days": 14})

    assert "upserted" in forecast_msg
    assert "upserted" in burnout_msg
    forecast = WorkloadForecast.query.filter_by(user_id=user.id, as_of_date=today).first()
    burnout = BurnoutSignal.query.filter_by(user_id=user.id, as_of_date=today).first()
    assert forecast is not None
    assert float(forecast.predicted_hours or 0.0) > 0
    assert burnout is not None
    assert float(burnout.score or 0.0) > 0
