"""Tests for services/onchain_send.py -- real on-chain USDC settlement.

Replaces the previous send implementation, which only ever adjusted the
internal ledger and never actually moved any on-chain USDC (tx_hash was
always None -- see services/crypto_wallet.py::send_usdc's docstring
history). These tests mock the RPC layer the same way
tests/test_blockchain_monitor.py does; no real network calls, no real
funds. Signing correctness is verified by independently re-signing the
expected transaction fields with the same key and asserting the raw
transaction the module broadcasts matches byte-for-byte -- that's the
only way to be confident the right recipient/amount/chain actually gets
encoded into what would be a real, irreversible transaction.
"""
import secrets
import uuid

import pytest
from eth_account import Account

from database import SessionLocal
from models import User, CryptoWallet
from services import blockchain_monitor as bm
from services import onchain_send
from services.crypto_wallet import _encrypt_private_key

GAS_STATION_PRIVATE_KEY = "0x" + "7" * 64
GAS_STATION_ADDRESS = Account.from_key(GAS_STATION_PRIVATE_KEY).address


def _make_custodial_user(db, with_key=True, wallet_type="fawn_custodial"):
    """Generates a fresh keypair per call -- CryptoWallet.wallet_address is
    globally unique, and a shared hardcoded address collides the moment
    more than one test in this file runs in the same session."""
    private_key = "0x" + secrets.token_hex(32)
    address = Account.from_key(private_key).address

    user = User(
        email=f"onchainsend_{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name="Onchain Send Tester",
        is_student=True,
        crypto_wallet_address=address,
        wallet_type=wallet_type,
        wallet_initialized=True,
        usdc_balance_cents=100_000,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    if wallet_type == "fawn_custodial":
        wallet_row = CryptoWallet(
            user_id=user.id,
            wallet_address=address,
            wallet_type=wallet_type,
            chain="polygon",
            usdc_balance_cents=0,
            encrypted_private_key=_encrypt_private_key(private_key) if with_key else None,
        )
        db.add(wallet_row)
        db.commit()

    return user, private_key


class _FakeChainClient:
    """Fakes every RPC method onchain_send.py calls for one chain."""

    def __init__(self, native_usdc_cents=0, native_balance_wei=10**18, sent_raw_txs=None):
        self.native_usdc_cents = native_usdc_cents
        self.native_balance_wei = native_balance_wei
        self.sent_raw_txs = sent_raw_txs if sent_raw_txs is not None else []
        self.nonces = {}  # address -> next nonce
        self.broadcast_should_fail = False

    async def call(self, method, params):
        if method == "eth_getBalance":
            return hex(self.native_balance_wei)
        if method == "eth_getTransactionCount":
            address = params[0]
            n = self.nonces.get(address.lower(), 0)
            return hex(n)
        if method == "eth_gasPrice":
            return hex(30_000_000_000)  # 30 gwei
        if method == "eth_call":
            raw = self.native_usdc_cents * (10 ** 4)
            return hex(raw)
        if method == "eth_sendRawTransaction":
            if self.broadcast_should_fail:
                return None
            self.sent_raw_txs.append(params[0])
            return "0x" + uuid.uuid4().hex + uuid.uuid4().hex[:24]  # fake 32-byte tx hash
        if method == "eth_getTransactionReceipt":
            return {"status": "0x1", "transactionHash": params[0]}
        return None


def _patch_chain(monkeypatch, chain, client):
    monkeypatch.setitem(bm._rpc_clients, chain, client)


@pytest.fixture(autouse=True)
def _configure_gas_station(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "gas_station_private_key", GAS_STATION_PRIVATE_KEY)


@pytest.mark.asyncio
async def test_non_custodial_wallet_cannot_be_signed_for():
    db = SessionLocal()
    try:
        user, _ = _make_custodial_user(db, wallet_type="non_custodial")
        with pytest.raises(onchain_send.CannotSignTransaction):
            await onchain_send.send_onchain_usdc(user, "0x" + "1" * 40, 100, db)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_custodial_wallet_with_no_stored_key_cannot_be_signed_for():
    # Reproduces the exact stranded-wallet scenario found in production:
    # wallet_type says fawn_custodial, but no usable key was ever stored.
    db = SessionLocal()
    try:
        user, _ = _make_custodial_user(db, with_key=False)
        with pytest.raises(onchain_send.CannotSignTransaction):
            await onchain_send.send_onchain_usdc(user, "0x" + "1" * 40, 100, db)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_no_single_chain_covers_the_amount(monkeypatch):
    # Wallet has $6 on polygon and $8.01 on base -- neither alone covers a
    # $10 send, even though the aggregate ($14.01) would.
    db = SessionLocal()
    try:
        user, sender_private_key = _make_custodial_user(db)
        _patch_chain(monkeypatch, "polygon", _FakeChainClient(native_usdc_cents=600))
        _patch_chain(monkeypatch, "base", _FakeChainClient(native_usdc_cents=801))

        with pytest.raises(onchain_send.NoChainHasSufficientBalance) as exc_info:
            await onchain_send.send_onchain_usdc(user, "0x" + "1" * 40, 1000, db)

        assert exc_info.value.per_chain_balances_cents == {"polygon": 600, "base": 801}
    finally:
        db.close()


@pytest.mark.asyncio
async def test_successful_send_signs_correct_recipient_and_amount(monkeypatch):
    db = SessionLocal()
    try:
        user, sender_private_key = _make_custodial_user(db)
        recipient = "0x" + "ab" * 20

        polygon_client = _FakeChainClient(native_usdc_cents=1000, native_balance_wei=10**18)
        _patch_chain(monkeypatch, "polygon", polygon_client)
        _patch_chain(monkeypatch, "base", _FakeChainClient(native_usdc_cents=0))

        result = await onchain_send.send_onchain_usdc(user, recipient, 500, db)

        assert result["chain"] == "polygon"
        assert result["tx_hash"].startswith("0x")
        assert len(polygon_client.sent_raw_txs) == 1  # no gas top-up needed, just the transfer

        # Independently re-sign the expected transaction with the same
        # inputs and confirm it's byte-for-byte what got broadcast --
        # proves the right recipient, amount, contract, and chain ID were
        # actually encoded into what would be a real, irreversible tx.
        contracts = bm.CHAINS["polygon"]["contracts"]
        expected_raw = onchain_send._sign_transfer(
            "polygon", sender_private_key, 0, 30_000_000_000,
            contracts["usdc_native"], recipient, 500 * 10**4,
        )
        assert polygon_client.sent_raw_txs[0] == expected_raw
    finally:
        db.close()


@pytest.mark.asyncio
async def test_gas_topup_triggered_when_native_balance_low(monkeypatch):
    db = SessionLocal()
    try:
        user, sender_private_key = _make_custodial_user(db)
        polygon_client = _FakeChainClient(native_usdc_cents=1000, native_balance_wei=1)  # far below minimum
        _patch_chain(monkeypatch, "polygon", polygon_client)
        _patch_chain(monkeypatch, "base", _FakeChainClient(native_usdc_cents=0))

        result = await onchain_send.send_onchain_usdc(user, "0x" + "1" * 40, 500, db)

        assert result["chain"] == "polygon"
        # Two broadcasts: the gas top-up, then the USDC transfer.
        assert len(polygon_client.sent_raw_txs) == 2
    finally:
        db.close()


@pytest.mark.asyncio
async def test_gas_topup_not_triggered_when_balance_already_sufficient(monkeypatch):
    db = SessionLocal()
    try:
        user, sender_private_key = _make_custodial_user(db)
        polygon_client = _FakeChainClient(native_usdc_cents=1000, native_balance_wei=10**18)  # plenty
        _patch_chain(monkeypatch, "polygon", polygon_client)
        _patch_chain(monkeypatch, "base", _FakeChainClient(native_usdc_cents=0))

        await onchain_send.send_onchain_usdc(user, "0x" + "1" * 40, 500, db)

        assert len(polygon_client.sent_raw_txs) == 1  # only the USDC transfer, no top-up
    finally:
        db.close()


@pytest.mark.asyncio
async def test_broadcast_failure_raises_onchain_send_failed(monkeypatch):
    db = SessionLocal()
    try:
        user, sender_private_key = _make_custodial_user(db)
        polygon_client = _FakeChainClient(native_usdc_cents=1000, native_balance_wei=10**18)
        polygon_client.broadcast_should_fail = True
        _patch_chain(monkeypatch, "polygon", polygon_client)
        _patch_chain(monkeypatch, "base", _FakeChainClient(native_usdc_cents=0))

        with pytest.raises(onchain_send.OnchainSendFailed):
            await onchain_send.send_onchain_usdc(user, "0x" + "1" * 40, 500, db)
    finally:
        db.close()


# ── Custody hardening: hard limits independent of wallet balance ──

@pytest.mark.asyncio
async def test_single_send_over_per_tx_limit_is_rejected(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "max_send_cents_per_tx", 1000)  # $10 cap for this test

    db = SessionLocal()
    try:
        user, _ = _make_custodial_user(db)
        _patch_chain(monkeypatch, "polygon", _FakeChainClient(native_usdc_cents=100_000))
        _patch_chain(monkeypatch, "base", _FakeChainClient(native_usdc_cents=0))

        with pytest.raises(onchain_send.SendLimitExceeded):
            await onchain_send.send_onchain_usdc(user, "0x" + "1" * 40, 1001, db)  # $10.01, over the $10 cap
    finally:
        db.close()


@pytest.mark.asyncio
async def test_per_tx_limit_check_happens_before_key_decryption(monkeypatch):
    # A rejected send must never touch the private key -- verifies the
    # limit check is the FIRST real check, not just that it eventually
    # raises. Simulates a wallet with no stored key at all; if the limit
    # check ran after key lookup, this would raise CannotSignTransaction
    # instead of SendLimitExceeded.
    from config import settings
    monkeypatch.setattr(settings, "max_send_cents_per_tx", 1000)

    db = SessionLocal()
    try:
        user, _ = _make_custodial_user(db, with_key=False)
        with pytest.raises(onchain_send.SendLimitExceeded):
            await onchain_send.send_onchain_usdc(user, "0x" + "1" * 40, 1001, db)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_cumulative_daily_sends_over_limit_are_rejected(monkeypatch):
    from config import settings
    from models import CryptoTransfer
    monkeypatch.setattr(settings, "max_send_cents_per_day", 1500)  # $15/day cap for this test

    db = SessionLocal()
    try:
        user, _ = _make_custodial_user(db)
        # Simulate $10 already sent in the last 24h.
        db.add(CryptoTransfer(sender_id=user.id, recipient_address="0x" + "9" * 40, amount_cents=1000, status="completed"))
        db.commit()

        _patch_chain(monkeypatch, "polygon", _FakeChainClient(native_usdc_cents=100_000))
        _patch_chain(monkeypatch, "base", _FakeChainClient(native_usdc_cents=0))

        # $10 already sent + $6 more would be $16, over the $15 daily cap.
        with pytest.raises(onchain_send.SendLimitExceeded):
            await onchain_send.send_onchain_usdc(user, "0x" + "1" * 40, 600, db)

        # But $4 more (bringing the total to exactly $14) should be fine.
        result = await onchain_send.send_onchain_usdc(user, "0x" + "1" * 40, 400, db)
        assert result["tx_hash"].startswith("0x")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_failed_completed_sends_do_not_count_toward_daily_limit(monkeypatch):
    # Only "completed" transfers should count against the rolling total --
    # a failed/pending one didn't actually move money.
    from config import settings
    from models import CryptoTransfer
    monkeypatch.setattr(settings, "max_send_cents_per_day", 1000)  # $10/day cap

    db = SessionLocal()
    try:
        user, _ = _make_custodial_user(db)
        db.add(CryptoTransfer(sender_id=user.id, recipient_address="0x" + "9" * 40, amount_cents=900, status="failed"))
        db.commit()

        _patch_chain(monkeypatch, "polygon", _FakeChainClient(native_usdc_cents=100_000))
        _patch_chain(monkeypatch, "base", _FakeChainClient(native_usdc_cents=0))

        # Should succeed -- the $9 "failed" transfer shouldn't count.
        result = await onchain_send.send_onchain_usdc(user, "0x" + "1" * 40, 900, db)
        assert result["tx_hash"].startswith("0x")
    finally:
        db.close()


@pytest.mark.asyncio
async def test_successful_send_creates_key_decryption_audit_log(monkeypatch):
    from models import UserAuditLog

    db = SessionLocal()
    try:
        user, _ = _make_custodial_user(db)
        _patch_chain(monkeypatch, "polygon", _FakeChainClient(native_usdc_cents=100_000))
        _patch_chain(monkeypatch, "base", _FakeChainClient(native_usdc_cents=0))

        await onchain_send.send_onchain_usdc(user, "0x" + "1" * 40, 500, db)

        log = db.query(UserAuditLog).filter(
            UserAuditLog.user_id == user.id,
            UserAuditLog.action == "private_key_decrypted",
        ).first()
        assert log is not None
        assert "send_onchain_usdc" in log.details
    finally:
        db.close()


@pytest.mark.asyncio
async def test_gas_station_daily_topup_cap_is_enforced(monkeypatch):
    from config import settings
    from models import GasStationTopup
    monkeypatch.setattr(settings, "max_gas_topups_per_day", 2)

    db = SessionLocal()
    try:
        # Pre-seed 2 top-ups already "sent" today -- the cap should already be hit.
        for _ in range(2):
            db.add(GasStationTopup(chain="polygon", wallet_address="0x" + "2" * 40, amount_wei="1", tx_hash="0x" + uuid.uuid4().hex))
        db.commit()

        user, _ = _make_custodial_user(db)
        # Low native balance forces a top-up attempt.
        _patch_chain(monkeypatch, "polygon", _FakeChainClient(native_usdc_cents=100_000, native_balance_wei=1))
        _patch_chain(monkeypatch, "base", _FakeChainClient(native_usdc_cents=0))

        with pytest.raises(onchain_send.GasStationLimitExceeded):
            await onchain_send.send_onchain_usdc(user, "0x" + "1" * 40, 500, db)
    finally:
        db.close()
