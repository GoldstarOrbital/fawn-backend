import uuid

from models import EmailLog, WaitlistEntry
from routers.waitlist import _unsubscribe_token


def _email():
    return f"agentic-{uuid.uuid4().hex}@example.com"


def test_waitlist_join_requires_explicit_marketing_opt_in(client, db):
    email = _email()
    response = client.post("/waitlist/join", json={"email": email, "source": "agentic"})

    assert response.status_code == 201
    entry = db.query(WaitlistEntry).filter(WaitlistEntry.email == email).one()
    assert entry.marketing_opt_in is False
    assert entry.consent_at is None
    assert db.query(EmailLog).filter(EmailLog.email == email).count() == 0


def test_opt_in_can_be_revoked_with_signed_unsubscribe_token(client, db):
    email = _email()
    response = client.post(
        "/waitlist/join",
        json={"email": email, "source": "agentic", "marketing_opt_in": True},
    )
    assert response.status_code == 201

    entry = db.query(WaitlistEntry).filter(WaitlistEntry.email == email).one()
    assert entry.marketing_opt_in is True
    assert entry.consent_at is not None

    unsubscribe = client.get(f"/waitlist/unsubscribe?token={_unsubscribe_token(email)}")
    assert unsubscribe.status_code == 200

    db.expire_all()
    entry = db.query(WaitlistEntry).filter(WaitlistEntry.email == email).one()
    assert entry.marketing_opt_in is False
    assert entry.unsubscribed_at is not None
