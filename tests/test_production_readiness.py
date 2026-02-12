from __future__ import annotations

import pytest

from intranet import create_app


def _set_prod_env(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.setenv("FLASK_SECRET_KEY", "prod-secret-for-tests")
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")


def test_production_requires_backup_encryption_key(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.setenv("FLASK_SECRET_KEY", "prod-secret-for-tests")
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5432/testdb")
    monkeypatch.setenv("GUNICORN_WORKERS", "1")

    with pytest.raises(RuntimeError, match="BACKUP_ENCRYPTION_KEY must be set in production"):
        create_app()


def test_production_rejects_sqlite_database(monkeypatch):
    _set_prod_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "sqlite:////tmp/prod-test.db")
    monkeypatch.setenv("GUNICORN_WORKERS", "1")
    monkeypatch.delenv("ALLOW_IN_MEMORY_RATELIMIT", raising=False)

    with pytest.raises(RuntimeError, match="PostgreSQL is required"):
        create_app()


def test_production_rejects_memory_ratelimit_with_multiworkers(monkeypatch):
    _set_prod_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5432/testdb")
    monkeypatch.setenv("RATE_LIMIT_STORAGE_URI", "memory://")
    monkeypatch.setenv("GUNICORN_WORKERS", "3")
    monkeypatch.delenv("ALLOW_IN_MEMORY_RATELIMIT", raising=False)

    with pytest.raises(RuntimeError, match="RATE_LIMIT_STORAGE_URI=memory:// is unsafe"):
        create_app()


def test_production_allows_memory_ratelimit_bypass_when_explicit(monkeypatch):
    _set_prod_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5432/testdb")
    monkeypatch.setenv("RATE_LIMIT_STORAGE_URI", "memory://")
    monkeypatch.setenv("GUNICORN_WORKERS", "3")
    monkeypatch.setenv("ALLOW_IN_MEMORY_RATELIMIT", "true")

    app = create_app()
    assert app.config["IS_PRODUCTION"] is True
