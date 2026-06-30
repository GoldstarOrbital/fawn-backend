"""
Unit webhook receiver.

On application.approved, finishes account setup by creating the Unit deposit
account. Direct sandbox KYC users are matched by unit_application_id. Hosted
Unit application-form users are matched by the fawnUserId tag FAWN sends when
creating the form.
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

    application = await unit_svc.get_application(application_id)
    if application.get("attributes", {}).get("status") != "approved":
        return

    user = db.query(User).filter(User.unit_application_id == application_id).first()
    if not user:
        tags = application.get("attributes", {}).get("tags", {}) or {}
        fawn_user_id = tags.get("fawnUserId")
        if fawn_user_id:
            user = db.query(User).filter(User.id == fawn_user_id).first()
            if user and not user.unit_application_id:
                user.unit_application_id = application_id
    if not user or user.unit_account_id:
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
            print(f"[Unit webhook] failed to process {event_type} ({event_id}): {e}")

        processed.append({"type": event_type, "duplicate": False})

    return {"received": True, "events": processed}
