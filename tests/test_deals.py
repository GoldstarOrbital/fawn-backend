"""Tests for /deals/schools endpoints."""


def test_unknown_school_404(client):
    resp = client.get("/deals/schools/invalidkey")
    assert resp.status_code == 404


def test_known_school_200_with_categories(client):
    resp = client.get("/deals/schools/berkeley")
    assert resp.status_code == 200
    body = resp.json()
    assert "categories" in body
