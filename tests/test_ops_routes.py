from __future__ import annotations


def test_healthz_returns_ok_with_no_store_cache(app):
    with app.test_client() as client:
        res = client.get("/healthz")
    assert res.status_code == 200
    payload = res.get_json()
    assert payload is not None
    assert payload.get("status") == "ok"
    assert payload.get("db") == "ok"
    assert "utc" in payload
    assert "no-store" in (res.headers.get("Cache-Control") or "")


def test_readyz_returns_ok_with_required_tables_present(app):
    with app.test_client() as client:
        res = client.get("/readyz")
    assert res.status_code == 200
    payload = res.get_json()
    assert payload is not None
    assert payload.get("status") == "ok"
    assert payload.get("db") == "ok"
    assert "utc" in payload
