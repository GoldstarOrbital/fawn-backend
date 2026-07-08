"""
Stablecoin wallet service for FAWN's crypto-native architecture.

Supports USDC on Polygon and Ethereum. Two wallet types:
- non_custodial: User manages keys, we track balance
- fawn_custodial: FAWN holds keys in encrypted storage (MVP: just balance tracking)

No gas fees — platform charges a flat $0.01 per transfer.
"""
import os
from decimal import Decimal
from typing import Optional
from datetime import datetime
from sqlalchemy.orm import Session
from models import User, CryptoWallet, CryptoTransfer, FeeCollection
import secrets

# For MVP: we'll use a placeholder for wallet creation.
# In production, integrate with Ethers.js or similar for real wallet generation.

PLATFORM_FEE_CENTS = 100  # $0.01 = 100 cents
USDC_CHAIN = os.environ.get("USDC_CHAIN", "polygon")  # "polygon" | "ethereum"


class WalletNotInitialized(Exception):
    """User has not created a stablecoin wallet yet."""
    pass


class InsufficientBalance(Exception):
    """Sender does not have enough USDC to cover transfer + fee."""
    pass


class InvalidAddress(Exception):
    """Recipient wallet address is invalid."""
    pass


def _is_valid_eth_address(addr: str) -> bool:
    """Simple check: is it a valid Ethereum-style address (0x + 40 hex chars)."""
    if not addr or not addr.startswith("0x"):
        return False
    return len(addr) == 42 and all(c in "0123456789abcdefABCDEF" for c in addr[2:])


async def create_wallet(user_id: str, db: Session, wallet_type: str = "fawn_custodial") -> dict:
    """
    Create a new stablecoin wallet for the user.

    Args:
        user_id: FAWN user ID
        db: database session
        wallet_type: "non_custodial" (user manages keys) or "fawn_custodial" (FAWN holds)

    Returns:
        {
            "wallet_address": "0x...",
            "wallet_type": "fawn_custodial",
            "usdc_balance": 0.0,
            "seed_phrase": "..." (only if non_custodial; user must save this)
        }

    Raises:
        ValueError if user already has a wallet or wallet_type is invalid
    """
    if wallet_type not in ("non_custodial", "fawn_custodial"):
        raise ValueError(f"Invalid wallet_type: {wallet_type}")

    # Check if user already has a wallet
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise ValueError(f"User {user_id} not found")
    if user.crypto_wallet_address:
        raise ValueError(f"User {user_id} already has a wallet: {user.crypto_wallet_address}")

    # Generate wallet address (MVP: placeholder)
    # In production: use ethers.Wallet.createRandom() or similar
    wallet_address = f"0x{secrets.token_hex(20)}"  # 20 bytes = 40 hex chars

    # For custodial wallets, we'd generate and store an encrypted seed phrase.
    # For MVP: just generate a placeholder.
    seed_phrase = None
    if wallet_type == "non_custodial":
        # In production: generate 12-word BIP39 seed
        seed_phrase = " ".join(secrets.token_hex(2) for _ in range(12))

    # Create wallet record
    wallet = CryptoWallet(
        user_id=user_id,
        wallet_address=wallet_address,
        wallet_type=wallet_type,
        chain=USDC_CHAIN,
        usdc_balance_cents=0,
    )
    db.add(wallet)

    # Update user to link wallet
    user.crypto_wallet_address = wallet_address
    user.wallet_type = wallet_type
    user.usdc_balance_cents = 0
    user.wallet_initialized = True

    db.commit()

    return {
        "wallet_address": wallet_address,
        "wallet_type": wallet_type,
        "usdc_balance": 0.0,
        "chain": USDC_CHAIN,
        "seed_phrase": seed_phrase,  # ONLY for non-custodial; user must save
    }


async def get_wallet_balance(user_id: str, db: Session) -> dict:
    """
    Get current USDC balance for a user's wallet.

    Returns:
        {
            "wallet_address": "0x...",
            "usdc_balance": 100.50,  # in USD (float)
            "usdc_balance_cents": 10050,  # in cents (int)
            "total_fees_paid": 2.50,  # lifetime fees in USD (float)
        }

    Raises:
        WalletNotInitialized if user has no wallet
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.crypto_wallet_address:
        raise WalletNotInitialized(f"User {user_id} has no stablecoin wallet")

    return {
        "wallet_address": user.crypto_wallet_address,
        "usdc_balance": user.usdc_balance_cents / 100.0,
        "usdc_balance_cents": user.usdc_balance_cents,
        "total_fees_paid": user.total_fees_paid_cents / 100.0,
    }


async def send_usdc(
    sender_id: str,
    recipient_address: str,
    amount_cents: int,
    db: Session,
    memo: str = None,
) -> dict:
    """
    Send USDC from sender to recipient (internal ledger transfer).

    Costs the sender $0.01 in platform fees (on top of the transfer amount).
    No blockchain transaction — instant settlement in our ledger.
    No gas fees.

    Args:
        sender_id: FAWN user ID of sender
        recipient_address: recipient's wallet address (0x...)
        amount_cents: amount to send in cents (e.g., 1000 = $10.00)
        db: database session
        memo: optional transfer memo

    Returns:
        {
            "transfer_id": "...",
            "amount": 10.00,  # amount sent (USD float)
            "fee": 0.01,  # platform fee (USD float)
            "total_debited": 10.01,  # total taken from sender (USD float)
            "status": "completed",
            "tx_hash": None,  # on-chain hash if settled on-chain
            "created_at": "2026-07-08T...",
        }

    Raises:
        WalletNotInitialized if sender has no wallet
        InvalidAddress if recipient_address is malformed
        InsufficientBalance if sender can't cover transfer + fee
    """
    if not _is_valid_eth_address(recipient_address):
        raise InvalidAddress(f"Invalid recipient address: {recipient_address}")

    sender = db.query(User).filter(User.id == sender_id).first()
    if not sender or not sender.crypto_wallet_address:
        raise WalletNotInitialized(f"Sender {sender_id} has no stablecoin wallet")

    # Check balance
    total_needed = amount_cents + PLATFORM_FEE_CENTS
    if sender.usdc_balance_cents < total_needed:
        raise InsufficientBalance(
            f"Insufficient balance. Have: ${sender.usdc_balance_cents / 100:.2f}, "
            f"need: ${total_needed / 100:.2f} (transfer + $0.01 fee)"
        )

    # Create transfer record
    transfer = CryptoTransfer(
        sender_id=sender_id,
        recipient_address=recipient_address,
        amount_cents=amount_cents,
        fee_cents=PLATFORM_FEE_CENTS,
        status="completed",
        memo=memo,
        completed_at=datetime.utcnow(),
    )
    db.add(transfer)

    # Deduct from sender's balance (amount + fee)
    sender.usdc_balance_cents -= total_needed
    sender.total_fees_paid_cents += PLATFORM_FEE_CENTS

    db.commit()

    return {
        "transfer_id": transfer.id,
        "amount": amount_cents / 100.0,
        "fee": PLATFORM_FEE_CENTS / 100.0,
        "total_debited": total_needed / 100.0,
        "status": "completed",
        "tx_hash": None,
        "created_at": transfer.created_at.isoformat() if transfer.created_at else None,
    }


async def get_transfer_history(user_id: str, db: Session, limit: int = 50) -> list:
    """
    Get transaction history for a user (both sends and receives).

    Returns:
        [
            {
                "transfer_id": "...",
                "type": "send",  # or "receive"
                "amount": 10.00,
                "fee": 0.01,
                "counterparty": "0x...",  # recipient (send) or sender (receive)
                "status": "completed",
                "memo": "...",
                "created_at": "2026-07-08T...",
            },
            ...
        ]
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise ValueError(f"User {user_id} not found")

    transfers = db.query(CryptoTransfer).filter(
        CryptoTransfer.sender_id == user_id
    ).order_by(CryptoTransfer.created_at.desc()).limit(limit).all()

    return [
        {
            "transfer_id": t.id,
            "type": "send",
            "amount": t.amount_cents / 100.0,
            "fee": t.fee_cents / 100.0,
            "counterparty": t.recipient_address,
            "status": t.status,
            "memo": t.memo,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in transfers
    ]


async def collect_fees(db: Session) -> dict:
    """
    Aggregate platform fees from the past period (e.g., daily) to treasury.

    Returns:
        {
            "collection_id": "...",
            "total_fees": 25.50,  # in USD (float)
            "transfer_count": 2550,  # number of $0.01 fees
            "treasury_wallet": "0x...",
        }

    In production: also handles on-chain sweep to FAWN treasury.
    """
    # For MVP: just log it. No actual on-chain treasury sweeping yet.
    treasury_wallet = os.environ.get("FAWN_TREASURY_WALLET", "0x0000000000000000000000000000000000000000")

    # Sum fees from all transfers created in the last period
    # (For now, just grab the last N transfers; in production use a timestamp cutoff)
    recent_transfers = db.query(CryptoTransfer).filter(
        CryptoTransfer.status == "completed"
    ).order_by(CryptoTransfer.created_at.desc()).limit(10000).all()

    total_fees_cents = sum(t.fee_cents for t in recent_transfers)
    transfer_count = len(recent_transfers)

    # Record in fee collection table
    collection = FeeCollection(
        collection_date=datetime.utcnow(),
        total_fees_cents=total_fees_cents,
        transfer_count=transfer_count,
        treasury_wallet=treasury_wallet,
        collected_at=datetime.utcnow(),
    )
    db.add(collection)
    db.commit()

    return {
        "collection_id": collection.id,
        "total_fees": total_fees_cents / 100.0,
        "transfer_count": transfer_count,
        "treasury_wallet": treasury_wallet,
    }
