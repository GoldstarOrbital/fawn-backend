"""Tests for /internal/resend-welcome-backfill — the one-time idempotent
backfill for waitlist entries that never got a welcome email because of
the onboarding@resend.dev sandbox-sender bug."""
import uuid

from database import SessionLocal
from models import WaitlistEntry, EmailLog


def _add_waitlist_entry(email):
    db = SessionLocal()
    try:
        db.add(WaitlistEntry(email=email, source="test"))
        db.commit()
    finally:
        db.close()


def _has_welcome_log(email):
    db = SessionLocal()
    try:
        return db.query(EmailLog).filter(EmailLog.email == email, EmailLog.email_number == 1).first() is not None
    finally:
        db.close()


def test_backfill_requires_admin_key(client):
    resp = client.post("/internal/resend-welcome-backfill")
    assert resp.status_code == 403


def test_backfill_sends_to_unconfirmed_entries(client, admin_key, monkeypatch):
    email = f"backfill_{uuid.uuid4().hex[:8]}@example.com"
    _add_waitlist_entry(email)

    monkeypatch.setattr("routers.email_automation._send_welcome_email", lambda e, pos: True)

    resp = client.post("/internal/resend-welcome-backfill", headers={"X-Admin-Key": admin_key})
    assert resp.status_code == 200
    body = resp.json()
    assert body["sent"] >= 1
    assert _has_welcome_log(email)


def test_backfill_is_idempotent_skips_already_confirmed(client, admin_key, monkeypatch):
    email = f"already_{uuid.uuid4().hex[:8]}@example.com"
    _add_waitlist_entry(email)

    call_count = {"n": 0}

    def fake_send(e, pos):
        call_count["n"] += 1
        return True
    monkeypatch.setattr("routers.email_automation._send_welcome_email", fake_send)

    first = client.post("/internal/resend-welcome-backfill", headers={"X-Admin-Key": admin_key}).json()
    second = client.post("/internal/resend-welcome-backfill", headers={"X-Admin-Key": admin_key}).json()

    assert _has_welcome_log(email)
    # The second run should skip everyone the first run already confirmed —
    # sent count on the second run should not include our test email again.
    assert second["already_sent_skipped"] >= first["sent"]


def test_backfill_counts_failures_without_crashing(client, admin_key, monkeypatch):
    email = f"failcase_{uuid.uuid4().hex[:8]}@example.com"
    _add_waitlist_entry(email)

    monkeypatch.setattr("routers.email_automation._send_welcome_email", lambda e, pos: False)

    resp = client.post("/internal/resend-welcome-backfill", headers={"X-Admin-Key": admin_key})
    assert resp.status_code == 200
    assert resp.json()["failed"] >= 1
    assert not _has_welcome_log(email)
