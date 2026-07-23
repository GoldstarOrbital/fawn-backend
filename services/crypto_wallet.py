"""
Stablecoin wallet service for FAWN's crypto-native architecture.

SECURITY:
- Supports USDC on Polygon and Ethereum
- Two wallet types: non_custodial (user manages keys) / fawn_custodial (FAWN holds keys)
- No gas fees — flat $0.01 per transfer
- All operations logged to UserAuditLog (7-year retention for compliance)
- Seed phrases never logged, returned only once
- Custodial private keys envelope-encrypted with Fernet (AES-256-GCM): a
  random per-wallet DEK encrypts the key, a master KEK (FAWN_ENCRYPTION_KEY)
  wraps the DEK. Wallets created before this existed still decrypt via the
  older direct-KEK scheme (key_version NULL) -- see _decrypt_private_key.
- EIP-55 checksum validation on recipient addresses
- Real BIP39 → HD wallet derivation via ethers.js (web3.py wrapper)
"""
import os
import asyncio
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

# Fernet encryption for custodial keys (AES-256-GCM)
try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:
    Fernet = None

# HD wallet derivation from BIP39 seed (Ethereum mainnet, m/44'/60'/0'/0/0 standard)
# keccak is also EIP-55 checksum validation's real hash function (see
# _is_valid_eth_address) -- Ethereum addresses are NOT SHA-256 checksummed.
try:
    from eth_keys import keys as eth_keys_module
    from eth_account import Account
    from eth_utils import to_checksum_address, keccak
except ImportError:
    eth_keys_module = None
    Account = None
    to_checksum_address = None
    keccak = None


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
    """
    Validate an Ethereum address, including real EIP-55 checksum
    verification for mixed-case input.

    Fixed a live crash + a wrong-algorithm bug found while adding the
    treasury wallet: this used to index into the hash *bytes* with
    `hash_bytes[i // 2]` (an int, not a byte) then call `.hex()` on that
    int, which raises AttributeError for any address whose checksum loop
    actually runs past a `continue` -- true of essentially every real
    checksummed address (a synthetic all-digit or all-repeated-letter test
    address never hit it, which is why this went unnoticed). It also
    hashed with SHA-256, not the Keccak-256 EIP-55 actually specifies, so
    even the non-crashing path would have accepted/rejected addresses
    incorrectly. Both are fixed below. A real recipient address pasted
    from any wallet app (mixed case is the norm) was very likely already
    crashing sends in production before this fix.
    """
    if not addr or not addr.startswith("0x"):
        return False
    if len(addr) != 42 or not all(c in "0123456789abcdefABCDEF" for c in addr[2:]):
        return False

    addr_no_prefix = addr[2:]
    if addr_no_prefix.isupper() or addr_no_prefix.islower():
        # All one case -- not checksummed, nothing to verify.
        return True

    # Mixed case: real EIP-55 checksum. keccak256 of the LOWERCASE address
    # (as ASCII text), then for each hex-letter position, the
    # corresponding hex DIGIT of the hash's own hex string decides
    # upper (>=8) vs lower (<8) case. Digits in the address are untouched
    # by the checksum and skipped.
    hash_hex = keccak(addr_no_prefix.lower().encode()).hex()
    for i, c in enumerate(addr_no_prefix):
        if c in "0123456789":
            continue
        nibble = int(hash_hex[i], 16)
        if nibble >= 8:
            if not c.isupper():
                return False  # Should be uppercase
        else:
            if not c.islower():
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
        # Enable hdwallet features (required by eth-account for mnemonic support)
        Account.enable_unaudited_hdwallet_features()

        # Derive account from seed phrase using standard path
        account = Account.from_mnemonic(seed_phrase, account_path=BIP39_DERIVATION_PATH)
        address = to_checksum_address(account.address)  # EIP-55 checksummed
        private_key = account.key.hex()  # Returns 0x-prefixed hex string

        return address, private_key
    except Exception as e:
        raise ValueError(f"Failed to derive wallet from seed phrase: {e}")


def _encrypt_private_key(private_key: str, encryption_key: Optional[str] = None) -> bytes:
    """
    Encrypt private key directly with the master key (legacy scheme, kept
    for backward compatibility with wallets created before envelope
    encryption existed -- see _encrypt_private_key_envelope for new
    wallets, and _decrypt_private_key for how a stored row picks which
    scheme decrypts it).

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


def _decrypt_private_key_legacy(encrypted_key: bytes, encryption_key: Optional[str] = None) -> str:
    """Decrypt a private key encrypted directly with the master key (the
    original, pre-envelope-encryption scheme). See _decrypt_private_key."""
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


def _active_kek() -> bytes:
    """Current Key Encryption Key, used to wrap/unwrap per-wallet DEKs.
    Not used to encrypt private keys directly (see module docstring on
    envelope encryption / CryptoWallet.wrapped_dek)."""
    key = os.environ.get("FAWN_ENCRYPTION_KEY")
    if not key:
        raise ValueError("FAWN_ENCRYPTION_KEY environment variable not set")
    return key.encode() if isinstance(key, str) else key


def _previous_kek() -> Optional[bytes]:
    """Prior KEK, if FAWN_ENCRYPTION_KEY has been rotated -- tried as a
    fallback so already-wrapped DEKs don't need re-wrapping synchronously
    with the rotation itself (see rotate_wallet_keys for the batch
    re-wrap that lets this eventually be retired)."""
    key = os.environ.get("FAWN_ENCRYPTION_KEY_PREVIOUS")
    if not key:
        return None
    return key.encode() if isinstance(key, str) else key


def _generate_dek() -> bytes:
    """A fresh, random per-wallet Data Encryption Key. Fernet.generate_key()
    produces a valid Fernet key itself, so the DEK can be used directly as
    a Fernet cipher key to encrypt the private key."""
    if Fernet is None:
        raise ImportError("cryptography library not installed. Install with: pip install cryptography")
    return Fernet.generate_key()


def _wrap_dek(dek: bytes, kek: bytes) -> bytes:
    """Encrypt (wrap) a DEK under a KEK for storage."""
    return Fernet(kek).encrypt(dek)


def _unwrap_dek(wrapped_dek: bytes) -> bytes:
    """Decrypt (unwrap) a DEK, trying the active KEK first and falling back
    to the previous KEK if set -- supports one-generation-back key
    rotation without a hard cutover."""
    try:
        return Fernet(_active_kek()).decrypt(wrapped_dek)
    except InvalidToken:
        previous = _previous_kek()
        if previous is None:
            raise ValueError("Failed to unwrap DEK with the active key, and no FAWN_ENCRYPTION_KEY_PREVIOUS is set to fall back to.")
        try:
            return Fernet(previous).decrypt(wrapped_dek)
        except InvalidToken:
            raise ValueError("Failed to unwrap DEK (invalid token under both the active and previous key).")


def _encrypt_private_key_envelope(private_key: str) -> tuple[bytes, bytes]:
    """
    Envelope-encrypt a private key: generate a random per-wallet DEK,
    encrypt the private key with it, then wrap the DEK with the master
    KEK. Bounds what the KEK ever directly touches to a 32-byte DEK
    rather than every private key, and makes KEK rotation cheap (re-wrap
    DEKs, not re-encrypt private keys).

    Returns:
        (ciphertext, wrapped_dek) -- both stored on CryptoWallet, alongside
        key_version="v2" so _decrypt_private_key knows how to reverse it.

    Raises:
        ImportError if cryptography not installed
        ValueError if no KEK is configured
    """
    dek = _generate_dek()
    ciphertext = Fernet(dek).encrypt(private_key.encode())
    wrapped_dek = _wrap_dek(dek, _active_kek())
    return ciphertext, wrapped_dek


def _decrypt_private_key_v2(encrypted_key: bytes, wrapped_dek: bytes) -> str:
    """Reverse of _encrypt_private_key_envelope: unwrap the DEK, then use
    it to decrypt the private key."""
    if Fernet is None:
        raise ImportError("cryptography library not installed")
    if not wrapped_dek:
        raise ValueError("key_version is 'v2' but no wrapped_dek is stored -- cannot decrypt.")
    try:
        dek = _unwrap_dek(wrapped_dek)
        return Fernet(dek).decrypt(encrypted_key).decode()
    except InvalidToken:
        raise ValueError("Failed to decrypt private key (DEK unwrapped, but the private key itself didn't decrypt under it).")


def _decrypt_private_key(encrypted_key: bytes, key_version: Optional[str] = None, wrapped_dek: Optional[bytes] = None) -> str:
    """Decrypt a custodial private key, dispatching by key_version.

    key_version=None (the default -- also matches every existing wallet
    row, whose key_version column is NULL) decrypts via the legacy
    direct-KEK scheme, exactly as this function always has -- every
    existing call site and test that calls _decrypt_private_key(x) with
    no other arguments is unaffected by envelope encryption's addition.
    key_version="v2" decrypts via the envelope scheme instead.
    """
    if key_version == "v2":
        return _decrypt_private_key_v2(encrypted_key, wrapped_dek)
    return _decrypt_private_key_legacy(encrypted_key)


def rotate_wallet_keys(db: Session) -> dict:
    """
    [ADMIN] Re-wrap every v2 wallet's DEK under the current active KEK.

    Only re-wraps DEKs (32 bytes each) -- never touches or re-encrypts
    the private keys themselves, which is the entire point of envelope
    encryption. Intended flow for rotating FAWN_ENCRYPTION_KEY:
    1. Set FAWN_ENCRYPTION_KEY_PREVIOUS to the current (soon-to-be-old) key.
    2. Set FAWN_ENCRYPTION_KEY to a newly generated key.
    3. Deploy, then call this once -- every v2 wallet's DEK gets unwrapped
       with the (now-previous) key and re-wrapped with the new active one.
    4. Once this reports zero failures, FAWN_ENCRYPTION_KEY_PREVIOUS can be
       cleared.
    Legacy (key_version is NULL) wallets are untouched -- they have no DEK
    to re-wrap; migrating them to v2 is a separate, opt-in operation, not
    something a key rotation should force.
    """
    wallets = db.query(CryptoWallet).filter(CryptoWallet.key_version == "v2").all()
    rotated, failures = 0, []
    for wallet in wallets:
        try:
            dek = _unwrap_dek(wallet.wrapped_dek)
            wallet.wrapped_dek = _wrap_dek(dek, _active_kek())
            rotated += 1
        except Exception as e:
            failures.append({"wallet_address": wallet.wallet_address, "error": str(e)})
    db.commit()
    return {"rotated": rotated, "failed": len(failures), "failures": failures}


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

    # Encrypt private key for storage (custodial only), using envelope
    # encryption (key_version "v2" -- see _encrypt_private_key_envelope).
    # Verify it round-trips BEFORE anything is persisted -- confirmed in
    # production: an earlier version of this function created
    # "fawn_custodial" wallets without ever persisting a usable key. The
    # wallet looked fully set up (real on-chain address, wallet_initialized
    # true) but nobody, not FAWN and not the user (custodial wallets never
    # return a seed phrase), held anything that could sign for it. Real
    # deposits sent there are permanently unrecoverable. Failing here,
    # before any DB write, means a bad key produces a clean wallet-creation
    # error instead of a wallet a user can deposit real money into that
    # nobody can ever move back out.
    encrypted_key = None
    wrapped_dek = None
    key_version = None
    if wallet_type == "fawn_custodial":
        encrypted_key, wrapped_dek = _encrypt_private_key_envelope(private_key_hex)
        key_version = "v2"
        if _decrypt_private_key(encrypted_key, key_version=key_version, wrapped_dek=wrapped_dek) != private_key_hex:
            raise ValueError("Custodial key failed round-trip verification -- refusing to create an unsignable wallet.")

    # Create wallet record in database
    wallet = CryptoWallet(
        user_id=user_id,
        wallet_address=wallet_address,
        wallet_type=wallet_type,
        chain=USDC_CHAIN,
        usdc_balance_cents=0,
        encrypted_private_key=encrypted_key,  # Now properly stored for custodial wallets
        wrapped_dek=wrapped_dek,
        key_version=key_version,
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


async def get_or_create_treasury_wallet(db: Session) -> tuple[CryptoWallet, Optional[str]]:
    """
    Return FAWN's treasury wallet -- the destination collect_fees() sweeps
    accumulated platform fees to -- creating it once if it doesn't exist
    yet. Identified by CryptoWallet.is_treasury == True, a fawn_custodial
    wallet with no owning user (user_id NULL).

    Returns:
        (wallet, seed_phrase) -- seed_phrase is the 12-word BIP39 phrase
        ONLY the one time a new treasury wallet is created, exactly the
        same one-time-reveal contract as a non-custodial user wallet
        (never logged, never persisted, returned once so it can be backed
        up externally -- e.g. imported into cold/hardware storage once
        real revenue volume justifies moving treasury off a hot wallet).
        Losing this backup doesn't strand funds either way: FAWN's own
        encrypted copy of the key can always sign for it going forward.
        On every subsequent call, seed_phrase is None.

    Raises:
        ValueError if the round-trip verification of the newly-created
        treasury key fails (same guard as create_wallet -- refuses to
        create a treasury wallet nobody can actually sign for).
    """
    from sqlalchemy.exc import IntegrityError

    existing = db.query(CryptoWallet).filter(CryptoWallet.is_treasury == True).first()  # noqa: E712
    if existing:
        return existing, None

    seed_phrase = _generate_seed_phrase()
    wallet_address, private_key_hex = _derive_wallet_from_seed(seed_phrase)

    encrypted_key, wrapped_dek = _encrypt_private_key_envelope(private_key_hex)
    if _decrypt_private_key(encrypted_key, key_version="v2", wrapped_dek=wrapped_dek) != private_key_hex:
        raise ValueError("Treasury key failed round-trip verification -- refusing to create an unsignable treasury wallet.")

    wallet = CryptoWallet(
        user_id=None,
        wallet_address=wallet_address,
        wallet_type="fawn_custodial",
        chain=USDC_CHAIN,
        usdc_balance_cents=0,
        encrypted_private_key=encrypted_key,
        wrapped_dek=wrapped_dek,
        key_version="v2",
        is_treasury=True,
    )
    db.add(wallet)
    try:
        db.commit()
    except IntegrityError:
        # The check above and this insert aren't atomic -- a concurrent
        # caller (the daily scheduler racing a manual admin call, or two
        # Railway replicas) could have created the treasury wallet in the
        # gap between them. models.py's idx_one_treasury_wallet partial
        # unique index is what turns that race into a real, catchable
        # conflict here instead of two treasury rows silently coexisting
        # (which would otherwise let fee sweeps split unpredictably across
        # two addresses). Roll back this attempt and use whichever wallet
        # actually won -- it's already the real treasury, this call just
        # lost the race to create it.
        db.rollback()
        winner = db.query(CryptoWallet).filter(CryptoWallet.is_treasury == True).first()  # noqa: E712
        if winner is None:
            # Should be unreachable -- a conflict means a row satisfying
            # the unique index exists -- but don't silently return nothing.
            raise RuntimeError("Treasury wallet creation conflicted, but no treasury wallet is now findable.")
        return winner, None
    db.refresh(wallet)

    # UserAuditLog is scoped to actions taken BY a user (its FK is NOT
    # NULL) -- treasury wallet creation is a system/admin action with no
    # owning user, so it's logged the same way other system-level startup
    # events in this codebase are (see main.py's "[startup]"/"[podcast]"
    # console logs), not via UserAuditLog.
    print(f"[treasury] created new treasury wallet: {wallet_address} (chain: {USDC_CHAIN})")

    return wallet, seed_phrase


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
    Send USDC from sender to recipient, settled with a real on-chain
    transaction (see services/onchain_send.py).

    Fee structure:
    - Internal (FAWN-to-FAWN): $0.01
    - External (outside wallets/banks): $0.50
    The fee itself is a FAWN platform fee deducted from the ledger only
    -- only amount_cents (not the fee) moves on-chain to the recipient.

    Only "fawn_custodial" wallets can be sent from -- FAWN needs a usable
    stored private key to sign. "non_custodial" wallets (and any
    custodial wallet with no usable key -- see create_wallet's
    round-trip guard) raise CannotSignTransaction; there is no ledger-
    only fallback, since silently moving nothing while claiming success
    is exactly the bug this replaces.

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
            "chain": "polygon" | "base",
            "tx_hash": "0x...",  # real on-chain transaction hash
            "created_at": "2026-07-08T...",
        }

    Raises:
        WalletNotInitialized if sender has no wallet
        InvalidAddress if recipient_address is malformed
        InsufficientBalance if sender can't cover transfer + fee (ledger check)
        onchain_send.CannotSignTransaction if sender's wallet can't be signed for
        onchain_send.SendLimitExceeded, onchain_send.VelocityLimitExceeded if a hard cap is hit
        onchain_send.NoChainHasSufficientBalance if no single chain covers the amount
        onchain_send.OnchainSendFailed if broadcasting the transaction fails

    A large first-time send doesn't raise or settle -- it returns with
    status "pending_review" and no tx_hash, held for an admin to approve
    via POST /admin/approve-transfer (see routers/admin_credit.py). This
    is the classic account-takeover pattern (immediate drain to a brand-
    new address); settling it instantly regardless of how suspicious it
    looks isn't acceptable for a wallet FAWN can sign for on the user's
    behalf.
    """
    from services import onchain_send
    from config import settings

    if not _is_valid_eth_address(recipient_address):
        raise InvalidAddress(f"Invalid recipient address: {recipient_address}")

    sender = db.query(User).filter(User.id == sender_id).first()
    if not sender or not sender.crypto_wallet_address:
        raise WalletNotInitialized(f"Sender {sender_id} has no stablecoin wallet")

    # Determine fee based on transfer type
    fee_cents = INTERNAL_TRANSFER_FEE_CENTS if is_internal else EXTERNAL_TRANSFER_FEE_CENTS

    # Cheap ledger pre-check before touching any RPC -- the authoritative
    # per-chain check happens inside send_onchain_usdc, since a wallet's
    # ledger total can exceed what any single chain actually holds.
    total_needed = amount_cents + fee_cents
    if sender.usdc_balance_cents < total_needed:
        fee_display = "$0.01" if is_internal else "$0.50"
        raise InsufficientBalance(
            f"Insufficient balance. Have: ${sender.usdc_balance_cents / 100:.2f}, "
            f"need: ${total_needed / 100:.2f} (transfer + {fee_display} fee)"
        )

    from services.address_risk import flag_if_risky_for_review

    is_large_new_recipient = (
        amount_cents >= settings.new_recipient_review_threshold_cents
        and onchain_send.is_first_time_recipient(sender_id, recipient_address, db)
    )
    # Checked regardless of amount/recipient-history -- a real-time
    # threat-intel flag (phishing, theft, mixer, etc.) doesn't get safer
    # just because the amount is small or it's a repeat recipient.
    is_flagged_risky = await flag_if_risky_for_review(sender_id, recipient_address, db)

    if is_large_new_recipient or is_flagged_risky:
        # The per-transaction hard cap is static (doesn't depend on when
        # it's checked), so enforce it here too, not just at approval
        # time -- a request that structurally can never be approved
        # shouldn't be admitted to the review queue in the first place,
        # where it would just sit stuck with no automatic cleanup.
        if amount_cents > settings.max_send_cents_per_tx:
            raise onchain_send.SendLimitExceeded(
                f"${amount_cents/100:.2f} exceeds the ${settings.max_send_cents_per_tx/100:.2f} "
                f"per-transaction limit."
            )

        hold_reason = (
            "recipient flagged by address risk check" if is_flagged_risky
            else "first-time recipient above review threshold"
        )
        transfer = CryptoTransfer(
            sender_id=sender_id,
            recipient_address=recipient_address,
            amount_cents=amount_cents,
            fee_cents=fee_cents,
            status="pending_review",
            memo=memo,
        )
        db.add(transfer)
        retention_expires = datetime.now(tz=timezone.utc) + timedelta(days=365 * 7)
        db.add(UserAuditLog(
            user_id=sender_id,
            action="send_held_for_review",
            details=json.dumps({
                "transfer_id": transfer.id,
                "recipient": recipient_address[:6] + "..." + recipient_address[-4:],
                "amount_cents": amount_cents,
                "reason": hold_reason,
                "timestamp": datetime.utcnow().isoformat(),
            }),
            retention_expires_at=retention_expires,
        ))
        db.commit()
        return {
            "transfer_id": transfer.id,
            "amount": amount_cents / 100.0,
            "fee": fee_cents / 100.0,
            "total_debited": total_needed / 100.0,
            "status": "pending_review",
            "chain": None,
            "tx_hash": None,
            "created_at": transfer.created_at.isoformat() if transfer.created_at else None,
        }

    # Settle on-chain FIRST -- only touch the ledger after real money has
    # actually moved. A failure here must leave the ledger untouched.
    settlement = await onchain_send.send_onchain_usdc(sender, recipient_address, amount_cents, db)

    # Create transfer record
    transfer = CryptoTransfer(
        sender_id=sender_id,
        recipient_address=recipient_address,
        amount_cents=amount_cents,
        fee_cents=fee_cents,
        status="completed",
        tx_hash=settlement["tx_hash"],
        chain=settlement["chain"],
        memo=memo,
        completed_at=datetime.utcnow(),
    )
    db.add(transfer)

    # Deduct from sender's balance (amount + fee)
    sender.usdc_balance_cents -= total_needed
    sender.total_fees_paid_cents += fee_cents

    # Only `amount_cents` moved on-chain just now (see this function's
    # docstring) -- the fee stays as real USDC sitting in sender's own
    # on-chain wallet even though the ledger already debited it. Track it
    # as owed-to-treasury so collect_fees() can sweep it later instead of
    # it silently drifting out of sync with the ledger forever.
    sender_wallet_row = db.query(CryptoWallet).filter(
        CryptoWallet.wallet_address.ilike(sender.crypto_wallet_address)
    ).first()
    if sender_wallet_row:
        # DB-side relative increment, not a Python-side read-modify-write
        # -- two genuinely concurrent sends from the same sender's wallet
        # (double-tap, multiple devices) would otherwise both read the
        # same starting value and the later commit would silently clobber
        # the earlier increment (last-writer-wins), under-tracking fees
        # actually owed to treasury. Same pattern collect_fees() uses for
        # its own claim-restore.
        db.query(CryptoWallet).filter(CryptoWallet.id == sender_wallet_row.id).update(
            {"pending_fee_cents": CryptoWallet.pending_fee_cents + fee_cents},
            synchronize_session=False,
        )

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
            "chain": settlement["chain"],
            "tx_hash": settlement["tx_hash"],
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
        "chain": settlement["chain"],
        "tx_hash": settlement["tx_hash"],
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
            "chain": t.chain,
            "tx_hash": t.tx_hash,
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
    [ADMIN ONLY] Sweep every custodial wallet's accumulated, unswept
    platform fee to FAWN's treasury wallet with a real on-chain transfer.

    Each completed send already deducts amount+fee from the sender's
    ledger balance but only ever moves `amount` on-chain (see send_usdc's
    docstring) -- the fee portion is real USDC that stays sitting in the
    sender's own on-chain wallet, tracked as CryptoWallet.pending_fee_cents
    until this sweeps it. Sweeping per-wallet in a periodic batch (rather
    than a second on-chain transfer on every single send) trades a bit of
    latency on fee realization for roughly half the gas cost per transfer.

    A per-wallet sweep failure (e.g. a stale on-chain balance, an RPC
    hiccup, a wallet that's since run out of native gas) does not block
    the others -- partial progress is committed and returned so the next
    run picks up exactly what's left.

    Concurrency: the admin endpoint is rate-limited but not mutually
    exclusive, and the daily scheduler could overlap a manual admin call.
    Without a claim step, two concurrent runs reading the same wallet's
    pending_fee_cents before either writes back would both sweep it --
    a real double-spend (an extra on-chain transfer out of the user's
    wallet beyond what's actually owed) followed by the second run's
    decrement driving pending_fee_cents negative, which would violate
    CryptoWallet's own >= 0 constraint at commit time and, worse, could
    take down the WHOLE batch's commit -- losing the audit trail for
    every other wallet already swept in the same run, even though their
    on-chain transfers already went through for real. Both are closed by
    atomically claiming each wallet's fee (zero it, but only if it still
    matches what was just read) before attempting the sweep -- the same
    claim-then-act pattern routers/admin_credit.py::approve_transfer uses
    for the analogous double-approval race -- and by committing each
    wallet's outcome immediately rather than batching the whole run into
    one commit at the end.

    Returns:
        {
            "total_fees": N,          # cents actually swept this run
            "transfers_settled": N,   # how many wallets were swept
            "status": "completed" | "partial" | "noop",
            "treasury_wallet": "0x...",
            "failures": [{"wallet_address", "pending_fee_cents", "error"}],
            # Only present the one time a new treasury wallet is created:
            "treasury_seed_phrase": "...",
            "_warning": "...",
        }
    """
    from services import onchain_send

    treasury, seed_phrase_once = await get_or_create_treasury_wallet(db)

    wallets = db.query(CryptoWallet).filter(
        CryptoWallet.pending_fee_cents > 0,
        CryptoWallet.is_treasury == False,  # noqa: E712
        CryptoWallet.wallet_type == "fawn_custodial",
    ).all()

    swept_cents = 0
    swept_count = 0
    failures = []

    for wallet in wallets:
        fee_amount = wallet.pending_fee_cents

        # Atomic claim: zero the fee only if it still equals what we just
        # read. If a concurrent run already claimed (or a new send grew)
        # this wallet's pending_fee_cents since the query above, this
        # matches zero rows and we skip -- never sweep a stale amount.
        claimed = db.query(CryptoWallet).filter(
            CryptoWallet.id == wallet.id,
            CryptoWallet.pending_fee_cents == fee_amount,
        ).update({"pending_fee_cents": 0}, synchronize_session=False)
        db.commit()
        if claimed == 0:
            continue

        try:
            settlement = await onchain_send.sweep_wallet_fee(wallet, treasury.wallet_address, fee_amount, db)
        except (Exception, asyncio.CancelledError) as e:
            # CancelledError is a BaseException (not Exception) since
            # Python 3.8 -- caught explicitly here because it's a real way
            # this await can be interrupted (the daily scheduler task
            # getting cancelled on shutdown/redeploy, or this request
            # hitting an upstream timeout). Restoring the fee is the whole
            # point of this branch; a plain `except Exception` would miss
            # exactly this case and silently lose the claimed fee with no
            # record at all.
            #
            # The sweep didn't happen (or we can't be sure it didn't --
            # either way the fee is still genuinely owed). Restore it
            # ADDITIVELY, not by setting back to fee_amount -- a new send
            # could have grown pending_fee_cents again while this sweep
            # attempt was in flight, and a blind overwrite would discard
            # that increment.
            db.query(CryptoWallet).filter(CryptoWallet.id == wallet.id).update(
                {"pending_fee_cents": CryptoWallet.pending_fee_cents + fee_amount},
                synchronize_session=False,
            )
            db.commit()
            failures.append({
                "wallet_address": wallet.wallet_address,
                "pending_fee_cents": fee_amount,
                "error": str(e),
            })
            if isinstance(e, asyncio.CancelledError):
                # Restore is done; still let cancellation actually
                # propagate rather than swallowing it -- suppressing a
                # CancelledError is its own well-known asyncio footgun.
                raise
            continue

        swept_cents += fee_amount
        swept_count += 1

        retention_expires = datetime.now(tz=timezone.utc) + timedelta(days=365 * 7)
        db.add(UserAuditLog(
            user_id=wallet.user_id,
            action="fee_swept_to_treasury",
            details=json.dumps({
                "wallet_address": wallet.wallet_address,
                "amount_cents": fee_amount,
                "treasury_wallet": treasury.wallet_address,
                "chain": settlement["chain"],
                "tx_hash": settlement["tx_hash"],
            }),
            retention_expires_at=retention_expires,
        ))
        # Commit per-wallet -- if a later wallet in this same run hits an
        # unexpected error, everything swept so far up to this point stays
        # recorded rather than being lost with the rest of an uncommitted batch.
        db.commit()

    if swept_cents > 0:
        db.add(FeeCollection(
            total_fees_cents=swept_cents,
            transfer_count=swept_count,
            treasury_wallet=treasury.wallet_address,
            collected_at=datetime.now(tz=timezone.utc),
        ))
        db.commit()

    if not wallets:
        status = "noop"
    elif failures:
        status = "partial"
    else:
        status = "completed"

    result = {
        "total_fees": swept_cents,
        "transfers_settled": swept_count,
        "status": status,
        "treasury_wallet": treasury.wallet_address,
        "failures": failures,
    }
    if seed_phrase_once:
        result["treasury_seed_phrase"] = seed_phrase_once
        result["_warning"] = (
            "A new treasury wallet was just created. Save this seed phrase now -- "
            "it will never be shown again. FAWN's own encrypted copy of the key can "
            "still sign for it going forward either way; this is only for an "
            "external/cold-storage backup."
        )
    return result
