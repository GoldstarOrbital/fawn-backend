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
