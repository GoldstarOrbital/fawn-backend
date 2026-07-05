"""
Lithic webhook / real-time auth receiver.

Lithic streams card events (authorization requests, settlements, disputes).
This receiver verifies the HMAC-SHA256 signature, dedupes on the event token
via LithicEvent, and acknowledges.

Real-time authorization (synchronously approving/declining a swipe) uses the
same signed channel; the decision logic is stubbed here — for now every
event is recorded and acknowledged. When ASA (Authorization Stream Access) is
enabled, the auth branch returns an approve/decline decision instead.
"""
import hashlib
import hmac
import json

from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.orm import Session

from database import get_db
from models import LithicEvent
from config import settings

router = APIRouter(prefix="/lithic", tags=["lithic"])


def _verify_signature(payload: bytes, sig_header: str, secret: str) -> bool:
    if not sig_header or not secret:
        return False
    try:
        expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        candidate = sig_header.split("=", 1)[-1].strip()
        return hmac.compare_digest(expected, candidate)
    except Exception:
        return False


@router.post("/webhook")
async def lithic_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig_header = request.headers.get("webhook-signature", "") or request.headers.get("x-lithic-signature", "")
    secret = settings.lithic_webhook_secret

    if not secret and not settings.allow_unsigned_lithic_webhooks:
        raise HTTPException(status_code=503, detail="Lithic webhook secret is not configured")
    if secret and not _verify_signature(payload, sig_header, secret):
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        body = json.loads(payload)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_id = body.get("token") or body.get("id", "")
    event_type = body.get("type") or body.get("event_type", "")

    if event_id:
        if db.query(LithicEvent).filter(LithicEvent.id == event_id).first():
            return {"received": True, "duplicate": True}
        db.add(LithicEvent(id=event_id, type=event_type))
        db.commit()

    # TODO: on authorization events, return an approve/decline decision (ASA)
    return {"received": True, "type": event_type, "duplicate": False}
