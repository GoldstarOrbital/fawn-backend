"""Tests for GET /admin/watchman-status."""
import os

from config import settings


def _admin_get(path):
    from main import app
    from starlette.testclient import TestClient
    client = TestClient(app)
    return client.get(path, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]})


def test_reports_not_configured_when_watchman_url_unset(monkeypatch):
    monkeypatch.setattr(settings, "watchman_url", "")
    resp = _admin_get("/admin/watchman-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is False
    assert body["reachable"] is None


def test_reports_reachable_when_configured_and_lookup_succeeds(monkeypatch):
    from services import watchman_screening

    monkeypatch.setattr(settings, "watchman_url", "http://fake-watchman:8084")

    class _FakeResponse:
        def raise_for_status(self):
            pass
        def json(self):
            return {"entities": []}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, params=None):
            return _FakeResponse()

    monkeypatch.setattr(watchman_screening.httpx, "AsyncClient", _FakeClient)

    resp = _admin_get("/admin/watchman-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["reachable"] is True


def test_requires_admin_key():
    from main import app
    from starlette.testclient import TestClient
    client = TestClient(app)
    resp = client.get("/admin/watchman-status")
    assert resp.status_code == 403
