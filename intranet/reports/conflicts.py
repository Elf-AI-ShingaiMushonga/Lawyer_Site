from __future__ import annotations

import csv
import io
import json

from ..extensions import db
from ..models import ConflictCheck, IntakeForm


def export_conflict_report_csv(conflict_check_id: int) -> str:
    check = db.session.get(ConflictCheck, conflict_check_id)
    if check is None:
        return "check_id,status,matches\n"

    intake = db.session.get(IntakeForm, check.intake_form_id)
    matches = []
    if check.result_json:
        try:
            payload = json.loads(check.result_json)
            matches = payload.get("matches", [])
        except json.JSONDecodeError:
            matches = []

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["check_id", "status", "intake_id", "matches"])
    writer.writerow([check.id, check.status, intake.id if intake else "", "; ".join(matches)])
    return output.getvalue()
