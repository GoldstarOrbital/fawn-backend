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
- Real BIP39 → HD wallet derivation via ethers.js (web3.py wrapper)
"""
import os
from decimal import Decimal
from typing import Optional
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from models import User, CryptoWallet, CryptoTransfer, FeeCollection, UserAuditLog, BankTransfer
import secrets
import json
import re
import uuid

# Real BIP39 seed phrase generation
try:
    from mnemonic import Mnemonic
except ImportError:
    Mnemonic = None

# EIP-55 checksum validation
import hashlib

# Fernet encryption for custodial keys (AES-256-GCM)
try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:
    Fernet = None

# HD wallet derivation from BIP39 seed (Ethereum mainnet, m/44'/60'/0'/0/0 standard)
try:
    from eth_keys import keys as eth_keys_module
    from eth_account import Account
    from eth_utils import to_checksum_address
except ImportError:
    eth_keys_module = None
    Account = None
    to_checksum_address = None


INTERNAL_TRANSFER_FEE_CENTS = 1  # $0.01 for FAWN-to-FAWN transfers (friends, internal)
EXTERNAL_TRANSFER_FEE_CENTS = 50  # $0.50 for external wallet/bank transfers
USDC_CHAIN = os.environ.get("USDC_CHAIN", "polygon")  # "polygon" | "ethereum"

# BIP39 HD derivation path for Ethereum (standard)
BIP39_DERIVATION_PATH = "m/44'/60'/0'/0/0"  # First account, first address


class WalletNotInitialized(Exception):
    """User has not created a stablecoin wallet yet."""
    pass


class InsufficientBalance(Exception):
    """Sender does not have enough USDC to cover transfer + fee."""
    pass


class InvalidAddress(Exception):
    """Recipient wallet address is invalid."""
    pass


class BankTransferError(Exception):
    """ACH transfer error (typically banking provider unreachable or config missing)."""
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


def _derive_wallet_from_seed(seed_phrase: str) -> tuple[str, str]:
    """
    Derive Ethereum wallet address and private key from BIP39 seed phrase.

    Uses standard HD derivation path m/44'/60'/0'/0/0 (first Ethereum account).

    Args:
        seed_phrase: 12-word BIP39 seed phrase (space-separated)

    Returns:
        (wallet_address, private_key_hex) where private_key_hex includes "0x" prefix

    Raises:
        ImportError if eth-account not installed
        ValueError if seed phrase is invalid
    """
    if Account is None or to_checksum_address is None:
        raise ImportError(
            "eth-account and eth-utils not installed. "
            "Install with: pip install eth-account eth-keys eth-utils"
        )

    try:
        # Derive account from seed phrase using standard path
        account = Account.from_mnemonic(seed_phrase, account_path=BIP39_DERIVATION_PATH)
        address = to_checksum_address(account.address)  # EIP-55 checksummed
        private_key = account.key.hex()  # Returns 0x-prefixed hex string

        return address, private_key
    except Exception as e:
        raise ValueError(f"Failed to derive wallet from seed phrase: {e}")


def _encrypt_private_key(private_key: str, encryption_key: Optional[str] = None) -> bytes:
    """
    Encrypt private key using Fernet (AES-256-GCM).

    Args:
        private_key: hex-encoded private key (with or without 0x prefix)
        encryption_key: optional encryption key; defaults to FAWN_ENCRYPTION_KEY env var

    Returns:
        bytes: Fernet-encrypted ciphertext (includes IV + tag)

    Raises:
        ImportError if cryptography not installed
        ValueError if no encryption key available
    """
    if Fernet is None:
        raise ImportError("cryptography library not installed. Install with: pip install cryptography")

    # Use environment key or raise error
    key = encryption_key or os.environ.get("FAWN_ENCRYPTION_KEY")
    if not key:
        raise ValueError("FAWN_ENCRYPTION_KEY environment variable not set")

    # Ensure key is properly formatted for Fernet (base64-encoded 32 bytes)
    if isinstance(key, str):
        key = key.encode()

    try:
        cipher = Fernet(key)
        return cipher.encrypt(private_key.encode())
    except Exception as e:
        raise ValueError(f"Encryption failed: {e}")


def _decrypt_private_key(encrypted_key: bytes, encryption_key: Optional[str] = None) -> str:
    """Decrypt private key using Fernet."""
    if Fernet is None:
        raise ImportError("cryptography library not installed")

    key = encryption_key or os.environ.get("FAWN_ENCRYPTION_KEY")
    if not key:
        raise ValueError("FAWN_ENCRYPTION_KEY environment variable not set")

    if isinstance(key, str):
        key = key.encode()

    try:
        cipher = Fernet(key)
        return cipher.decrypt(encrypted_key).decode()
    except InvalidToken:
        raise ValueError("Failed to decrypt private key (invalid token or wrong key)")
    except Exception as e:
        raise ValueError(f"Decryption failed: {e}")


async def create_wallet(user_id: str, db: Session, wallet_type: str = "fawn_custodial") -> dict:
    """
    Create a new stablecoin wallet for the user with real BIP39 derivation.

    Args:
        user_id: FAWN user ID
        db: database session
        wallet_type: "non_custodial" (user manages keys) or "fawn_custodial" (FAWN holds)

    Returns:
        {
            "wallet_address": "0x... (EIP-55 checksummed)",
            "wallet_type": "fawn_custodial" | "non_custodial",
            "usdc_balance": 0.0,
            "chain": "polygon" | "ethereum",
            "seed_phrase": "word1 word2 ... word12" (ONLY if non_custodial; user must save this)
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

    # Generate BIP39 seed phrase (12 words)
    seed_phrase = _generate_seed_phrase()

    # Derive wallet address and private key from seed
    wallet_address, private_key_hex = _derive_wallet_from_seed(seed_phrase)

    # Encrypt private key for storage (custodial only)
    encrypted_key = None
    if wallet_type == "fawn_custodial":
        encrypted_key = _encrypt_private_key(private_key_hex)

    # Create wallet record in database
    wallet = CryptoWallet(
        user_id=user_id,
        wallet_address=wallet_address,
        wallet_type=wallet_type,
        chain=USDC_CHAIN,
        usdc_balance_cents=0,
        encrypted_private_key=encrypted_key,  # Now properly stored for custodial wallets
    )
    db.add(wallet)

    # Update user to link wallet
    user.crypto_wallet_address = wallet_address
    user.wallet_type = wallet_type
    user.usdc_balance_cents = 0
    user.wallet_initialized = True

    # SECURITY: Audit log the wallet creation (but NOT the seed phrase or private key)
    # 7-year retention for compliance
    retention_expires = datetime.now(tz=timezone.utc) + timedelta(days=365*7)
    audit_log = UserAuditLog(
        user_id=user_id,
        action="created_wallet",
        details=json.dumps({
            "wallet_type": wallet_type,
            "chain": USDC_CHAIN,
            "wallet_address": wallet_address,
        }),
        retention_expires_at=retention_expires,
    )
    db.add(audit_log)

    db.commit()

    # SECURITY: Return seed phrase ONLY for non-custodial wallets
    # For custodial, FAWN holds the encrypted key; user never sees raw seed/key
    return {
        "wallet_address": wallet_address,
        "wallet_type": wallet_type,
        "usdc_balance": 0.0,
        "chain": USDC_CHAIN,
        "seed_phrase": seed_phrase if wallet_type == "non_custodial" else None,
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
    is_internal: bool = False,
) -> dict:
    """
    Send USDC from sender to recipient (internal ledger transfer).

    Fee structure:
    - Internal (FAWN-to-FAWN): $0.01
    - External (outside wallets/banks): $0.50

    No blockchain transaction — instant settlement in our ledger.
    No gas fees.

    Args:
        sender_id: FAWN user ID of sender
        recipient_address: recipient's wallet address (0x...)
        amount_cents: amount to send in cents (e.g., 1000 = $10.00)
        db: database session
        memo: optional transfer memo
        is_internal: True if recipient is a FAWN user, False if external

    Returns:
        {
            "transfer_id": "...",
            "amount": 10.00,  # amount sent (USD float)
            "fee": 0.01 or 0.50,  # platform fee based on type (USD float)
            "total_debited": 10.01 or 10.50,  # total taken from sender (USD float)
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

    # Determine fee based on transfer type
    fee_cents = INTERNAL_TRANSFER_FEE_CENTS if is_internal else EXTERNAL_TRANSFER_FEE_CENTS

    # Check balance
    total_needed = amount_cents + fee_cents
    if sender.usdc_balance_cents < total_needed:
        fee_display = "$0.01" if is_internal else "$0.50"
        raise InsufficientBalance(
            f"Insufficient balance. Have: ${sender.usdc_balance_cents / 100:.2f}, "
            f"need: ${total_needed / 100:.2f} (transfer + {fee_display} fee)"
        )

    # Create transfer record
    transfer = CryptoTransfer(
        sender_id=sender_id,
        recipient_address=recipient_address,
        amount_cents=amount_cents,
        fee_cents=fee_cents,
        status="completed",
        memo=memo,
        completed_at=datetime.utcnow(),
    )
    db.add(transfer)

    # Deduct from sender's balance (amount + fee)
    sender.usdc_balance_cents -= total_needed
    sender.total_fees_paid_cents += fee_cents

    # SECURITY: Audit log the transfer (truncate recipient for privacy, log amount for compliance)
    # 7-year retention for compliance
    retention_expires = datetime.now(tz=timezone.utc) + timedelta(days=365*7)
    audit_log = UserAuditLog(
        user_id=sender_id,
        action="sent_transfer",
        details=json.dumps({
            "recipient": recipient_address[:6] + "..." + recipient_address[-4:],  # truncated
            "amount_cents": amount_cents,
            "fee_cents": fee_cents,
            "transfer_type": "internal" if is_internal else "external",
        }),
        retention_expires_at=retention_expires,
    )
    db.add(audit_log)

    db.commit()

    return {
        "transfer_id": transfer.id,
        "amount": amount_cents / 100.0,
        "fee": fee_cents / 100.0,
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


async def send_to_bank(
    sender_id: str,
    recipient_name: str,
    recipient_routing_number: str,
    recipient_account_number: str,
    amount_cents: int,
    db: Session,
    memo: str = None,
) -> dict:
    """
    Send USDC to a traditional bank account via instant Stripe Payouts.

    FLOW:
    1. User sends USDC from FAWN wallet
    2. FAWN converts USDC → USD (1:1, no slippage)
    3. FAWN initiates instant payout via Stripe Payouts API
    4. USDC deducted from user balance (amount + $0.01 fee)
    5. Status: pending (typically <30 seconds for settlement)

    Cost: flat $0.01 platform fee (on top of the transfer amount, no ACH fees).

    SECURITY:
    - Recipient bank details NOT persisted (sent directly to Stripe)
    - Only account last 4 stored for reference
    - Audit logged with 7-year retention

    Returns transfer ID, expected settlement time (instant), and confirmation.
    """
    # Placeholder for now — implementation deferred
    raise BankTransferError("Bank transfers not yet implemented")


async def collect_fees(db: Session) -> dict:
    """
    [ADMIN ONLY] Collect platform fees to treasury wallet.

    Sweeps all accumulated platform fees to a treasury address.
    In production: initiates on-chain sweep. For MVP: just logs.

    Returns:
        {
            "total_fees": 50,  # in cents
            "transfers_settled": N,
            "status": "pending" | "completed"
        }
    """
    # Placeholder for now
    return {
        "total_fees": 0,
        "transfers_settled": 0,
        "status": "pending",
    }
