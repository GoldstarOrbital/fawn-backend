"""
Autonomous multi-chain settlement layer for FAWN.

Detects incoming USDC transfers across every chain FAWN supports, every 15
seconds, and auto-credits user balances. Uses Alchemy as primary RPC per
chain with fallback to public RPCs for resilience (DeFi-grade fault
tolerance).

Detection works off ERC-20 Transfer *event logs* (eth_getLogs), not a
balanceOf() diff -- a balance diff can only tell you the total changed, not
who sent it, when, on which chain, or in which transaction. Event logs give
individual, attributable deposit records (services/blockchain_monitor.py ->
models.CryptoDeposit), which is what lets a user actually see "$8.01
received on Base from 0x1887...c3dd, tx 0xabc..." instead of a balance
number silently moving with no explanation.

A FAWN wallet is a single EVM address (0x...), and the same address can
independently hold funds on any EVM chain -- a user sending "USDC" has no
reason to know or care which chain FAWN happens to be watching. Missing a
chain silently strands real user funds (confirmed in production: a Base
deposit was invisible to a Polygon-only monitor). CHAINS below is the single
place to add support for a new chain; the rest of the file is chain-agnostic.

Within a chain, there can also be multiple ERC-20 contracts all called
"USDC" (native Circle-issued vs. older bridged versions) -- also confirmed
in production as a separate miss. Each chain's `contracts` list should
include every variant in circulation.

Per-wallet-per-chain scanning is checkpointed (models.ChainScanCheckpoint)
so each cycle only queries new blocks, not full history. The first time a
wallet+chain is seen, a bounded historical look-back runs once to record
(but NOT double-credit) any deposits that predate this feature -- see
BACKFILL_BLOCK_LOOKBACK below.

Settlement is atomic per deposit: record CryptoDeposit -> credit ledger ->
audit log, all in one commit.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Optional
import httpx
from sqlalchemy.orm import Session
from database import SessionLocal
from models import User, UserAuditLog, CryptoDeposit, ChainScanCheckpoint
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

TRANSFER_EVENT_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# How far back to look the FIRST time a wallet+chain is scanned, to catch
# deposits that arrived before this feature existed. ~40,000 blocks is
# roughly a day on both Polygon and Base (~2s block time). Historical finds
# beyond this window won't be recorded -- acceptable, since this is a
# one-time bridge from the old balance-diff system, not the steady-state
# behavior (steady-state is fully incremental and never misses anything).
BACKFILL_BLOCK_LOOKBACK = 40_000

# eth_getLogs block-range per call, conservative enough to work across
# every public RPC fallback (Alchemy allows much more, but we don't special-
# case it -- simpler and still fast: 40,000 blocks / 2,000 = 20 calls max).
LOG_CHUNK_SIZE = 2_000


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
                async with httpx.AsyncClient(timeout=15.0) as client:
                    response = await client.post(endpoint, json=payload)
                    response.raise_for_status()
                    result = response.json()

                    if "error" in result:
                        # Any JSON-RPC-level error (rate limits, archive-access
                        # restrictions, a free-tier endpoint getting its key
                        # disabled, etc.) means THIS endpoint can't serve the
                        # request right now -- not that the request itself is
                        # invalid. Always fall through to the next configured
                        # endpoint; only give up once every endpoint has been
                        # tried (confirmed in production: a narrower substring
                        # check here caused Polygon scanning to silently stall
                        # for ~20 hours when polygon-rpc.com started returning
                        # "API key disabled, reason: tenant disabled" -- an
                        # error that didn't match the old whitelist, so the
                        # working rpc.ankr.com/1rpc.io fallbacks were never
                        # tried).
                        self.failure_count[endpoint] = self.failure_count.get(endpoint, 0) + 1
                        if attempt < len(self.endpoints) - 1:
                            continue
                        error = result.get("error", {})
                        print(f"[blockchain:{self.chain}] All endpoints failed, last error: {error.get('message', error)}")
                        return None

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


async def _get_latest_block(chain: str) -> Optional[int]:
    result = await _rpc_clients[chain].call("eth_blockNumber", [])
    if result and result.startswith("0x"):
        try:
            return int(result, 16)
        except Exception:
            return None
    return None


async def _fetch_transfer_logs(chain: str, contract: str, to_address: str, from_block: int, to_block: int) -> tuple[list[dict], bool]:
    """Fetch every Transfer event log where `to` == to_address, on one
    contract, chunked into LOG_CHUNK_SIZE windows for public-RPC
    compatibility.

    Returns (logs, all_windows_succeeded). The second value matters: if a
    window's eth_getLogs call fails (e.g. an RPC's archive-access
    restriction), `result` is None and that window is silently skipped --
    which must NOT be mistaken for "confirmed zero deposits in this range".
    Callers use all_windows_succeeded to decide whether to trust an empty
    result or fall back to a balance-based safety net (see
    _get_combined_balance)."""
    if from_block > to_block:
        return [], True

    padded_to = "0x" + "0" * 24 + to_address.lower().replace("0x", "")
    rpc_client = _rpc_clients[chain]
    logs = []
    all_succeeded = True

    window_start = from_block
    while window_start <= to_block:
        window_end = min(window_start + LOG_CHUNK_SIZE - 1, to_block)
        result = await rpc_client.call("eth_getLogs", [{
            "fromBlock": hex(window_start),
            "toBlock": hex(window_end),
            "address": contract,
            "topics": [TRANSFER_EVENT_TOPIC, None, padded_to],
        }])
        if result is None:
            all_succeeded = False
        else:
            logs.extend(result)
        window_start = window_end + 1

    return logs, all_succeeded


def _decode_transfer_log(log: dict) -> Optional[dict]:
    """Decode one Transfer(address,address,uint256) log entry."""
    try:
        topics = log["topics"]
        from_address = "0x" + topics[1][-40:]
        amount_raw = int(log["data"], 16)
        # USDC has 6 decimals: 1_000_000 raw units = 100 cents.
        amount_cents = amount_raw // (10 ** 4)
        return {
            "from_address": from_address,
            "amount_cents": amount_cents,
            "tx_hash": log["transactionHash"],
            "block_number": int(log["blockNumber"], 16),
        }
    except (KeyError, IndexError, ValueError) as e:
        print(f"[blockchain] Failed to decode transfer log: {e}")
        return None


async def _get_combined_balance(chain: str, wallet_address: str) -> Optional[int]:
    """Safety-net path: raw balanceOf() sum across a chain's USDC contract
    variants, in cents. Only used when eth_getLogs was unreliable this
    cycle -- see the fallback block in _scan_wallet_chain. Event logs are
    preferred because they give per-transfer attribution (source address,
    tx hash); this just guarantees a real deposit is never silently missed
    if getLogs access is degraded (e.g. an RPC's archive-range restriction)."""
    method_sig = "0x70a08231"
    padded_addr = wallet_address.lower().replace("0x", "").zfill(64)
    call_data = method_sig + padded_addr
    rpc_client = _rpc_clients[chain]

    balances = []
    for contract in CHAINS[chain]["contracts"].values():
        result = await rpc_client.call("eth_call", [{"to": contract, "data": call_data}, "latest"])
        if result and result.startswith("0x"):
            try:
                balances.append(int(result, 16) // (10 ** 4))
            except Exception:
                pass

    return sum(balances) if balances else None


async def _scan_wallet_chain(user: User, chain: str, db: Session) -> int:
    """
    Scan one (wallet, chain) for new deposits since the last checkpoint,
    across every USDC contract variant on that chain. Records each new
    transfer as a CryptoDeposit and credits the ledger (unless this is the
    one-time historical backfill pass, which records but doesn't credit).

    Returns the number of deposits newly credited (not counting backfill-only ones).
    """
    wallet_address = user.crypto_wallet_address
    latest_block = await _get_latest_block(chain)
    if latest_block is None:
        return 0

    checkpoint = db.query(ChainScanCheckpoint).filter(
        ChainScanCheckpoint.wallet_address == wallet_address,
        ChainScanCheckpoint.chain == chain,
    ).first()

    is_backfill = checkpoint is None
    from_block = (
        max(latest_block - BACKFILL_BLOCK_LOOKBACK, 0) if is_backfill
        else checkpoint.last_scanned_block + 1
    )

    if from_block > latest_block:
        return 0  # nothing new since last check

    credited_count = 0
    logs_fully_reliable = True
    for contract in CHAINS[chain]["contracts"].values():
        logs, succeeded = await _fetch_transfer_logs(chain, contract, wallet_address, from_block, latest_block)
        if not succeeded:
            logs_fully_reliable = False
        for log in logs:
            decoded = _decode_transfer_log(log)
            if not decoded or decoded["amount_cents"] <= 0:
                continue

            # Idempotency: skip if we've already recorded this exact transfer.
            exists = db.query(CryptoDeposit).filter(
                CryptoDeposit.chain == chain,
                CryptoDeposit.tx_hash == decoded["tx_hash"],
                CryptoDeposit.contract_address == contract,
                CryptoDeposit.to_address == wallet_address,
            ).first()
            if exists:
                continue

            deposit = CryptoDeposit(
                user_id=user.id,
                chain=chain,
                contract_address=contract,
                from_address=decoded["from_address"],
                to_address=wallet_address,
                amount_cents=decoded["amount_cents"],
                tx_hash=decoded["tx_hash"],
                block_number=decoded["block_number"],
                credited_to_ledger=not is_backfill,
            )
            db.add(deposit)

            if not is_backfill:
                old_balance = user.usdc_balance_cents
                user.usdc_balance_cents += decoded["amount_cents"]
                retention_expires = datetime.now(tz=timezone.utc) + timedelta(days=365 * 7)
                audit = UserAuditLog(
                    user_id=user.id,
                    action="blockchain_deposit_settled",
                    details=json.dumps({
                        "chain": chain,
                        "contract": contract,
                        "from_address": decoded["from_address"],
                        "tx_hash": decoded["tx_hash"],
                        "amount_cents": decoded["amount_cents"],
                        "ledger_before_cents": old_balance,
                        "ledger_after_cents": user.usdc_balance_cents,
                        "timestamp": datetime.utcnow().isoformat(),
                    }),
                    retention_expires_at=retention_expires,
                )
                db.add(audit)
                credited_count += 1
                print(f"[blockchain] ✓ SETTLED: {user.email} +${decoded['amount_cents']/100:.2f} "
                      f"on {chain} from {decoded['from_address']} (tx {decoded['tx_hash'][:10]}...)")

    # Safety net: if eth_getLogs was unreliable this cycle (some RPCs
    # restrict historical log queries without a paid tier), don't let a
    # real deposit go undetected just because we couldn't get individual
    # attribution. Fall back to a raw balance check and credit the gap
    # without per-transfer detail rather than silently miss it -- this is
    # exactly the failure mode that caused the original production bug.
    if not is_backfill and not logs_fully_reliable:
        combined_balance = await _get_combined_balance(chain, wallet_address)
        if combined_balance is not None and combined_balance > user.usdc_balance_cents:
            gap = combined_balance - user.usdc_balance_cents
            old_balance = user.usdc_balance_cents
            user.usdc_balance_cents = combined_balance
            retention_expires = datetime.now(tz=timezone.utc) + timedelta(days=365 * 7)
            db.add(UserAuditLog(
                user_id=user.id,
                action="blockchain_deposit_settled_fallback",
                details=json.dumps({
                    "chain": chain,
                    "reason": "eth_getLogs unreliable this cycle, used balance-diff fallback",
                    "amount_cents": gap,
                    "ledger_before_cents": old_balance,
                    "ledger_after_cents": user.usdc_balance_cents,
                    "timestamp": datetime.utcnow().isoformat(),
                }),
                retention_expires_at=retention_expires,
            ))
            db.add(CryptoDeposit(
                user_id=user.id,
                chain=chain,
                contract_address="multiple",
                from_address="unknown (event-log detection unavailable this cycle)",
                to_address=wallet_address,
                amount_cents=gap,
                tx_hash=f"balance-fallback-{chain}-{int(datetime.utcnow().timestamp())}",
                block_number=latest_block,
                credited_to_ledger=True,
            ))
            credited_count += 1
            print(f"[blockchain] ⚠ FALLBACK SETTLED: {user.email} +${gap/100:.2f} on {chain} "
                  f"(event logs unreliable, used balance diff)")

    if checkpoint:
        checkpoint.last_scanned_block = latest_block
    else:
        checkpoint = ChainScanCheckpoint(
            wallet_address=wallet_address,
            chain=chain,
            last_scanned_block=latest_block,
            is_backfilled=True,
        )
        db.add(checkpoint)

    db.commit()
    return credited_count


async def _monitor_loop():
    """
    Autonomous settlement loop: for every user wallet, scan every configured
    chain for new deposits every 15 seconds.
    """
    print("[blockchain] 🚀 SETTLEMENT LAYER ONLINE")
    print(f"[blockchain] Chains: {', '.join(CHAINS.keys())}")
    for chain in CHAINS:
        print(f"[blockchain:{chain}] {len(_rpc_clients[chain].endpoints)} RPC endpoints configured")
    if settings.alchemy_api_key:
        print("[blockchain] ✓ Alchemy enabled (primary, all chains)")
    else:
        print("[blockchain] ⚠ Alchemy not set - using public RPCs (rate-limited)")

    check_interval = 15  # seconds
    settled_count = 0

    while True:
        try:
            db = SessionLocal()
            try:
                users_with_wallets = db.query(User).filter(
                    User.crypto_wallet_address.isnot(None)
                ).all()

                if users_with_wallets:
                    print(f"[blockchain] 🔍 Checking {len(users_with_wallets)} wallets for deposits...")

                    for user in users_with_wallets:
                        for chain in CHAINS:
                            try:
                                settled_count += await _scan_wallet_chain(user, chain, db)
                            except Exception as e:
                                print(f"[blockchain] Scan error for {user.email} on {chain}: {e}")
                                db.rollback()

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
