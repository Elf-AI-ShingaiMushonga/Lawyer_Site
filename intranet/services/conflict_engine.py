from __future__ import annotations

import json

from ..extensions import db
from ..types import ConflictReport


class ConflictEngine:
    """Conflict checking against known entities, contacts, and matters."""

    @staticmethod
    def run_check(intake_id: int) -> ConflictReport:
        from ..models import ConflictCheck, Contact, Entity, IntakeForm, Matter

        intake = db.session.get(IntakeForm, intake_id)
        if intake is None:
            return ConflictReport(conflict_check_id=None, status="error", matched_entities=[], notes="intake not found")

        payload = {}
        if intake.data_json:
            try:
                payload = json.loads(intake.data_json)
            except json.JSONDecodeError:
                payload = {}

        names = [str(n).strip() for n in payload.get("entities", []) if str(n).strip()]
        if not names:
            names = [str(payload.get("client_name") or "").strip()]
        names = [n for n in names if n]

        matches: set[str] = set()
        for name in names:
            like = f"%{name}%"
            for row in Contact.query.filter(Contact.name.ilike(like)).limit(10).all():
                matches.add(f"contact:{row.name}")
            for row in Matter.query.filter(Matter.client_name.ilike(like)).limit(10).all():
                matches.add(f"matter-client:{row.client_name}")
            for row in Entity.query.filter(Entity.name.ilike(like)).limit(10).all():
                matches.add(f"entity:{row.name}")

        status = "clear" if not matches else "potential_conflict"
        check = ConflictCheck(
            intake_form_id=intake.id,
            status=status,
            result_json=json.dumps({"matches": sorted(matches)}),
            override_required=bool(matches),
        )
        db.session.add(check)
        db.session.commit()

        return ConflictReport(
            conflict_check_id=check.id,
            status=status,
            matched_entities=sorted(matches),
            notes="Manual sign-off required" if matches else "No direct matches detected",
        )
