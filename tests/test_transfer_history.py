"""Tests for GET /transfers/history merging sends and received deposits.

Deposits (from services/blockchain_monitor.py) previously only nudged a
balance number with no visible transaction entry. This endpoint now merges
CryptoTransfer (sends) and CryptoDeposit (received, source-attributed)
into one chronological list.
"""
import uuid
from datetime import datetime, timedelta

from jose import jwt

from database import SessionLocal
from models import User, CryptoDeposit
from config import settings


def _make_user_with_wallet(db):
    user = User(
        email=f"th_{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name="Transfer History Tester",
        is_student=True,
        crypto_wallet_address="0x" + uuid.uuid4().hex[:40].ljust(40, "0"),
        wallet_initialized=True,
        usdc_balance_cents=1000,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _auth_for(user_id):
    expire = datetime.utcnow() + timedelta(minutes=settings.jwt_expire_minutes)
    token = jwt.encode({"sub": user_id, "exp": expire}, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return {"Authorization": f"Bearer {token}"}


def test_received_deposit_appears_in_history_with_source_attribution(client):
    db = SessionLocal()
    try:
        user = _make_user_with_wallet(db)
        deposit = CryptoDeposit(
            user_id=user.id,
            chain="base",
            contract_address="0x833589fCD6eDb6E08f4c7C32D4f71b54bda02913",
            from_address="0x1887fa9edadeab7562b01cc3f4fa246ace2c3cdd",
            to_address=user.crypto_wallet_address,
            amount_cents=801,
            tx_hash="0xrealtxhash",
            block_number=12345,
            credited_to_ledger=True,
        )
        db.add(deposit)
        db.commit()
        user_id = user.id
    finally:
        db.close()

    resp = _client_get(user_id, "/transfers/history")
    assert resp.status_code == 200, resp.text
    items = resp.json()

    receives = [i for i in items if i["type"] == "receive"]
    assert len(receives) == 1
    assert receives[0]["chain"] == "base"
    assert receives[0]["tx_hash"] == "0xrealtxhash"
    assert receives[0]["counterparty"] == "0x1887fa9edadeab7562b01cc3f4fa246ace2c3cdd"
    assert receives[0]["amount"] == 8.01


def test_backfilled_unconfirmed_deposits_are_excluded(client):
    """credited_to_ledger=False deposits (backfill-only, historical record
    that predates a wallet's checkpoint) shouldn't appear as if they were
    live, freshly-credited transactions."""
    db = SessionLocal()
    try:
        user = _make_user_with_wallet(db)
        deposit = CryptoDeposit(
            user_id=user.id,
            chain="polygon",
            contract_address="0x3c499c542cef5E3811e1192ce70d8cc03d5c3359",
            from_address="0x" + "9" * 40,
            to_address=user.crypto_wallet_address,
            amount_cents=200,
            tx_hash="0xbackfilled",
            block_number=999,
            credited_to_ledger=False,
        )
        db.add(deposit)
        db.commit()
        user_id = user.id
    finally:
        db.close()

    resp = _client_get(user_id, "/transfers/history")
    assert resp.status_code == 200
    items = resp.json()
    assert all(i.get("tx_hash") != "0xbackfilled" for i in items)


def _client_get(user_id, path):
    from main import app
    from starlette.testclient import TestClient
    client = TestClient(app)
    return client.get(path, headers=_auth_for(user_id))
