"""Fawn Bucks: off-chain, non-transferable, non-redeemable service credits."""
import json
import os
import uuid
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from dependencies import get_current_user
from models import BuckCredit, BuckFundingPayment, BuckLedgerEntry, User

router = APIRouter(prefix="/bucks", tags=["bucks"])
FEE_CENTS_PER_DOLLAR = 1


class FundingRequest(BaseModel):
    dollars: int = Field(..., ge=1, le=10000)


def _stripe_key():
    return settings.stripe_secret_key or os.environ.get("STRIPE_SECRET_KEY", "")


@router.get("/pricing")
def pricing():
    return {"fee_cents_per_dollar": FEE_CENTS_PER_DOLLAR, "minimum_dollars": 1,
            "description": "1 Buck is one whole-dollar Fawn service credit; Bucks are off-chain, non-transferable, and non-redeemable."}


@router.post("/checkout")
def create_checkout(req: FundingRequest, current_user: User = Depends(get_current_user)):
    key = _stripe_key()
    if not key:
        raise HTTPException(status_code=503, detail="Stripe payments are not configured")
    amount_cents = req.dollars * 100
    fee_cents = req.dollars * FEE_CENTS_PER_DOLLAR
    stripe.api_key = key
    try:
        session = stripe.checkout.Session.create(
            mode="payment", customer_email=current_user.email,
            line_items=[{"price_data": {"currency": "usd", "product_data": {"name": "Fawn Bucks funding"},
                                         "unit_amount": amount_cents + fee_cents}, "quantity": 1}],
            metadata={"fawn_product": "bucks", "user_id": current_user.id,
                      "amount_cents": str(amount_cents), "fee_cents": str(fee_cents), "buck_count": str(req.dollars)},
            success_url=f"{settings.frontend_base_url.rstrip('/')}/?bucks=success",
            cancel_url=f"{settings.frontend_base_url.rstrip('/')}/?bucks=cancelled",
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Could not create Stripe checkout") from exc
    return {"checkout_url": session.url, "session_id": session.id, "amount_cents": amount_cents,
            "fee_cents": fee_cents, "total_cents": amount_cents + fee_cents, "bucks": req.dollars}


@router.get("")
def get_bucks(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    payments = db.query(BuckFundingPayment).filter(BuckFundingPayment.user_id == current_user.id).order_by(BuckFundingPayment.created_at.desc()).all()
    credits = db.query(BuckCredit).filter(BuckCredit.user_id == current_user.id, BuckCredit.status == "active").order_by(BuckCredit.issued_at.desc()).all()
    return {"balance": len(credits), "serial_numbers": [c.serial_number for c in credits],
            "history": [{"id": p.id, "amount_cents": p.amount_cents, "fee_cents": p.fee_cents,
                         "total_cents": p.total_cents, "bucks": p.buck_count, "status": p.status,
                         "created_at": p.created_at.isoformat() if p.created_at else None} for p in payments]}


def _metadata_int(metadata, key):
    try:
        return int(metadata.get(key, 0))
    except (TypeError, ValueError):
        return 0


def handle_buck_event(event: dict, db: Session):
    """Process final Stripe events after the shared signature check."""
    event_type = event.get("type", "")
    obj = event.get("data", {}).get("object", {})
    metadata = obj.get("metadata") or {}
    if event_type == "checkout.session.completed":
        if metadata.get("fawn_product") != "bucks" or obj.get("payment_status") not in ("paid", None):
            return {"received": True, "type": event_type, "skipped": "not a paid Bucks session"}
    elif event_type in ("charge.refunded", "charge.dispute.created"):
        payment_intent_id = obj.get("payment_intent")
        payment = db.query(BuckFundingPayment).filter(BuckFundingPayment.stripe_payment_intent_id == payment_intent_id).first()
        if not payment:
            return {"received": True, "type": event_type, "skipped": "payment not found"}
        reason = "refund" if event_type == "charge.refunded" else "dispute"
        if reason == "refund":
            payment.refunded_cents = max(payment.refunded_cents, int(obj.get("amount_refunded", payment.total_cents)))
        else:
            payment.disputed_at = datetime.now(timezone.utc)
        active = db.query(BuckCredit).filter(BuckCredit.payment_id == payment.id, BuckCredit.status == "active").all()
        if active:
            serials = [c.serial_number for c in active]
            now = datetime.now(timezone.utc)
            for credit in active:
                credit.status = "reversed"
                credit.reversed_at = now
            db.add(BuckLedgerEntry(user_id=payment.user_id, payment_id=payment.id, entry_type=f"{reason}_reversal",
                                   bucks_delta=-len(active), serial_numbers=json.dumps(serials), reason=reason))
        payment.status = "disputed" if reason == "dispute" else "refunded"
        db.commit()
        return {"received": True, "type": event_type, "reversed": len(active)}
    else:
        return {"received": True, "type": event_type, "skipped": "not a Bucks event"}

    session_id = obj.get("id")
    if not session_id or metadata.get("user_id") is None:
        return {"received": True, "type": event_type, "skipped": "missing Bucks metadata"}
    existing = db.query(BuckFundingPayment).filter(BuckFundingPayment.stripe_checkout_session_id == session_id).first()
    if existing:
        return {"received": True, "type": event_type, "duplicate": True}
    amount_cents = _metadata_int(metadata, "amount_cents")
    fee_cents = _metadata_int(metadata, "fee_cents")
    buck_count = _metadata_int(metadata, "buck_count")
    if amount_cents <= 0 or fee_cents != buck_count or buck_count <= 0 or amount_cents != buck_count * 100:
        return {"received": True, "type": event_type, "skipped": "invalid Bucks metadata"}
    payment = BuckFundingPayment(user_id=metadata["user_id"], stripe_checkout_session_id=session_id,
                                 stripe_payment_intent_id=obj.get("payment_intent"), amount_cents=amount_cents,
                                 fee_cents=fee_cents, total_cents=amount_cents + fee_cents,
                                 buck_count=buck_count, status="succeeded", completed_at=datetime.now(timezone.utc))
    db.add(payment)
    db.flush()
    serials = []
    for _ in range(buck_count):
        serial = f"FB-{uuid.uuid4().hex.upper()}"
        serials.append(serial)
        db.add(BuckCredit(user_id=payment.user_id, payment_id=payment.id, serial_number=serial))
    db.add(BuckLedgerEntry(user_id=payment.user_id, payment_id=payment.id, entry_type="issuance",
                           bucks_delta=buck_count, serial_numbers=json.dumps(serials), reason="successful Stripe payment"))
    db.commit()
    return {"received": True, "type": event_type, "bucks_issued": buck_count, "payment_id": payment.id}
