from __future__ import annotations

import intranet
from intranet import create_app


def test_schema_sync_runs_by_default_in_dev(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    monkeypatch.delenv("ENABLE_SCHEMA_COMPAT_SYNC", raising=False)
    monkeypatch.setattr(intranet.sys, "argv", ["python", "app.py", "run"])

    calls = {"count": 0}

    def _fake_sync():
        calls["count"] += 1

    monkeypatch.setattr(intranet, "sync_schema_compatibility", _fake_sync)
    create_app()
    assert calls["count"] == 1


def test_schema_sync_skips_during_flask_db_commands(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret")
    monkeypatch.delenv("ENABLE_SCHEMA_COMPAT_SYNC", raising=False)
    monkeypatch.setattr(intranet.sys, "argv", ["flask", "--app", "app.py", "db", "upgrade"])

    calls = {"count": 0}

    def _fake_sync():
        calls["count"] += 1

    monkeypatch.setattr(intranet, "sync_schema_compatibility", _fake_sync)
    create_app()
    assert calls["count"] == 0
