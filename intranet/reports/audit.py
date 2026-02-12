from __future__ import annotations

import json

from ..models import AuditLog


def export_audit_extract_jsonl(limit: int = 500) -> str:
    rows = AuditLog.query.order_by(AuditLog.at.desc()).limit(limit).all()
    lines = []
    for row in rows:
        lines.append(
            json.dumps(
                {
                    "id": row.id,
                    "at": row.at.isoformat() if row.at else None,
                    "actor_user_id": row.actor_user_id,
                    "action": row.action,
                    "entity_type": row.entity_type,
                    "entity_id": row.entity_id,
                    "ip": row.ip,
                    "details_json": row.details_json,
                }
            )
        )
    return "\n".join(lines)
