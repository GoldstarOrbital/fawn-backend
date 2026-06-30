import hashlib
import hmac
import json
import time
import uuid

import routers.stripe_webhook as stripe_webhook_router
from database import SessionLocal
from models import FoundingMember


def _checkout_event(email=None):
    unique = uuid.uuid4().hex
    return {
        "id": f"evt_{unique}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": f"cs_{unique}",
                "amount_total": 4900,
                "customer": f"cus_{unique}",
                "customer_details": {"email": email or f"buyer_{unique}@example.com"},
            }
        },
    }


def _stripe_signature(payload: bytes, secret: str) -> str:
    timestamp = str(int(time.time()))
    signed = f"{timestamp}.{payload.decode('utf-8')}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def test_stripe_webhook_fails_closed_without_secret(client, monkeypatch):
    monkeypatch.setattr(stripe_webhook_router.settings, "stripe_webhook_secret", "")
    monkeypatch.setattr(stripe_webhook_router.settings, "allow_unsigned_stripe_webhooks", False)
    payload = _checkout_event()

    resp = client.post("/stripe/webhook", json=payload)

    assert resp.status_code == 500
    assert "secret is not configured" in resp.json()["detail"]


def test_stripe_webhook_accepts_valid_signature_and_creates_member(client, monkeypatch):
    secret = "whsec_test_secret"
    event = _checkout_event()
    payload = json.dumps(event, separators=(",", ":")).encode("utf-8")
    monkeypatch.setattr(stripe_webhook_router.settings, "stripe_webhook_secret", secret)
    monkeypatch.setattr(stripe_webhook_router.settings, "allow_unsigned_stripe_webhooks", False)
    monkeypatch.setattr(stripe_webhook_router, "_notify_alex", lambda *args, **kwargs: None)
    monkeypatch.setattr(stripe_webhook_router, "_welcome_customer", lambda *args, **kwargs: None)
    monkeypatch.setattr(stripe_webhook_router, "capture", lambda *args, **kwargs: None)

    resp = client.post(
        "/stripe/webhook",
        content=payload,
        headers={
            "content-type": "application/json",
            "stripe-signature": _stripe_signature(payload, secret),
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["tier"] == "founding"

    db = SessionLocal()
    try:
        member = db.query(FoundingMember).filter(FoundingMember.stripe_session_id == event["data"]["object"]["id"]).first()
        assert member is not None
        assert member.email == event["data"]["object"]["customer_details"]["email"]
    finally:
        db.close()
