"""
Autonomous multi-chain settlement layer for FAWN.

Detects incoming USDC transfers across every chain FAWN supports, every 60
seconds, and auto-credits user balances. Uses Alchemy as primary RPC per
chain with fallback to public RPCs for resilience (DeFi-grade fault
tolerance).

A FAWN wallet is a single EVM address (0x...), and the same address can
independently hold funds on any EVM chain -- a user sending "USDC" has no
reason to know or care which chain FAWN happens to be watching. Missing a
chain silently strands real user funds (confirmed in production: a Base
deposit was invisible to a Polygon-only monitor). CHAINS below is the single
place to add support for a new chain; the rest of the file is chain-agnostic
and sums across everything configured.

Within a chain, there can also be multiple ERC-20 contracts all called
"USDC" (native Circle-issued vs. older bridged versions) -- also confirmed
in production as a separate miss. Each chain's `contracts` list should
include every variant in circulation; balances are summed across all of
them, on all chains, into one combined total.

Settlement is atomic: on-chain balance detection -> ledger update -> audit log.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Optional
import httpx
from sqlalchemy.orm import Session
from database import SessionLocal
from models import User, UserAuditLog
from config import settings

# ---- Chain + contract registry ----
# alchemy_slug is the subdomain Alchemy uses for that chain's RPC
# (https://{alchemy_slug}.g.alchemy.com/v2/{key}). fallback_rpcs are public
# endpoints tried in order if Alchemy isn't configured or fails.
CHAINS = {
    "polygon": {
        "alchemy_slug": "polygon-mainnet",
        "fallback_rpcs": [
            "https://polygon-rpc.com",
            "https://rpc.ankr.com/polygon",
            "https://1rpc.io/matic",
        ],
        "contracts": {
            # Native USDC (Circle-issued directly on Polygon since 2023) --
            # what Robinhood, Coinbase, and most modern senders use.
            "usdc_native": "0x3c499c542cef5E3811e1192ce70d8cc03d5c3359",
            # USDC.e -- the older PoS-bridged version. Still in circulation.
            "usdc_bridged": "0x2791Bca1f2de4661ED88A30C99a7a9449Aa84174",
        },
    },
    "base": {
        "alchemy_slug": "base-mainnet",
        "fallback_rpcs": [
            "https://mainnet.base.org",
            "https://base.publicnode.com",
            "https://1rpc.io/base",
        ],
        "contracts": {
            # Native USDC (Circle-issued directly on Base).
            "usdc_native": "0x833589fCD6eDb6E08f4c7C32D4f71b54bda02913",
            # USDbC -- the older Base-bridged version.
            "usdc_bridged": "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA",
        },
    },
}


def _get_rpc_endpoints(chain: str) -> list[str]:
    """Build the RPC endpoint list for one chain: Alchemy first (if
    configured), then that chain's public fallbacks."""
    cfg = CHAINS[chain]
    rpcs = []
    if settings.alchemy_api_key:
        rpcs.append(f"https://{cfg['alchemy_slug']}.g.alchemy.com/v2/{settings.alchemy_api_key}")
    rpcs.extend(cfg["fallback_rpcs"])
    return rpcs


class RPCClient:
    """RPC client for one chain, with automatic fallback on failures."""

    def __init__(self, chain: str):
        self.chain = chain
        self.endpoints = _get_rpc_endpoints(chain)
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
                print(f"[blockchain:{self.chain}] All endpoints failed: {e}")
                return None

        return None


_rpc_clients = {chain: RPCClient(chain) for chain in CHAINS}


async def _get_contract_balance(rpc_client: RPCClient, contract: str, wallet_address: str) -> Optional[int]:
    """Query on-chain balanceOf() for a single ERC-20 contract, in cents."""
    method_sig = "0x70a08231"
    padded_addr = wallet_address.lower().replace("0x", "").zfill(64)
    call_data = method_sig + padded_addr

    result = await rpc_client.call("eth_call", [
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
            print(f"[blockchain:{rpc_client.chain}] Parse error ({contract}): {e}")
            return None

    return None


async def _get_usdc_balance(wallet_address: str) -> Optional[int]:
    """
    Query combined on-chain USDC balance across every configured chain and
    every USDC contract variant on each (see CHAINS above).

    Returns balance in cents, or None only if EVERY query on EVERY chain
    failed (so a transient RPC error on one chain/contract doesn't zero out
    real balances found on the others).
    """
    tasks = []
    for chain, cfg in CHAINS.items():
        rpc_client = _rpc_clients[chain]
        for contract in cfg["contracts"].values():
            tasks.append(_get_contract_balance(rpc_client, contract, wallet_address))

    balances = await asyncio.gather(*tasks)

    successful = [b for b in balances if b is not None]
    if not successful:
        return None

    return sum(successful)


async def _settle_deposit(wallet_address: str, db: Session) -> bool:
    """
    Atomic settlement: check combined on-chain balance and credit the
    difference if a deposit is detected.

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
                "chains_checked": list(CHAINS.keys()),
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
    detect deposits on-chain across every configured chain, auto-credit
    ledger.
    """
    print("[blockchain] 🚀 SETTLEMENT LAYER ONLINE")
    print(f"[blockchain] Chains: {', '.join(CHAINS.keys())}")
    for chain in CHAINS:
        print(f"[blockchain:{chain}] {len(_rpc_clients[chain].endpoints)} RPC endpoints configured")
    if settings.alchemy_api_key:
        print("[blockchain] ✓ Alchemy enabled (primary, all chains)")
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
