"""
Emergency admin endpoint for manual balance credits (undetected on-chain deposits, etc)
SECURITY: Requires X-Admin-Key header (same as /fees/collect)
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
from database import get_db
from models import User, UserAuditLog, ChainScanCheckpoint, CryptoDeposit
from routers.admin import require_admin_key
import json

router = APIRouter(prefix="/admin", tags=["admin"])


class RewindCheckpointRequest(BaseModel):
    wallet_address: str
    chain: str
    blocks_back: int = 15000


@router.post("/rewind-checkpoint")
async def rewind_checkpoint(
    req: RewindCheckpointRequest,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin_key),
):
    """
    Rewind a (wallet, chain) scan checkpoint so the NEXT background/sync-now
    cycle re-scans that range via the normal event-log path -- lets the
    automated system catch a deposit it missed (e.g. during an RPC outage
    that predates the checkpoint-advancement fix) itself, with real
    per-transfer attribution, instead of an admin computing and injecting a
    balance delta by hand.

    Deliberately rewinds rather than deletes the checkpoint: deleting it
    would make the next scan a fresh backfill pass, which records but does
    NOT credit deposits (by design, for pre-existing balances). Rewinding
    keeps it in normal incremental/crediting mode, just starting from an
    earlier block. Already-recorded deposits in the re-scanned range are
    no-ops (CryptoDeposit's chain+tx_hash+contract+to_address unique
    constraint), so this is safe to run even if some of the range was
    already scanned successfully.
    """
    checkpoint = db.query(ChainScanCheckpoint).filter(
        ChainScanCheckpoint.wallet_address.ilike(req.wallet_address),
        ChainScanCheckpoint.chain == req.chain,
    ).first()
    if not checkpoint:
        raise HTTPException(status_code=404, detail=f"No checkpoint for {req.wallet_address} on {req.chain}")

    old_block = checkpoint.last_scanned_block
    checkpoint.last_scanned_block = max(old_block - req.blocks_back, 0)
    db.commit()

    return {
        "wallet_address": req.wallet_address,
        "chain": req.chain,
        "old_last_scanned_block": old_block,
        "new_last_scanned_block": checkpoint.last_scanned_block,
        "blocks_back": req.blocks_back,
    }


@router.get("/rpc-health")
async def rpc_health(
    chain: str,
    _: str = Depends(require_admin_key),
):
    """Debug helper: call eth_blockNumber against every configured RPC
    endpoint for one chain, from Railway's own network path, and report
    exactly what each one returns. A checkpoint that stops advancing could
    mean every endpoint is failing in a way a local curl from a different
    network wouldn't reproduce (e.g. a public RPC rate-limiting or
    blocking Railway's egress IP specifically)."""
    from services import blockchain_monitor as bm
    import httpx as httpx_lib

    if chain not in bm.CHAINS:
        raise HTTPException(status_code=400, detail=f"Unknown chain: {chain}")

    endpoints = bm._get_rpc_endpoints(chain)
    results = []
    for endpoint in endpoints:
        display = endpoint if "alchemy" not in endpoint else endpoint.rsplit("/", 1)[0] + "/***"
        entry = {"endpoint": display}
        try:
            async with httpx_lib.AsyncClient(timeout=15.0) as client:
                resp = await client.post(endpoint, json={
                    "jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1,
                })
                entry["http_status"] = resp.status_code
                body = resp.json()
                if "error" in body:
                    entry["rpc_error"] = body["error"]
                else:
                    entry["result_block"] = int(body["result"], 16) if body.get("result") else None
        except Exception as e:
            entry["exception"] = f"{type(e).__name__}: {e}"
        results.append(entry)

    return {"chain": chain, "endpoints_tried": results}


@router.get("/wallet-scan-status")
async def wallet_scan_status(
    wallet_address: str,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin_key),
):
    """Debug helper: see exactly what the blockchain monitor has recorded
    for a wallet -- per-chain checkpoint (last scanned block, backfilled
    status) and recent CryptoDeposit records. Lets us tell apart "monitor
    hasn't reached this block yet" from "monitor is stuck/erroring" from
    "logs missed it, fallback should catch it next cycle" without needing
    direct Railway log access."""
    checkpoints = db.query(ChainScanCheckpoint).filter(
        ChainScanCheckpoint.wallet_address.ilike(wallet_address)
    ).all()
    deposits = db.query(CryptoDeposit).join(User).filter(
        User.crypto_wallet_address.ilike(wallet_address)
    ).order_by(CryptoDeposit.created_at.desc()).limit(20).all()

    return {
        "wallet_address": wallet_address,
        "checkpoints": [
            {
                "chain": c.chain,
                "last_scanned_block": c.last_scanned_block,
                "is_backfilled": c.is_backfilled,
                "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            }
            for c in checkpoints
        ],
        "recent_deposits": [
            {
                "chain": d.chain,
                "contract_address": d.contract_address,
                "from_address": d.from_address,
                "amount_cents": d.amount_cents,
                "tx_hash": d.tx_hash,
                "block_number": d.block_number,
                "credited_to_ledger": d.credited_to_ledger,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in deposits
        ],
    }

class ManualCreditRequest(BaseModel):
    wallet_address: str
    amount_cents: int
    reason: str = "manual_deposit_credit"

@router.post("/credit-balance")
async def manual_credit_balance(
    req: ManualCreditRequest,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin_key),
):
    """
    Manually credit a user's USDC balance.

    SECURITY: Requires X-Admin-Key header (same as /fees/collect)
    """
    # Find user by wallet
    user = db.query(User).filter(
        User.crypto_wallet_address.ilike(req.wallet_address)
    ).first()

    if not user:
        raise HTTPException(
            status_code=404,
            detail=f"No user found with wallet: {req.wallet_address}"
        )

    # Credit the balance
    old_balance = user.usdc_balance_cents
    user.usdc_balance_cents += req.amount_cents

    # Create audit log (7-year retention)
    retention_expires = datetime.now(tz=timezone.utc) + timedelta(days=365*7)
    audit = UserAuditLog(
        user_id=user.id,
        action="manual_balance_credit",
        details=json.dumps({
            "wallet": req.wallet_address,
            "amount_cents": req.amount_cents,
            "old_balance_cents": old_balance,
            "new_balance_cents": user.usdc_balance_cents,
            "reason": req.reason,
            "timestamp": datetime.utcnow().isoformat(),
        }),
        retention_expires_at=retention_expires,
    )
    db.add(audit)
    db.commit()

    return {
        "user_id": user.id,
        "email": user.email,
        "wallet": req.wallet_address,
        "amount_credited": f"${req.amount_cents / 100:.2f}",
        "old_balance": f"${old_balance / 100:.2f}",
        "new_balance": f"${user.usdc_balance_cents / 100:.2f}",
        "timestamp": datetime.utcnow().isoformat(),
        "status": "credited"
    }
