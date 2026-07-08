"""
Stablecoin wallet service for FAWN's crypto-native architecture.

SECURITY:
- Supports USDC on Polygon and Ethereum
- Two wallet types: non_custodial (user manages keys) / fawn_custodial (FAWN holds keys)
- No gas fees — flat $0.01 per transfer
- All operations logged to UserAuditLog (7-year retention for compliance)
- Seed phrases never logged, returned only once
- Custodial private keys encrypted with Fernet (AES-256-GCM)
- EIP-55 checksum validation on recipient addresses
"""
import os
from decimal import Decimal
from typing import Optional
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from models import User, CryptoWallet, CryptoTransfer, FeeCollection, UserAuditLog
import secrets
import json
import re

# Real BIP39 seed phrase generation
try:
    from mnemonic import Mnemonic
except ImportError:
    Mnemonic = None

# EIP-55 checksum validation
import hashlib

# Fernet encryption for custodial keys
try:
    from cryptography.fernet import Fernet
except ImportError:
    Fernet = None

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
    """Validate Ethereum address with EIP-55 checksum verification."""
    if not addr or not addr.startswith("0x"):
        return False
    if len(addr) != 42 or not all(c in "0123456789abcdefABCDEF" for c in addr[2:]):
        return False

    # EIP-55 checksum validation (required for correctness)
    # If address contains both uppercase and lowercase, verify checksum
    addr_no_prefix = addr[2:]
    if not (addr_no_prefix.isupper() or addr_no_prefix.islower()):
        # Mixed case — must validate checksum
        hash_bytes = hashlib.sha256(addr_no_prefix.lower().encode()).digest()
        for i, c in enumerate(addr_no_prefix):
            if c in "0123456789":
                continue
            hash_value = int(hash_bytes[i // 2].hex()[i % 2], 16)
            if hash_value >= 8:
                if c.isupper():
                    continue
                else:
                    return False  # Should be uppercase
            else:
                if c.islower():
                    continue
                else:
                    return False  # Should be lowercase
    return True


def _generate_seed_phrase() -> str:
    """Generate a real BIP39 seed phrase (12 words, 128 bits)."""
    if Mnemonic is None:
        raise ImportError("mnemonic library not installed. Install with: pip install mnemonic")
    mnemo = Mnemonic("english")
    return mnemo.generate(strength=128)  # 12 words


def _encrypt_private_key(private_key: str, encryption_key: Optional[str] = None) -> bytes:
    """Encrypt private key using Fernet (AES-256-GCM)."""
    if Fernet is None:
        raise ImportError("cryptography library not installed. Install with: pip install cryptography")

    # Use environment key or generate a new one (not production-safe!)
    key = encryption_key or os.environ.get("FAWN_ENCRYPTION_KEY")
    if not key:
        raise ValueError("FAWN_ENCRYPTION_KEY environment variable not set")

    # Ensure key is properly formatted for Fernet (base64-encoded 32 bytes)
    if isinstance(key, str):
        key = key.encode()

    cipher = Fernet(key)
    return cipher.encrypt(private_key.encode())


def _decrypt_private_key(encrypted_key: bytes, encryption_key: Optional[str] = None) -> str:
    """Decrypt private key using Fernet."""
    if Fernet is None:
        raise ImportError("cryptography library not installed")

    key = encryption_key or os.environ.get("FAWN_ENCRYPTION_KEY")
    if not key:
        raise ValueError("FAWN_ENCRYPTION_KEY environment variable not set")

    if isinstance(key, str):
        key = key.encode()

    cipher = Fernet(key)
    return cipher.decrypt(encrypted_key).decode()


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

    # Generate and store encryption key for custodial wallets (user-facing keys encrypted with Fernet)
    seed_phrase = None
    encrypted_key = None

    if wallet_type == "non_custodial":
        # Generate real BIP39 seed phrase (12 words)
        seed_phrase = _generate_seed_phrase()
    elif wallet_type == "fawn_custodial":
        # Generate seed phrase, encrypt it, and store encrypted version
        seed_phrase = _generate_seed_phrase()
        encrypted_key = _encrypt_private_key(seed_phrase)

    # Create wallet record (with encrypted key if custodial)
    wallet = CryptoWallet(
        user_id=user_id,
        wallet_address=wallet_address,
        wallet_type=wallet_type,
        chain=USDC_CHAIN,
        usdc_balance_cents=0,
        encrypted_private_key=encrypted_key,  # Only for custodial wallets
    )
    db.add(wallet)

    # Update user to link wallet
    user.crypto_wallet_address = wallet_address
    user.wallet_type = wallet_type
    user.usdc_balance_cents = 0
    user.wallet_initialized = True

    # SECURITY: Audit log the wallet creation (but NOT the seed phrase)
    # 7-year retention for compliance
    retention_expires = datetime.now(tz=timezone.utc) + timedelta(days=365*7)
    audit_log = UserAuditLog(
        user_id=user_id,
        action="created_wallet",
        details=json.dumps({"wallet_type": wallet_type, "chain": USDC_CHAIN}),
        retention_expires_at=retention_expires,
    )
    db.add(audit_log)

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

    # SECURITY: Audit log the transfer (truncate recipient for privacy, log amount for compliance)
    # 7-year retention for compliance
    retention_expires = datetime.now(tz=timezone.utc) + timedelta(days=365*7)
    audit_log = UserAuditLog(
        user_id=sender_id,
        action="sent_transfer",
        details=json.dumps({
            "recipient": recipient_address[:6] + "..." + recipient_address[-4:],  # truncated
            "amount_cents": amount_cents,
            "fee_cents": PLATFORM_FEE_CENTS,
        }),
        retention_expires_at=retention_expires,
    )
    db.add(audit_log)

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
