from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from threading import Lock
from typing import Callable

from flask import Flask
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.wrappers import Response


class LazyMountedWSGIApp:
    """Load and serve a mounted WSGI app on first request."""

    def __init__(
        self,
        module_path: Path,
        module_name: str,
        app_attr: str,
        extra_sys_paths: list[Path],
        logger,
    ) -> None:
        self._module_path = module_path
        self._module_name = module_name
        self._app_attr = app_attr
        self._extra_sys_paths = [str(path) for path in extra_sys_paths]
        self._logger = logger
        self._target: Callable | None = None
        self._load_error: str | None = None
        self._lock = Lock()

    def _load_target(self) -> Callable:
        if self._target is not None:
            return self._target
        if self._load_error:
            raise RuntimeError(self._load_error)

        with self._lock:
            if self._target is not None:
                return self._target
            if self._load_error:
                raise RuntimeError(self._load_error)
            try:
                for extra_path in self._extra_sys_paths:
                    if extra_path not in sys.path:
                        sys.path.append(extra_path)

                spec = importlib.util.spec_from_file_location(self._module_name, self._module_path)
                if spec is None or spec.loader is None:
                    raise ImportError(f"Could not load module spec for {self._module_path}")

                module = importlib.util.module_from_spec(spec)
                sys.modules[self._module_name] = module
                spec.loader.exec_module(module)

                target = getattr(module, self._app_attr, None)
                if not callable(target):
                    raise RuntimeError(
                        f"Mounted module '{self._module_name}' does not expose callable '{self._app_attr}'."
                    )
                self._target = target
                self._logger.info("Mounted UFC demo app from %s", self._module_path)
                return target
            except Exception as exc:  # noqa: BLE001
                self._load_error = str(exc)
                self._logger.exception("Failed to initialize UFC demo app mount.")
                raise

    def __call__(self, environ, start_response):
        try:
            target = self._load_target()
        except Exception:
            error_message = self._load_error or "Unknown startup error."
            response = Response(
                f"UFC prediction demo is unavailable right now.\n{error_message}",
                status=503,
                content_type="text/plain; charset=utf-8",
            )
            return response(environ, start_response)
        return target(environ, start_response)


def configure_ufc_mount(app: Flask) -> None:
    app.config.setdefault("UFC_DEMO_PATH", "/ufc/")
    app.config.setdefault("UFC_DEMO_ENABLED", False)
    app.config.setdefault("UFC_DEMO_ERROR", None)

    if app.config.get("_UFC_MOUNTED", False):
        return

    repo_root = Path(__file__).resolve().parent.parent
    ufc_root = repo_root / "UFC_Elf"
    ufc_entrypoint = ufc_root / "app.py"

    if not ufc_entrypoint.exists():
        app.config["UFC_DEMO_ERROR"] = f"Missing UFC entrypoint: {ufc_entrypoint}"
        app.logger.warning("Skipping UFC demo mount: %s", app.config["UFC_DEMO_ERROR"])
        return

    embedded_site_packages = sorted(
        path
        for pattern in ("venv/lib/python*/site-packages", "venv/lib/python*/dist-packages")
        for path in ufc_root.glob(pattern)
        if path.is_dir()
    )

    lazy_ufc_app = LazyMountedWSGIApp(
        module_path=ufc_entrypoint,
        module_name="ufc_elf_embedded_app",
        app_attr="app",
        extra_sys_paths=[ufc_root, *embedded_site_packages],
        logger=app.logger,
    )
    app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {"/ufc": lazy_ufc_app})
    app.config["UFC_DEMO_ENABLED"] = True
    app.config["_UFC_MOUNTED"] = True
