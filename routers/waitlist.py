from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr
from typing import Optional
import os
import httpx
from slowapi import Limiter
from slowapi.util import get_remote_address

from database import get_db
from models import WaitlistEntry, EmailLog
from config import settings
from services.analytics import capture, EVENTS

WELCOME_EMAIL_NUMBER = 1  # EmailLog marker — distinguishes the immediate
                          # welcome email from the day-3+ nurture sequence (#2-#5)

router = APIRouter(prefix="/waitlist", tags=["waitlist"])
limiter = Limiter(key_func=get_remote_address)


class WaitlistJoin(BaseModel):
    email: EmailStr
    name: Optional[str] = None
    source: Optional[str] = "landing"
    referral_code: Optional[str] = None  # ?ref= param from landing page


def _send_welcome_email(email: str, position: int) -> bool:
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        print(f"[waitlist] RESEND_API_KEY not set — could not send welcome email to {email}")
        return False

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#0a0a0a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0a0a0a;padding:40px 20px;">
    <tr>
      <td align="center">
        <table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;">
          <tr>
            <td style="padding-bottom:32px;">
              <span style="font-size:28px;font-weight:800;color:#00c896;letter-spacing:-0.5px;">FAWN</span>
            </td>
          </tr>
          <tr>
            <td style="background:#111;border-radius:12px;padding:40px;">
              <h1 style="margin:0 0 12px;font-size:26px;font-weight:700;color:#ffffff;line-height:1.2;">
                You're on the list.
              </h1>
              <p style="margin:0 0 24px;font-size:16px;color:#999;line-height:1.6;">
                You're <strong style="color:#00c896;">#{position}</strong> on the FAWN waitlist — the banking app built for students who are done being charged to exist.
              </p>
              <hr style="border:none;border-top:1px solid #222;margin:0 0 24px;">
              <p style="margin:0 0 8px;font-size:15px;font-weight:600;color:#ffffff;">
                Want to lock in a founding member spot?
              </p>
              <p style="margin:0 0 24px;font-size:15px;color:#999;line-height:1.6;">
                For <strong style="color:#ffffff;">$49</strong>, get lifetime perks — no fees, early access, and your name in the app forever. Only a limited number of spots available.
              </p>
              <a href="https://goldstarorbital.github.io/fawn-landing/founding.html"
                 style="display:inline-block;background:#00c896;color:#0a0a0a;font-weight:700;font-size:15px;padding:14px 28px;border-radius:8px;text-decoration:none;">
                Become a Founding Member →
              </a>
            </td>
          </tr>
          <tr>
            <td style="padding-top:24px;text-align:center;">
              <p style="margin:0;font-size:13px;color:#444;">
                FAWN · You're receiving this because you joined our waitlist.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

    try:
        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": f"FAWN <{settings.from_email}>",
                "to": [email],
                "subject": "You're on the FAWN waitlist",
                "html": html,
            },
            timeout=10.0,
        )
        if resp.status_code not in (200, 201):
            print(f"[waitlist] welcome email to {email} failed: {resp.status_code} {resp.text[:300]}")
            return False
        return True
    except Exception as e:
        # Never crash the signup flow over an email failure — but always log it,
        # otherwise a broken sender is invisible until a real person reports it.
        print(f"[waitlist] welcome email to {email} raised: {e}")
        return False


@router.post("/join", status_code=201)
@limiter.limit("5/minute")
def join_waitlist(request: Request, req: WaitlistJoin, db: Session = Depends(get_db)):
    existing = db.query(WaitlistEntry).filter(WaitlistEntry.email == req.email).first()
    if existing:
        return {"message": "You're already on the list!", "position": _position(db, existing)}

    entry = WaitlistEntry(
        email=req.email,
        name=req.name,
        source=req.source,
        referral_code=req.referral_code,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    position = db.query(WaitlistEntry).count()
    if _send_welcome_email(req.email, position):
        db.add(EmailLog(email=req.email, email_number=WELCOME_EMAIL_NUMBER))
        db.commit()
    capture(EVENTS["WAITLIST_JOINED"], req.email, {"position": position, "source": req.source})
    return {"message": "You're on the list!", "position": position}


@router.get("/count")
def waitlist_count(db: Session = Depends(get_db)):
    return {"count": db.query(WaitlistEntry).count()}


def _position(db: Session, entry: WaitlistEntry) -> int:
    return db.query(WaitlistEntry).filter(WaitlistEntry.created_at <= entry.created_at).count()
