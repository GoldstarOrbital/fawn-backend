"""
routers/unit_webhook.py

Unit webhook receiver. On application.approved: finishes account setup
(creates the deposit account) immediately, the same way auth.py's
register() does on the instant-approve path — this is what actually makes
that "account creation will retry via webhook" comment in auth.py true,
instead of relying purely on /accounts/refresh-application-status polling
or the sandbox-only /accounts/activate-sandbox unstick button.

Signature verified via UNIT_WEBHOOK_SECRET (HMAC-SHA1 of the raw body,
base64-encoded, compared against the X-Unit-Signature header — see
https://www.unit.co/docs/api/webhooks/). Production should fail closed when
the secret is missing. Local/dev can opt into unsigned webhooks with
ALLOW_UNSIGNED_UNIT_WEBHOOKS=true.

Register in Unit's dashboard → Webhooks:
  URL: https://web-production-13d5b.up.railway.app/unit/webhook
  Events: application.approved, application.denied, application.pendingReview
"""
import base64
import hashlib
import hmac
import json

from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.orm import Session

from database import get_db
from models import User, UnitEvent
from config import settings
from services import unit as unit_svc

router = APIRouter(prefix="/unit", tags=["unit"])


def _verify_unit_signature(payload: bytes, sig_header: str, secret: str) -> bool:
    if not sig_header or not secret:
        return False
    try:
        expected = base64.b64encode(
            hmac.new(secret.encode("utf-8"), payload, hashlib.sha1).digest()
        ).decode("utf-8")
        return hmac.compare_digest(expected, sig_header)
    except Exception:
        return False


async def _handle_application_approved(event: dict, db: Session):
    relationships = event.get("relationships", {})
    application_id = relationships.get("application", {}).get("data", {}).get("id")
    if not application_id:
        return

    user = db.query(User).filter(User.unit_application_id == application_id).first()
    if not user or user.unit_account_id:
        return  # not ours, or already finished (e.g. by the sandbox-activate button)

    # Don't trust customer_id from the request body — until UNIT_WEBHOOK_SECRET
    # is configured this endpoint is unauthenticated, so a forged payload could
    # otherwise link an attacker-supplied customer/account to this user. Re-fetch
    # the application from Unit directly (our own bearer token, not request data)
    # and only proceed if Unit itself confirms it's approved.
    application = await unit_svc.get_application(application_id)
    if application.get("attributes", {}).get("status") != "approved":
        return
    customer_id = application.get("relationships", {}).get("customer", {}).get("data", {}).get("id")
    if not customer_id:
        return

    user.unit_customer_id = customer_id
    account = await unit_svc.create_deposit_account(customer_id)
    user.unit_account_id = account["id"]
    db.commit()


@router.post("/webhook")
async def unit_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig_header = request.headers.get("x-unit-signature", "")
    secret = settings.unit_webhook_secret

    if not secret and not settings.allow_unsigned_unit_webhooks:
        raise HTTPException(status_code=503, detail="Unit webhook secret is not configured")
    if secret and not _verify_unit_signature(payload, sig_header, secret):
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        body = json.loads(payload)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    events = body.get("data", [])
    if isinstance(events, dict):
        events = [events]

    processed = []
    for event in events:
        event_id = event.get("id", "")
        event_type = event.get("type", "")

        if event_id:
            existing = db.query(UnitEvent).filter(UnitEvent.id == event_id).first()
            if existing:
                processed.append({"type": event_type, "duplicate": True})
                continue
            db.add(UnitEvent(id=event_id, type=event_type))
            db.commit()

        try:
            if event_type == "application.approved":
                await _handle_application_approved(event, db)
        except Exception as e:
            # Never fail the whole webhook delivery over one event — log and move on,
            # Unit will not retry a 200, but a 5xx would retry-storm every event again.
            print(f"[Unit webhook] failed to process {event_type} ({event_id}): {e}")

        processed.append({"type": event_type, "duplicate": False})

    return {"received": True, "events": processed}
