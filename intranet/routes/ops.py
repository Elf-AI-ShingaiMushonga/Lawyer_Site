from __future__ import annotations

from flask import jsonify
from sqlalchemy import text

from ..extensions import db


def register_ops_routes(app):
    @app.get("/healthz")
    def healthz():
        try:
            db.session.execute(text("SELECT 1"))
            return jsonify({"status": "ok"}), 200
        except Exception:
            db.session.rollback()
            return jsonify({"status": "error"}), 503
