"""Tests for the large-first-time-recipient send review queue.

services/crypto_wallet.py::send_usdc holds (doesn't settle) a send that's
both above new_recipient_review_threshold_cents AND to a recipient the
sender has never successfully sent to before -- the classic account-
takeover pattern is immediate drain to a brand-new address. An admin
approves (executes the real send) or rejects (nothing ever moved) via
routers/admin_credit.py.
"""
import os
import uuid
from datetime import datetime, timedelta

import pytest
from jose import jwt

from config import settings
from database import SessionLocal
from models import User, CryptoWallet, CryptoTransfer, UserAuditLog
from services import address_risk
from services import blockchain_monitor as bm
from services.crypto_wallet import _encrypt_private_key, send_usdc


class _CleanAddressRiskClient:
    """send_usdc now unconditionally checks address risk on every send --
    default every test in this file to a mocked "clean" response so
    existing tests don't make real network calls to GoPlus. Tests that
    specifically want a "flagged" response override this per-test."""
    def __init__(self, *a, **kw):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    async def get(self, url, params=None):
        class _R:
            def raise_for_status(self):
                pass
            def json(self):
                return {"result": {f: "0" for f in address_risk.RISK_FLAGS} | {"data_source": ""}}
        return _R()


@pytest.fixture(autouse=True)
def _mock_address_risk_clean(monkeypatch):
    monkeypatch.setattr(address_risk.httpx, "AsyncClient", _CleanAddressRiskClient)


def _make_custodial_user(db):
    import secrets
    from eth_account import Account
    private_key = "0x" + secrets.token_hex(32)
    address = Account.from_key(private_key).address

    user = User(
        email=f"reviewq_{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name="Review Queue Tester",
        is_student=True,
        crypto_wallet_address=address,
        wallet_type="fawn_custodial",
        wallet_initialized=True,
        usdc_balance_cents=1_000_000,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    db.add(CryptoWallet(
        user_id=user.id, wallet_address=address, wallet_type="fawn_custodial",
        chain="polygon", usdc_balance_cents=0,
        encrypted_private_key=_encrypt_private_key(private_key),
    ))
    db.commit()
    return user


class _FakeChainClient:
    def __init__(self, native_usdc_cents=1_000_000, native_balance_wei=10**18):
        self.native_usdc_cents = native_usdc_cents
        self.native_balance_wei = native_balance_wei
        self.sent_raw_txs = []

    async def call(self, method, params):
        if method == "eth_getBalance":
            return hex(self.native_balance_wei)
        if method == "eth_getTransactionCount":
            return hex(0)
        if method == "eth_gasPrice":
            return hex(30_000_000_000)
        if method == "eth_call":
            return hex(self.native_usdc_cents * (10 ** 4))
        if method == "eth_sendRawTransaction":
            self.sent_raw_txs.append(params[0])
            return "0x" + uuid.uuid4().hex + uuid.uuid4().hex[:24]
        if method == "eth_getTransactionReceipt":
            return {"status": "0x1"}
        return None


def _patch_chains(monkeypatch):
    monkeypatch.setitem(bm._rpc_clients, "polygon", _FakeChainClient())
    monkeypatch.setitem(bm._rpc_clients, "base", _FakeChainClient(native_usdc_cents=0))


def _auth_for(user_id):
    expire = datetime.utcnow() + timedelta(minutes=settings.jwt_expire_minutes)
    token = jwt.encode({"sub": user_id, "exp": expire}, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return {"Authorization": f"Bearer {token}"}


def _admin_post(path, body):
    from main import app
    from starlette.testclient import TestClient
    client = TestClient(app)
    return client.post(path, json=body, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]})


def _admin_get(path):
    from main import app
    from starlette.testclient import TestClient
    client = TestClient(app)
    return client.get(path, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]})


@pytest.mark.asyncio
async def test_large_first_time_send_is_held_not_settled(monkeypatch):
    monkeypatch.setattr(settings, "new_recipient_review_threshold_cents", 50_000)
    _patch_chains(monkeypatch)

    db = SessionLocal()
    try:
        user = _make_custodial_user(db)
        recipient = "0x" + "7" * 40
        balance_before = user.usdc_balance_cents

        result = await send_usdc(user.id, recipient, 60_000, db, is_internal=False)

        assert result["status"] == "pending_review"
        assert result["tx_hash"] is None
        assert result["chain"] is None

        db.refresh(user)
        assert user.usdc_balance_cents == balance_before  # ledger untouched

        transfer = db.query(CryptoTransfer).filter(CryptoTransfer.id == result["transfer_id"]).first()
        assert transfer.status == "pending_review"

        log = db.query(UserAuditLog).filter(
            UserAuditLog.user_id == user.id, UserAuditLog.action == "send_held_for_review"
        ).first()
        assert log is not None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_small_send_to_new_recipient_settles_immediately(monkeypatch):
    monkeypatch.setattr(settings, "new_recipient_review_threshold_cents", 50_000)
    _patch_chains(monkeypatch)

    db = SessionLocal()
    try:
        user = _make_custodial_user(db)
        result = await send_usdc(user.id, "0x" + "7" * 40, 10_000, db, is_internal=False)  # under threshold
        assert result["status"] == "completed"
        assert result["tx_hash"] is not None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_large_send_to_repeat_recipient_settles_immediately(monkeypatch):
    monkeypatch.setattr(settings, "new_recipient_review_threshold_cents", 50_000)
    _patch_chains(monkeypatch)

    db = SessionLocal()
    try:
        user = _make_custodial_user(db)
        recipient = "0x" + "7" * 40
        db.add(CryptoTransfer(sender_id=user.id, recipient_address=recipient, amount_cents=100, status="completed"))
        db.commit()

        result = await send_usdc(user.id, recipient, 60_000, db, is_internal=False)  # over threshold, but known recipient
        assert result["status"] == "completed"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_admin_approve_executes_the_real_send(monkeypatch):
    monkeypatch.setattr(settings, "new_recipient_review_threshold_cents", 50_000)
    _patch_chains(monkeypatch)

    db = SessionLocal()
    try:
        user = _make_custodial_user(db)
        balance_before = user.usdc_balance_cents
        result = await send_usdc(user.id, "0x" + "7" * 40, 60_000, db, is_internal=False)
        transfer_id = result["transfer_id"]
    finally:
        db.close()

    resp = _admin_post("/admin/approve-transfer", {"transfer_id": transfer_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    assert body["tx_hash"].startswith("0x")

    db2 = SessionLocal()
    try:
        transfer = db2.query(CryptoTransfer).filter(CryptoTransfer.id == transfer_id).first()
        assert transfer.status == "completed"
        assert transfer.tx_hash is not None

        refreshed_user = db2.query(User).filter(User.id == user.id).first()
        assert refreshed_user.usdc_balance_cents == balance_before - 60_000 - 50  # amount + $0.50 external fee
    finally:
        db2.close()


def test_admin_reject_leaves_ledger_untouched():
    db = SessionLocal()
    try:
        user = _make_custodial_user(db)
        balance_before = user.usdc_balance_cents
        transfer = CryptoTransfer(
            sender_id=user.id, recipient_address="0x" + "7" * 40,
            amount_cents=60_000, fee_cents=50, status="pending_review",
        )
        db.add(transfer)
        db.commit()
        transfer_id = transfer.id
    finally:
        db.close()

    resp = _admin_post("/admin/reject-transfer", {"transfer_id": transfer_id, "reason": "looked suspicious"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"

    db2 = SessionLocal()
    try:
        transfer = db2.query(CryptoTransfer).filter(CryptoTransfer.id == transfer_id).first()
        assert transfer.status == "rejected"
        assert transfer.tx_hash is None

        refreshed_user = db2.query(User).filter(User.id == user.id).first()
        assert refreshed_user.usdc_balance_cents == balance_before  # never touched

        log = db2.query(UserAuditLog).filter(
            UserAuditLog.user_id == user.id, UserAuditLog.action == "send_rejected_after_review"
        ).first()
        assert log is not None
    finally:
        db2.close()


def test_admin_approve_rejects_if_balance_no_longer_sufficient():
    db = SessionLocal()
    try:
        user = _make_custodial_user(db)
        transfer = CryptoTransfer(
            sender_id=user.id, recipient_address="0x" + "7" * 40,
            amount_cents=60_000, fee_cents=50, status="pending_review",
        )
        db.add(transfer)
        # Drain the balance after the hold was created, before approval.
        user.usdc_balance_cents = 100
        db.commit()
        transfer_id = transfer.id
    finally:
        db.close()

    resp = _admin_post("/admin/approve-transfer", {"transfer_id": transfer_id})
    assert resp.status_code == 402

    db2 = SessionLocal()
    try:
        transfer = db2.query(CryptoTransfer).filter(CryptoTransfer.id == transfer_id).first()
        assert transfer.status == "pending_review"  # unchanged, still held
    finally:
        db2.close()


def test_pending_transfers_endpoint_lists_held_sends():
    db = SessionLocal()
    try:
        user = _make_custodial_user(db)
        db.add(CryptoTransfer(
            sender_id=user.id, recipient_address="0x" + "7" * 40,
            amount_cents=60_000, fee_cents=50, status="pending_review",
        ))
        db.commit()
        user_id = user.id
    finally:
        db.close()

    resp = _admin_get("/admin/pending-transfers")
    assert resp.status_code == 200
    pending = resp.json()["pending"]
    assert any(p["sender_id"] == user_id for p in pending)


def test_admin_endpoints_require_admin_key():
    from main import app
    from starlette.testclient import TestClient
    client = TestClient(app)

    assert client.get("/admin/pending-transfers").status_code == 403
    assert client.post("/admin/approve-transfer", json={"transfer_id": "x"}).status_code == 403
    assert client.post("/admin/reject-transfer", json={"transfer_id": "x"}).status_code == 403


def test_concurrent_approval_cannot_double_send():
    # Simulates the race directly: a transfer already claimed (status=
    # "approving", as the first of two concurrent requests would leave
    # it) must make a second approve attempt fail with 409, not silently
    # re-execute the send.
    db = SessionLocal()
    try:
        user = _make_custodial_user(db)
        transfer = CryptoTransfer(
            sender_id=user.id, recipient_address="0x" + "7" * 40,
            amount_cents=60_000, fee_cents=50, status="approving",
        )
        db.add(transfer)
        db.commit()
        transfer_id = transfer.id
    finally:
        db.close()

    resp = _admin_post("/admin/approve-transfer", {"transfer_id": transfer_id})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_hold_creation_rejects_amount_over_per_tx_cap(monkeypatch):
    # A held request that structurally can never be approved (over the
    # hard per-tx cap) must be rejected up front, not silently admitted
    # to the review queue where it would sit stuck forever.
    monkeypatch.setattr(settings, "new_recipient_review_threshold_cents", 50_000)
    monkeypatch.setattr(settings, "max_send_cents_per_tx", 100_000)  # $1,000 cap

    db = SessionLocal()
    try:
        user = _make_custodial_user(db)
        from services import onchain_send
        with pytest.raises(onchain_send.SendLimitExceeded):
            await send_usdc(user.id, "0x" + "7" * 40, 150_000, db, is_internal=False)  # $1,500, over cap

        # Nothing should have been created.
        assert db.query(CryptoTransfer).filter(CryptoTransfer.sender_id == user.id).count() == 0
    finally:
        db.close()


class _FlaggedAddressRiskClient:
    def __init__(self, *a, **kw):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    async def get(self, url, params=None):
        class _R:
            def raise_for_status(self):
                pass
            def json(self):
                return {"result": {f: "0" for f in address_risk.RISK_FLAGS} | {"stealing_attack": "1", "data_source": "SlowMist"}}
        return _R()


@pytest.mark.asyncio
async def test_flagged_address_holds_even_small_amount_to_repeat_recipient(monkeypatch):
    # Proves the address-risk hold trigger is independent of the existing
    # amount/first-time-recipient trigger -- a small send to a recipient
    # the sender has sent to before must still be held if that recipient
    # is flagged by real-time threat intel.
    monkeypatch.setattr(address_risk.httpx, "AsyncClient", _FlaggedAddressRiskClient)
    _patch_chains(monkeypatch)

    db = SessionLocal()
    try:
        user = _make_custodial_user(db)
        recipient = "0x" + "7" * 40
        # Establish recipient history (repeat recipient) at a small amount.
        db.add(CryptoTransfer(sender_id=user.id, recipient_address=recipient, amount_cents=100, status="completed"))
        db.commit()

        result = await send_usdc(user.id, recipient, 500, db, is_internal=False)  # small, repeat recipient
        assert result["status"] == "pending_review"

        log = db.query(UserAuditLog).filter(
            UserAuditLog.user_id == user.id,
            UserAuditLog.action == "recipient_flagged_by_address_risk_check",
        ).first()
        assert log is not None
    finally:
        db.close()
