"""
routers/stripe_webhook.py

Stripe webhook endpoint. On checkout.session.completed:
  1. Writes a FoundingMember row (idempotent via StripeEvent dedup)
  2. Sends Alex a sale notification (Resend email + optional Pushover push)
  3. Sends the customer a welcome email with their member number

Signature verified via STRIPE_WEBHOOK_SECRET.
Register in Stripe → Developers → Webhooks:
  URL: https://web-production-13d5b.up.railway.app/stripe/webhook
  Events: checkout.session.completed, payment_intent.succeeded
"""

import os
import hmac
import hashlib
import time
import json
from typing import Optional

import httpx
from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.orm import Session

from database import get_db
from models import FoundingMember, StripeEvent
from config import settings
from services.analytics import capture, EVENTS

router = APIRouter(prefix="/stripe", tags=["stripe"])

ALEX_EMAIL = "alexmarcusgoldsmith@gmail.com"
FAWN_FROM = f"FAWN <{settings.from_email}>"

AMOUNT_TO_TIER = {
    4900: "founding",
    9700: "inner_circle",
    30000: "dev_sprint",
}
TIER_LABELS = {
    "founding": ("Founding Member", "$49"),
    "inner_circle": ("Inner Circle", "$97"),
    "dev_sprint": ("Dev Sprint", "$300"),
}


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
        expected = hmac.new(
            secret.encode("utf-8"), signed, hashlib.sha256
        ).hexdigest()
        if abs(int(time.time()) - int(timestamp)) > 300:
            return False
        return hmac.compare_digest(expected, provided)
    except Exception:
        return False


def _resend(to: str, subject: str, html: str) -> bool:
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        print(f"[stripe_webhook] RESEND_API_KEY not set — could not send '{subject}' to {to}")
        return False
    try:
        r = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"from": FAWN_FROM, "to": [to], "subject": subject, "html": html},
            timeout=8,
        )
        if r.status_code not in (200, 201):
            print(f"[stripe_webhook] email '{subject}' to {to} failed: {r.status_code} {r.text[:300]}")
            return False
        return True
    except Exception as e:
        print(f"[stripe_webhook] email '{subject}' to {to} raised: {e}")
        return False


def _pushover(title: str, message: str, url: Optional[str] = None) -> bool:
    token = os.environ.get("PUSHOVER_APP_TOKEN", "")
    user = os.environ.get("PUSHOVER_USER_KEY", "")
    if not token or not user:
        return False
    try:
        payload = {"token": token, "user": user, "title": title, "message": message,
                   "priority": 1, "sound": "cashregister"}
        if url:
            payload["url"] = url
            payload["url_title"] = "View in Stripe"
        r = httpx.post("https://api.pushover.net/1/messages.json", data=payload, timeout=6)
        return r.status_code == 200
    except Exception:
        return False


def _next_member_number(db: Session) -> int:
    result = db.query(FoundingMember).count()
    return result + 1


def _notify_alex(tier: str, amount: int, customer_email: str, member_number: int):
    label, price = TIER_LABELS.get(tier, ("Unknown", f"${amount/100:.2f}"))
    subject = f"FAWN SALE: {label} #{member_number} — {price}"
    html = f"""
    <div style="font-family:-apple-system,sans-serif;max-width:480px;padding:24px;background:#0a0a0a;color:#f0f0f0;border-radius:16px;">
      <h1 style="color:#00c896;margin:0 0 4px;">+{price} 💰</h1>
      <p style="color:#888;margin:0 0 20px;">{label} #{member_number} just joined FAWN.</p>
      <table style="width:100%;border-collapse:collapse;font-size:14px;">
        <tr><td style="padding:6px 0;color:#666;">Tier</td><td style="padding:6px 0;font-weight:600;">{label}</td></tr>
        <tr><td style="padding:6px 0;color:#666;">Member #</td><td style="padding:6px 0;font-weight:600;">#{member_number}</td></tr>
        <tr><td style="padding:6px 0;color:#666;">Amount</td><td style="padding:6px 0;font-weight:600;">{price}</td></tr>
        <tr><td style="padding:6px 0;color:#666;">Customer</td><td style="padding:6px 0;font-weight:600;">{customer_email}</td></tr>
      </table>
      <p style="margin-top:20px;font-size:12px;color:#555;">
        <a href="https://dashboard.stripe.com/payments" style="color:#00c896;">View in Stripe →</a>
      </p>
    </div>
    """
    _resend(ALEX_EMAIL, subject, html)
    _pushover(f"FAWN: +{price}", f"{label} #{member_number} from {customer_email}",
              "https://dashboard.stripe.com/payments")


def _welcome_customer(email: str, tier: str, member_number: int):
    label, price = TIER_LABELS.get(tier, ("Member", ""))
    dashboard_url = f"https://goldstarorbital.github.io/fawn-landing/member.html?email={email}"
    html = f"""
    <div style="font-family:-apple-system,sans-serif;max-width:520px;padding:32px;background:#0a0a0a;color:#f0f0f0;border-radius:16px;">
      <h1 style="color:#00c896;font-size:2rem;margin:0 0 4px;">Welcome to FAWN</h1>
      <p style="color:#888;margin:0 0 24px;font-size:0.95rem;">You're Founding Member #{member_number}. We're building banking that doesn't suck — and you helped make it real.</p>

      <div style="background:#111;border:1px solid #1e1e1e;border-radius:12px;padding:20px;margin-bottom:24px;">
        <div style="font-size:0.7rem;color:#555;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px;">Your membership</div>
        <div style="font-size:1.6rem;font-weight:800;color:#f0f0f0;">#{member_number}</div>
        <div style="font-size:0.85rem;color:#00c896;margin-top:4px;">{label}</div>
      </div>

      <p style="font-size:0.9rem;color:#aaa;margin-bottom:20px;">
        What happens next:
        <br>• You'll get updates as we build — real ones, not marketing fluff.
        <br>• When FAWN launches, you're first in with perks locked in.
        <br>• Your member dashboard tracks your status and referral code.
      </p>

      <a href="{dashboard_url}" style="display:inline-block;background:#00c896;color:#000;font-weight:700;text-decoration:none;padding:12px 24px;border-radius:8px;font-size:0.9rem;">
        View your member dashboard →
      </a>

      <p style="margin-top:28px;font-size:0.75rem;color:#444;">
        Questions? Reply to this email or reach out to Alex directly.<br>
        30-day full refund, no questions asked — see <a href="https://goldstarorbital.github.io/fawn-landing/founding-terms.html" style="color:#666;">terms</a>.
      </p>
    </div>
    """
    _resend(email, f"Welcome to FAWN, Founding Member #{member_number}", html)


@router.post("/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    secret = settings.stripe_webhook_secret or os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    if not secret and not settings.allow_unsigned_stripe_webhooks:
        raise HTTPException(status_code=500, detail="Stripe webhook secret is not configured")
    if secret and not _verify_stripe_signature(payload, sig_header, secret):
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        event = json.loads(payload)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_id = event.get("id", "")
    event_type = event.get("type", "")

    # Idempotency check — skip duplicate deliveries
    if event_id:
        existing = db.query(StripeEvent).filter(StripeEvent.id == event_id).first()
        if existing:
            return {"received": True, "duplicate": True, "type": event_type}
        db.add(StripeEvent(id=event_id, type=event_type))
        db.commit()

    obj = event.get("data", {}).get("object", {})

    if event_type == "checkout.session.completed":
        amount = obj.get("amount_total", 0)
        customer_email = obj.get("customer_details", {}).get("email", "")
        stripe_customer_id = obj.get("customer", "")
        session_id = obj.get("id", "")
        tier = AMOUNT_TO_TIER.get(amount)

        if not tier or not customer_email:
            return {"received": True, "type": event_type, "skipped": "unknown tier or no email"}

        # Deduplicate by session_id
        existing_member = db.query(FoundingMember).filter(
            FoundingMember.stripe_session_id == session_id
        ).first()
        if existing_member:
            return {"received": True, "duplicate_session": True}

        member_number = _next_member_number(db)
        member = FoundingMember(
            email=customer_email,
            member_number=member_number,
            tier=tier,
            amount_cents=amount,
            stripe_customer_id=stripe_customer_id or None,
            stripe_session_id=session_id or None,
        )
        db.add(member)
        db.commit()

        _notify_alex(tier, amount, customer_email, member_number)
        _welcome_customer(customer_email, tier, member_number)
        capture(EVENTS["FOUNDING_PAYMENT_SUCCEEDED"], customer_email,
                {"tier": tier, "amount_cents": amount, "member_number": member_number})

        label, price = TIER_LABELS.get(tier, ("Unknown", ""))
        return {
            "received": True,
            "type": event_type,
            "tier": tier,
            "member_number": member_number,
            "email": customer_email,
        }

    elif event_type == "payment_intent.succeeded":
        # Secondary event — just acknowledge, checkout.session.completed is the primary
        return {"received": True, "type": event_type}

    return {"received": True, "type": event_type}


@router.get("/recent-sales")
def recent_sales():
    """Public-safe recent sales list from Stripe."""
    stripe_key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not stripe_key:
        return {"sales": [], "stripe_connected": False}
    try:
        r = httpx.get(
            "https://api.stripe.com/v1/payment_intents",
            params={"limit": 10},
            headers={"Authorization": f"Bearer {stripe_key}"},
            timeout=6,
        )
        if r.status_code != 200:
            return {"sales": [], "stripe_connected": True}
        intents = r.json().get("data", [])
        sales = []
        for pi in intents:
            if pi.get("status") != "succeeded":
                continue
            amount = pi.get("amount", 0)
            tier = AMOUNT_TO_TIER.get(amount)
            if not tier:
                continue
            label, price = TIER_LABELS.get(tier, ("Unknown", ""))
            sales.append({"tier": label, "amount_label": price, "created": pi.get("created")})
        return {"sales": sales, "stripe_connected": True}
    except Exception as e:
        return {"sales": [], "stripe_connected": True, "error": str(e)}
