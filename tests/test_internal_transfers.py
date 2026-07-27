import uuid

import pytest

from models import CryptoWallet, User
from services.crypto_wallet import INTERNAL_TRANSFER_FEE_CENTS, send_usdc, get_transfer_history


def _user(db, address, balance):
    user = User(
        email=f"internal_{uuid.uuid4().hex}@example.com",
        hashed_password="x",
        full_name="Pilot Student",
        is_student=True,
        crypto_wallet_address=address,
        wallet_type="fawn_custodial",
        wallet_initialized=True,
        usdc_balance_cents=balance,
    )
    db.add(user)
    db.flush()
    db.add(CryptoWallet(
        user_id=user.id,
        wallet_address=address,
        wallet_type="fawn_custodial",
        chain="polygon",
        usdc_balance_cents=balance,
    ))
    db.commit()
    return user


@pytest.mark.asyncio
async def test_internal_transfer_is_immediate_and_visible_to_both_users(db):
    sender = _user(db, "0x" + uuid.uuid4().hex + uuid.uuid4().hex[:8], 5_000)
    recipient = _user(db, "0x" + uuid.uuid4().hex + uuid.uuid4().hex[:8], 100)

    result = await send_usdc(
        sender_id=sender.id,
        recipient_address=recipient.crypto_wallet_address,
        amount_cents=1_000,
        db=db,
        memo="lunch",
        is_internal=True,
        idempotency_key="pilot-send-1",
    )

    retry = await send_usdc(
        sender_id=sender.id,
        recipient_address=recipient.crypto_wallet_address,
        amount_cents=1_000,
        db=db,
        memo="lunch",
        is_internal=True,
        idempotency_key="pilot-send-1",
    )

    assert result["status"] == "completed"
    assert result["chain"] == "internal"
    assert result["tx_hash"] is None
    assert retry["transfer_id"] == result["transfer_id"]

    db.refresh(sender)
    db.refresh(recipient)
    assert sender.usdc_balance_cents == 5_000 - 1_000 - INTERNAL_TRANSFER_FEE_CENTS
    assert recipient.usdc_balance_cents == 1_100

    sender_history = await get_transfer_history(sender.id, db)
    recipient_history = await get_transfer_history(recipient.id, db)
    assert sender_history[0]["type"] == "send"
    assert recipient_history[0]["type"] == "receive"
    assert recipient_history[0]["amount"] == 10.0
    assert recipient_history[0]["fee"] == 0.0
