from __future__ import annotations

import csv
import io
import json

from ..extensions import db
from ..models import ConflictCheck, ConflictSemanticHit, IntakeForm


def export_conflict_report_csv(conflict_check_id: int) -> str:
    check = db.session.get(ConflictCheck, conflict_check_id)
    if check is None:
        return "check_id,status,matches,semantic_hits\n"

    intake = db.session.get(IntakeForm, check.intake_form_id)
    matches = []
    semantic_status = "unknown"
    if check.result_json:
        try:
            payload = json.loads(check.result_json)
            matches = payload.get("matches", [])
            semantic_status = str(payload.get("semantic_status") or semantic_status)
        except json.JSONDecodeError:
            matches = []
            semantic_status = "unavailable"

    semantic_rows = (
        ConflictSemanticHit.query.filter_by(conflict_check_id=check.id)
        .order_by(ConflictSemanticHit.semantic_rank.asc(), ConflictSemanticHit.similarity_score.desc())
        .all()
    )
    semantic_hits = [
        f"{row.candidate_entity} -> matter:{row.matter_id or '-'} score:{float(row.similarity_score or 0.0):.2f}"
        for row in semantic_rows
    ]

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["check_id", "status", "intake_id", "matches", "semantic_hits", "semantic_status"])
    writer.writerow(
        [
            check.id,
            check.status,
            intake.id if intake else "",
            "; ".join(matches),
            "; ".join(semantic_hits),
            semantic_status,
        ]
    )
    return output.getvalue()
