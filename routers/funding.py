"""Add Funds: pull money from an external bank account into a FAWN
deposit account via ACH.

Uses Unit's inline-counterparty ACH payment — no Plaid Link integration
yet, so there's no verification that the caller actually owns the
external account they entered. That's a real fraud surface (unauthorized
ACH debit is a known abuse pattern), so limits here are deliberately
conservative until Plaid verification is added on top of this. Treat
"add Plaid ownership verification" as the next step before raising these
caps, the same way P2P's Tier 2 external-send is gated on confirming
which rail Unit's sponsor bank supports.

ACH settles in days, not instantly, and can be returned by the sending
bank — never treat a "completed" Unit API call here as final/irreversible
the way a P2P Book Payment is.
"""
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session
from slowapi import Limiter
from slowapi.util import get_remote_address

from database import get_db
from models import User, FundingRequest, P2PAuditLog
from schemas import AddFundsRequest, FundingRequestOut, FundingRequestList
from dependencies import get_current_user
from services import unit as unit_svc

router = APIRouter(prefix="/funding", tags=["funding"])
limiter = Limiter(key_func=get_remote_address)

# Conservative until Plaid-verified ownership exists — see module docstring.
PER_REQUEST_LIMIT_CENTS = 50_000      # $500 per request
DAILY_LIMIT_CENTS = 100_000           # $1,000 rolling 24h


def _to_out(r: FundingRequest) -> FundingRequestOut:
    return FundingRequestOut(
        id=r.id, amount_cents=r.amount_cents, status=r.status,
        external_account_last4=r.external_account_last4,
        created_at=r.created_at.isoformat() if r.created_at else "",
        completed_at=r.completed_at.isoformat() if r.completed_at else None,
        error_message=r.error_message,
    )


def _check_limits(db: Session, user_id: str, amount_cents: int):
    if amount_cents > PER_REQUEST_LIMIT_CENTS:
        raise HTTPException(status_code=400, detail=f"Add Funds requests are capped at ${PER_REQUEST_LIMIT_CENTS / 100:.2f} for now.")
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    day_total = db.query(func.coalesce(func.sum(FundingRequest.amount_cents), 0)).filter(
        FundingRequest.user_id == user_id,
        FundingRequest.status != "failed",
        FundingRequest.created_at >= since,
    ).scalar() or 0
    if day_total + amount_cents > DAILY_LIMIT_CENTS:
        raise HTTPException(status_code=400, detail=f"This would exceed your 24-hour Add Funds limit of ${DAILY_LIMIT_CENTS / 100:.2f}.")


@router.post("/add-funds", response_model=FundingRequestOut, status_code=201)
@limiter.limit("10/minute")
async def add_funds(request: Request, req: AddFundsRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not current_user.unit_account_id:
        raise HTTPException(status_code=400, detail="You need an active FAWN bank account before you can add funds.")

    existing = db.query(FundingRequest).filter(FundingRequest.idempotency_key == req.idempotency_key).first()
    if existing:
        return _to_out(existing)

    _check_limits(db, current_user.id, req.amount_cents)

    last4 = req.account_number[-4:]
    funding = FundingRequest(
        user_id=current_user.id,
        amount_cents=req.amount_cents,
        external_account_last4=last4,
        idempotency_key=req.idempotency_key,
    )
    db.add(funding)
    db.flush()
    db.add(P2PAuditLog(
        transfer_id=funding.id, user_id=current_user.id, event_type="funding_requested",
        metadata_json=json.dumps({"amount_cents": req.amount_cents, "external_account_last4": last4}),
    ))

    try:
        payment = await unit_svc.create_ach_funding_payment(
            unit_account_id=current_user.unit_account_id,
            routing_number=req.routing_number,
            account_number=req.account_number,
            account_type=req.account_type,
            account_holder_name=req.account_holder_name,
            amount_cents=req.amount_cents,
            idempotency_key=req.idempotency_key,
        )
        funding.unit_payment_id = payment["id"]
        funding.status = "completed"  # "completed" = the ACH pull was successfully initiated, not that funds have settled
        funding.completed_at = datetime.now(timezone.utc)
        db.add(P2PAuditLog(
            transfer_id=funding.id, user_id=current_user.id, event_type="funding_initiated",
            metadata_json=json.dumps({"unit_payment_id": payment["id"]}),
        ))
    except Exception as e:
        funding.status = "failed"
        funding.error_message = str(e)[:500]
        db.add(P2PAuditLog(
            transfer_id=funding.id, user_id=current_user.id, event_type="funding_failed",
            metadata_json=json.dumps({"error": str(e)[:300]}),
        ))
        db.commit()
        raise HTTPException(status_code=502, detail="Couldn't start that transfer. No money was moved — try again.")

    db.commit()
    return _to_out(funding)


@router.get("/history", response_model=FundingRequestList)
def funding_history(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = (
        db.query(FundingRequest)
        .filter(FundingRequest.user_id == current_user.id)
        .order_by(FundingRequest.created_at.desc())
        .limit(50)
        .all()
    )
    return FundingRequestList(requests=[_to_out(r) for r in rows])
