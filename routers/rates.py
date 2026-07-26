"""
Live rates / markets hub router for FAWN.

Thin HTTP wrapper over services/rates.py. Exposes current crypto spot prices,
a symbol-to-symbol converter, and a per-user list of recently viewed
conversions.

READ-ONLY / TRACKING-ONLY: nothing in this router moves money -- it never
touches balances, never creates transfers, and never calls settlement or
on-chain code. The only write is an append-only tracking breadcrumb to the
existing UserAuditLog table.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database import get_db
from dependencies import get_current_user
from models import User
from services import rates as rates_service

router = APIRouter(prefix="/rates", tags=["rates"])


@router.get("/crypto")
def get_crypto_rates(
    current_user: User = Depends(get_current_user),
):
    """Current spot USD prices for FAWN's tracked tokens (<=60s cached)."""
    snapshot = rates_service.get_crypto_prices()
    if not snapshot["available"]:
        raise HTTPException(status_code=503, detail="Live rates temporarily unavailable")
    return snapshot


@router.get("/convert")
def convert_rate(
    amount: float = Query(..., ge=0, le=1_000_000_000),
    from_symbol: str = Query(..., alias="from", min_length=1, max_length=12),
    to_symbol: str = Query(..., alias="to", min_length=1, max_length=12),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Convert an amount between two tracked symbols (or USD) at spot rates."""
    try:
        result = rates_service.convert(amount, from_symbol, to_symbol)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not result.get("available"):
        raise HTTPException(status_code=503, detail="Live rates temporarily unavailable")

    # Tracking-only breadcrumb. It must never break the response, so failures
    # are swallowed after rolling back the (non-money) audit write.
    try:
        rates_service.log_conversion(db, current_user.id, result)
    except Exception:
        db.rollback()

    return result


@router.get("/history")
def conversion_history(
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Recent conversions this user has viewed (tracking-only, read from audit log)."""
    return rates_service.get_conversion_history(db, current_user.id, limit=limit)
