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
from routers.admin import require_admin_key
from routers.waitlist import _send_welcome_email, WELCOME_EMAIL_NUMBER

router = APIRouter(prefix="/internal", tags=["internal"])

OWNER_EMAIL = "alexmarcusgoldsmith@gmail.com"
FROM_ADDRESS = f"Alex at FAWN <{settings.from_email}>"

NURTURE_SCHEDULE = [
    {"email_number": 2, "days_after": 3,  "build_fn": build_email_2},
    {"email_number": 3, "days_after": 7,  "build_fn": build_email_3},
    {"email_number": 4, "days_after": 14, "build_fn": build_email_4},
    {"email_number": 5, "days_after": 21, "build_fn": build_email_5},
]


def _send_email(subject: str, html: str, to: str, email_number: int | None = None) -> bool:
    """Send a single email via Resend. Returns True on success."""
    if not settings.resend_api_key:
        print(f"[email_automation] email_number={email_number} to {to} skipped: no resend_api_key configured")
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
        if r.status_code not in (200, 201):
            print(f"[email_automation] email_number={email_number} to {to} failed: {r.status_code} {r.text[:300]}")
            return False
        return True
    except Exception as e:
        print(f"[email_automation] email_number={email_number} to {to} raised: {e}")
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
    # NOTE: caller commits in a batch — do not commit per-row mid-iteration.


@router.post("/process-nurture")
def process_nurture(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin_key),
):
    """
    Iterate every waitlist entry and send any nurture emails that are due
    but have not yet been sent.

    Designed to be idempotent — safe to call multiple times.
    """
    using_test_domain = "resend.dev" in settings.from_email
    now = datetime.now(timezone.utc)

    # Snapshot all entries to plain dicts up front so subsequent commits
    # can't expire/detach the ORM rows we're iterating.
    raw_entries = db.query(WaitlistEntry).all()
    entries = [
        {
            "email": e.email,
            "name": e.name,
            "created_at": (
                e.created_at.replace(tzinfo=timezone.utc)
                if e.created_at is not None and e.created_at.tzinfo is None
                else e.created_at
            ),
        }
        for e in raw_entries
        if e.created_at is not None
    ]

    sent_count = 0
    skipped_count = 0

    for entry in entries:
        created = entry["created_at"]
        email_addr = entry["email"]
        entry_name = entry["name"]

        for step in NURTURE_SCHEDULE:
            due_at = created + timedelta(days=step["days_after"])
            if now < due_at:
                continue  # not due yet

            if _already_sent(db, email_addr, step["email_number"]):
                skipped_count += 1
                continue

            name = entry_name or email_addr.split("@")[0]
            subject, html = step["build_fn"](name)

            if using_test_domain:
                subject = f"[TO: {email_addr}] {subject}"
                recipient = OWNER_EMAIL
            else:
                recipient = email_addr

            success = _send_email(subject, html, recipient, email_number=step["email_number"])
            if success:
                _log_sent(db, email_addr, step["email_number"])
                sent_count += 1

    # Single commit at the end — avoids mid-iteration session churn.
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        return {
            "status": "partial",
            "sent": sent_count,
            "already_sent_skipped": skipped_count,
            "total_entries": len(entries),
            "commit_error": str(e),
        }

    return {
        "status": "ok",
        "sent": sent_count,
        "already_sent_skipped": skipped_count,
        "total_entries": len(entries),
    }


@router.post("/resend-welcome-backfill")
def resend_welcome_backfill(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin_key),
):
    """One-time backfill: send the waitlist welcome/confirmation email to
    every existing entry that never got one, then mark it sent.

    Needed because the welcome email was silently failing for anyone who
    wasn't the Resend account owner — it was being sent from Resend's
    shared sandbox sender, which can't deliver to real third parties (now
    fixed). Real signups before that fix landed never got a confirmation.
    Idempotent via EmailLog(email_number=1) — safe to call more than once;
    already-confirmed entries are always skipped.
    """
    entries = db.query(WaitlistEntry).order_by(WaitlistEntry.created_at.asc()).all()

    sent_count = 0
    skipped_count = 0
    failed_count = 0

    for i, entry in enumerate(entries, start=1):
        already = (
            db.query(EmailLog)
            .filter(EmailLog.email == entry.email, EmailLog.email_number == WELCOME_EMAIL_NUMBER)
            .first()
        )
        if already:
            skipped_count += 1
            continue

        if _send_welcome_email(entry.email, i):
            db.add(EmailLog(email=entry.email, email_number=WELCOME_EMAIL_NUMBER))
            sent_count += 1
        else:
            failed_count += 1

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        return {
            "status": "partial",
            "sent": sent_count,
            "already_sent_skipped": skipped_count,
            "failed": failed_count,
            "total_entries": len(entries),
            "commit_error": str(e),
        }

    return {
        "status": "ok",
        "sent": sent_count,
        "already_sent_skipped": skipped_count,
        "failed": failed_count,
        "total_entries": len(entries),
    }
