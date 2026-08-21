"""Stablecoin redemption — sell USDC back to FAWN for USD less a 1-cent fee.

FAWN's own cash-out rail, independent of Stripe.

  POST   /redemptions                       request a redemption
  GET    /redemptions                       my redemptions
  GET    /redemptions/quote?amount_cents=   preview limits before requesting
  POST   /redemptions/{id}/cancel           cancel while still pending
  GET    /redemptions/admin/queue           [admin] open requests + float usage
  POST   /redemptions/admin/{id}/approve    [admin] accept the obligation
  POST   /redemptions/admin/{id}/reject     [admin] refund the hold
  POST   /redemptions/admin/{id}/mark-paid  [admin] record the USD payment
  POST   /redemptions/admin/{id}/mark-failed [admin] payment bounced -> refund

MONEY-SAFETY INVARIANTS
-----------------------
1. USDC leaves the user's spendable balance at REQUEST time and is held on the
   redemption row. A pending redemption can never be double-spent.
2. Rejection, cancellation, and payment failure refund the exact held amount.
   Escrow is always either held, refunded, or consumed — never lost, never
   duplicated.
3. The disclosed one-cent fee is recorded separately and
   `payout_cents + fee_cents == usdc_cents` (also a DB CHECK).
4. Every balance-changing path takes a row lock (`with_for_update`) on both the
   user and the redemption, so concurrent requests cannot race the balance.
5. FAWN never moves fiat here. An operator pays through a real banking rail and
   records the reference. `mark-paid` is bookkeeping, not a payment instruction.

REGULATORY NOTE (read before enabling)
--------------------------------------
Buying and selling convertible virtual currency for fiat makes the operator a
money transmitter / exchanger under FinCEN guidance — an MSB, with registration
and likely state money-transmitter licensing. Dropping Stripe does not remove
that; it makes FAWN the principal instead of a licensed processor's customer.
`redemptions_enabled` defaults to False for exactly this reason. See
docs/merchant/COMPLIANCE.md and get counsel before flipping it on.
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Security
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from dependencies import get_current_user
from models import User, UserAuditLog
from models_redemption import OPEN_STATUSES, PAYOUT_METHODS, StablecoinRedemption

router = APIRouter(prefix="/redemptions", tags=["redemptions"])
REDEMPTION_FEE_CENTS = 1

ADMIN_HEADER = APIKeyHeader(name="X-Admin-Key", auto_error=False)


def _admin_key(key: Optional[str] = Security(ADMIN_HEADER)) -> str:
    if not settings.admin_api_key or not key or key != settings.admin_api_key:
        raise HTTPException(status_code=403, detail="Invalid or missing admin key")
    return key


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _audit(db: Session, user_id: str, action: str, details: dict, request: Request | None = None) -> None:
    db.add(UserAuditLog(
        user_id=user_id,
        action=action,
        details=json.dumps(details, separators=(",", ":"), sort_keys=True),
        ip_address=request.client.host if request and request.client else None,
        user_agent=(request.headers.get("user-agent") or "")[:255] if request else None,
        retention_expires_at=_now() + timedelta(days=365 * 7),
    ))


def _require_enabled() -> None:
    if not settings.redemptions_enabled:
        raise HTTPException(
            status_code=503,
            detail="Cash-out is not enabled yet. Your USDC balance is unaffected.",
        )


def _open_float_cents(db: Session) -> int:
    """Total USD currently promised across open redemptions."""
    return int(db.query(func.coalesce(func.sum(StablecoinRedemption.payout_cents), 0)).filter(
        StablecoinRedemption.status.in_(OPEN_STATUSES)
    ).scalar() or 0)


def _redeemed_last_24h(db: Session, user_id: str) -> int:
    """Sum of this user's redemptions in the last 24h that still count against
    their cap: anything not refunded back to them."""
    since = _now() - timedelta(hours=24)
    return int(db.query(func.coalesce(func.sum(StablecoinRedemption.usdc_cents), 0)).filter(
        StablecoinRedemption.user_id == user_id,
        StablecoinRedemption.requested_at >= since,
        StablecoinRedemption.status.in_(("requested", "approved", "paid")),
    ).scalar() or 0)


def _payload(row: StablecoinRedemption) -> dict:
    return {
        "id": row.id,
        "usdc_cents": row.usdc_cents,
        "usdc": round(row.usdc_cents / 100, 2),
        "payout_cents": row.payout_cents,
        "payout_usd": round(row.payout_cents / 100, 2),
        "fee_cents": row.fee_cents,
        "fee_usd": round(row.fee_cents / 100, 2),
        "rate": "1:1",
        "status": row.status,
        "held_cents": row.held_cents,
        "payout_method": row.payout_method,
        "payout_reference": row.payout_reference,
        "destination_label": row.destination_label,
        "review_notes": row.review_notes,
        "requested_at": row.requested_at.isoformat() if row.requested_at else None,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "paid_at": row.paid_at.isoformat() if row.paid_at else None,
    }


# ── SCHEMAS ────────────────────────────────────────────────────────────────

class RedemptionRequest(BaseModel):
    amount_cents: int = Field(gt=0, le=100_000_000, description="USDC to sell back, in cents")
    destination_label: Optional[str] = Field(default=None, max_length=120,
                                             description="Human label only, e.g. 'Chase ****1234'. Never full bank details.")
    idempotency_key: Optional[str] = Field(default=None, max_length=100)


class PaidRequest(BaseModel):
    payout_method: str = Field(description=f"One of: {', '.join(PAYOUT_METHODS)}")
    payout_reference: str = Field(min_length=2, max_length=140, description="ACH trace / wire ref / check number")
    reviewer: str = Field(min_length=2, max_length=80)
    note: Optional[str] = Field(default=None, max_length=1000)

    @field_validator("payout_method")
    @classmethod
    def _method(cls, v):
        if v not in PAYOUT_METHODS:
            raise ValueError(f"payout_method must be one of: {', '.join(PAYOUT_METHODS)}")
        return v


class ReviewRequest(BaseModel):
    reviewer: str = Field(min_length=2, max_length=80)
    notes: Optional[str] = Field(default=None, max_length=1000)


# ── USER ENDPOINTS ─────────────────────────────────────────────────────────

@router.get("/quote")
def quote(
    amount_cents: int = Query(gt=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Preview whether a redemption would be accepted, without creating one."""
    balance = current_user.usdc_balance_cents or 0
    used = _redeemed_last_24h(db, current_user.id)
    reasons = []
    if not settings.redemptions_enabled:
        reasons.append("Cash-out is not enabled yet")
    if amount_cents < settings.redemption_min_cents:
        reasons.append(f"Minimum is ${settings.redemption_min_cents / 100:.2f}")
    if amount_cents > settings.redemption_max_cents:
        reasons.append(f"Maximum is ${settings.redemption_max_cents / 100:.2f} per request")
    if amount_cents > balance:
        reasons.append("Amount exceeds your balance")
    if used + amount_cents > settings.redemption_daily_max_cents:
        reasons.append(f"Would exceed your 24h limit of ${settings.redemption_daily_max_cents / 100:.2f}")
    return {
        "amount_cents": amount_cents,
        "payout_cents": amount_cents - REDEMPTION_FEE_CENTS,
        "rate": "1:1",
        "fee_cents": REDEMPTION_FEE_CENTS,
        "eligible": not reasons,
        "reasons": reasons,
        "balance_cents": balance,
        "redeemed_last_24h_cents": used,
        "daily_limit_cents": settings.redemption_daily_max_cents,
    }


@router.post("", status_code=201)
def request_redemption(
    req: RedemptionRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Sell USDC back to FAWN less the disclosed one-cent fee."""
    _require_enabled()

    if req.idempotency_key:
        existing = db.query(StablecoinRedemption).filter(
            StablecoinRedemption.idempotency_key == req.idempotency_key
        ).first()
        if existing:
            return _payload(existing)  # replay, not a second redemption

    if req.amount_cents < settings.redemption_min_cents:
        raise HTTPException(status_code=422, detail=f"Minimum redemption is ${settings.redemption_min_cents / 100:.2f}")
    if req.amount_cents > settings.redemption_max_cents:
        raise HTTPException(status_code=422, detail=f"Maximum redemption is ${settings.redemption_max_cents / 100:.2f} per request")

    # Lock the user row so two concurrent requests cannot both pass the
    # balance check and overdraw.
    user = db.query(User).filter(User.id == current_user.id).with_for_update().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    balance = user.usdc_balance_cents or 0
    if req.amount_cents > balance:
        raise HTTPException(status_code=402, detail=f"Insufficient balance. Have ${balance / 100:.2f}, requested ${req.amount_cents / 100:.2f}")

    used = _redeemed_last_24h(db, user.id)
    if used + req.amount_cents > settings.redemption_daily_max_cents:
        raise HTTPException(
            status_code=429,
            detail=f"This would exceed your 24-hour cash-out limit of ${settings.redemption_daily_max_cents / 100:.2f}",
        )

    # FAWN must not promise more dollars than it can actually pay.
    payout_cents = req.amount_cents - REDEMPTION_FEE_CENTS
    if settings.redemption_float_cents > 0:
        if _open_float_cents(db) + payout_cents > settings.redemption_float_cents:
            raise HTTPException(
                status_code=503,
                detail="Cash-out capacity is temporarily exhausted. Your balance is unaffected — try again later.",
            )

    # Escrow: move it out of spendable balance now.
    user.usdc_balance_cents = balance - req.amount_cents
    row = StablecoinRedemption(
        user_id=user.id,
        usdc_cents=req.amount_cents,
        payout_cents=payout_cents,
        fee_cents=REDEMPTION_FEE_CENTS,
        held_cents=req.amount_cents,
        status="requested",
        destination_label=req.destination_label,
        idempotency_key=req.idempotency_key,
    )
    db.add(row)
    _audit(db, user.id, "redemption_requested", {
        "amount_cents": req.amount_cents, "balance_after": user.usdc_balance_cents,
    }, request)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if req.idempotency_key:
            existing = db.query(StablecoinRedemption).filter(
                StablecoinRedemption.idempotency_key == req.idempotency_key
            ).first()
            if existing:
                return _payload(existing)
        raise HTTPException(status_code=409, detail="Could not create redemption")
    db.refresh(row)
    return _payload(row)


@router.get("")
def my_redemptions(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.query(StablecoinRedemption).filter(
        StablecoinRedemption.user_id == current_user.id
    ).order_by(StablecoinRedemption.requested_at.desc()).limit(100).all()
    return {"count": len(rows), "redemptions": [_payload(r) for r in rows]}


@router.post("/{redemption_id}/cancel")
def cancel_redemption(
    redemption_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cancel a redemption that hasn't been paid. Refunds the held USDC."""
    row = db.query(StablecoinRedemption).filter(
        StablecoinRedemption.id == redemption_id,
        StablecoinRedemption.user_id == current_user.id,
    ).with_for_update().first()
    if not row:
        raise HTTPException(status_code=404, detail="Redemption not found")
    if row.status != "requested":
        raise HTTPException(status_code=409, detail=f"Cannot cancel a redemption that is '{row.status}'")

    _release_hold(db, row, "cancelled", reviewer=None, notes="Cancelled by user")
    _audit(db, current_user.id, "redemption_cancelled",
           {"redemption_id": row.id, "refunded_cents": row.usdc_cents}, request)
    db.commit()
    db.refresh(row)
    return _payload(row)


def _release_hold(db: Session, row: StablecoinRedemption, new_status: str,
                  reviewer: Optional[str], notes: Optional[str]) -> None:
    """Refund the escrow to the user and close the redemption.

    Refunds exactly `held_cents` and zeroes it, so a double-call cannot credit
    the balance twice.
    """
    refund = row.held_cents or 0
    if refund:
        user = db.query(User).filter(User.id == row.user_id).with_for_update().first()
        if user:
            user.usdc_balance_cents = (user.usdc_balance_cents or 0) + refund
    row.held_cents = 0
    row.status = new_status
    row.reviewed_by = reviewer
    row.review_notes = notes
    row.closed_at = _now()


# ── ADMIN ENDPOINTS ────────────────────────────────────────────────────────

@router.get("/admin/queue", dependencies=[Depends(_admin_key)])
def admin_queue(db: Session = Depends(get_db)):
    """Open redemptions plus FAWN's current dollar obligation."""
    rows = db.query(StablecoinRedemption).filter(
        StablecoinRedemption.status.in_(OPEN_STATUSES)
    ).order_by(StablecoinRedemption.requested_at.asc()).all()
    open_cents = _open_float_cents(db)
    return {
        "count": len(rows),
        "open_obligation_cents": open_cents,
        "open_obligation_usd": round(open_cents / 100, 2),
        "configured_float_cents": settings.redemption_float_cents,
        "float_remaining_cents": (settings.redemption_float_cents - open_cents) if settings.redemption_float_cents > 0 else None,
        "redemptions_enabled": settings.redemptions_enabled,
        "queue": [_payload(r) for r in rows],
    }


@router.post("/admin/{redemption_id}/approve", dependencies=[Depends(_admin_key)])
def approve(redemption_id: str, req: ReviewRequest, db: Session = Depends(get_db)):
    """Accept the obligation. Escrow stays held; no dollars have moved yet."""
    row = db.query(StablecoinRedemption).filter(
        StablecoinRedemption.id == redemption_id).with_for_update().first()
    if not row:
        raise HTTPException(status_code=404, detail="Redemption not found")
    if row.status != "requested":
        raise HTTPException(status_code=409, detail=f"Cannot approve a redemption that is '{row.status}'")
    row.status = "approved"
    row.approved_at = _now()
    row.reviewed_by = req.reviewer
    row.review_notes = req.notes
    _audit(db, row.user_id, "redemption_approved",
           {"redemption_id": row.id, "reviewer": req.reviewer, "payout_cents": row.payout_cents})
    db.commit()
    db.refresh(row)
    return _payload(row)


@router.post("/admin/{redemption_id}/reject", dependencies=[Depends(_admin_key)])
def reject(redemption_id: str, req: ReviewRequest, db: Session = Depends(get_db)):
    """Decline and refund the held USDC."""
    row = db.query(StablecoinRedemption).filter(
        StablecoinRedemption.id == redemption_id).with_for_update().first()
    if not row:
        raise HTTPException(status_code=404, detail="Redemption not found")
    if row.status not in ("requested", "approved"):
        raise HTTPException(status_code=409, detail=f"Cannot reject a redemption that is '{row.status}'")
    refunded = row.held_cents
    _release_hold(db, row, "rejected", req.reviewer, req.notes)
    _audit(db, row.user_id, "redemption_rejected",
           {"redemption_id": row.id, "reviewer": req.reviewer, "refunded_cents": refunded})
    db.commit()
    db.refresh(row)
    return _payload(row)


@router.post("/admin/{redemption_id}/mark-paid", dependencies=[Depends(_admin_key)])
def mark_paid(redemption_id: str, req: PaidRequest, db: Session = Depends(get_db)):
    """Record that the USD payment was actually sent.

    Bookkeeping only — this endpoint does not move money. The operator must
    have already sent the dollars through a real banking rail and should paste
    that rail's reference here. The escrowed USDC is consumed (FAWN keeps it,
    having paid the user its dollar value).
    """
    row = db.query(StablecoinRedemption).filter(
        StablecoinRedemption.id == redemption_id).with_for_update().first()
    if not row:
        raise HTTPException(status_code=404, detail="Redemption not found")
    if row.status != "approved":
        raise HTTPException(status_code=409, detail="Only an approved redemption can be marked paid")

    row.status = "paid"
    row.held_cents = 0            # consumed, not refunded
    row.payout_method = req.payout_method
    row.payout_reference = req.payout_reference
    row.payout_note = req.note
    row.reviewed_by = req.reviewer
    row.paid_at = _now()
    row.closed_at = _now()
    _audit(db, row.user_id, "redemption_paid", {
        "redemption_id": row.id, "payout_cents": row.payout_cents,
        "method": req.payout_method, "reference": req.payout_reference,
        "reviewer": req.reviewer,
    })
    db.commit()
    db.refresh(row)
    return _payload(row)


@router.post("/admin/{redemption_id}/mark-failed", dependencies=[Depends(_admin_key)])
def mark_failed(redemption_id: str, req: ReviewRequest, db: Session = Depends(get_db)):
    """The USD payment failed (bounced ACH, bad details). Refund the hold."""
    row = db.query(StablecoinRedemption).filter(
        StablecoinRedemption.id == redemption_id).with_for_update().first()
    if not row:
        raise HTTPException(status_code=404, detail="Redemption not found")
    if row.status not in ("requested", "approved"):
        raise HTTPException(status_code=409, detail=f"Cannot fail a redemption that is '{row.status}'")
    refunded = row.held_cents
    _release_hold(db, row, "failed", req.reviewer, req.notes)
    _audit(db, row.user_id, "redemption_failed",
           {"redemption_id": row.id, "reviewer": req.reviewer, "refunded_cents": refunded})
    db.commit()
    db.refresh(row)
    return _payload(row)
