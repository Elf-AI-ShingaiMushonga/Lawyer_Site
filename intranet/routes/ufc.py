from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

from flask import Response, current_app, jsonify


def _load_ufc_module():
    for module_name in ("UFC_Elf.app", "ufc_elf.app"):
        try:
            return importlib.import_module(module_name)
        except Exception:
            continue

    repo_root = Path(__file__).resolve().parents[2]
    candidates = [
        repo_root / "UFC_Elf" / "app.py",
        repo_root / "ufc_elf" / "app.py",
    ]
    for app_path in candidates:
        if not app_path.exists():
            continue

        module_name = f"ufc_embedded_app_{app_path.parent.name.lower()}"
        if str(app_path.parent) not in sys.path:
            sys.path.append(str(app_path.parent))
        spec = importlib.util.spec_from_file_location(module_name, app_path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    searched = ", ".join(str(path) for path in candidates)
    raise ModuleNotFoundError(f"Could not locate UFC module. Searched: {searched}")


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
            raise RuntimeError(f"UFC demo failed to initialize: {exc}") from exc
        app.logger.warning("Failed to register UFC demo routes: %s", exc)

    @app.get("/ufc/")
    def ufc_unavailable():
        message = current_app.config.get("UFC_DEMO_ERROR") or "Unknown startup error."
        return Response(
            f"UFC prediction demo is unavailable right now.\n{message}",
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
