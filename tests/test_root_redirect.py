from __future__ import annotations


def test_root_redirects_to_login_when_not_authenticated(app):
    client = app.test_client()
    response = client.get("/")

    assert response.status_code == 302
    assert response.headers.get("Location", "").endswith("/login")
