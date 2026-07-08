"""
routers/member.py

Magic-link authentication + dashboard for FAWN founding members.

Flow:
  POST /member/request-link   { email }  → sends a one-time link to the email
  GET  /member/verify?token=  → validates token, returns a signed JWT
  GET  /member/me             → JWT-protected, returns member profile + perks

The JWT is the same HS256 scheme used by the auth router (settings.jwt_secret).
"""

import os
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import httpx
from jose import jwt as pyjwt, JWTError, ExpiredSignatureError
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from database import get_db
from models import FoundingMember, MagicLinkToken
from config import settings
from services.analytics import capture, EVENTS
from rate_limiting import limiter

router = APIRouter(prefix="/member", tags=["member"])

FAWN_FROM = f"FAWN <{settings.from_email}>"
DASHBOARD_BASE = "https://goldstarorbital.github.io/fawn-landing/member.html"
LINK_EXPIRY_MINUTES = 15
JWT_EXPIRY_HOURS = 72


# ── helpers ────────────────────────────────────────────────────────────────────

def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _make_jwt(email: str, member_number: int) -> str:
    payload = {
        "sub": email,
        "member_number": member_number,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS),
    }
    return pyjwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def _decode_jwt(token: str) -> dict:
    try:
        return pyjwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired — request a new link")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")


def _send_magic_link(email: str, raw_token: str, member_number: int):
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        return False
    link = f"{DASHBOARD_BASE}?token={raw_token}"
    label_line = f"Founding Member #{member_number}"
    html = f"""
    <div style="font-family:-apple-system,sans-serif;max-width:480px;padding:32px;background:#0a0a0a;color:#f0f0f0;border-radius:16px;">
      <h2 style="color:#00c896;margin:0 0 8px;">Sign in to your FAWN dashboard</h2>
      <p style="color:#888;margin:0 0 24px;font-size:0.9rem;">{label_line} · Link expires in {LINK_EXPIRY_MINUTES} minutes</p>
      <a href="{link}" style="display:inline-block;background:#00c896;color:#000;font-weight:700;text-decoration:none;padding:14px 28px;border-radius:8px;font-size:0.95rem;">
        Open my dashboard →
      </a>
      <p style="margin-top:24px;font-size:0.75rem;color:#444;">
        If you didn't request this, ignore this email. Link works once.
      </p>
    </div>
    """
    try:
        r = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"from": FAWN_FROM, "to": [email], "subject": "Your FAWN dashboard link", "html": html},
            timeout=8,
        )
        if r.status_code not in (200, 201):
            print(f"[member] magic link email to {email} failed: {r.status_code} {r.text[:300]}")
            return False
        return True
    except Exception as e:
        print(f"[member] magic link email to {email} raised: {e}")
        return False


# ── schemas ────────────────────────────────────────────────────────────────────

class MagicLinkRequest(BaseModel):
    email: EmailStr


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.post("/request-link")
@limiter.limit("5/minute")
def request_magic_link(request: Request, body: MagicLinkRequest, db: Session = Depends(get_db)):
    """Send a one-time magic link to a founding member's email."""
    member = (
        db.query(FoundingMember)
        .filter(FoundingMember.email == body.email.lower(), FoundingMember.refunded == False)
        .order_by(FoundingMember.member_number)
        .first()
    )
    if not member:
        # Return the same response to prevent email enumeration
        return {"sent": True, "note": "If that email is a founding member, a link is on its way."}

    raw_token = secrets.token_urlsafe(32)
    token_record = MagicLinkToken(
        email=member.email,
        token_hash=_token_hash(raw_token),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=LINK_EXPIRY_MINUTES),
    )
    db.add(token_record)
    db.commit()

    _send_magic_link(member.email, raw_token, member.member_number)
    capture(EVENTS["MAGIC_LINK_REQUESTED"], member.email, {"member_number": member.member_number})
    return {"sent": True}


@router.get("/verify")
def verify_magic_link(token: str = Query(...), db: Session = Depends(get_db)):
    """Exchange a magic link token for a JWT session token."""
    h = _token_hash(token)
    record = db.query(MagicLinkToken).filter(
        MagicLinkToken.token_hash == h,
        MagicLinkToken.used == False,
    ).first()

    if not record:
        raise HTTPException(status_code=401, detail="Invalid or already-used link")

    now = datetime.now(timezone.utc)
    expires = record.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if now > expires:
        raise HTTPException(status_code=401, detail="Link expired — request a new one")

    # Mark used
    record.used = True
    db.commit()

    member = (
        db.query(FoundingMember)
        .filter(FoundingMember.email == record.email, FoundingMember.refunded == False)
        .order_by(FoundingMember.member_number)
        .first()
    )
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")

    jwt_token = _make_jwt(member.email, member.member_number)
    capture(EVENTS["MAGIC_LINK_CONSUMED"], member.email, {"member_number": member.member_number})
    return {"token": jwt_token, "member_number": member.member_number, "email": member.email}


@router.get("/me")
def get_member_profile(
    authorization: str = Query(default=None, alias="token"),
    db: Session = Depends(get_db),
):
    """Return the authenticated member's profile. Pass ?token=<jwt>."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Token required")

    claims = _decode_jwt(authorization)
    email = claims.get("sub", "")

    member = (
        db.query(FoundingMember)
        .filter(FoundingMember.email == email, FoundingMember.refunded == False)
        .order_by(FoundingMember.member_number)
        .first()
    )
    if not member:
        raise HTTPException(status_code=404, detail="Member not found")

    tier_label, price = {
        "founding": ("Founding Member", "$49"),
        "inner_circle": ("Inner Circle", "$97"),
        "dev_sprint": ("Dev Sprint", "$300"),
    }.get(member.tier, ("Member", ""))

    # Perks by tier
    perks = {
        "founding": [
            "Locked-in $0/month pricing forever",
            "Feature voting rights",
            "Priority onboarding when we launch",
            "Direct line to Alex (founder)",
        ],
        "inner_circle": [
            "Everything in Founding",
            "Early beta access",
            "Monthly 1:1 product call with Alex",
            "Input on roadmap priorities",
        ],
        "dev_sprint": [
            "Everything in Inner Circle",
            "Dedicated dev sprint on your feature request",
            "Your name in the product credits",
        ],
    }.get(member.tier, [])

    return {
        "member_number": member.member_number,
        "email": member.email,
        "tier": member.tier,
        "tier_label": tier_label,
        "price": price,
        "joined_at": member.joined_at.isoformat() if member.joined_at else None,
        "perks": perks,
        "refund_eligible": True,  # 30-day policy — front-end checks date
    }
