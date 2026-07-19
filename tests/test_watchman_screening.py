"""Tests for services/watchman_screening.py -- optional supplementary
sanctions screening via a self-hosted Moov Watchman instance. Additive
to services/sanctions_screening.py's self-built OFAC scraper, not a
replacement, and a clean no-op until WATCHMAN_URL is configured -- these
tests verify both the disabled-by-default state and the behavior once
"deployed" (mocked HTTP responses, no real Watchman instance needed)."""
import uuid

import httpx
import pytest

from config import settings
from database import SessionLocal
from models import SanctionedAddress, UserAuditLog, User
from services import sanctions_screening as screening
from services import watchman_screening


def _make_user(db):
    user = User(
        email=f"watchman_{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name="Watchman Tester",
        is_student=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


class _FakeResponse:
    def __init__(self, json_body):
        self._json = json_body
    def raise_for_status(self):
        pass
    def json(self):
        return self._json


def _fake_httpx_client(json_body=None, raise_exc=None):
    class _Client:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, params=None):
            if raise_exc:
                raise raise_exc
            return _FakeResponse(json_body)
    return _Client


@pytest.mark.asyncio
async def test_returns_none_when_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "watchman_url", "")
    # No AsyncClient mock needed -- must not even attempt a network call.
    monkeypatch.setattr(watchman_screening.httpx, "AsyncClient", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not be called")))
    result = await watchman_screening.check_address_against_watchman("0x" + "1" * 40)
    assert result is None


@pytest.mark.asyncio
async def test_returns_false_when_configured_and_no_match(monkeypatch):
    monkeypatch.setattr(settings, "watchman_url", "http://fake-watchman:8084")
    monkeypatch.setattr(watchman_screening.httpx, "AsyncClient", _fake_httpx_client(json_body={"entities": []}))
    result = await watchman_screening.check_address_against_watchman("0x" + "1" * 40)
    assert result is False


@pytest.mark.asyncio
async def test_returns_true_on_high_confidence_match(monkeypatch):
    monkeypatch.setattr(settings, "watchman_url", "http://fake-watchman:8084")
    monkeypatch.setattr(watchman_screening.httpx, "AsyncClient", _fake_httpx_client(
        json_body={"entities": [{"name": "Test Entity", "match": 0.98}]}
    ))
    result = await watchman_screening.check_address_against_watchman("0x" + "1" * 40)
    assert result is True


@pytest.mark.asyncio
async def test_returns_false_on_low_confidence_match(monkeypatch):
    # Below MIN_MATCH_SCORE -- a weak fuzzy match shouldn't block a send.
    monkeypatch.setattr(settings, "watchman_url", "http://fake-watchman:8084")
    monkeypatch.setattr(watchman_screening.httpx, "AsyncClient", _fake_httpx_client(
        json_body={"entities": [{"name": "Loosely Similar", "match": 0.60}]}
    ))
    result = await watchman_screening.check_address_against_watchman("0x" + "1" * 40)
    assert result is False


@pytest.mark.asyncio
async def test_fails_open_on_lookup_failure(monkeypatch):
    monkeypatch.setattr(settings, "watchman_url", "http://fake-watchman:8084")
    monkeypatch.setattr(watchman_screening.httpx, "AsyncClient", _fake_httpx_client(raise_exc=httpx.ConnectError("down")))
    result = await watchman_screening.check_address_against_watchman("0x" + "1" * 40)
    assert result is None


@pytest.mark.asyncio
async def test_watchman_flag_blocks_send_even_when_ofac_list_is_clean(monkeypatch):
    # Integration: an address absent from the local OFAC table (never
    # flagged by services/sanctions_screening.py's own scraper) must
    # still be blocked if Watchman's broader list set flags it.
    monkeypatch.setattr(settings, "watchman_url", "http://fake-watchman:8084")
    monkeypatch.setattr(watchman_screening.httpx, "AsyncClient", _fake_httpx_client(
        json_body={"entities": [{"name": "UN-listed entity", "match": 0.99}]}
    ))

    db = SessionLocal()
    try:
        user = _make_user(db)
        recipient = "0x" + "3" * 40
        # Confirm it's genuinely absent from the local OFAC table.
        assert screening.is_sanctioned(recipient, db) is False

        with pytest.raises(screening.RecipientSanctioned):
            await screening.check_recipient_not_sanctioned(user.id, recipient, db)

        log = db.query(UserAuditLog).filter(
            UserAuditLog.user_id == user.id,
            UserAuditLog.action == "send_blocked_sanctioned_recipient",
        ).first()
        assert log is not None
        assert '"source": "watchman"' in log.details
    finally:
        db.close()
