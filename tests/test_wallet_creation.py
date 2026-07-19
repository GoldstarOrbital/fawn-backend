"""Tests for services/crypto_wallet.py's create_wallet -- specifically the
custodial-key round-trip guard.

Reproduces a real production incident: several "fawn_custodial" wallets
were created (real on-chain addresses, wallet_initialized=True, real
deposits later sent to them) without a usable private key ever being
persisted -- an earlier version of create_wallet apparently didn't store
the key at all. Since custodial wallets never return a seed phrase to the
user (that's the whole point of "custodial" -- FAWN holds the key
instead), those wallets' real funds are likely unrecoverable: nobody,
not FAWN and not the user, holds anything that can sign for them.

create_wallet now verifies the encrypted key round-trips BEFORE anything
is persisted, so a broken key produces a clean creation error instead of
a wallet a user can deposit real money into that nobody can ever sign for.
"""
import uuid

import pytest

from database import SessionLocal
from models import User, CryptoWallet
from services import crypto_wallet


def _make_bare_user(db):
    user = User(
        email=f"walletcreate_{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name="Wallet Creation Tester",
        is_student=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.mark.asyncio
async def test_custodial_wallet_gets_a_decryptable_key():
    db = SessionLocal()
    try:
        user = _make_bare_user(db)
        result = await crypto_wallet.create_wallet(user.id, db, wallet_type="fawn_custodial")

        assert result["wallet_type"] == "fawn_custodial"
        assert result["seed_phrase"] is None  # custodial never returns it

        row = db.query(CryptoWallet).filter(
            CryptoWallet.wallet_address == result["wallet_address"]
        ).first()
        assert row is not None
        assert row.encrypted_private_key is not None
        # Must actually decrypt to a real private key, not just be non-null.
        decrypted = crypto_wallet._decrypt_private_key(row.encrypted_private_key)
        hex_part = decrypted[2:] if decrypted.startswith("0x") else decrypted
        assert len(hex_part) == 64
        assert all(c in "0123456789abcdefABCDEF" for c in hex_part)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_non_custodial_wallet_returns_seed_and_stores_no_key():
    db = SessionLocal()
    try:
        user = _make_bare_user(db)
        result = await crypto_wallet.create_wallet(user.id, db, wallet_type="non_custodial")

        assert result["wallet_type"] == "non_custodial"
        assert result["seed_phrase"] is not None
        assert len(result["seed_phrase"].split()) == 12

        row = db.query(CryptoWallet).filter(
            CryptoWallet.wallet_address == result["wallet_address"]
        ).first()
        assert row is not None
        assert row.encrypted_private_key is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_custodial_wallet_creation_fails_loudly_if_key_does_not_roundtrip(monkeypatch):
    # Simulates exactly the production failure mode: encryption "succeeds"
    # (returns bytes) but decrypting it back doesn't recover the original
    # key -- e.g. a wrong encryption key, storage corruption, or (as
    # apparently happened) a code path that skipped real key storage
    # entirely. This must NOT silently hand back a working-looking wallet.
    monkeypatch.setattr(crypto_wallet, "_decrypt_private_key", lambda *a, **kw: "0xnotthesamekey")

    db = SessionLocal()
    try:
        user = _make_bare_user(db)
        with pytest.raises(ValueError, match="round-trip"):
            await crypto_wallet.create_wallet(user.id, db, wallet_type="fawn_custodial")

        # Nothing should have been persisted -- not the wallet, not the
        # user's crypto_wallet_address. A half-created, unsignable wallet
        # is exactly the bug being fixed.
        db.refresh(user)
        assert user.crypto_wallet_address is None
        assert user.wallet_initialized is False
        assert db.query(CryptoWallet).filter(CryptoWallet.user_id == user.id).first() is None
    finally:
        db.close()
