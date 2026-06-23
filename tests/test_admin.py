"""Tests for admin-key protected endpoints."""


def test_admin_endpoint_without_key_403(client):
    resp = client.get("/admin/waitlist")
    assert resp.status_code == 403


def test_admin_endpoint_with_wrong_key_403(client):
    resp = client.get("/admin/waitlist", headers={"X-Admin-Key": "wrong-key"})
    assert resp.status_code == 403


def test_admin_endpoint_with_correct_key_200(client, admin_key):
    resp = client.get("/admin/waitlist", headers={"X-Admin-Key": admin_key})
    assert resp.status_code == 200


def test_deals_admin_endpoint_without_key_403(client):
    resp = client.get("/deals/suggestions")
    assert resp.status_code == 403


def test_deals_admin_endpoint_with_wrong_key_403(client):
    resp = client.get("/deals/suggestions", headers={"X-Admin-Key": "wrong-key"})
    assert resp.status_code == 403
