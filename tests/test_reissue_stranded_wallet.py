"""Tests for POST /admin/reissue-stranded-wallet — the operator fix for
custodial wallets created before the encrypted-key fix (no usable signing
key). It must never abandon on-chain funds and must produce a usable key.
No real network: the on-chain balance check is monkeypatched.
"""
import secrets
import uuid

from eth_account import Account

from database import SessionLocal
from models import User, CryptoWallet
from services import blockchain_monitor as bm
from services import crypto_wallet
from services.crypto_wallet import _encrypt_private_key_envelope

ADMIN_HEADERS = {"X-Admin-Key": "test-admin-key-12345"}


def _stranded_user(db):
    """A fawn_custodial wallet with NO usable key — the exact production bug."""
    pk = "0x" + secrets.token_hex(32)
    address = Account.from_key(pk).address
    email = f"stranded_{uuid.uuid4().hex[:10]}@example.com"
    user = User(
        email=email, hashed_password="x", full_name="Stranded User", is_student=True,
        crypto_wallet_address=address, wallet_type="fawn_custodial",
        wallet_initialized=True, usdc_balance_cents=4200,
    )
    db.add(user); db.commit(); db.refresh(user)
    db.add(CryptoWallet(
        user_id=user.id, wallet_address=address, wallet_type="fawn_custodial",
        chain="polygon", usdc_balance_cents=0, encrypted_private_key=None,
    ))
    db.commit()
    return email, address


def _key_usable_for(email):
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.email == email).first()
        w = db.query(CryptoWallet).filter(CryptoWallet.user_id == u.id).first()
        if not w or not w.encrypted_private_key:
            return False, u.crypto_wallet_address
        pk = crypto_wallet._decrypt_private_key(w.encrypted_private_key, key_version=w.key_version, wrapped_dek=w.wrapped_dek)
        return Account.from_key(pk).address.lower() == w.wallet_address.lower(), u.crypto_wallet_address
    finally:
        db.close()


def test_dry_run_does_not_change_anything(client, monkeypatch):
    monkeypatch.setattr(bm, "_get_combined_balance", lambda chain, addr: _async(0))
    db = SessionLocal()
    try:
        email, old = _stranded_user(db)
    finally:
        db.close()
    r = client.post(f"/admin/reissue-stranded-wallet?email={email}", headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "dry_run"
    usable, addr = _key_usable_for(email)
    assert usable is False and addr == old  # unchanged


def test_reissue_creates_usable_wallet(client, monkeypatch):
    monkeypatch.setattr(bm, "_get_combined_balance", lambda chain, addr: _async(0))
    db = SessionLocal()
    try:
        email, old = _stranded_user(db)
    finally:
        db.close()
    r = client.post(f"/admin/reissue-stranded-wallet?email={email}&confirm=true", headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "reissued"
    assert body["new_wallet_address"] and body["new_wallet_address"] != old
    usable, addr = _key_usable_for(email)
    assert usable is True and addr == body["new_wallet_address"]


def test_refuses_when_onchain_funds_present(client, monkeypatch):
    monkeypatch.setattr(bm, "_get_combined_balance", lambda chain, addr: _async(500))  # $5 on-chain
    db = SessionLocal()
    try:
        email, old = _stranded_user(db)
    finally:
        db.close()
    r = client.post(f"/admin/reissue-stranded-wallet?email={email}&confirm=true", headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "flagged_has_funds"
    usable, addr = _key_usable_for(email)
    assert usable is False and addr == old  # left untouched, funds not abandoned


def test_usable_wallet_left_alone(client, monkeypatch):
    monkeypatch.setattr(bm, "_get_combined_balance", lambda chain, addr: _async(0))
    pk = "0x" + secrets.token_hex(32)
    address = Account.from_key(pk).address
    enc, wrapped = _encrypt_private_key_envelope(pk)
    email = f"good_{uuid.uuid4().hex[:10]}@example.com"
    db = SessionLocal()
    try:
        u = User(email=email, hashed_password="x", full_name="Good User", is_student=True,
                 crypto_wallet_address=address, wallet_type="fawn_custodial",
                 wallet_initialized=True, usdc_balance_cents=0)
        db.add(u); db.commit(); db.refresh(u)
        db.add(CryptoWallet(user_id=u.id, wallet_address=address, wallet_type="fawn_custodial",
                            chain="polygon", usdc_balance_cents=0,
                            encrypted_private_key=enc, wrapped_dek=wrapped, key_version="v2"))
        db.commit()
    finally:
        db.close()
    r = client.post(f"/admin/reissue-stranded-wallet?email={email}&confirm=true", headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "already_ok"


def test_requires_admin_key(client):
    r = client.post("/admin/reissue-stranded-wallet?email=x@y.com")
    assert r.status_code == 403


async def _async_impl(v):
    return v

def _async(v):
    # helper so monkeypatched _get_combined_balance stays awaitable
    return _async_impl(v)
