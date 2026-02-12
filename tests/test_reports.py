from __future__ import annotations

import datetime as dt

from intranet.extensions import db
from intranet.models import Invoice, InvoiceLine
from intranet.reports.ledes import generate_ledes_1998b


def test_ledes_export_contains_header_and_line(seed_user_matter):
    matter = seed_user_matter["matter"]
    user = seed_user_matter["user"]

    invoice = Invoice(
        matter_id=matter.id,
        client_name=matter.client_name,
        period_start=dt.date(2026, 1, 1),
        period_end=dt.date(2026, 1, 31),
        status="approved",
        subtotal=1000.0,
        tax_total=150.0,
        total=1150.0,
        created_by=user.id,
    )
    db.session.add(invoice)
    db.session.flush()

    db.session.add(
        InvoiceLine(
            invoice_id=invoice.id,
            description="Drafting affidavit",
            hours=2.0,
            rate=500.0,
            amount=1000.0,
            tax_amount=150.0,
        )
    )
    db.session.commit()

    payload = generate_ledes_1998b(invoice)
    assert "INVOICE_NUMBER" in payload
    assert "Drafting affidavit" in payload
