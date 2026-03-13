from __future__ import annotations

from intranet.extensions import db
from intranet.models import User


def test_story_routes_not_registered(app):
    routes = {rule.rule for rule in app.url_map.iter_rules()}
    assert "/story" not in routes
    assert "/story-mode" not in routes


def test_story_endpoints_return_404(app):
    client = app.test_client()
    assert client.get("/story").status_code == 404
    assert client.get("/story-mode").status_code == 404


def test_login_page_has_no_story_cta(app):
    with app.app_context():
        user = User(email="existing@example.com", full_name="Existing User", role="admin", password_hash="x")
        user.set_password("TestPassword123!")
        db.session.add(user)
        db.session.commit()

    client = app.test_client()
    response = client.get("/login")
    assert response.status_code == 200
    assert b"story-mode" not in response.data
    assert b"/story" not in response.data
    assert b"Access Dashboard" in response.data
