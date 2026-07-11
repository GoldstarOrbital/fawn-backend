"""
Blockchain monitor for Polygon USDC transfers.

Listens for incoming USDC transfers to user wallets and auto-credits balances.
Runs as a background task on Railway. Queries every 30 seconds for new transfers.
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

# Polygon chain configs
POLYGON_MAINNET_RPC = "https://polygon-rpc.com"
POLYGON_MUMBAI_RPC = "https://rpc-mumbai.maticvigil.com"

# USDC contract on Polygon mainnet
USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99a7a9449Aa84174"

# Batch size for eth_getLogs calls
BATCH_SIZE = 1000

# Track processed tx hashes to avoid double-crediting
_PROCESSED_TXS = set()


def _get_rpc_url() -> str:
    """Get the appropriate RPC URL based on network."""
    network = os.environ.get("POLYGON_NETWORK", "mainnet").lower()
    if "mumbai" in network or "testnet" in network:
        return POLYGON_MUMBAI_RPC
    return POLYGON_MAINNET_RPC


async def _call_rpc(method: str, params: list) -> dict:
    """Make a JSON-RPC call to Polygon."""
    rpc_url = _get_rpc_url()
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": 1,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(rpc_url, json=payload)
            response.raise_for_status()
            result = response.json()
            if "error" in result:
                print(f"[blockchain] RPC error: {result['error']}")
                return None
            return result.get("result")
        except Exception as e:
            print(f"[blockchain] RPC call failed: {e}")
            return None


async def _get_latest_block() -> Optional[int]:
    """Get the latest block number on Polygon."""
    result = await _call_rpc("eth_blockNumber", [])
    if result:
        return int(result, 16)
    return None


async def _get_logs(
    from_block: int,
    to_block: int,
    address: str = USDC_CONTRACT,
    topic0: str = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",  # Transfer event
) -> list:
    """Fetch transfer logs in a block range."""
    result = await _call_rpc("eth_getLogs", [
        {
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
            "address": address,
            "topics": [topic0],
        }
    ])
    return result if result else []


async def _process_transfer(log: dict, db: Session) -> bool:
    """
    Process a single transfer log.

    Returns True if credited, False if already processed or no matching user.
    """
    tx_hash = log["transactionHash"]

    # Skip if already processed
    if tx_hash in _PROCESSED_TXS:
        return False

    # Parse log data
    # Transfer event: indexed(from), indexed(to), value
    # topics[0] = keccak256("Transfer(address,indexed address,indexed address,uint256)")
    # topics[1] = from_address (padded)
    # topics[2] = to_address (padded)
    # data = amount (uint256)

    if len(log.get("topics", [])) < 3:
        return False

    to_address = "0x" + log["topics"][2][-40:]  # Extract address from topic
    amount_hex = log.get("data", "0x0")
    try:
        amount_cents = int(amount_hex, 16) // (10 ** 6)  # USDC has 6 decimals, convert to cents
    except (ValueError, TypeError):
        return False

    if amount_cents <= 0:
        return False

    # Find user with this wallet
    user = db.query(User).filter(
        User.crypto_wallet_address.ilike(to_address)
    ).first()

    if not user:
        print(f"[blockchain] Transfer to unknown wallet {to_address}: {amount_cents}¢")
        return False

    # Credit the user
    old_balance = user.usdc_balance_cents
    user.usdc_balance_cents += amount_cents

    # Create audit log
    retention_expires = datetime.now(tz=timezone.utc) + timedelta(days=365*7)
    audit = UserAuditLog(
        user_id=user.id,
        action="blockchain_usdc_deposit",
        details=json.dumps({
            "tx_hash": tx_hash,
            "wallet": to_address,
            "amount_cents": amount_cents,
            "old_balance_cents": old_balance,
            "new_balance_cents": user.usdc_balance_cents,
            "block_number": log.get("blockNumber"),
        }),
        retention_expires_at=retention_expires,
    )
    db.add(audit)
    db.commit()

    print(f"[blockchain] ✓ Credited {user.email}: ${amount_cents/100:.2f} (tx: {tx_hash[:10]}...)")
    _PROCESSED_TXS.add(tx_hash)
    return True


async def _monitor_loop():
    """Main monitor loop: check for transfers every 30 seconds."""
    print("[blockchain] Starting USDC transfer monitor on Polygon...")

    last_block = None
    check_interval = 30  # seconds

    while True:
        try:
            latest = await _get_latest_block()
            if not latest:
                print("[blockchain] Could not fetch latest block, retrying...")
                await asyncio.sleep(check_interval)
                continue

            if last_block is None:
                # First run: start from current block (no backlog)
                last_block = latest
                print(f"[blockchain] Starting from block {last_block}")
                await asyncio.sleep(check_interval)
                continue

            if latest <= last_block:
                # No new blocks yet
                await asyncio.sleep(check_interval)
                continue

            # Fetch logs in batches (RPC limit ~5000 per call)
            print(f"[blockchain] Scanning blocks {last_block+1} to {latest}...")
            db = SessionLocal()
            try:
                batch_start = last_block + 1
                while batch_start <= latest:
                    batch_end = min(batch_start + BATCH_SIZE, latest)
                    logs = await _get_logs(batch_start, batch_end)

                    if logs:
                        for log in logs:
                            try:
                                await _process_transfer(log, db)
                            except Exception as e:
                                print(f"[blockchain] Failed to process log: {e}")

                    batch_start = batch_end + 1

                last_block = latest
            finally:
                db.close()

            await asyncio.sleep(check_interval)

        except asyncio.CancelledError:
            print("[blockchain] Monitor shutting down...")
            raise
        except Exception as e:
            print(f"[blockchain] Monitor error (will retry): {e}")
            await asyncio.sleep(check_interval)


def start_blockchain_monitor():
    """Start the blockchain monitor as a background task."""
    import asyncio
    loop = asyncio.get_event_loop()
    task = loop.create_task(_monitor_loop())
    return task
