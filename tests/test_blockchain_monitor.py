"""Tests for services/blockchain_monitor.py's event-log-based deposit detection.

Covers three real bugs found via live production deposit tests on 2026-07-16,
plus the redesign that followed:
1. Raw-units-to-cents conversion was off by 100x (divided by 10**6 instead
   of 10**4), silently undervaluing every on-chain USDC balance.
2. The monitor only watched the bridged USDC.e contract on Polygon, missing
   deposits sent as native USDC (what most modern senders actually send).
3. The monitor only watched Polygon at all -- a real Base deposit was
   completely invisible.
4. The original balanceOf()-diff design only moved a balance number with no
   record of WHERE a deposit came from. Redesigned around Transfer event
   logs (models.CryptoDeposit) so every credit is individually attributed
   to a real transaction: chain, sender address, tx hash, block number.
"""
import uuid

import pytest

from database import SessionLocal
from models import User, CryptoDeposit, ChainScanCheckpoint
from services import blockchain_monitor as bm


def _make_user(db):
    """Creates the user within the SAME session the test will use for
    scanning, so the returned object stays attached (not detached-then-
    reused across sessions, which SQLAlchemy rejects on refresh).

    Wallet address is randomly generated per call -- crypto_wallet_address
    is globally unique, and other test files in this suite create users
    with their own hardcoded test addresses (a fixed literal here collided
    with tests/test_onramp.py's fixture)."""
    user = User(
        email=f"bm_{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name="Blockchain Monitor Tester",
        is_student=True,
        crypto_wallet_address="0x" + uuid.uuid4().hex[:40].ljust(40, "0"),
        wallet_initialized=True,
        usdc_balance_cents=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _random_address():
    return "0x" + uuid.uuid4().hex[:40].ljust(40, "0")


def _transfer_log(from_addr, to_addr, amount_raw, tx_hash, block_number):
    padded_from = "0x" + "0" * 24 + from_addr.lower().replace("0x", "")
    padded_to = "0x" + "0" * 24 + to_addr.lower().replace("0x", "")
    return {
        "topics": [bm.TRANSFER_EVENT_TOPIC, padded_from, padded_to],
        "data": hex(amount_raw),
        "transactionHash": tx_hash,
        "blockNumber": hex(block_number),
    }


class _FakeRPCClient:
    """Fakes eth_blockNumber and eth_getLogs for one chain."""

    def __init__(self, chain, latest_block, logs_by_contract):
        self.chain = chain
        self.latest_block = latest_block
        self.logs_by_contract = logs_by_contract  # {contract: [log, ...]}

    async def call(self, method, params):
        if method == "eth_blockNumber":
            return hex(self.latest_block)
        if method == "eth_getLogs":
            contract = params[0]["address"]
            return self.logs_by_contract.get(contract, [])
        return None


class _FakeRPCClientLogsUnavailable:
    """Simulates an RPC where eth_getLogs always fails (e.g. an archive-
    access restriction) but eth_call (balanceOf) still works -- the exact
    scenario that motivated the balance-diff fallback."""

    def __init__(self, chain, latest_block, balance_by_contract):
        self.chain = chain
        self.latest_block = latest_block
        self.balance_by_contract = balance_by_contract

    async def call(self, method, params):
        if method == "eth_blockNumber":
            return hex(self.latest_block)
        if method == "eth_getLogs":
            return None  # simulates every window failing
        if method == "eth_call":
            contract = params[0]["to"]
            raw = self.balance_by_contract.get(contract, 0)
            return hex(raw)
        return None


def _patch_chain(monkeypatch, chain, latest_block, logs_by_contract=None):
    monkeypatch.setitem(
        bm._rpc_clients, chain,
        _FakeRPCClient(chain, latest_block, logs_by_contract or {}),
    )


@pytest.mark.asyncio
async def test_raw_units_to_cents_conversion_is_correct(monkeypatch):
    # 1.000000 USDC (6 decimals) = 1_000_000 raw units = $1.00 = 100 cents.
    contracts = bm.CHAINS["polygon"]["contracts"]
    db = SessionLocal()
    try:
        user = _make_user(db)

        # First scan establishes the checkpoint (backfill, empty logs here).
        _patch_chain(monkeypatch, "polygon", latest_block=100)
        _patch_chain(monkeypatch, "base", latest_block=100)
        await bm._scan_wallet_chain(user, "polygon", db)

        # Now a real deposit arrives.
        log = _transfer_log(_random_address(), user.crypto_wallet_address, 1_000_000, "0xaaa1", 150)
        _patch_chain(monkeypatch, "polygon", latest_block=200, logs_by_contract={contracts["usdc_native"]: [log]})

        credited = await bm._scan_wallet_chain(user, "polygon", db)
        db.refresh(user)
        assert credited == 1
        assert user.usdc_balance_cents == 100
    finally:
        db.close()


@pytest.mark.asyncio
async def test_deposit_is_recorded_with_source_attribution(monkeypatch):
    # This is the actual feature request: a deposit must be individually
    # recorded with chain, sender, and tx hash -- not just a balance bump.
    sender = _random_address()
    contracts = bm.CHAINS["base"]["contracts"]
    db = SessionLocal()
    try:
        user = _make_user(db)

        _patch_chain(monkeypatch, "base", latest_block=1000)
        _patch_chain(monkeypatch, "polygon", latest_block=1000)
        await bm._scan_wallet_chain(user, "base", db)  # establishes checkpoint

        log = _transfer_log(sender, user.crypto_wallet_address, 5_000_000, "0xdeadbeef", 1500)
        _patch_chain(monkeypatch, "base", latest_block=2000, logs_by_contract={contracts["usdc_native"]: [log]})
        await bm._scan_wallet_chain(user, "base", db)

        record = db.query(CryptoDeposit).filter(
            CryptoDeposit.user_id == user.id, CryptoDeposit.tx_hash == "0xdeadbeef"
        ).first()
        assert record is not None
        assert record.chain == "base"
        assert record.from_address.lower() == sender.lower()
        assert record.amount_cents == 500
        assert record.credited_to_ledger is True
    finally:
        db.close()


@pytest.mark.asyncio
async def test_duplicate_transfer_is_not_double_credited(monkeypatch):
    # Same tx_hash appearing again in a later scan window must not re-credit.
    contracts = bm.CHAINS["polygon"]["contracts"]
    db = SessionLocal()
    try:
        user = _make_user(db)

        _patch_chain(monkeypatch, "polygon", latest_block=50)
        _patch_chain(monkeypatch, "base", latest_block=50)
        await bm._scan_wallet_chain(user, "polygon", db)  # establishes checkpoint

        log = _transfer_log(_random_address(), user.crypto_wallet_address, 1_000_000, "0xrepeat", 75)
        _patch_chain(monkeypatch, "polygon", latest_block=100, logs_by_contract={contracts["usdc_native"]: [log]})

        first = await bm._scan_wallet_chain(user, "polygon", db)
        assert first == 1

        # Simulate the checkpoint not having advanced past this block yet
        # (re-running the same window) -- dedupe must catch it.
        checkpoint = db.query(ChainScanCheckpoint).filter(
            ChainScanCheckpoint.wallet_address == user.crypto_wallet_address,
            ChainScanCheckpoint.chain == "polygon",
        ).first()
        checkpoint.last_scanned_block = 74  # rewind so the same block gets rescanned
        db.commit()

        second = await bm._scan_wallet_chain(user, "polygon", db)
        db.refresh(user)
        assert second == 0  # already recorded, not credited again
        assert user.usdc_balance_cents == 100  # unchanged
    finally:
        db.close()


@pytest.mark.asyncio
async def test_first_scan_backfills_without_double_crediting(monkeypatch):
    # A wallet with no checkpoint yet (first time scanned) should record
    # historical deposits for visibility but NOT credit them -- they're
    # presumed already reflected in the current balance from the old
    # balance-diff system.
    contracts = bm.CHAINS["polygon"]["contracts"]
    db = SessionLocal()
    try:
        user = _make_user(db)
        user.usdc_balance_cents = 300  # pre-existing balance from old system
        db.commit()

        log = _transfer_log(_random_address(), user.crypto_wallet_address, 3_000_000, "0xhistoric", 42)
        _patch_chain(monkeypatch, "polygon", latest_block=100, logs_by_contract={contracts["usdc_native"]: [log]})
        _patch_chain(monkeypatch, "base", latest_block=100)

        credited = await bm._scan_wallet_chain(user, "polygon", db)
        db.refresh(user)

        assert credited == 0  # backfill pass credits nothing
        assert user.usdc_balance_cents == 300  # unchanged

        record = db.query(CryptoDeposit).filter(CryptoDeposit.user_id == user.id).first()
        assert record is not None
        assert record.credited_to_ledger is False  # recorded for visibility only

        checkpoint = db.query(ChainScanCheckpoint).filter(
            ChainScanCheckpoint.wallet_address == user.crypto_wallet_address,
            ChainScanCheckpoint.chain == "polygon",
        ).first()
        assert checkpoint.is_backfilled is True
    finally:
        db.close()


@pytest.mark.asyncio
async def test_second_scan_after_backfill_credits_new_deposits(monkeypatch):
    contracts = bm.CHAINS["polygon"]["contracts"]

    db = SessionLocal()
    try:
        user = _make_user(db)

        # First scan: empty, just establishes the checkpoint.
        _patch_chain(monkeypatch, "polygon", latest_block=100)
        _patch_chain(monkeypatch, "base", latest_block=100)
        await bm._scan_wallet_chain(user, "polygon", db)

        # Second scan: a genuinely new deposit arrives after the checkpoint.
        log = _transfer_log(_random_address(), user.crypto_wallet_address, 2_000_000, "0xnew1", 150)
        _patch_chain(monkeypatch, "polygon", latest_block=200, logs_by_contract={contracts["usdc_native"]: [log]})

        credited = await bm._scan_wallet_chain(user, "polygon", db)
        db.refresh(user)

        assert credited == 1
        assert user.usdc_balance_cents == 200
    finally:
        db.close()


@pytest.mark.asyncio
async def test_falls_back_to_balance_diff_when_event_logs_are_unreliable(monkeypatch):
    # Reproduces a real risk found while shipping this: if eth_getLogs
    # fails on every endpoint (e.g. an RPC's archive-access restriction),
    # a real deposit must still be detected and credited -- just without
    # per-transfer attribution -- rather than silently missed. This is
    # exactly the failure mode that caused the original production bug.
    contracts = bm.CHAINS["polygon"]["contracts"]
    db = SessionLocal()
    try:
        user = _make_user(db)

        # Establish a checkpoint with a normal (working) client first.
        _patch_chain(monkeypatch, "polygon", latest_block=100)
        _patch_chain(monkeypatch, "base", latest_block=100)
        await bm._scan_wallet_chain(user, "polygon", db)

        # Now getLogs is broken, but the wallet genuinely has $3.00 on-chain.
        monkeypatch.setitem(
            bm._rpc_clients, "polygon",
            _FakeRPCClientLogsUnavailable("polygon", latest_block=200, balance_by_contract={
                contracts["usdc_native"]: 3_000_000,
                contracts["usdc_bridged"]: 0,
            }),
        )

        credited = await bm._scan_wallet_chain(user, "polygon", db)
        db.refresh(user)

        assert credited == 1
        assert user.usdc_balance_cents == 300

        fallback_record = db.query(CryptoDeposit).filter(
            CryptoDeposit.user_id == user.id,
            CryptoDeposit.contract_address == "multiple",
        ).first()
        assert fallback_record is not None
        assert fallback_record.amount_cents == 300
    finally:
        db.close()
