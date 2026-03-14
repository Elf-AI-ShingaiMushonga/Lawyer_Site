from __future__ import annotations


def test_root_renders_single_experience_landing_page_when_not_authenticated(app):
    client = app.test_client()
    response = client.get("/")

    assert response.status_code == 200
    assert b"Choose an experience" in response.data
    assert b"DM-Inc Lawyer Site" in response.data
    assert b"Open Lawyer Site" in response.data
    assert b"UFC Removed" in response.data
