"""Tests for services/address_risk.py -- GoPlus malicious-address risk
scoring. A fraud SIGNAL, not a legal mandate like OFAC screening -- a
flagged recipient routes to the review-hold queue rather than being
hard-blocked, and any lookup failure fails open (never blocks a
legitimate send over a third party's downtime)."""
import uuid

import httpx
import pytest

from database import SessionLocal
from models import User, UserAuditLog
from services import address_risk


def _make_user(db):
    user = User(
        email=f"addrrisk_{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name="Address Risk Tester",
        is_student=True,
        crypto_wallet_address="0x" + uuid.uuid4().hex[:40].ljust(40, "0"),
        wallet_initialized=True,
        usdc_balance_cents=100_000,
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


CLEAN_RESULT = {"code": 1, "message": "ok", "result": {f: "0" for f in address_risk.RISK_FLAGS} | {"data_source": ""}}
FLAGGED_RESULT = {"code": 1, "message": "ok", "result": {f: "0" for f in address_risk.RISK_FLAGS} | {"stealing_attack": "1", "data_source": "SlowMist"}}


@pytest.mark.asyncio
async def test_clean_address_not_flagged(monkeypatch):
    monkeypatch.setattr(address_risk.httpx, "AsyncClient", _fake_httpx_client(json_body=CLEAN_RESULT))
    risk = await address_risk.check_address_risk("0x" + "1" * 40)
    assert risk["flagged"] is False
    assert risk["reasons"] == []


@pytest.mark.asyncio
async def test_flagged_address_detected_with_reason(monkeypatch):
    monkeypatch.setattr(address_risk.httpx, "AsyncClient", _fake_httpx_client(json_body=FLAGGED_RESULT))
    risk = await address_risk.check_address_risk("0x" + "1" * 40)
    assert risk["flagged"] is True
    assert "stealing_attack" in risk["reasons"]
    assert risk["data_source"] == "SlowMist"


@pytest.mark.asyncio
async def test_lookup_failure_returns_none_not_false(monkeypatch):
    # None (unknown) must be distinguishable from a confirmed-clean result
    # -- callers that conflate "lookup failed" with "confirmed clean"
    # would silently skip real flags whenever the API is unreachable.
    monkeypatch.setattr(address_risk.httpx, "AsyncClient", _fake_httpx_client(raise_exc=httpx.ConnectError("down")))
    risk = await address_risk.check_address_risk("0x" + "1" * 40)
    assert risk is None


@pytest.mark.asyncio
async def test_flag_if_risky_fails_open_on_lookup_failure(monkeypatch):
    monkeypatch.setattr(address_risk.httpx, "AsyncClient", _fake_httpx_client(raise_exc=httpx.ConnectError("down")))
    db = SessionLocal()
    try:
        user = _make_user(db)
        result = await address_risk.flag_if_risky_for_review(user.id, "0x" + "1" * 40, db)
        assert result is False  # fails open -- does not block/hold on unknown status
    finally:
        db.close()


@pytest.mark.asyncio
async def test_flag_if_risky_logs_audit_entry_when_flagged(monkeypatch):
    monkeypatch.setattr(address_risk.httpx, "AsyncClient", _fake_httpx_client(json_body=FLAGGED_RESULT))
    db = SessionLocal()
    try:
        user = _make_user(db)
        recipient = "0x" + "1" * 40
        result = await address_risk.flag_if_risky_for_review(user.id, recipient, db)
        assert result is True

        log = db.query(UserAuditLog).filter(
            UserAuditLog.user_id == user.id,
            UserAuditLog.action == "recipient_flagged_by_address_risk_check",
        ).first()
        assert log is not None
        assert recipient in log.details
        assert "stealing_attack" in log.details
    finally:
        db.close()


@pytest.mark.asyncio
async def test_flag_if_risky_no_audit_entry_when_clean(monkeypatch):
    monkeypatch.setattr(address_risk.httpx, "AsyncClient", _fake_httpx_client(json_body=CLEAN_RESULT))
    db = SessionLocal()
    try:
        user = _make_user(db)
        result = await address_risk.flag_if_risky_for_review(user.id, "0x" + "1" * 40, db)
        assert result is False

        log = db.query(UserAuditLog).filter(
            UserAuditLog.user_id == user.id,
            UserAuditLog.action == "recipient_flagged_by_address_risk_check",
        ).first()
        assert log is None
    finally:
        db.close()
