from __future__ import annotations


def test_demo_landing_page_shows_both_experiences(app):
    client = app.test_client()
    response = client.get("/")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "DM-Inc Intranet" in body
    assert "UFC Prediction" in body
    assert 'href="/login"' in body
