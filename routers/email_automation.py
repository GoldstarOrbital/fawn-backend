"""
routers/email_automation.py

Internal endpoint called by a cron job to process the FAWN nurture sequence.
Cron should POST /internal/process-nurture on a schedule (e.g. every hour or daily).

Nurture schedule (days after WaitlistEntry.created_at):
  Email #2 —  3 days  — Overdraft fee explainer
  Email #3 —  7 days  — How FAWN makes money (transparency)
  Email #4 — 14 days  — Referral push with social proof
  Email #5 — 21 days  — Beta access teaser
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import and_
from datetime import datetime, timezone, timedelta
import httpx

from database import get_db
from models import WaitlistEntry, EmailLog
from config import settings
from email_templates import (
    build_email_2,
    build_email_3,
    build_email_4,
    build_email_5,
)

router = APIRouter(prefix="/internal", tags=["internal"])

OWNER_EMAIL = "alexmarcusgoldsmith@gmail.com"
FROM_ADDRESS = f"Alex at FAWN <{settings.from_email}>"

NURTURE_SCHEDULE = [
    {"email_number": 2, "days_after": 3,  "build_fn": build_email_2},
    {"email_number": 3, "days_after": 7,  "build_fn": build_email_3},
    {"email_number": 4, "days_after": 14, "build_fn": build_email_4},
    {"email_number": 5, "days_after": 21, "build_fn": build_email_5},
]


def _send_email(subject: str, html: str, to: str) -> bool:
    """Send a single email via Resend. Returns True on success."""
    if not settings.resend_api_key:
        return False
    try:
        r = httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {settings.resend_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": FROM_ADDRESS,
                "to": [to],
                "subject": subject,
                "html": html,
            },
            timeout=10,
        )
        return r.status_code in (200, 201)
    except Exception:
        return False


def _already_sent(db: Session, email: str, email_number: int) -> bool:
    return (
        db.query(EmailLog)
        .filter(and_(EmailLog.email == email, EmailLog.email_number == email_number))
        .first()
        is not None
    )


def _log_sent(db: Session, email: str, email_number: int):
    log = EmailLog(email=email, email_number=email_number)
    db.add(log)
    db.commit()


@router.post("/process-nurture")
def process_nurture(db: Session = Depends(get_db)):
    """
    Iterate every waitlist entry and send any nurture emails that are due
    but have not yet been sent.

    Designed to be idempotent — safe to call multiple times.
    """
    using_test_domain = "resend.dev" in settings.from_email
    now = datetime.now(timezone.utc)

    entries = db.query(WaitlistEntry).all()
    sent_count = 0
    skipped_count = 0

    for entry in entries:
        # Make created_at timezone-aware if stored naive
        created = entry.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)

        for step in NURTURE_SCHEDULE:
            due_at = created + timedelta(days=step["days_after"])
            if now < due_at:
                continue  # not due yet

            if _already_sent(db, entry.email, step["email_number"]):
                skipped_count += 1
                continue

            name = entry.name or entry.email.split("@")[0]
            subject, html = step["build_fn"](name)

            if using_test_domain:
                subject = f"[TO: {entry.email}] {subject}"
                recipient = OWNER_EMAIL
            else:
                recipient = entry.email

            success = _send_email(subject, html, recipient)
            if success:
                _log_sent(db, entry.email, step["email_number"])
                sent_count += 1

    return {
        "status": "ok",
        "sent": sent_count,
        "already_sent_skipped": skipped_count,
        "total_entries": len(entries),
    }
