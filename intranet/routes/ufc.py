from __future__ import annotations

import importlib

from flask import Response, current_app, jsonify


def _load_ufc_module():
    return importlib.import_module("UFC_Elf.app")


def register_ufc_routes(app):
    app.config.setdefault("UFC_DEMO_PATH", "/ufc/")
    app.config.setdefault("UFC_DEMO_ENABLED", False)
    app.config.setdefault("UFC_DEMO_ERROR", None)

    try:
        ufc_module = _load_ufc_module()
        ufc_blueprint = getattr(ufc_module, "ufc_bp", None)
        if ufc_blueprint is None:
            raise RuntimeError("UFC_Elf.app does not expose 'ufc_bp'.")

        app.register_blueprint(ufc_blueprint, url_prefix="/ufc")
        app.config["UFC_DEMO_ENABLED"] = True
        app.config["UFC_DEMO_ERROR"] = None
        return
    except Exception as exc:  # noqa: BLE001
        app.config["UFC_DEMO_ERROR"] = str(exc)
        if app.config.get("UFC_STRICT_INIT", False):
            raise RuntimeError(f"UFC module failed to initialize: {exc}") from exc
        app.logger.warning("Failed to register UFC routes: %s", exc)

    @app.get("/ufc/")
    def ufc_unavailable():
        message = current_app.config.get("UFC_DEMO_ERROR") or "Unknown startup error."
        return Response(
            f"UFC prediction is unavailable right now.\n{message}",
            status=503,
            content_type="text/plain; charset=utf-8",
        )

    @app.post("/ufc/api/predict")
    def ufc_unavailable_predict_api():
        message = current_app.config.get("UFC_DEMO_ERROR") or "Unknown startup error."
        return jsonify({"ok": False, "error": message}), 503

    @app.get("/ufc/healthz")
    def ufc_unavailable_healthz():
        message = current_app.config.get("UFC_DEMO_ERROR") or "Unknown startup error."
        return jsonify({"ok": False, "error": message}), 503

    @app.post("/ufc/api/jobs")
    def ufc_unavailable_jobs_create():
        message = current_app.config.get("UFC_DEMO_ERROR") or "Unknown startup error."
        return jsonify({"ok": False, "error": message}), 503

    @app.get("/ufc/api/jobs/active")
    def ufc_unavailable_jobs_active():
        message = current_app.config.get("UFC_DEMO_ERROR") or "Unknown startup error."
        return jsonify({"ok": False, "error": message, "job": None}), 503

    @app.get("/ufc/api/jobs/<job_id>")
    def ufc_unavailable_jobs_status(job_id: str):
        message = current_app.config.get("UFC_DEMO_ERROR") or "Unknown startup error."
        return jsonify({"ok": False, "error": message, "job_id": job_id}), 503
