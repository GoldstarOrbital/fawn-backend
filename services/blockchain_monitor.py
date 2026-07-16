"""
Autonomous blockchain settlement layer for FAWN.

Detects incoming USDC transfers on Polygon every 60 seconds and auto-credits
user balances. Uses Alchemy as primary RPC with fallback to public RPCs for
resilience (DeFi-grade fault tolerance).

Architecture:
- Primary: Alchemy (enterprise-grade, no rate limits)
- Fallback 1: Blast (public RPC pool)
- Fallback 2: 1RPC (public RPC)
- Fallback 3: Polygon RPC (backup)

Settlement is atomic: on-chain balance detection → ledger update → audit log.
"""
import asyncio
import os
import json
from datetime import datetime, timedelta, timezone
from typing import Optional
import httpx
from sqlalchemy.orm import Session
from database import SessionLocal
from models import User, UserAuditLog
from config import settings

# Polygon Mainnet USDC contracts. There are two live tokens both called
# "USDC" on Polygon: native USDC (issued directly by Circle since 2023,
# what Coinbase/Robinhood/most modern senders use) and USDC.e (the older
# PoS-bridged version, what some older tooling still uses). Watching only
# one silently misses deposits sent via the other — sum both so no
# variant of "USDC on Polygon" is ever missed.
USDC_CONTRACT_NATIVE = "0x3c499c542cef5E3811e1192ce70d8cc03d5c3359"
USDC_CONTRACT_BRIDGED = "0x2791Bca1f2de4661ED88A30C99a7a9449Aa84174"
USDC_CONTRACTS = [USDC_CONTRACT_NATIVE, USDC_CONTRACT_BRIDGED]

# RPC endpoints with fallback chain (DeFi-grade fault tolerance)
def _get_rpc_endpoints() -> list[str]:
    """Build RPC endpoint list with fallbacks."""
    rpcs = []

    # Primary: Alchemy (if configured)
    if settings.alchemy_api_key:
        rpcs.append(f"https://polygon-mainnet.g.alchemy.com/v2/{settings.alchemy_api_key}")

    # Fallback chain (public RPCs, ranked by reliability)
    rpcs.extend([
        "https://polygon-rpc.com",  # Public, established
        "https://rpc.ankr.com/polygon",  # Ankr backup
        "https://1rpc.io/matic",  # 1RPC backup
    ])

    return rpcs


class RPCClient:
    """RPC client with automatic fallback on failures."""

    def __init__(self):
        self.endpoints = _get_rpc_endpoints()
        self.current_idx = 0
        self.failure_count = {}

    async def call(self, method: str, params: list) -> Optional[dict]:
        """Make RPC call with automatic failover to next endpoint on error."""
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": 1,
        }

        # Try each endpoint
        for attempt in range(len(self.endpoints)):
            idx = (self.current_idx + attempt) % len(self.endpoints)
            endpoint = self.endpoints[idx]

            try:
                async with httpx.AsyncClient(timeout=12.0) as client:
                    response = await client.post(endpoint, json=payload)
                    response.raise_for_status()
                    result = response.json()

                    if "error" in result:
                        error = result.get("error", {})
                        error_msg = error.get("message", str(error))

                        # Rate limit: try next endpoint
                        if "429" in str(error_msg) or "rate" in str(error_msg).lower():
                            continue

                        # Real error
                        return None

                    # Success: update current index and return
                    self.current_idx = idx
                    self.failure_count[endpoint] = 0
                    return result.get("result")

            except Exception as e:
                self.failure_count[endpoint] = self.failure_count.get(endpoint, 0) + 1
                if attempt < len(self.endpoints) - 1:
                    continue
                print(f"[blockchain] All endpoints failed: {e}")
                return None

        return None


_rpc_client = RPCClient()


async def _get_contract_balance(contract: str, wallet_address: str) -> Optional[int]:
    """Query on-chain balanceOf() for a single ERC-20 contract, in cents."""
    method_sig = "0x70a08231"
    padded_addr = wallet_address.lower().replace("0x", "").zfill(64)
    call_data = method_sig + padded_addr

    result = await _rpc_client.call("eth_call", [
        {
            "to": contract,
            "data": call_data,
        },
        "latest",
    ])

    if result and result.startswith("0x"):
        try:
            balance_raw = int(result, 16)
            # USDC has 6 decimals: 1.000000 USDC = 1_000_000 raw units = 100 cents.
            # So 1 cent = 10_000 raw units -- divide by 10**4, not 10**6.
            return balance_raw // (10 ** 4)
        except Exception as e:
            print(f"[blockchain] Parse error ({contract}): {e}")
            return None

    return None


async def _get_usdc_balance(wallet_address: str) -> Optional[int]:
    """
    Query combined on-chain USDC balance across both native and bridged
    USDC contracts on Polygon (see USDC_CONTRACTS comment above).

    Returns balance in cents (USDC has 6 decimals, so 1 USDC = 100 cents),
    or None only if EVERY contract query failed (so a transient RPC error
    on one contract doesn't zero out a real balance on the other).
    """
    balances = await asyncio.gather(*[
        _get_contract_balance(contract, wallet_address) for contract in USDC_CONTRACTS
    ])

    successful = [b for b in balances if b is not None]
    if not successful:
        return None

    return sum(successful)


async def _settle_deposit(wallet_address: str, db: Session) -> bool:
    """
    Atomic settlement: check on-chain balance and credit difference if deposit detected.

    Returns True if deposit was credited, False otherwise.
    """
    on_chain_balance = await _get_usdc_balance(wallet_address)
    if on_chain_balance is None:
        return False

    user = db.query(User).filter(
        User.crypto_wallet_address.ilike(wallet_address)
    ).first()

    if not user:
        return False

    ledger_balance = user.usdc_balance_cents
    difference = on_chain_balance - ledger_balance

    # Only credit if on-chain > ledger (new deposit detected)
    if difference > 0:
        old_balance = user.usdc_balance_cents
        user.usdc_balance_cents = on_chain_balance

        # Atomic: create audit log before commit (settlement finality)
        retention_expires = datetime.now(tz=timezone.utc) + timedelta(days=365*7)
        audit = UserAuditLog(
            user_id=user.id,
            action="blockchain_deposit_settled",
            details=json.dumps({
                "wallet": wallet_address,
                "on_chain_cents": on_chain_balance,
                "ledger_before_cents": old_balance,
                "deposit_detected_cents": difference,
                "ledger_after_cents": user.usdc_balance_cents,
                "timestamp": datetime.utcnow().isoformat(),
            }),
            retention_expires_at=retention_expires,
        )
        db.add(audit)
        db.commit()

        print(f"[blockchain] ✓ SETTLED: {user.email} +${difference/100:.2f} (on-chain detected)")
        return True

    return False


async def _monitor_loop():
    """
    Autonomous settlement loop: query all user wallets every 60 seconds,
    detect deposits on-chain, auto-credit ledger.

    Architecture: Fully autonomous, no intermediaries, DeFi-grade resilience.
    """
    print("[blockchain] 🚀 SETTLEMENT LAYER ONLINE")
    print(f"[blockchain] RPC Chain: {len(_rpc_client.endpoints)} endpoints configured")
    if settings.alchemy_api_key:
        print("[blockchain] ✓ Alchemy enabled (primary)")
    else:
        print("[blockchain] ⚠ Alchemy not set - using public RPCs (rate-limited)")

    check_interval = 60  # seconds
    settled_count = 0

    while True:
        try:
            db = SessionLocal()
            try:
                # Query all wallets
                users_with_wallets = db.query(User).filter(
                    User.crypto_wallet_address.isnot(None)
                ).all()

                if users_with_wallets:
                    print(f"[blockchain] 🔍 Checking {len(users_with_wallets)} wallets for deposits...")

                    for user in users_with_wallets:
                        try:
                            if await _settle_deposit(user.crypto_wallet_address, db):
                                settled_count += 1
                        except Exception as e:
                            print(f"[blockchain] Settlement error for {user.email}: {e}")

                    if settled_count > 0:
                        print(f"[blockchain] 📊 Total settlements this session: {settled_count}")

            finally:
                db.close()

            await asyncio.sleep(check_interval)

        except asyncio.CancelledError:
            print("[blockchain] ⏹ Settlement layer shutting down")
            raise
        except Exception as e:
            print(f"[blockchain] Monitor error (will retry): {e}")
            await asyncio.sleep(check_interval)


def start_blockchain_monitor():
    """Start the autonomous settlement layer as a background task."""
    loop = asyncio.get_event_loop()
    task = loop.create_task(_monitor_loop())
    return task
