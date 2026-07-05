"""Tests for /accounts/activate-sandbox and /accounts/refresh-application-status
— the Stripe Connect/Treasury KYC-unstick + polling endpoints."""
import uuid

from database import SessionLocal
from models import User


def _create_pending_user(email):
    db = SessionLocal()
    try:
        user = User(
            email=email.lower(),
            hashed_password="not-a-real-hash",
            full_name="Pending Student",
            is_student=True,
            stripe_account_id=f"acct_{uuid.uuid4().hex[:10]}",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user.id
    finally:
        db.close()


def _token_for(user_id):
    from datetime import datetime, timedelta
    from jose import jwt
    from config import settings
    expire = datetime.utcnow() + timedelta(minutes=settings.jwt_expire_minutes)
    return jwt.encode({"sub": user_id, "exp": expire}, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _active_stripe_account(account_id):
    return {"id": account_id, "capabilities": {"treasury": "active", "card_issuing": "active"}}


def _pending_stripe_account(account_id):
    return {"id": account_id, "capabilities": {"treasury": "pending"}}


def test_activate_sandbox_with_no_application_400(client):
    db = SessionLocal()
    try:
        user = User(email=f"noapp_{uuid.uuid4().hex[:8]}@example.com", hashed_password="x", full_name="No App")
        db.add(user)
        db.commit()
        db.refresh(user)
        user_id = user.id
    finally:
        db.close()

    resp = client.post("/accounts/activate-sandbox", headers=_auth(_token_for(user_id)))
    assert resp.status_code == 400


def test_activate_sandbox_happy_path(client, monkeypatch):
    import config
    monkeypatch.setattr(config.settings, "stripe_secret_key", "sk_test_fake")

    user_id = _create_pending_user(f"pending_{uuid.uuid4().hex[:8]}@example.com")
    token = _token_for(user_id)

    db = SessionLocal()
    account_id = db.query(User).filter(User.id == user_id).first().stripe_account_id
    db.close()

    async def fake_get_account(acct_id):
        return _active_stripe_account(acct_id)

    async def fake_create_financial_account(acct_id):
        return {"id": "fa_fake456"}

    monkeypatch.setattr("routers.accounts.stripe_svc.get_account", fake_get_account)
    monkeypatch.setattr("routers.accounts.stripe_svc.create_financial_account", fake_create_financial_account)

    resp = client.post("/accounts/activate-sandbox", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["account_active"] is True
    assert body["stripe_financial_account_id"] == "fa_fake456"

    # Idempotent: calling again with an already-active account is a no-op, not an error
    second = client.post("/accounts/activate-sandbox", headers=_auth(token))
    assert second.status_code == 200
    assert second.json()["stripe_financial_account_id"] == "fa_fake456"


def test_activate_sandbox_refuses_outside_sandbox(client, monkeypatch):
    import config
    monkeypatch.setattr(config.settings, "stripe_secret_key", "sk_live_not_a_test_key")

    user_id = _create_pending_user(f"prodguard_{uuid.uuid4().hex[:8]}@example.com")
    resp = client.post("/accounts/activate-sandbox", headers=_auth(_token_for(user_id)))
    assert resp.status_code == 403


def test_refresh_application_status_activates_pending_account(client, monkeypatch):
    user_id = _create_pending_user(f"refresh_{uuid.uuid4().hex[:8]}@example.com")
    token = _token_for(user_id)

    async def fake_get_account(acct_id):
        return _active_stripe_account(acct_id)

    async def fake_create_financial_account(acct_id):
        return {"id": "fa_refreshed789"}

    monkeypatch.setattr("routers.accounts.stripe_svc.get_account", fake_get_account)
    monkeypatch.setattr("routers.accounts.stripe_svc.create_financial_account", fake_create_financial_account)

    resp = client.post("/accounts/refresh-application-status", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["account_active"] is True
    assert resp.json()["stripe_financial_account_id"] == "fa_refreshed789"

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        assert user.stripe_financial_account_id == "fa_refreshed789"
    finally:
        db.close()


def test_refresh_application_status_leaves_pending_account_alone(client, monkeypatch):
    user_id = _create_pending_user(f"stillpending_{uuid.uuid4().hex[:8]}@example.com")
    token = _token_for(user_id)

    async def fake_get_account(acct_id):
        return _pending_stripe_account(acct_id)

    monkeypatch.setattr("routers.accounts.stripe_svc.get_account", fake_get_account)

    resp = client.post("/accounts/refresh-application-status", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["account_active"] is False
    assert resp.json()["application_pending"] is True
