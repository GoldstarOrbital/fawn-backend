"""
Real on-chain USDC settlement for FAWN sends.

Confirmed in production (2026-07-18): the previous send implementation
(services/crypto_wallet.py::send_usdc) only ever adjusted the internal
usdc_balance_cents ledger number. It never signed or broadcast an actual
blockchain transaction -- the sender's real on-chain wallet balance never
changed, and nothing was ever delivered to the recipient's real wallet,
FAWN user or external. This module replaces that with real settlement:
for a "fawn_custodial" wallet (the only kind FAWN can sign for -- see
services/crypto_wallet.py::create_wallet, "non_custodial" wallets never
give FAWN a usable key), it decrypts the stored private key just long
enough to sign a real ERC-20 transfer, broadcasts it, and returns the
real transaction hash.

Scope (v1):
- Only "fawn_custodial" wallets can be sent FROM. "non_custodial" wallets
  would need client-side signing (the user's own wallet/extension signs
  a transaction FAWN never has the key for) -- a materially different,
  separate feature, not attempted here. CannotSignTransaction is raised
  clearly rather than silently doing nothing or faking success.
- Only the NATIVE USDC contract per chain is used for sends (matches
  services/blockchain_monitor.py's own preference -- native USDC is what
  most modern senders actually use; bridged variants add real complexity
  for comparatively little benefit and aren't attempted here).
- A send must be fully coverable by a SINGLE chain's real on-chain
  balance. A wallet's usdc_balance_cents is a chain-agnostic ledger
  total, but a real on-chain transfer has to come from one specific
  chain. If no single chain has enough, this fails clearly rather than
  attempting to split a send across two separate transactions.
- FAWN sponsors gas: USDC transfers need the sender's wallet to hold a
  small amount of the chain's native gas token (MATIC/POL on Polygon,
  ETH on Base), which a self-custodial user's wallet has no reason to
  hold. A FAWN-controlled "gas station" wallet (GAS_STATION_PRIVATE_KEY)
  tops up the sender's wallet with just enough native token before the
  USDC transfer, when needed.
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from eth_account import Account
from eth_utils import to_checksum_address

from sqlalchemy import func as sa_func

from database import SessionLocal
from models import User, CryptoWallet, UserAuditLog, CryptoTransfer, GasStationTopup
from services import blockchain_monitor as bm
from services.crypto_wallet import _decrypt_private_key, _is_valid_eth_address
from config import settings

# EVM chain IDs -- required for transaction signing (EIP-155 replay
# protection). Must match services.blockchain_monitor.CHAINS' keys.
CHAIN_IDS = {
    "polygon": 137,
    "base": 8453,
}

ERC20_TRANSFER_SELECTOR = "0xa9059cbb"  # transfer(address,uint256)

# Conservative fixed gas limit for a simple ERC-20 transfer. Real-world
# USDC transfers on Polygon/Base typically use 45,000-65,000 gas; this
# leaves headroom without needing a live eth_estimateGas round-trip on
# every send (estimation can itself be flaky on the same degraded public
# RPCs that motivated blockchain_monitor's fallback chain).
ERC20_TRANSFER_GAS_LIMIT = 90_000

# Plain native-token transfer (the gas station top-up itself).
NATIVE_TRANSFER_GAS_LIMIT = 21_000

# Minimum native-token balance (in wei) a sender's wallet must hold
# before a USDC transfer is attempted. Topped up from the gas station
# wallet if short. Generous relative to actual Polygon/Base gas costs
# (typically well under a cent) so a single top-up covers gas-price
# spikes without needing a second top-up mid-send.
MIN_GAS_BALANCE_WEI = {
    "polygon": 10 ** 16,  # 0.01 MATIC/POL
    "base": 2 * 10 ** 14,  # 0.0002 ETH
}
GAS_TOPUP_WEI = {
    "polygon": 5 * 10 ** 16,  # 0.05 MATIC/POL
    "base": 10 ** 15,  # 0.001 ETH
}


class CannotSignTransaction(Exception):
    """Sender's wallet isn't one FAWN can sign for (non-custodial, or a
    custodial wallet with no usable stored key -- see
    services/crypto_wallet.py::create_wallet's round-trip guard)."""
    pass


class NoChainHasSufficientBalance(Exception):
    """No single chain's real on-chain USDC balance covers the requested
    amount. per_chain_balances_cents is attached for a precise error
    message (the aggregate ledger total can be misleading here -- e.g.
    $14.01 total but split $6/$8.01 across two chains can't cover a $10
    single-chain send)."""
    def __init__(self, message: str, per_chain_balances_cents: dict):
        super().__init__(message)
        self.per_chain_balances_cents = per_chain_balances_cents


class OnchainSendFailed(Exception):
    """The transaction was built and signed but broadcasting failed."""
    pass


class SendLimitExceeded(Exception):
    """A hard custody safeguard, not a balance check -- a compromised
    session, a bug, or a leaked key should never be able to move more than
    this in one transaction or one day, independent of how much the
    wallet actually holds."""
    pass


class GasStationLimitExceeded(Exception):
    """The gas station wallet has hit its platform-wide daily top-up cap.
    Protects it from a runaway loop draining its balance -- each
    individual top-up is tiny, but unbounded repetition isn't."""
    pass


class VelocityLimitExceeded(Exception):
    """Too many sends in too short a window, independent of amount -- a
    compromised account draining via many small transactions (each
    individually under the per-tx $ cap) still trips this."""
    pass


def _check_send_limits(sender: User, amount_cents: int, db: Session) -> None:
    """Hard per-transaction, rolling-24h, and velocity (count-based) caps
    on custodial sends. These exist independent of the sender's real
    balance -- a wallet legitimately holding $10,000 still shouldn't be
    able to move all of it in a single compromised-session transaction,
    and a wallet sending 50 small transfers in five minutes is suspicious
    regardless of how small each one is."""
    if amount_cents > settings.max_send_cents_per_tx:
        raise SendLimitExceeded(
            f"${amount_cents/100:.2f} exceeds the ${settings.max_send_cents_per_tx/100:.2f} "
            f"per-transaction limit."
        )

    now = datetime.now(tz=timezone.utc)
    since_24h = now - timedelta(hours=24)
    since_1h = now - timedelta(hours=1)

    sent_last_24h = db.query(sa_func.coalesce(sa_func.sum(CryptoTransfer.amount_cents), 0)).filter(
        CryptoTransfer.sender_id == sender.id,
        CryptoTransfer.created_at >= since_24h,
        CryptoTransfer.status == "completed",
    ).scalar()

    if sent_last_24h + amount_cents > settings.max_send_cents_per_day:
        raise SendLimitExceeded(
            f"This send would bring your rolling 24h total to "
            f"${(sent_last_24h + amount_cents)/100:.2f}, over the "
            f"${settings.max_send_cents_per_day/100:.2f} daily limit. "
            f"Already sent in the last 24h: ${sent_last_24h/100:.2f}."
        )

    count_last_hour = db.query(sa_func.count(CryptoTransfer.id)).filter(
        CryptoTransfer.sender_id == sender.id,
        CryptoTransfer.created_at >= since_1h,
        CryptoTransfer.status == "completed",
    ).scalar()
    if count_last_hour >= settings.max_sends_per_hour:
        raise VelocityLimitExceeded(
            f"You've made {count_last_hour} sends in the last hour, at the "
            f"{settings.max_sends_per_hour}/hour limit. Try again shortly."
        )

    count_last_day = db.query(sa_func.count(CryptoTransfer.id)).filter(
        CryptoTransfer.sender_id == sender.id,
        CryptoTransfer.created_at >= since_24h,
        CryptoTransfer.status == "completed",
    ).scalar()
    if count_last_day >= settings.max_sends_per_day:
        raise VelocityLimitExceeded(
            f"You've made {count_last_day} sends in the last 24h, at the "
            f"{settings.max_sends_per_day}/day limit. Try again tomorrow."
        )


def is_first_time_recipient(sender_id: str, recipient_address: str, db: Session) -> bool:
    """True if sender has no prior completed send to this exact address.
    Used to decide whether a large send needs a manual-review hold --
    immediate drain to a brand-new address is the classic
    account-takeover pattern."""
    prior = db.query(CryptoTransfer).filter(
        CryptoTransfer.sender_id == sender_id,
        CryptoTransfer.recipient_address.ilike(recipient_address),
        CryptoTransfer.status == "completed",
    ).first()
    return prior is None


def _get_gas_station_account():
    key = settings.gas_station_private_key
    if not key:
        raise RuntimeError("GAS_STATION_PRIVATE_KEY is not configured -- cannot sponsor gas for on-chain sends.")
    return Account.from_key(key)


async def _get_native_balance(chain: str, address: str) -> Optional[int]:
    result = await bm._rpc_clients[chain].call("eth_getBalance", [address, "latest"])
    if result and result.startswith("0x"):
        try:
            return int(result, 16)
        except Exception:
            return None
    return None


async def _get_nonce(chain: str, address: str) -> Optional[int]:
    result = await bm._rpc_clients[chain].call("eth_getTransactionCount", [address, "pending"])
    if result and result.startswith("0x"):
        try:
            return int(result, 16)
        except Exception:
            return None
    return None


async def _get_gas_price(chain: str) -> Optional[int]:
    result = await bm._rpc_clients[chain].call("eth_gasPrice", [])
    if result and result.startswith("0x"):
        try:
            return int(result, 16)
        except Exception:
            return None
    return None


async def _broadcast(chain: str, raw_tx_hex: str) -> str:
    """Broadcast a signed raw transaction. Returns the tx hash.

    Raises OnchainSendFailed on any RPC-level rejection (e.g. nonce too
    low, insufficient funds for gas) -- these are real failures, not
    something a fallback should paper over the way blockchain_monitor's
    read-path fallback does for detection.
    """
    result = await bm._rpc_clients[chain].call("eth_sendRawTransaction", [raw_tx_hex])
    if not result or not isinstance(result, str) or not result.startswith("0x"):
        raise OnchainSendFailed(f"Broadcast failed on {chain}: RPC did not return a transaction hash.")
    return result


def _sign_transfer(chain: str, private_key: str, nonce: int, gas_price: int, contract: str, to_address: str, amount_raw: int) -> str:
    """Build, sign, and RLP-encode an ERC-20 transfer(address,uint256) call.
    Returns the raw signed transaction as a 0x-prefixed hex string.
    Never logs or returns the private key."""
    padded_to = to_address.lower().replace("0x", "").zfill(64)
    padded_amount = hex(amount_raw)[2:].zfill(64)
    data = ERC20_TRANSFER_SELECTOR + padded_to + padded_amount

    tx = {
        "chainId": CHAIN_IDS[chain],
        "nonce": nonce,
        "to": to_checksum_address(contract),
        "value": 0,
        "gas": ERC20_TRANSFER_GAS_LIMIT,
        "gasPrice": gas_price,
        "data": data,
    }
    signed = Account.sign_transaction(tx, private_key)
    return signed.raw_transaction.hex() if hasattr(signed, "raw_transaction") else signed.rawTransaction.hex()


def _sign_native_transfer(chain: str, private_key: str, nonce: int, gas_price: int, to_address: str, amount_wei: int) -> str:
    tx = {
        "chainId": CHAIN_IDS[chain],
        "nonce": nonce,
        "to": to_checksum_address(to_address),
        "value": amount_wei,
        "gas": NATIVE_TRANSFER_GAS_LIMIT,
        "gasPrice": gas_price,
    }
    signed = Account.sign_transaction(tx, private_key)
    return signed.raw_transaction.hex() if hasattr(signed, "raw_transaction") else signed.rawTransaction.hex()


async def _ensure_gas(chain: str, wallet_address: str, db: Session) -> None:
    """Top up wallet_address's native-gas balance from the gas station
    wallet if it's below the minimum needed for a USDC transfer. Waits
    for the top-up to actually land (polls for a receipt) before
    returning, so the subsequent USDC-transfer nonce is correct and the
    transfer doesn't race a still-pending top-up.

    Enforces a platform-wide daily cap on top-up COUNT before sending one
    -- the gas station wallet has no other spend limit, so this is what
    stands between a bug/abuse loop and it being drained."""
    import asyncio

    balance = await _get_native_balance(chain, wallet_address)
    if balance is not None and balance >= MIN_GAS_BALANCE_WEI[chain]:
        return

    since = datetime.now(tz=timezone.utc) - timedelta(hours=24)
    topups_last_24h = db.query(sa_func.count(GasStationTopup.id)).filter(
        GasStationTopup.created_at >= since,
    ).scalar()
    if topups_last_24h >= settings.max_gas_topups_per_day:
        raise GasStationLimitExceeded(
            f"Gas station has hit its daily top-up cap ({settings.max_gas_topups_per_day}/day). "
            f"This protects the gas wallet from a runaway drain -- if this is legitimate volume, "
            f"raise MAX_GAS_TOPUPS_PER_DAY."
        )

    gas_account = _get_gas_station_account()
    nonce = await _get_nonce(chain, gas_account.address)
    gas_price = await _get_gas_price(chain)
    if nonce is None or gas_price is None:
        raise OnchainSendFailed(f"Could not fetch nonce/gas price on {chain} to fund gas top-up.")

    raw_tx = _sign_native_transfer(chain, gas_account.key.hex(), nonce, gas_price, wallet_address, GAS_TOPUP_WEI[chain])
    tx_hash = await _broadcast(chain, raw_tx)

    db.add(GasStationTopup(
        chain=chain,
        wallet_address=wallet_address,
        amount_wei=str(GAS_TOPUP_WEI[chain]),
        tx_hash=tx_hash,
    ))
    db.commit()

    # Wait for the top-up to confirm (bounded) before proceeding -- the
    # USDC transfer's own nonce/gas depend on this having actually landed.
    for _ in range(30):
        receipt = await bm._rpc_clients[chain].call("eth_getTransactionReceipt", [tx_hash])
        if receipt is not None:
            return
        await asyncio.sleep(2)
    raise OnchainSendFailed(f"Gas top-up {tx_hash} on {chain} did not confirm in time.")


async def _get_native_usdc_balance(chain: str, wallet_address: str) -> Optional[int]:
    """Raw balanceOf() for just the native USDC contract on one chain, in
    cents. Deliberately narrower than blockchain_monitor's
    _get_combined_balance (which sums every contract variant) -- sends
    only move native USDC (see module docstring), so only that contract's
    balance is actually spendable via this path."""
    contract = bm.CHAINS[chain]["contracts"]["usdc_native"]
    method_sig = "0x70a08231"
    padded_addr = wallet_address.lower().replace("0x", "").zfill(64)
    result = await bm._rpc_clients[chain].call("eth_call", [{"to": contract, "data": method_sig + padded_addr}, "latest"])
    if result and result.startswith("0x"):
        try:
            return int(result, 16) // (10 ** 4)
        except Exception:
            return None
    return None


async def send_onchain_usdc(
    sender: User,
    recipient_address: str,
    amount_cents: int,
    db: Session,
) -> dict:
    """
    Sign and broadcast a real on-chain native-USDC transfer from sender's
    wallet to recipient_address. Only works for "fawn_custodial" senders
    with a usable stored key.

    Returns:
        {"chain": "polygon"|"base", "tx_hash": "0x...", "amount_cents": int}

    Raises:
        CannotSignTransaction, SendLimitExceeded, VelocityLimitExceeded,
        NoChainHasSufficientBalance, GasStationLimitExceeded, OnchainSendFailed
    """
    if sender.wallet_type != "fawn_custodial":
        raise CannotSignTransaction(
            f"FAWN cannot sign for a {sender.wallet_type or 'unknown'} wallet -- "
            f"only fawn_custodial wallets can be sent from server-side."
        )

    # This function signs and broadcasts a real, irreversible transaction
    # -- it must not trust its callers to have validated recipient_address
    # already. Both current call sites (crypto_wallet.send_usdc and the
    # admin approve-transfer endpoint) do validate first, but a malformed
    # address reaching _sign_transfer's zfill(64) padding would silently
    # encode a DIFFERENT destination address rather than failing loudly --
    # this is the one function in the codebase where that failure mode is
    # not acceptable to leave to caller discipline.
    if not _is_valid_eth_address(recipient_address):
        raise CannotSignTransaction(f"Invalid recipient address: {recipient_address}")

    # Hard custody limits, checked before anything touches the key or the
    # network -- fail fast on an out-of-bounds request.
    _check_send_limits(sender, amount_cents, db)

    # OFAC screening -- a legal requirement, checked before the key is
    # ever touched. See services/sanctions_screening.py.
    from services.sanctions_screening import check_recipient_not_sanctioned
    await check_recipient_not_sanctioned(sender.id, recipient_address, db)

    wallet_row = db.query(CryptoWallet).filter(
        CryptoWallet.wallet_address.ilike(sender.crypto_wallet_address)
    ).first()
    if not wallet_row or not wallet_row.encrypted_private_key:
        raise CannotSignTransaction(
            f"No usable signing key stored for wallet {sender.crypto_wallet_address}. "
            f"This wallet cannot be sent from until re-created with a working custodial key."
        )

    try:
        private_key = _decrypt_private_key(wallet_row.encrypted_private_key)
    except Exception as e:
        raise CannotSignTransaction(f"Failed to decrypt signing key: {e}")

    # Audit every key decryption as a security-relevant event, independent
    # of whether the send itself succeeds -- who/when/which wallet a
    # private key was decrypted for should always be reconstructable.
    retention_expires = datetime.now(tz=timezone.utc) + timedelta(days=365 * 7)
    db.add(UserAuditLog(
        user_id=sender.id,
        action="private_key_decrypted",
        details=json.dumps({
            "wallet_address": sender.crypto_wallet_address,
            "purpose": "send_onchain_usdc",
            "amount_cents": amount_cents,
            "timestamp": datetime.utcnow().isoformat(),
        }),
        retention_expires_at=retention_expires,
    ))
    db.commit()

    try:
        # Find a single chain whose real on-chain native-USDC balance
        # covers the full amount. A ledger total spanning multiple
        # chains can't be split across two transactions in v1 (see
        # module docstring).
        per_chain_balances = {}
        chosen_chain = None
        for chain in bm.CHAINS:
            bal = await _get_native_usdc_balance(chain, sender.crypto_wallet_address)
            per_chain_balances[chain] = bal
            if bal is not None and bal >= amount_cents and chosen_chain is None:
                chosen_chain = chain

        if chosen_chain is None:
            readable = ", ".join(
                f"{c}: ${(v or 0)/100:.2f}" if v is not None else f"{c}: unknown"
                for c, v in per_chain_balances.items()
            )
            raise NoChainHasSufficientBalance(
                f"No single chain has enough native USDC to cover ${amount_cents/100:.2f}. "
                f"Per-chain balances -- {readable}.",
                per_chain_balances,
            )

        await _ensure_gas(chosen_chain, sender.crypto_wallet_address, db)

        contract = bm.CHAINS[chosen_chain]["contracts"]["usdc_native"]
        nonce = await _get_nonce(chosen_chain, sender.crypto_wallet_address)
        gas_price = await _get_gas_price(chosen_chain)
        if nonce is None or gas_price is None:
            raise OnchainSendFailed(f"Could not fetch nonce/gas price on {chosen_chain}.")

        amount_raw = amount_cents * (10 ** 4)  # cents -> raw USDC units (6 decimals)
        raw_tx = _sign_transfer(chosen_chain, private_key, nonce, gas_price, contract, recipient_address, amount_raw)
        tx_hash = await _broadcast(chosen_chain, raw_tx)

        return {"chain": chosen_chain, "tx_hash": tx_hash, "amount_cents": amount_cents}
    finally:
        # Best-effort: drop our only local reference so it isn't
        # retained longer than needed. Python can't guarantee secure
        # erasure, but this keeps the key out of any enclosing scope's
        # long-lived state and out of every code path above that returns
        # or raises.
        private_key = None
