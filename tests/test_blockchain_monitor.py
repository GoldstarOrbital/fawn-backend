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


class _FakeRPCClientEverythingFails:
    """Simulates an RPC where BOTH eth_getLogs AND eth_call fail -- e.g. a
    full outage, not just an archive-access restriction. Neither the event
    log path nor the balance-diff fallback can produce a trustworthy
    result this cycle."""

    def __init__(self, chain, latest_block):
        self.chain = chain
        self.latest_block = latest_block

    async def call(self, method, params):
        if method == "eth_blockNumber":
            return hex(self.latest_block)
        return None  # eth_getLogs and eth_call both fail


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
async def test_rpc_client_falls_through_to_next_endpoint_on_any_json_rpc_error(monkeypatch):
    # Reproduces a real production incident: Polygon scanning silently
    # stalled for ~20 hours because polygon-rpc.com (the first fallback)
    # started returning a JSON-RPC error whose message ("API key disabled,
    # reason: tenant disabled") didn't match the old whitelist of
    # retry-worthy substrings ("429"/"rate"/"archive"/"block range"). The
    # client gave up on the whole call instead of trying rpc.ankr.com /
    # 1rpc.io/matic, which were working fine. A fallback list is only
    # useful if ANY endpoint-level error moves on to the next endpoint.
    client = bm.RPCClient("polygon")
    assert len(client.endpoints) >= 2

    calls = []

    class _FakeResponse:
        def __init__(self, payload):
            self._payload = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self._payload

    class _FakeHttpxClient:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, json):
            calls.append(url)
            if url == client.endpoints[0]:
                return _FakeResponse({"error": {"message": "API key disabled, reason: tenant disabled"}})
            return _FakeResponse({"result": "0x64"})

    monkeypatch.setattr(bm.httpx, "AsyncClient", _FakeHttpxClient)

    result = await client.call("eth_blockNumber", [])
    assert result == "0x64"
    assert len(calls) >= 2  # actually fell through past the failing first endpoint


def test_polygon_fallbacks_do_not_depend_on_quota_exhausted_or_keyed_endpoints():
    endpoints = bm._get_rpc_endpoints("polygon")
    assert "https://polygon-bor-rpc.publicnode.com" in endpoints
    assert "https://polygon.drpc.org" in endpoints
    assert not any("1rpc.io" in endpoint or "rpc.ankr.com" in endpoint or "polygon-rpc.com" in endpoint for endpoint in endpoints)


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


@pytest.mark.asyncio
async def test_checkpoint_does_not_advance_when_logs_and_fallback_both_fail(monkeypatch):
    # Reproduces a real production bug: the checkpoint used to advance to
    # latest_block every cycle regardless of whether the scan actually
    # succeeded. If eth_getLogs failed AND the balance-diff fallback also
    # failed (e.g. eth_call failing too, not just an archive restriction),
    # the block range was still silently marked "scanned" -- permanently
    # hiding any deposit inside it from every future scan, since future
    # scans only look at blocks after the checkpoint. The checkpoint must
    # only advance when we can actually trust what we saw this cycle.
    db = SessionLocal()
    try:
        user = _make_user(db)

        _patch_chain(monkeypatch, "polygon", latest_block=100)
        _patch_chain(monkeypatch, "base", latest_block=100)
        await bm._scan_wallet_chain(user, "polygon", db)  # establishes checkpoint at block 100

        monkeypatch.setitem(
            bm._rpc_clients, "polygon",
            _FakeRPCClientEverythingFails("polygon", latest_block=200),
        )
        credited = await bm._scan_wallet_chain(user, "polygon", db)
        db.refresh(user)

        assert credited == 0
        assert user.usdc_balance_cents == 0  # nothing credited on a total failure

        checkpoint = db.query(ChainScanCheckpoint).filter(
            ChainScanCheckpoint.wallet_address == user.crypto_wallet_address,
            ChainScanCheckpoint.chain == "polygon",
        ).first()
        # Must stay at 100, NOT jump to 200 -- blocks 101-200 were never
        # actually seen and must be retried, not silently marked done.
        assert checkpoint.last_scanned_block == 100

        # Now the RPC recovers, and blocks 101-200 (including a real
        # deposit) get retried on the next cycle -- proving nothing was
        # permanently lost by the earlier total failure.
        sender = _random_address()
        contracts = bm.CHAINS["polygon"]["contracts"]
        log = _transfer_log(sender, user.crypto_wallet_address, 1_000_000, "0xrecovered1", 150)
        _patch_chain(monkeypatch, "polygon", latest_block=200, logs_by_contract={contracts["usdc_native"]: [log]})

        credited2 = await bm._scan_wallet_chain(user, "polygon", db)
        db.refresh(user)
        assert credited2 == 1
        assert user.usdc_balance_cents == 100
    finally:
        db.close()


@pytest.mark.asyncio
async def test_fallback_compares_per_chain_balance_not_total_wallet_balance(monkeypatch):
    # Reproduces the second half of the same production bug: the fallback
    # compared a single chain's raw on-chain balance against the user's
    # TOTAL balance across every chain. For a wallet with funds on more
    # than one chain, one chain's raw balance is routinely smaller than
    # the combined total, so the fallback's guard condition
    # (combined_balance > user.usdc_balance_cents) was permanently false
    # and a real gap on that chain could never be detected. Confirmed in
    # production: a real $1 Polygon deposit ($6.00 raw) went undetected
    # because it was never greater than the wallet's $13.01 total (mostly
    # from Base). The comparison must be scoped to what THIS chain has
    # actually contributed to the ledger, not the whole-wallet balance.
    contracts = bm.CHAINS["polygon"]["contracts"]
    db = SessionLocal()
    try:
        user = _make_user(db)
        # Simulate a wallet whose balance is mostly attributable to a
        # DIFFERENT chain (e.g. Base) -- total balance far exceeds what
        # Polygon alone has ever contributed.
        user.usdc_balance_cents = 1301
        db.commit()

        _patch_chain(monkeypatch, "polygon", latest_block=100)
        _patch_chain(monkeypatch, "base", latest_block=100)
        await bm._scan_wallet_chain(user, "polygon", db)  # establishes checkpoint, nothing credited yet

        # Polygon's raw on-chain balance ($6.00) is smaller than the
        # wallet's total ($13.01) but larger than what Polygon itself has
        # ever been credited for (still $0 -- this test's Polygon has
        # contributed nothing to the ledger yet).
        monkeypatch.setitem(
            bm._rpc_clients, "polygon",
            _FakeRPCClientLogsUnavailable("polygon", latest_block=200, balance_by_contract={
                contracts["usdc_native"]: 6_000_000,
                contracts["usdc_bridged"]: 0,
            }),
        )

        credited = await bm._scan_wallet_chain(user, "polygon", db)
        db.refresh(user)

        assert credited == 1
        assert user.usdc_balance_cents == 1301 + 600  # the real Polygon gap, credited on top of the existing total
    finally:
        db.close()


@pytest.mark.asyncio
async def test_fallback_does_not_recredit_pre_existing_chain_baseline(monkeypatch):
    # Reproduces a real production double-credit: a chain whose balance was
    # entirely established before CryptoDeposit tracking existed (e.g. via
    # a one-off manual reconciliation) has zero credited CryptoDeposit rows
    # for that chain. The FIRST time its balance-diff fallback ever fires,
    # comparing against "sum of credited CryptoDeposit rows" alone sees
    # $0 already credited and re-credits the chain's ENTIRE on-chain
    # balance as if none of it had ever been accounted for -- a real
    # incident credited a chain's full pre-existing balance a second time.
    # ChainScanCheckpoint.pre_ledger_baseline_cents exists to prevent
    # exactly this: it represents balance already reflected in the ledger
    # that predates CryptoDeposit tracking, and must be added to the
    # credited-rows sum before comparing against live on-chain balance.
    contracts = bm.CHAINS["base"]["contracts"]
    db = SessionLocal()
    try:
        user = _make_user(db)
        # This chain's $8.01 was already fully reflected in the ledger
        # long ago (e.g. a manual reconciliation), with no CryptoDeposit
        # rows to show for it -- exactly the real-world scenario.
        user.usdc_balance_cents = 801
        db.commit()

        _patch_chain(monkeypatch, "polygon", latest_block=100)
        _patch_chain(monkeypatch, "base", latest_block=100)
        await bm._scan_wallet_chain(user, "base", db)  # establishes checkpoint

        checkpoint = db.query(ChainScanCheckpoint).filter(
            ChainScanCheckpoint.wallet_address == user.crypto_wallet_address,
            ChainScanCheckpoint.chain == "base",
        ).first()
        checkpoint.pre_ledger_baseline_cents = 801  # seeded, as an admin would via /admin/set-chain-baseline
        db.commit()

        # Base's event logs become unreliable, but its on-chain balance is
        # UNCHANGED from what's already correctly reflected in the ledger.
        monkeypatch.setitem(
            bm._rpc_clients, "base",
            _FakeRPCClientLogsUnavailable("base", latest_block=200, balance_by_contract={
                contracts["usdc_native"]: 8_010_000,  # still $8.01, nothing new
                contracts["usdc_bridged"]: 0,
            }),
        )

        credited = await bm._scan_wallet_chain(user, "base", db)
        db.refresh(user)

        # Must NOT re-credit the pre-existing $8.01 a second time.
        assert credited == 0
        assert user.usdc_balance_cents == 801
    finally:
        db.close()
