from __future__ import annotations

import datetime as dt
from ..timeutils import utc_now

import sqlalchemy as sa
from flask import jsonify
from sqlalchemy import text

from ..extensions import db


def register_ops_routes(app):
    def _probe_payload() -> tuple[dict, int]:
        try:
            db.session.execute(text("SELECT 1"))
            return {"status": "ok", "db": "ok", "utc": utc_now().replace(microsecond=0).isoformat() + "Z"}, 200
        except Exception:
            db.session.rollback()
            return {"status": "error", "db": "unreachable"}, 503

    def _json_no_store(payload: dict, status_code: int):
        response = jsonify(payload)
        response.status_code = status_code
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return response

    @app.get("/healthz")
    def healthz():
        payload, status_code = _probe_payload()
        return _json_no_store(payload, status_code)

    @app.get("/readyz")
    def readyz():
        payload, status_code = _probe_payload()
        if status_code != 200:
            return _json_no_store(payload, status_code)

        try:
            inspector = sa.inspect(db.engine)
            table_names = set(inspector.get_table_names())
            required_tables = {"user", "matter", "task", "document_file"}
            missing_tables = sorted(required_tables - table_names)
            if missing_tables:
                return _json_no_store(
                    {
                        "status": "error",
                        "db": "ok",
                        "reason": "missing_tables",
                        "missing_tables": missing_tables,
                        "utc": utc_now().replace(microsecond=0).isoformat() + "Z",
                    },
                    503,
                )
            return _json_no_store(payload, 200)
        except Exception:
            db.session.rollback()
            return _json_no_store(
                {
                    "status": "error",
                    "db": "ok",
                    "reason": "readiness_check_failed",
                    "utc": utc_now().replace(microsecond=0).isoformat() + "Z",
                },
                503,
            )
