"""
Column webhook receiver.

Column posts account/transfer lifecycle events (e.g. ach.credit.completed,
book.transfer.completed). This receiver verifies the HMAC-SHA256 signature,
dedupes on the event id via ColumnEvent (mirroring the Unit webhook), and
acknowledges. Business handling per event type is intentionally a stub for
now — wired up as the Column cutover progresses.
"""
import hashlib
import hmac
import json

from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.orm import Session

from database import get_db
from models import ColumnEvent
from config import settings

router = APIRouter(prefix="/column", tags=["column"])


def _verify_signature(payload: bytes, sig_header: str, secret: str) -> bool:
    if not sig_header or not secret:
        return False
    try:
        expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        # Column may send the digest bare or prefixed (e.g. "sha256=...").
        candidate = sig_header.split("=", 1)[-1].strip()
        return hmac.compare_digest(expected, candidate)
    except Exception:
        return False


@router.post("/webhook")
async def column_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig_header = request.headers.get("column-signature", "") or request.headers.get("x-column-signature", "")
    secret = settings.column_webhook_secret

    if not secret and not settings.allow_unsigned_column_webhooks:
        raise HTTPException(status_code=503, detail="Column webhook secret is not configured")
    if secret and not _verify_signature(payload, sig_header, secret):
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        body = json.loads(payload)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_id = body.get("id") or body.get("event_id", "")
    event_type = body.get("type") or body.get("event_type", "")

    if event_id:
        if db.query(ColumnEvent).filter(ColumnEvent.id == event_id).first():
            return {"received": True, "duplicate": True}
        db.add(ColumnEvent(id=event_id, type=event_type))
        db.commit()

    # TODO: dispatch on event_type as banking cutover progresses
    #   e.g. ach.credit.completed -> mark FundingRequest completed
    return {"received": True, "type": event_type, "duplicate": False}
