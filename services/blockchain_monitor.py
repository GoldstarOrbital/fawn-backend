"""
Blockchain monitor for Polygon USDC transfers (simplified version).

Queries user's USDC balance on-chain every 60 seconds and credits difference.
More reliable than log-based approach for high-latency/rate-limited RPCs.
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

# Polygon Mainnet
POLYGON_RPC = "https://polygon-rpc.com"
USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99a7a9449Aa84174"

# Track last known on-chain balance per user to detect changes
_LAST_BALANCE_CACHE = {}


async def _call_rpc(method: str, params: list) -> dict:
    """Make a JSON-RPC call with retry logic."""
    for attempt in range(3):
        try:
            payload = {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
                "id": 1,
            }
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(POLYGON_RPC, json=payload)
                response.raise_for_status()
                result = response.json()
                if "error" in result:
                    if attempt < 2:
                        await asyncio.sleep(1)
                        continue
                    print(f"[blockchain] RPC error: {result['error']}")
                    return None
                return result.get("result")
        except Exception as e:
            if attempt < 2:
                await asyncio.sleep(1)
                continue
            print(f"[blockchain] RPC failed (attempt {attempt+1}/3): {e}")
            return None
    return None


async def _get_usdc_balance(wallet_address: str) -> Optional[int]:
    """Query user's USDC balance on Polygon using eth_call."""
    # balanceOf(address) signature
    method_sig = "0x70a08231"  # keccak256("balanceOf(address)")
    padded_addr = wallet_address.lower().replace("0x", "").zfill(64)
    call_data = method_sig + padded_addr

    result = await _call_rpc("eth_call", [
        {
            "to": USDC_CONTRACT,
            "data": call_data,
        },
        "latest",
    ])

    if result and result.startswith("0x"):
        try:
            balance_wei = int(result, 16)
            balance_cents = balance_wei // (10 ** 6)  # USDC has 6 decimals
            return balance_cents
        except Exception as e:
            print(f"[blockchain] Failed to parse balance: {e}")
            return None
    return None


async def _check_and_credit_balance(wallet_address: str, db: Session) -> bool:
    """
    Check on-chain USDC balance and credit difference to user if higher than our ledger.
    Returns True if credited something.
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

    # Only credit if on-chain is higher (deposit detected)
    if difference > 0:
        old_balance = user.usdc_balance_cents
        user.usdc_balance_cents = on_chain_balance

        retention_expires = datetime.now(tz=timezone.utc) + timedelta(days=365*7)
        audit = UserAuditLog(
            user_id=user.id,
            action="blockchain_usdc_deposit_detected",
            details=json.dumps({
                "wallet": wallet_address,
                "on_chain_balance_cents": on_chain_balance,
                "ledger_balance_cents": old_balance,
                "difference_cents": difference,
                "new_balance_cents": user.usdc_balance_cents,
                "timestamp": datetime.utcnow().isoformat(),
            }),
            retention_expires_at=retention_expires,
        )
        db.add(audit)
        db.commit()

        print(f"[blockchain] ✓ Detected deposit for {user.email}: +${difference/100:.2f}")
        return True

    return False


async def _monitor_loop():
    """Main monitor loop: check all users' on-chain balances every 60 seconds."""
    print("[blockchain] Starting USDC balance monitor on Polygon...")
    check_interval = 60  # seconds

    while True:
        try:
            db = SessionLocal()
            try:
                # Get all users with wallets
                users_with_wallets = db.query(User).filter(
                    User.crypto_wallet_address.isnot(None)
                ).all()

                if users_with_wallets:
                    print(f"[blockchain] Checking {len(users_with_wallets)} wallets...")
                    for user in users_with_wallets:
                        try:
                            await _check_and_credit_balance(user.crypto_wallet_address, db)
                        except Exception as e:
                            print(f"[blockchain] Error checking {user.email}: {e}")

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
