"""Tests for services/sanctions_screening.py -- OFAC SDN address screening.

Compliance groundwork: sends to a recipient address on OFAC's sanctions
list must be blocked before any money moves. Extraction correctness is
tested against a synthetic CSV snippet shaped exactly like the real OFAC
export (verified by hand against a live fetch while building this) --
no real network calls in these tests.
"""
import uuid

import httpx
import pytest

from database import SessionLocal
from models import User, SanctionedAddress, SanctionsListRefresh, UserAuditLog
from services import sanctions_screening as screening

SANCTIONED_ADDRESS = "0x038989cbb1710c72b9920dc4fa529158f463e72c"

# Shaped like a real OFAC SDN CSV row -- multiple "alt." addresses in one
# remarks field, plus a longer hex run immediately after a valid address
# (regression case for the truncation bug caught while building this:
# without a negative lookahead, a regex capturing exactly 40 hex chars
# would silently grab the first 40 of a longer run instead of the real
# 40-char address). Only used by pure-extraction tests (no DB writes),
# so the hardcoded address is safe to reuse across those.
SAMPLE_CSV = (
    '25308,"YAN, Xiaobing","individual","SDNTK",-0-,-0-,-0-,-0-,-0-,-0-,-0-,'
    '"Digital Currency Address - ETH 0x038989cbb1710c72b9920dc4fa529158f463e72c; '
    'alt. Digital Currency Address - ETH 0x0330070FD38Ec3bB94F58FA55D40368271E9e54A; '
    'Digital Currency Address - XBT 12QtD5BFwRsdNsAZY76UVE1xyCGNTojH9h."\n'
)


def _random_address():
    """SanctionedAddress.address is globally unique -- every test that
    inserts a row needs its own address to avoid colliding with other
    tests in this file when they share a session/DB."""
    return "0x" + uuid.uuid4().hex[:40].ljust(40, "0")


def _make_user(db):
    user = User(
        email=f"sanctions_{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name="Sanctions Screening Tester",
        is_student=True,
        crypto_wallet_address="0x" + uuid.uuid4().hex[:40].ljust(40, "0"),
        wallet_type="fawn_custodial",
        wallet_initialized=True,
        usdc_balance_cents=100_000,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_extract_evm_addresses_from_real_shaped_csv():
    addresses = screening._extract_evm_addresses(SAMPLE_CSV)
    addr_map = dict(addresses)

    assert "0x038989cbb1710c72b9920dc4fa529158f463e72c" in addr_map
    assert "0x0330070fd38ec3bb94f58fa55d40368271e9e54a" in addr_map  # lowercased
    assert addr_map["0x038989cbb1710c72b9920dc4fa529158f463e72c"] == "ETH"
    # The Bitcoin-format address must NOT be captured -- only 0x-format.
    assert not any("12QtD5BFwRsdNsAZY76UVE1xyCGNTojH9h" in a for a, _ in addresses)


def test_extract_evm_addresses_does_not_truncate_adjacent_hex():
    # Regression: a naive {40}-char regex with no boundary check would
    # match the FIRST 40 hex chars of a 41+ char run and silently produce
    # a wrong address. Build a case where a valid 40-char address is
    # immediately followed by more hex characters (no separator).
    csv_text = 'Digital Currency Address - ETH 0x' + 'a' * 40 + 'bb; more text'
    addresses = screening._extract_evm_addresses(csv_text)
    # Must find NOTHING here -- 0x + 42 hex chars is not a valid 40-char
    # match with a clean boundary, so this should be correctly rejected
    # rather than matched-and-truncated.
    assert addresses == []


def test_is_sanctioned_true_and_false():
    db = SessionLocal()
    try:
        sanctioned = _random_address()
        db.add(SanctionedAddress(address=sanctioned, currency_label="ETH"))
        db.commit()

        assert screening.is_sanctioned(sanctioned, db) is True
        assert screening.is_sanctioned(sanctioned.upper(), db) is True  # case-insensitive
        assert screening.is_sanctioned(_random_address(), db) is False
        assert screening.is_sanctioned(None, db) is False
    finally:
        db.close()


def test_check_recipient_not_sanctioned_blocks_and_logs():
    db = SessionLocal()
    try:
        sanctioned = _random_address()
        db.add(SanctionedAddress(address=sanctioned, currency_label="ETH"))
        db.commit()
        user = _make_user(db)

        with pytest.raises(screening.RecipientSanctioned):
            screening.check_recipient_not_sanctioned(user.id, sanctioned, db)

        log = db.query(UserAuditLog).filter(
            UserAuditLog.user_id == user.id,
            UserAuditLog.action == "send_blocked_sanctioned_recipient",
        ).first()
        assert log is not None
        assert sanctioned in log.details
    finally:
        db.close()


def test_check_recipient_not_sanctioned_allows_clean_address():
    db = SessionLocal()
    try:
        user = _make_user(db)
        # Should not raise.
        screening.check_recipient_not_sanctioned(user.id, "0x" + "1" * 40, db)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_refresh_sanctions_list_success(monkeypatch):
    addr1, addr2 = _random_address(), _random_address()
    csv_text = (
        '25308,"Test Entity","individual","SDNTK",-0-,-0-,-0-,-0-,-0-,-0-,-0-,'
        f'"Digital Currency Address - ETH {addr1}; '
        f'alt. Digital Currency Address - ETH {addr2}; '
        'Digital Currency Address - XBT 12QtD5BFwRsdNsAZY76UVE1xyCGNTojH9h."\n'
    )

    class _FakeResponse:
        def __init__(self, text):
            self.text = text
        def raise_for_status(self):
            pass

    class _FakeHttpxClient:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url):
            return _FakeResponse(csv_text)

    monkeypatch.setattr(screening.httpx, "AsyncClient", _FakeHttpxClient)

    db = SessionLocal()
    try:
        result = await screening.refresh_sanctions_list(db)
        assert result["status"] == "success"
        assert result["addresses_found"] == 2  # two 0x-format addresses in the sample

        stored = db.query(SanctionedAddress).filter(
            SanctionedAddress.address == addr1.lower()
        ).first()
        assert stored is not None

        refresh_log = db.query(SanctionsListRefresh).order_by(SanctionsListRefresh.created_at.desc()).first()
        assert refresh_log.status == "success"
        assert refresh_log.addresses_found == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_refresh_sanctions_list_failure_does_not_crash(monkeypatch):
    class _FakeHttpxClient:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url):
            raise httpx.ConnectError("simulated network failure")

    monkeypatch.setattr(screening.httpx, "AsyncClient", _FakeHttpxClient)

    db = SessionLocal()
    try:
        result = await screening.refresh_sanctions_list(db)
        assert result["status"] == "failed"

        refresh_log = db.query(SanctionsListRefresh).order_by(SanctionsListRefresh.created_at.desc()).first()
        assert refresh_log.status == "failed"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_refresh_does_not_duplicate_an_address_seen_across_two_refreshes(monkeypatch):
    # The real regression this guards: refresh_sanctions_list runs on a
    # recurring loop, so the SAME address will be seen again on every
    # future refresh. It must update the existing row, not insert a
    # second one (which would violate the unique constraint or, worse,
    # silently succeed with duplicate rows if the constraint were ever
    # relaxed).
    addr = _random_address()
    csv_text = f'"Digital Currency Address - ETH {addr}."\n'

    class _FakeResponse:
        def __init__(self, text):
            self.text = text
        def raise_for_status(self):
            pass

    class _FakeHttpxClient:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url):
            return _FakeResponse(csv_text)

    monkeypatch.setattr(screening.httpx, "AsyncClient", _FakeHttpxClient)

    db = SessionLocal()
    try:
        await screening.refresh_sanctions_list(db)
        await screening.refresh_sanctions_list(db)  # same address, second refresh

        count = db.query(SanctionedAddress).filter(SanctionedAddress.address == addr.lower()).count()
        assert count == 1
    finally:
        db.close()
