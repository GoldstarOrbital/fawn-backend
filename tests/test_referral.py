"""Tests for the referral program, including the real reward payout.

The payout is a ledger credit against the custodial float: when a new user
applies a referral code, BOTH the inviter and the new user are credited
settings.referral_bonus_cents on their usdc_balance_cents, exactly once,
with an audit log per credit. No RPC, no real funds.
"""
import json
import uuid

from config import settings
from database import SessionLocal
from models import User, UserAuditLog
from routers.referral import REFERRAL_BONUS_ACTION


def _register(client, name="Referral Tester"):
    email = f"ref_{uuid.uuid4().hex[:10]}@example.com"
    r = client.post("/auth/register", json={
        "full_name": name,
        "email": email,
        "password": "supersecret123",
        "is_student": True,
    })
    assert r.status_code in (200, 201), r.text
    token = r.json()["access_token"]
    return email, {"Authorization": f"Bearer {token}"}


def _balance_cents(email):
    db = SessionLocal()
    try:
        return db.query(User).filter(User.email == email).first().usdc_balance_cents or 0
    finally:
        db.close()


def test_referral_use_pays_both_sides(client):
    inviter_email, inviter_hdrs = _register(client, "Ivy Inviter")
    invitee_email, invitee_hdrs = _register(client, "Nina Newuser")

    code = client.get("/referral/code", headers=inviter_hdrs).json()["code"]

    inviter_before = _balance_cents(inviter_email)
    invitee_before = _balance_cents(invitee_email)

    r = client.post("/referral/use", json={"code": code}, headers=invitee_hdrs)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bonus_cents"] == settings.referral_bonus_cents
    assert "earned" in body["message"]

    assert _balance_cents(inviter_email) == inviter_before + settings.referral_bonus_cents
    assert _balance_cents(invitee_email) == invitee_before + settings.referral_bonus_cents


def test_referral_bonus_is_audit_logged(client):
    inviter_email, inviter_hdrs = _register(client)
    _, invitee_hdrs = _register(client)

    code = client.get("/referral/code", headers=inviter_hdrs).json()["code"]
    client.post("/referral/use", json={"code": code}, headers=invitee_hdrs)

    db = SessionLocal()
    try:
        inviter = db.query(User).filter(User.email == inviter_email).first()
        logs = db.query(UserAuditLog).filter(
            UserAuditLog.user_id == inviter.id,
            UserAuditLog.action == REFERRAL_BONUS_ACTION,
        ).all()
        assert len(logs) == 1
        details = json.loads(logs[0].details)
        assert details["amount_cents"] == settings.referral_bonus_cents
        assert details["role"] == "inviter"
        assert inviter.referral_count == 1
    finally:
        db.close()


def test_referral_cannot_be_applied_twice(client):
    _, inviter_hdrs = _register(client)
    invitee_email, invitee_hdrs = _register(client)
    _, second_inviter_hdrs = _register(client)

    code = client.get("/referral/code", headers=inviter_hdrs).json()["code"]
    code2 = client.get("/referral/code", headers=second_inviter_hdrs).json()["code"]

    assert client.post("/referral/use", json={"code": code}, headers=invitee_hdrs).status_code == 200
    balance_after_first = _balance_cents(invitee_email)

    # Second application (same or different code) is rejected and pays nothing
    r = client.post("/referral/use", json={"code": code2}, headers=invitee_hdrs)
    assert r.status_code == 400
    assert _balance_cents(invitee_email) == balance_after_first


def test_self_referral_rejected_and_unpaid(client):
    email, hdrs = _register(client)
    code = client.get("/referral/code", headers=hdrs).json()["code"]
    before = _balance_cents(email)

    r = client.post("/referral/use", json={"code": code}, headers=hdrs)
    assert r.status_code == 400
    assert _balance_cents(email) == before


def test_unknown_code_404(client):
    _, hdrs = _register(client)
    r = client.post("/referral/use", json={"code": "NOPE-0000"}, headers=hdrs)
    assert r.status_code == 404


def test_code_endpoint_reports_earnings(client):
    inviter_email, inviter_hdrs = _register(client)
    _, invitee_hdrs = _register(client)

    code_info = client.get("/referral/code", headers=inviter_hdrs).json()
    assert code_info["rewards_enabled"] is True
    assert code_info["bonus_cents_per_referral"] == settings.referral_bonus_cents
    assert code_info["total_earned_cents"] == 0

    client.post("/referral/use", json={"code": code_info["code"]}, headers=invitee_hdrs)

    code_info = client.get("/referral/code", headers=inviter_hdrs).json()
    assert code_info["referrals"] == 1
    assert code_info["total_earned_cents"] == settings.referral_bonus_cents


def test_rewards_can_be_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "referral_rewards_enabled", False)

    inviter_email, inviter_hdrs = _register(client)
    invitee_email, invitee_hdrs = _register(client)
    code = client.get("/referral/code", headers=inviter_hdrs).json()["code"]

    inviter_before = _balance_cents(inviter_email)
    r = client.post("/referral/use", json={"code": code}, headers=invitee_hdrs)
    assert r.status_code == 200
    assert r.json()["bonus_cents"] == 0
    assert _balance_cents(inviter_email) == inviter_before
    assert _balance_cents(invitee_email) == 0
