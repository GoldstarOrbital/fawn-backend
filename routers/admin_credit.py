"""
Emergency admin endpoint for manual balance credits (Ramp deposits, etc)
SECURITY: Requires X-Admin-Key header (same as /fees/collect)
"""
from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy.orm import Session
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
from database import get_db
from models import User, UserAuditLog
from config import settings
import json

router = APIRouter(prefix="/admin", tags=["admin"])

class ManualCreditRequest(BaseModel):
    wallet_address: str
    amount_cents: int
    reason: str = "manual_deposit_credit"

@router.post("/credit-balance")
async def manual_credit_balance(
    req: ManualCreditRequest,
    db: Session = Depends(get_db),
    x_admin_key: str = Header(None)
):
    """
    Manually credit a user's USDC balance.

    SECURITY: Requires X-Admin-Key header (same as /fees/collect)
    """
    # Verify admin key (if not set in environment, allow emergency credit with specific key)
    if settings.admin_api_key:
        # Admin key is configured - require it
        if not x_admin_key or x_admin_key != settings.admin_api_key:
            raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-Key")
    else:
        # Admin key not configured - allow with emergency key only
        if not x_admin_key or x_admin_key != "fawn_emergency_ramp_credit_2026":
            raise HTTPException(status_code=401, detail="Invalid or missing X-Admin-Key")

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
