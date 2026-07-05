"""
Stripe BaaS (Connect + Treasury) webhook receiver.

On account.updated, once the connected account's `treasury` capability
becomes "active", finishes account setup by creating the Treasury
Financial Account. Direct-KYC users and hosted-onboarding users are both
matched by stripe_account_id, which is set as soon as the Connect account
is created (see routers/auth.py and routers/stripe_onboarding.py).

Register this endpoint separately from the existing founding-member
checkout webhook (routers/stripe_webhook.py) in Stripe Dashboard ->
Developers -> Webhooks:
  URL: https://web-production-13d5b.up.railway.app/stripe/baas-webhook
  Events: account.updated, treasury.financial_account.created,
          treasury.inbound_transfer.succeeded, treasury.inbound_transfer.failed
"""
import hashlib
import hmac
import json
import time

from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.orm import Session

from database import get_db
from models import User, StripeEvent
from config import settings
from services import stripe_baas as stripe_svc

router = APIRouter(prefix="/stripe", tags=["stripe-baas-webhook"])


def _verify_stripe_signature(payload: bytes, sig_header: str, secret: str) -> bool:
    if not sig_header or not secret:
        return False
    try:
        parts = dict(p.split("=", 1) for p in sig_header.split(","))
        timestamp = parts.get("t")
        provided = parts.get("v1")
        if not timestamp or not provided:
            return False
        signed = f"{timestamp}.{payload.decode('utf-8')}".encode("utf-8")
        expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
        if abs(int(time.time()) - int(timestamp)) > 300:
            return False
        return hmac.compare_digest(expected, provided)
    except Exception:
        return False


async def _handle_account_updated(obj: dict, db: Session):
    account_id = obj.get("id")
    if not account_id:
        return
    if not stripe_svc.account_is_active(obj):
        return

    user = db.query(User).filter(User.stripe_account_id == account_id).first()
    if not user or user.stripe_financial_account_id:
        return

    financial_account = await stripe_svc.create_financial_account(account_id)
    user.stripe_financial_account_id = financial_account["id"]
    db.commit()


@router.post("/baas-webhook")
async def stripe_baas_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    secret = settings.stripe_baas_webhook_secret

    if not secret and not settings.allow_unsigned_baas_webhooks:
        raise HTTPException(status_code=503, detail="Stripe BaaS webhook secret is not configured")
    if secret and not _verify_stripe_signature(payload, sig_header, secret):
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        event = json.loads(payload)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_id = event.get("id", "")
    event_type = event.get("type", "")

    if event_id:
        existing = db.query(StripeEvent).filter(StripeEvent.id == event_id).first()
        if existing:
            return {"received": True, "duplicate": True, "type": event_type}
        db.add(StripeEvent(id=event_id, type=event_type))
        db.commit()

    obj = event.get("data", {}).get("object", {})

    try:
        if event_type == "account.updated":
            await _handle_account_updated(obj, db)
    except Exception as e:
        print(f"[Stripe BaaS webhook] failed to process {event_type} ({event_id}): {e}")

    return {"received": True, "type": event_type}
