"""Tests for POST /wallet/sync-now: on-demand deposit sync.

Lets a user get instant confirmation right after sending crypto instead of
waiting for the next services/blockchain_monitor.py background cycle. The
endpoint reuses services.blockchain_monitor._scan_wallet_chain directly (same
event-log detection, same checkpoint/dedupe logic as the background loop) --
these tests fake the RPC layer the same way tests/test_blockchain_monitor.py
does and drive the endpoint over HTTP with a JWT auth header, the same
pattern tests/test_transfer_history.py uses.
"""
import uuid
from datetime import datetime, timedelta

from jose import jwt

from database import SessionLocal
from models import User
from config import settings
from services import blockchain_monitor as bm


def _make_user_with_wallet(db):
    user = User(
        email=f"sync_{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name="Sync Now Tester",
        is_student=True,
        crypto_wallet_address="0x" + uuid.uuid4().hex[:40].ljust(40, "0"),
        wallet_initialized=True,
        usdc_balance_cents=0,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _auth_for(user_id):
    expire = datetime.utcnow() + timedelta(minutes=settings.jwt_expire_minutes)
    token = jwt.encode({"sub": user_id, "exp": expire}, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return {"Authorization": f"Bearer {token}"}


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
    """Fakes eth_blockNumber and eth_getLogs for one chain (same pattern as
    tests/test_blockchain_monitor.py's _FakeRPCClient)."""

    def __init__(self, chain, latest_block, logs_by_contract=None):
        self.chain = chain
        self.latest_block = latest_block
        self.logs_by_contract = logs_by_contract or {}

    async def call(self, method, params):
        if method == "eth_blockNumber":
            return hex(self.latest_block)
        if method == "eth_getLogs":
            contract = params[0]["address"]
            return self.logs_by_contract.get(contract, [])
        return None


def _patch_chain(monkeypatch, chain, latest_block, logs_by_contract=None):
    monkeypatch.setitem(
        bm._rpc_clients, chain,
        _FakeRPCClient(chain, latest_block, logs_by_contract),
    )


def _sync_now(user_id):
    from main import app
    from starlette.testclient import TestClient
    test_client = TestClient(app)
    return test_client.post("/wallet/sync-now", headers=_auth_for(user_id))


def test_pending_deposit_credited_immediately_via_sync_now(monkeypatch):
    """A deposit that just landed on-chain is credited synchronously by
    POST /wallet/sync-now, without waiting for the background loop's next
    cycle."""
    db = SessionLocal()
    try:
        user = _make_user_with_wallet(db)
        user_id = user.id
        wallet_address = user.crypto_wallet_address
    finally:
        db.close()

    # First call establishes checkpoints for both chains (backfill pass,
    # no deposits yet -> nothing credited).
    _patch_chain(monkeypatch, "polygon", latest_block=100)
    _patch_chain(monkeypatch, "base", latest_block=100)
    resp0 = _sync_now(user_id)
    assert resp0.status_code == 200, resp0.text
    assert resp0.json()["newly_credited_count"] == 0

    # A real deposit now lands on Polygon (native USDC contract) -- the
    # background loop wouldn't see this for up to check_interval seconds,
    # but sync-now must detect and credit it right away.
    contracts = bm.CHAINS["polygon"]["contracts"]
    sender = _random_address()
    log = _transfer_log(sender, wallet_address, 2_500_000, "0xsyncnow1", 150)
    _patch_chain(monkeypatch, "polygon", latest_block=200, logs_by_contract={contracts["usdc_native"]: [log]})
    _patch_chain(monkeypatch, "base", latest_block=200)

    resp = _sync_now(user_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["newly_credited_count"] == 1
    assert body["usdc_balance_cents"] == 250
    assert body["usdc_balance"] == 2.5
    assert body["wallet_address"] == wallet_address
    assert len(body["new_deposits"]) == 1
    assert body["new_deposits"][0]["chain"] == "polygon"
    assert body["new_deposits"][0]["tx_hash"] == "0xsyncnow1"
    assert body["new_deposits"][0]["from_address"].lower() == sender.lower()
    assert body["new_deposits"][0]["amount_cents"] == 250


def test_sync_now_twice_with_no_new_deposit_does_not_double_credit(monkeypatch):
    """Calling sync-now twice back-to-back with no new on-chain activity in
    between must not credit the same deposit twice (idempotency already
    lives in _scan_wallet_chain's checkpoint + CryptoDeposit unique dedupe
    constraint -- this just confirms it end-to-end through the endpoint)."""
    db = SessionLocal()
    try:
        user = _make_user_with_wallet(db)
        user_id = user.id
        wallet_address = user.crypto_wallet_address
    finally:
        db.close()

    _patch_chain(monkeypatch, "polygon", latest_block=50)
    _patch_chain(monkeypatch, "base", latest_block=50)
    backfill_resp = _sync_now(user_id)
    assert backfill_resp.status_code == 200, backfill_resp.text

    contracts = bm.CHAINS["polygon"]["contracts"]
    sender = _random_address()
    log = _transfer_log(sender, wallet_address, 1_000_000, "0xidempotent1", 75)
    _patch_chain(monkeypatch, "polygon", latest_block=100, logs_by_contract={contracts["usdc_native"]: [log]})
    _patch_chain(monkeypatch, "base", latest_block=100)

    resp1 = _sync_now(user_id)
    assert resp1.status_code == 200, resp1.text
    body1 = resp1.json()
    assert body1["newly_credited_count"] == 1
    assert body1["usdc_balance_cents"] == 100

    # Call again immediately -- same chain state, no new on-chain deposit.
    resp2 = _sync_now(user_id)
    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    assert body2["newly_credited_count"] == 0
    assert body2["usdc_balance_cents"] == 100  # unchanged, not double-credited
    assert body2["new_deposits"] == []


def test_sync_now_requires_jwt_auth(client):
    """POST /wallet/sync-now with no Authorization header is rejected."""
    resp = client.post("/wallet/sync-now")
    assert resp.status_code == 401
