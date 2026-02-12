from __future__ import annotations

import csv
import io

from ..models import Invoice, InvoiceLine


def generate_ledes_1998b(invoice: Invoice) -> str:
    """Generate a minimal LEDES 1998B compatible text payload."""
    output = io.StringIO(newline="")
    writer = csv.writer(output)

    headers = [
        "INVOICE_NUMBER",
        "CLIENT_ID",
        "LAW_FIRM_MATTER_ID",
        "LINE_ITEM_NUMBER",
        "LINE_ITEM_DESCRIPTION",
        "LINE_ITEM_NUMBER_OF_UNITS",
        "LINE_ITEM_UNIT_COST",
        "LINE_ITEM_TOTAL",
    ]
    writer.writerow(headers)

    lines = InvoiceLine.query.filter_by(invoice_id=invoice.id).order_by(InvoiceLine.id.asc()).all()
    for i, line in enumerate(lines, start=1):
        writer.writerow(
            [
                str(invoice.id),
                invoice.client_name,
                str(invoice.matter_id),
                i,
                (line.description or "")[:80],
                f"{float(line.hours or 0):.2f}",
                f"{float(line.rate or 0):.2f}",
                f"{float(line.amount or 0):.2f}",
            ]
        )

    return output.getvalue()
