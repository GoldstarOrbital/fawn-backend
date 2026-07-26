"""Tests for the live rates / markets hub service (services/rates.py).

These exercise the SERVICE functions directly (the router is intentionally
not registered yet). Every test monkeypatches services.rates._fetch_prices_
from_coingecko so NO real network call is ever made, and the in-process price
cache is cleared before each test for determinism.

Nothing here moves money: rates is read-only market data plus a tracking-only
audit-log breadcrumb. One test explicitly asserts the user's balance is
untouched by the tracking write.
"""
import uuid

import pytest

from database import SessionLocal
from models import User, UserAuditLog
from services import rates as rates_service


FAKE_PRICES = {
    "btc": 60000.0,
    "eth": 3000.0,
    "usdc": 1.0,
    "matic": 0.50,
    "usdt": 1.0,
    "sol": 150.0,
}


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset the in-process price cache before and after every test."""
    rates_service._PRICE_CACHE.clear()
    yield
    rates_service._PRICE_CACHE.clear()


def _patch_fetch(monkeypatch, prices=None, calls=None):
    """Patch the CoinGecko fetch to return canned prices (no network)."""
    def fake():
        if calls is not None:
            calls.append(1)
        return dict(prices if prices is not None else FAKE_PRICES)
    monkeypatch.setattr(rates_service, "_fetch_prices_from_coingecko", fake)


def _make_user(db):
    user = User(
        email=f"rates_{uuid.uuid4().hex[:10]}@example.com",
        hashed_password="x",
        full_name="Rates Tester",
        is_student=True,
        usdc_balance_cents=5000,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


# ── get_crypto_prices ──

def test_get_crypto_prices_returns_available_prices(monkeypatch):
    _patch_fetch(monkeypatch)
    snap = rates_service.get_crypto_prices()
    assert snap["available"] is True
    assert snap["prices"]["btc"] == 60000.0
    assert snap["prices"]["usdc"] == 1.0
    assert snap["source"] == "coingecko"
    assert snap["as_of"] is not None
    assert snap["cached"] is False
    assert snap["stale"] is False


def test_get_crypto_prices_caches_within_ttl(monkeypatch):
    calls = []
    _patch_fetch(monkeypatch, calls=calls)

    first = rates_service.get_crypto_prices()
    second = rates_service.get_crypto_prices()

    assert first["cached"] is False
    assert second["cached"] is True          # served from cache
    assert len(calls) == 1                    # only ONE network fetch
    assert second["prices"] == first["prices"]


def test_get_crypto_prices_force_refresh_bypasses_cache(monkeypatch):
    calls = []
    _patch_fetch(monkeypatch, calls=calls)
    rates_service.get_crypto_prices()
    rates_service.get_crypto_prices(force_refresh=True)
    assert len(calls) == 2


def test_get_crypto_prices_unavailable_when_no_cache_and_fetch_fails(monkeypatch):
    def boom():
        raise RuntimeError("network down")
    monkeypatch.setattr(rates_service, "_fetch_prices_from_coingecko", boom)

    snap = rates_service.get_crypto_prices()
    assert snap["available"] is False
    assert snap["prices"] == {}
    assert snap["as_of"] is None


def test_get_crypto_prices_serves_stale_cache_on_failure(monkeypatch):
    # Prime the cache with a good fetch...
    _patch_fetch(monkeypatch)
    good = rates_service.get_crypto_prices()
    assert good["available"] is True

    # ...then expire it and make the next fetch fail; stale cache is served.
    rates_service._PRICE_CACHE["expires_at"] = 0

    def boom():
        raise RuntimeError("network down")
    monkeypatch.setattr(rates_service, "_fetch_prices_from_coingecko", boom)

    snap = rates_service.get_crypto_prices()
    assert snap["available"] is True
    assert snap["stale"] is True
    assert snap["prices"]["btc"] == 60000.0


# ── convert ──

def test_convert_crypto_to_usd(monkeypatch):
    _patch_fetch(monkeypatch)
    res = rates_service.convert(2, "btc", "usd")
    assert res["available"] is True
    assert res["converted_amount"] == 120000.0   # 2 * 60000
    assert res["rate"] == 60000.0
    assert res["usd_value"] == 120000.0


def test_convert_usd_to_crypto(monkeypatch):
    _patch_fetch(monkeypatch)
    res = rates_service.convert(3000, "usd", "eth")
    assert res["converted_amount"] == 1.0        # 3000 USD / 3000 per ETH


def test_convert_crypto_to_crypto(monkeypatch):
    _patch_fetch(monkeypatch)
    res = rates_service.convert(1, "btc", "eth")
    assert res["converted_amount"] == 20.0       # 60000 / 3000
    assert res["rate"] == 20.0


def test_convert_is_case_insensitive(monkeypatch):
    _patch_fetch(monkeypatch)
    res = rates_service.convert(1, "BTC", "USD")
    assert res["converted_amount"] == 60000.0


def test_convert_unknown_symbol_raises(monkeypatch):
    _patch_fetch(monkeypatch)
    with pytest.raises(ValueError):
        rates_service.convert(1, "btc", "doge")


def test_convert_negative_amount_raises(monkeypatch):
    _patch_fetch(monkeypatch)
    with pytest.raises(ValueError):
        rates_service.convert(-5, "btc", "usd")


def test_convert_unavailable_when_prices_down(monkeypatch):
    def boom():
        raise RuntimeError("network down")
    monkeypatch.setattr(rates_service, "_fetch_prices_from_coingecko", boom)
    res = rates_service.convert(1, "btc", "usd")
    assert res["available"] is False


# ── tracking-only persistence via UserAuditLog ──

def test_log_and_read_back_conversion_history(monkeypatch):
    _patch_fetch(monkeypatch)
    db = SessionLocal()
    try:
        user = _make_user(db)
        balance_before = user.usdc_balance_cents

        res = rates_service.convert(1, "btc", "usd")
        rates_service.log_conversion(db, user.id, res)

        history = rates_service.get_conversion_history(db, user.id)
        assert history["count"] == 1
        entry = history["conversions"][0]
        assert entry["from"] == "btc"
        assert entry["to"] == "usd"
        assert entry["converted_amount"] == 60000.0

        # Persisted to UserAuditLog with the expected action...
        logs = db.query(UserAuditLog).filter(
            UserAuditLog.user_id == user.id,
            UserAuditLog.action == rates_service.CONVERSION_ACTION,
        ).all()
        assert len(logs) == 1

        # ...and NO money moved as a result of the tracking write.
        db.refresh(user)
        assert user.usdc_balance_cents == balance_before
    finally:
        db.close()


def test_log_conversion_noop_for_unavailable(monkeypatch):
    db = SessionLocal()
    try:
        user = _make_user(db)
        rates_service.log_conversion(db, user.id, {"available": False})
        history = rates_service.get_conversion_history(db, user.id)
        assert history["count"] == 0
    finally:
        db.close()
