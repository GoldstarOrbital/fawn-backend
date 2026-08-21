"""Tests for stablecoin redemption (sell USDC back to FAWN less a 1-cent fee).

These are money-safety tests. The invariants that matter:
  - escrow is debited at request time (no double-spend while pending)
  - reject / cancel / fail refund EXACTLY the held amount, once
  - paid consumes the hold (no refund)
  - payout is always exactly 1:1, never a spread
  - float capacity stops FAWN promising more dollars than it can pay
"""
import uuid

import pytest

from config import settings
from database import SessionLocal
from models import User
from models_redemption import StablecoinRedemption

ADMIN = {"X-Admin-Key": "test-admin-key-12345"}


@pytest.fixture(autouse=True)
def _enable_redemptions(monkeypatch):
    monkeypatch.setattr(settings, "redemptions_enabled", True)
    monkeypatch.setattr(settings, "redemption_min_cents", 500)
    monkeypatch.setattr(settings, "redemption_max_cents", 100_000)
    monkeypatch.setattr(settings, "redemption_daily_max_cents", 250_000)
    monkeypatch.setattr(settings, "redemption_float_cents", 0)


def _user(client, balance_cents=50_000):
    email = f"redeem_{uuid.uuid4().hex[:10]}@example.com"
    r = client.post("/auth/register", json={
        "full_name": "Ada Lovelace", "email": email,
        "password": "Zx9-quantum-River-42", "is_student": True})
    assert r.status_code in (200, 201), r.text
    hdrs = {"Authorization": f"Bearer {r.json()['access_token']}"}
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.email == email).first()
        u.usdc_balance_cents = balance_cents
        db.commit()
        uid = u.id
    finally:
        db.close()
    return hdrs, uid


def _balance(user_id):
    db = SessionLocal()
    try:
        return db.query(User).filter(User.id == user_id).first().usdc_balance_cents
    finally:
        db.close()


def test_request_debits_balance_immediately(client):
    h, uid = _user(client, 50_000)
    r = client.post("/redemptions", headers=h, json={"amount_cents": 20_000})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "requested"
    assert body["held_cents"] == 20_000
    # escrowed out of spendable balance right away
    assert _balance(uid) == 30_000


def test_payout_deducts_the_disclosed_one_cent_fee(client):
    h, _ = _user(client)
    body = client.post("/redemptions", headers=h, json={"amount_cents": 12_345}).json()
    assert body["usdc_cents"] == 12_345
    assert body["payout_cents"] == 12_344
    assert body["fee_cents"] == 1
    assert body["rate"] == "1:1"


def test_pending_redemption_cannot_be_double_spent(client):
    h, uid = _user(client, 10_000)
    assert client.post("/redemptions", headers=h, json={"amount_cents": 10_000}).status_code == 201
    assert _balance(uid) == 0
    # the same funds cannot be redeemed again
    second = client.post("/redemptions", headers=h, json={"amount_cents": 10_000})
    assert second.status_code == 402


def test_cancel_refunds_exactly_once(client):
    h, uid = _user(client, 50_000)
    rid = client.post("/redemptions", headers=h, json={"amount_cents": 20_000}).json()["id"]
    assert _balance(uid) == 30_000

    assert client.post(f"/redemptions/{rid}/cancel", headers=h).status_code == 200
    assert _balance(uid) == 50_000

    # a second cancel must not credit the balance again
    again = client.post(f"/redemptions/{rid}/cancel", headers=h)
    assert again.status_code == 409
    assert _balance(uid) == 50_000


def test_reject_refunds_the_hold(client):
    h, uid = _user(client, 50_000)
    rid = client.post("/redemptions", headers=h, json={"amount_cents": 15_000}).json()["id"]
    r = client.post(f"/redemptions/admin/{rid}/reject", headers=ADMIN,
                    json={"reviewer": "alex", "notes": "unverified destination"})
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"
    assert r.json()["held_cents"] == 0
    assert _balance(uid) == 50_000


def test_paid_consumes_hold_and_does_not_refund(client):
    h, uid = _user(client, 50_000)
    rid = client.post("/redemptions", headers=h, json={"amount_cents": 20_000}).json()["id"]
    client.post(f"/redemptions/admin/{rid}/approve", headers=ADMIN, json={"reviewer": "alex"})
    r = client.post(f"/redemptions/admin/{rid}/mark-paid", headers=ADMIN, json={
        "payout_method": "ach", "payout_reference": "ACH-TRACE-99887766", "reviewer": "alex"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "paid"
    assert body["held_cents"] == 0
    assert body["payout_reference"] == "ACH-TRACE-99887766"
    # user was paid in dollars; the USDC is NOT returned to their balance
    assert _balance(uid) == 30_000


def test_cannot_mark_paid_without_approval(client):
    h, _ = _user(client)
    rid = client.post("/redemptions", headers=h, json={"amount_cents": 5_000}).json()["id"]
    r = client.post(f"/redemptions/admin/{rid}/mark-paid", headers=ADMIN, json={
        "payout_method": "ach", "payout_reference": "X-1", "reviewer": "alex"})
    assert r.status_code == 409


def test_failed_payment_refunds_user(client):
    h, uid = _user(client, 40_000)
    rid = client.post("/redemptions", headers=h, json={"amount_cents": 25_000}).json()["id"]
    client.post(f"/redemptions/admin/{rid}/approve", headers=ADMIN, json={"reviewer": "alex"})
    assert _balance(uid) == 15_000
    r = client.post(f"/redemptions/admin/{rid}/mark-failed", headers=ADMIN,
                    json={"reviewer": "alex", "notes": "ACH returned R01"})
    assert r.status_code == 200
    assert r.json()["status"] == "failed"
    assert _balance(uid) == 40_000  # made whole


def test_idempotency_key_prevents_duplicate(client):
    h, uid = _user(client, 50_000)
    key = "order-" + uuid.uuid4().hex[:8]
    a = client.post("/redemptions", headers=h, json={"amount_cents": 10_000, "idempotency_key": key})
    b = client.post("/redemptions", headers=h, json={"amount_cents": 10_000, "idempotency_key": key})
    assert a.status_code == 201
    assert a.json()["id"] == b.json()["id"]
    assert _balance(uid) == 40_000  # debited once, not twice


def test_min_and_max_enforced(client):
    h, _ = _user(client, 500_000)
    assert client.post("/redemptions", headers=h, json={"amount_cents": 100}).status_code == 422
    assert client.post("/redemptions", headers=h, json={"amount_cents": 500_000}).status_code == 422


def test_insufficient_balance_rejected(client):
    h, _ = _user(client, 1_000)
    r = client.post("/redemptions", headers=h, json={"amount_cents": 50_000})
    assert r.status_code == 402


def test_float_capacity_blocks_overcommitment(client, monkeypatch):
    h, uid = _user(client, 100_000)
    # Float is a GLOBAL obligation across all users (that's the point), and the
    # shared test DB already holds open redemptions from earlier tests. Size the
    # cap relative to that existing baseline so this asserts the rule, not the
    # order tests happened to run in.
    baseline = client.get("/redemptions/admin/queue", headers=ADMIN).json()["open_obligation_cents"]
    monkeypatch.setattr(settings, "redemption_float_cents", baseline + 30_000)

    assert client.post("/redemptions", headers=h, json={"amount_cents": 25_000}).status_code == 201
    # only $50 of headroom left; a $200 request must be refused, balance untouched
    over = client.post("/redemptions", headers=h, json={"amount_cents": 20_000})
    assert over.status_code == 503
    assert _balance(uid) == 75_000


def test_disabled_by_default_blocks_requests(client, monkeypatch):
    monkeypatch.setattr(settings, "redemptions_enabled", False)
    h, uid = _user(client, 50_000)
    r = client.post("/redemptions", headers=h, json={"amount_cents": 10_000})
    assert r.status_code == 503
    assert _balance(uid) == 50_000  # balance untouched


def test_quote_reports_eligibility_without_creating(client):
    h, uid = _user(client, 50_000)
    q = client.get("/redemptions/quote?amount_cents=20000", headers=h).json()
    assert q["eligible"] is True
    assert q["payout_cents"] == 19_999 and q["fee_cents"] == 1
    assert _balance(uid) == 50_000  # nothing created

    bad = client.get("/redemptions/quote?amount_cents=999999", headers=h).json()
    assert bad["eligible"] is False and bad["reasons"]


def test_admin_queue_reports_obligation(client):
    h, _ = _user(client, 50_000)
    client.post("/redemptions", headers=h, json={"amount_cents": 12_000})
    q = client.get("/redemptions/admin/queue", headers=ADMIN)
    assert q.status_code == 200
    assert q.json()["open_obligation_cents"] >= 12_000


def test_admin_endpoints_require_admin_key(client):
    assert client.get("/redemptions/admin/queue").status_code == 403


def test_cannot_redeem_another_users_redemption(client):
    h1, _ = _user(client, 50_000)
    h2, _ = _user(client, 50_000)
    rid = client.post("/redemptions", headers=h1, json={"amount_cents": 10_000}).json()["id"]
    assert client.post(f"/redemptions/{rid}/cancel", headers=h2).status_code == 404


def test_payout_plus_fee_is_a_db_constraint(client):
    """A redemption cannot hide a fee or pay out more than the held USDC."""
    h, uid = _user(client, 50_000)
    db = SessionLocal()
    try:
        row = StablecoinRedemption(user_id=uid, usdc_cents=10_000, payout_cents=9_999,
                                  fee_cents=0, held_cents=10_000)
        db.add(row)
        with pytest.raises(Exception):
            db.commit()
    finally:
        db.rollback()
        db.close()
