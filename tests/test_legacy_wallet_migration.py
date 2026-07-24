import uuid

from database import SessionLocal
from migrate_legacy_wallets import _legacy_rows
from models import CryptoWallet, User


def _user(**overrides):
    db = SessionLocal()
    suffix = uuid.uuid4().hex[:10]
    user = User(
        email=f"legacy_{suffix}@example.com", username=f"legacy{suffix}", hashed_password="x", full_name="Legacy Tester",
        **overrides,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    db.close()
    return user


def test_legacy_migration_finds_user_only_wallet_state():
    user = _user(wallet_initialized=True, wallet_type="non_custodial", crypto_wallet_address="0x" + uuid.uuid4().hex + uuid.uuid4().hex[:8], usdc_balance_cents=2400)
    db = SessionLocal()
    rows = _legacy_rows(db)
    db.close()
    assert any(legacy is None and candidate.id == user.id for legacy, candidate in rows)


def test_legacy_migration_skips_usable_active_custodial_wallet():
    address = "0x" + uuid.uuid4().hex + uuid.uuid4().hex[:8]
    user = _user(wallet_initialized=True, wallet_type="fawn_custodial", crypto_wallet_address=address)
    db = SessionLocal()
    db.add(CryptoWallet(
        user_id=user.id, wallet_address=address, wallet_type="fawn_custodial", chain="polygon",
        encrypted_private_key=b"test-key", wrapped_dek=b"test-dek", key_version="v2", status="active",
    ))
    db.commit()
    rows = _legacy_rows(db)
    db.close()
    assert not any(candidate.id == user.id for _, candidate in rows)
