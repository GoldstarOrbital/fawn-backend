"""Tests for /p2p — handles, send/confirm, request/pay, split, limits,
step-up, idempotency, disputes.

Users are created directly via the ORM rather than through the
rate-limited /auth/register endpoint (registration itself is already
covered by test_auth.py, and this file registers far more than 5 users
per run, which would trip the "5/minute" limiter). UNIT_API_TOKEN is
unset in tests, so confirm logic monkeypatches
services.unit.create_book_payment to avoid any real network call.
"""
import uuid

import pytest
from jose import jwt

from database import SessionLocal
from models import User
from config import settings


def _register(client, email, full_name="Test Student"):
    """Create a user directly (bypasses the rate-limited HTTP endpoint)
    and return a JWT for them, signed the same way auth.py does."""
    from routers.auth import _hash

    db = SessionLocal()
    try:
        user = User(
            email=email.lower(),
            hashed_password=_hash("supersecret1"),
            full_name=full_name,
            phone="5551234567",
            is_student=True,
            school="berkeley",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        user_id = user.id
    finally:
        db.close()

    from datetime import datetime, timedelta
    expire = datetime.utcnow() + timedelta(minutes=settings.jwt_expire_minutes)
    token = jwt.encode({"sub": user_id, "exp": expire}, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token


def _activate_account(email, unit_account_id=None):
    """Simulate an approved Unit deposit account, bypassing real KYC."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email.lower()).first()
        user.unit_account_id = unit_account_id or f"acc_{uuid.uuid4().hex[:10]}"
        db.commit()
        return user.id
    finally:
        db.close()


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _claim_handle(client, token, handle):
    """Goes through the real (rate-limited) endpoint — only use this in
    tests that are specifically exercising the claim-handle behavior."""
    resp = client.post("/p2p/handles", json={"handle": handle}, headers=_auth(token))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _set_handle_direct(email, handle):
    """Fixture/setup helper — writes the Handle row directly so fixture
    setup doesn't burn the claim-handle endpoint's rate limit budget."""
    from models import Handle

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email.lower()).first()
        db.add(Handle(user_id=user.id, handle=handle))
        db.commit()
    finally:
        db.close()


@pytest.fixture()
def two_active_users(client):
    """Two registered, account-active users, each with a claimed handle.

    Handles must be globally unique, and this fixture is function-scoped
    (a fresh pair per test) against a session-scoped DB — so handles need
    a per-invocation suffix too, not just the emails.
    """
    suffix = uuid.uuid4().hex[:8]
    sender_email = f"sender_{suffix}@example.com"
    recipient_email = f"recipient_{suffix}@example.com"
    sender_handle = f"sender_{suffix}"
    recipient_handle = f"recip_{suffix}"

    sender_token = _register(client, sender_email, "Sender One")
    recipient_token = _register(client, recipient_email, "Recipient Two")

    _activate_account(sender_email)
    _activate_account(recipient_email)

    _set_handle_direct(sender_email, sender_handle)
    _set_handle_direct(recipient_email, recipient_handle)

    return {
        "sender_token": sender_token,
        "recipient_token": recipient_token,
        "sender_email": sender_email,
        "recipient_email": recipient_email,
        "sender_handle": sender_handle,
        "recipient_handle": recipient_handle,
    }


def _mock_book_payment(monkeypatch, payment_id="pmt_test123"):
    async def fake_create_book_payment(*args, **kwargs):
        return {"id": payment_id, "type": "bookPayment"}
    monkeypatch.setattr("routers.p2p.unit_svc.create_book_payment", fake_create_book_payment)


# --- Handles ---

def test_claim_handle_then_lookup(client, two_active_users):
    recipient_handle = two_active_users["recipient_handle"]
    resp = client.get(
        "/p2p/handles/lookup",
        params={"handle": f"@{recipient_handle}"},
        headers=_auth(two_active_users["sender_token"]),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["claimable"] is False
    assert body["handle"] == recipient_handle
    assert body["display_name"] == "Recipient T."  # first name + last initial, never full identity


def test_claiming_taken_handle_fails_409(client, two_active_users):
    resp = client.post(
        "/p2p/handles", json={"handle": two_active_users["recipient_handle"]},
        headers=_auth(two_active_users["sender_token"]),
    )
    assert resp.status_code == 409


def test_invalid_handle_format_rejected_422(client, two_active_users):
    resp = client.post(
        "/p2p/handles", json={"handle": "a"},  # too short
        headers=_auth(two_active_users["sender_token"]),
    )
    assert resp.status_code == 422


# --- Send happy path: first-ever send always requires step-up ---

def test_first_send_requires_step_up_then_confirm_completes(client, two_active_users, monkeypatch):
    _mock_book_payment(monkeypatch)
    token = two_active_users["sender_token"]
    recipient_handle = two_active_users["recipient_handle"]
    key = f"send-{uuid.uuid4()}"

    create = client.post(
        "/p2p/transfers",
        json={"to_handle": f"@{recipient_handle}", "amount_cents": 500, "note": "pizza", "idempotency_key": key},
        headers=_auth(token),
    )
    assert create.status_code == 201, create.text
    body = create.json()
    assert body["status"] == "requires_step_up"
    assert body["step_up_required"] is True
    transfer_id = body["id"]

    # Confirming without acknowledging the step-up is rejected
    blocked = client.post(
        f"/p2p/transfers/{transfer_id}/confirm",
        json={"step_up_acknowledged": False},
        headers=_auth(token),
    )
    assert blocked.status_code == 428

    confirmed = client.post(
        f"/p2p/transfers/{transfer_id}/confirm",
        json={"step_up_acknowledged": True},
        headers=_auth(token),
    )
    assert confirmed.status_code == 200, confirmed.text
    out = confirmed.json()
    assert out["status"] == "completed"
    assert out["direction"] == "sent"
    assert out["counterparty_handle"] == recipient_handle


def test_send_to_self_rejected_400(client, two_active_users):
    resp = client.post(
        "/p2p/transfers",
        json={"to_handle": f"@{two_active_users['sender_handle']}", "amount_cents": 100, "idempotency_key": str(uuid.uuid4())},
        headers=_auth(two_active_users["sender_token"]),
    )
    assert resp.status_code == 400


def test_send_to_unknown_handle_404(client, two_active_users):
    resp = client.post(
        "/p2p/transfers",
        json={"to_handle": "@nobodyhere", "amount_cents": 100, "idempotency_key": str(uuid.uuid4())},
        headers=_auth(two_active_users["sender_token"]),
    )
    assert resp.status_code == 404


# --- Idempotency ---

def test_duplicate_idempotency_key_returns_same_transfer_not_a_new_one(client, two_active_users):
    token = two_active_users["sender_token"]
    key = f"idem-{uuid.uuid4()}"
    payload = {"to_handle": f"@{two_active_users['recipient_handle']}", "amount_cents": 250, "idempotency_key": key}

    first = client.post("/p2p/transfers", json=payload, headers=_auth(token))
    second = client.post("/p2p/transfers", json=payload, headers=_auth(token))

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]

    feed = client.get("/p2p/transfers", headers=_auth(token)).json()["transfers"]
    matching = [t for t in feed if t["id"] == first.json()["id"]]
    assert len(matching) == 1  # never double-created


def test_confirming_twice_is_idempotent(client, two_active_users, monkeypatch):
    _mock_book_payment(monkeypatch, payment_id="pmt_double_confirm")
    token = two_active_users["sender_token"]
    key = f"confirm-twice-{uuid.uuid4()}"

    create = client.post(
        "/p2p/transfers",
        json={"to_handle": f"@{two_active_users['recipient_handle']}", "amount_cents": 300, "idempotency_key": key},
        headers=_auth(token),
    )
    transfer_id = create.json()["id"]
    client.post(f"/p2p/transfers/{transfer_id}/confirm", json={"step_up_acknowledged": True}, headers=_auth(token))

    second_confirm = client.post(
        f"/p2p/transfers/{transfer_id}/confirm", json={"step_up_acknowledged": True}, headers=_auth(token)
    )
    assert second_confirm.status_code == 200
    assert second_confirm.json()["status"] == "completed"


# --- Limits ---

def test_per_transaction_limit_enforced(client, two_active_users):
    resp = client.post(
        "/p2p/transfers",
        json={"to_handle": f"@{two_active_users['recipient_handle']}", "amount_cents": 999_999, "idempotency_key": str(uuid.uuid4())},
        headers=_auth(two_active_users["sender_token"]),
    )
    assert resp.status_code == 400
    assert "capped" in resp.json()["detail"]


# --- Request + pay ---

def test_request_then_pay_flow(client, two_active_users, monkeypatch):
    _mock_book_payment(monkeypatch, payment_id="pmt_request_pay")
    requester_token = two_active_users["recipient_token"]  # recipient requests from sender
    payer_token = two_active_users["sender_token"]
    sender_handle = two_active_users["sender_handle"]

    req = client.post(
        "/p2p/requests",
        json={"from_handle": f"@{sender_handle}", "amount_cents": 400, "note": "rent split", "idempotency_key": str(uuid.uuid4())},
        headers=_auth(requester_token),
    )
    assert req.status_code == 201, req.text
    request_body = req.json()
    assert request_body["status"] == "requested"
    assert request_body["direction"] == "request_outgoing"
    request_id = request_body["id"]

    pay = client.post(f"/p2p/requests/{request_id}/pay", headers=_auth(payer_token))
    assert pay.status_code == 201, pay.text
    linked = pay.json()
    assert linked["status"] == "requires_step_up"  # payer's first-ever send
    linked_id = linked["id"]

    confirm = client.post(
        f"/p2p/transfers/{linked_id}/confirm", json={"step_up_acknowledged": True}, headers=_auth(payer_token)
    )
    assert confirm.status_code == 200
    assert confirm.json()["status"] == "completed"

    # The original request should now show completed too
    feed = client.get("/p2p/transfers", headers=_auth(requester_token)).json()["transfers"]
    original = next(t for t in feed if t["id"] == request_id)
    assert original["status"] == "completed"


# --- Split the bill ---

def test_split_creates_one_request_per_recipient_with_shared_group(client, two_active_users):
    third_email = f"third_{uuid.uuid4().hex[:8]}@example.com"
    _register(client, third_email, "Third Person")
    _activate_account(third_email)
    _set_handle_direct(third_email, "thirdperson")

    creator_token = two_active_users["recipient_token"]
    resp = client.post(
        "/p2p/splits",
        json={
            "total_amount_cents": 1000,
            "recipient_handles": [f"@{two_active_users['sender_handle']}", "@thirdperson"],
            "note": "tacos",
            "idempotency_key": str(uuid.uuid4()),
        },
        headers=_auth(creator_token),
    )
    assert resp.status_code == 201, resp.text
    transfers = resp.json()["transfers"]
    assert len(transfers) == 2
    assert transfers[0]["group_id"] == transfers[1]["group_id"]
    assert sum(t["amount_cents"] for t in transfers) == 1000


def test_split_too_small_to_divide_rejected(client, two_active_users):
    third_email = f"tiny_{uuid.uuid4().hex[:8]}@example.com"
    _register(client, third_email, "Tiny Split")
    _activate_account(third_email)
    _set_handle_direct(third_email, "tinysplit")

    resp = client.post(
        "/p2p/splits",
        json={
            "total_amount_cents": 1,
            "recipient_handles": [f"@{two_active_users['sender_handle']}", "@tinysplit"],
            "idempotency_key": str(uuid.uuid4()),
        },
        headers=_auth(two_active_users["recipient_token"]),
    )
    assert resp.status_code == 400


# --- Disputes ---

def test_dispute_lifecycle_admin_refund(client, two_active_users, monkeypatch, admin_key):
    _mock_book_payment(monkeypatch, payment_id="pmt_to_dispute")
    sender_token = two_active_users["sender_token"]
    key = f"dispute-{uuid.uuid4()}"

    create = client.post(
        "/p2p/transfers",
        json={"to_handle": f"@{two_active_users['recipient_handle']}", "amount_cents": 600, "idempotency_key": key},
        headers=_auth(sender_token),
    )
    transfer_id = create.json()["id"]
    client.post(f"/p2p/transfers/{transfer_id}/confirm", json={"step_up_acknowledged": True}, headers=_auth(sender_token))

    dispute = client.post(
        f"/p2p/transfers/{transfer_id}/dispute",
        json={"reason": "I never received the item I paid for."},
        headers=_auth(sender_token),
    )
    assert dispute.status_code == 201, dispute.text
    dispute_id = dispute.json()["id"]

    listed = client.get("/p2p/admin/disputes", headers={"X-Admin-Key": admin_key})
    assert listed.status_code == 200
    assert any(d["id"] == dispute_id for d in listed.json())

    resolve = client.post(
        f"/p2p/admin/disputes/{dispute_id}/resolve",
        params={"action": "refund"},
        headers={"X-Admin-Key": admin_key},
    )
    assert resolve.status_code == 200
    assert resolve.json()["status"] == "refunded"


def test_dispute_without_admin_key_403(client, two_active_users):
    resp = client.get("/p2p/admin/disputes")
    assert resp.status_code == 403


# --- Tier 2 stub ---

def test_external_transfer_returns_501(client, two_active_users):
    resp = client.post("/p2p/external-transfers", headers=_auth(two_active_users["sender_token"]))
    assert resp.status_code == 501
