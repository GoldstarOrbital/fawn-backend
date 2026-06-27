"""Tests for /funding — Add Funds via ACH (inline counterparty, pulls from
an external bank account into the user's FAWN deposit account)."""
import uuid
from datetime import datetime, timedelta
from jose import jwt

from database import SessionLocal
from models import User, FundingRequest
from config import settings


def _create_active_user(email, unit_account_id="acc_funding_test"):
    db = SessionLocal()
    try:
        user = User(
            email=email.lower(), hashed_password="x", full_name="Funding Tester",
            is_student=True, unit_account_id=unit_account_id,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user.id
    finally:
        db.close()


def _token_for(user_id):
    expire = datetime.utcnow() + timedelta(minutes=settings.jwt_expire_minutes)
    return jwt.encode({"sub": user_id, "exp": expire}, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _valid_payload(amount_cents=10000, idempotency_key=None):
    return {
        "amount_cents": amount_cents,
        "routing_number": "812345678",
        "account_number": "123456789",
        "account_type": "Checking",
        "account_holder_name": "Funding Tester",
        "idempotency_key": idempotency_key or str(uuid.uuid4()),
    }


def _mock_unit_payment(monkeypatch, payment_id="pmt_funding_fake"):
    async def fake(**kwargs):
        return {"id": payment_id, "type": "achPayment"}
    monkeypatch.setattr("routers.funding.unit_svc.create_ach_funding_payment", fake)


def test_add_funds_without_active_account_400(client):
    user_id = _create_active_user(f"noacct_{uuid.uuid4().hex[:8]}@example.com", unit_account_id=None)
    resp = client.post("/funding/add-funds", json=_valid_payload(), headers=_auth(_token_for(user_id)))
    assert resp.status_code == 400


def test_add_funds_happy_path_never_stores_full_account_number(client, monkeypatch):
    _mock_unit_payment(monkeypatch)
    user_id = _create_active_user(f"funder_{uuid.uuid4().hex[:8]}@example.com")
    token = _token_for(user_id)

    payload = _valid_payload(amount_cents=15000)
    resp = client.post("/funding/add-funds", json=payload, headers=_auth(token))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    assert body["external_account_last4"] == "6789"  # last 4 of "123456789"
    assert body["amount_cents"] == 15000

    db = SessionLocal()
    row = db.query(FundingRequest).filter(FundingRequest.id == body["id"]).first()
    db.close()
    assert row.external_account_last4 == "6789"
    # The full account number must never be persisted anywhere on this row
    assert "123456789" not in (row.external_account_last4 or "")


def test_duplicate_idempotency_key_returns_same_request(client, monkeypatch):
    _mock_unit_payment(monkeypatch)
    user_id = _create_active_user(f"idem_{uuid.uuid4().hex[:8]}@example.com")
    token = _token_for(user_id)
    payload = _valid_payload(amount_cents=5000)

    first = client.post("/funding/add-funds", json=payload, headers=_auth(token))
    second = client.post("/funding/add-funds", json=payload, headers=_auth(token))
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]


def test_per_request_limit_enforced(client):
    user_id = _create_active_user(f"toobig_{uuid.uuid4().hex[:8]}@example.com")
    resp = client.post(
        "/funding/add-funds",
        json=_valid_payload(amount_cents=999_999),
        headers=_auth(_token_for(user_id)),
    )
    assert resp.status_code == 400
    assert "capped" in resp.json()["detail"]


def test_daily_limit_enforced_across_multiple_requests(client, monkeypatch):
    _mock_unit_payment(monkeypatch)
    user_id = _create_active_user(f"daily_{uuid.uuid4().hex[:8]}@example.com")
    token = _token_for(user_id)

    # Two $400 requests = $800, under the $1000 daily cap
    r1 = client.post("/funding/add-funds", json=_valid_payload(amount_cents=40000), headers=_auth(token))
    r2 = client.post("/funding/add-funds", json=_valid_payload(amount_cents=40000), headers=_auth(token))
    assert r1.status_code == 201
    assert r2.status_code == 201

    # A third $400 would push the day total to $1200, over the cap
    r3 = client.post("/funding/add-funds", json=_valid_payload(amount_cents=40000), headers=_auth(token))
    assert r3.status_code == 400
    assert "24-hour" in r3.json()["detail"]


def test_invalid_routing_number_rejected_422(client):
    user_id = _create_active_user(f"badrouting_{uuid.uuid4().hex[:8]}@example.com")
    payload = _valid_payload()
    payload["routing_number"] = "123"
    resp = client.post("/funding/add-funds", json=payload, headers=_auth(_token_for(user_id)))
    assert resp.status_code == 422


def test_unit_failure_marks_request_failed_not_silently_lost(client, monkeypatch):
    async def fake_fail(**kwargs):
        raise RuntimeError("simulated Unit outage")
    monkeypatch.setattr("routers.funding.unit_svc.create_ach_funding_payment", fake_fail)

    user_id = _create_active_user(f"failcase_{uuid.uuid4().hex[:8]}@example.com")
    resp = client.post("/funding/add-funds", json=_valid_payload(), headers=_auth(_token_for(user_id)))
    assert resp.status_code == 502

    db = SessionLocal()
    row = db.query(FundingRequest).filter(FundingRequest.user_id == user_id).first()
    db.close()
    assert row is not None
    assert row.status == "failed"
    assert "simulated Unit outage" in row.error_message


def test_funding_history_lists_only_own_requests(client, monkeypatch):
    _mock_unit_payment(monkeypatch)
    user_a = _create_active_user(f"usera_{uuid.uuid4().hex[:8]}@example.com")
    user_b = _create_active_user(f"userb_{uuid.uuid4().hex[:8]}@example.com")

    client.post("/funding/add-funds", json=_valid_payload(amount_cents=2000), headers=_auth(_token_for(user_a)))

    resp_a = client.get("/funding/history", headers=_auth(_token_for(user_a)))
    resp_b = client.get("/funding/history", headers=_auth(_token_for(user_b)))
    assert resp_a.status_code == 200
    assert len(resp_a.json()["requests"]) >= 1
    assert len(resp_b.json()["requests"]) == 0
