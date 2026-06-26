"""Tests for /accounts/activate-sandbox — the sandbox-only self-service
KYC unstick endpoint."""
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
            unit_application_id=f"app_{uuid.uuid4().hex[:10]}",
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
    user_id = _create_pending_user(f"pending_{uuid.uuid4().hex[:8]}@example.com")
    token = _token_for(user_id)

    async def fake_approve(application_id):
        return {"id": application_id, "type": "individualApplication"}

    async def fake_get_application(application_id):
        return {
            "id": application_id,
            "attributes": {"status": "approved"},
            "relationships": {"customer": {"data": {"id": "cust_fake123"}}},
        }

    async def fake_create_deposit_account(customer_id):
        return {"id": "acc_fake456"}

    monkeypatch.setattr("routers.accounts.unit_svc.approve_application_sandbox", fake_approve)
    monkeypatch.setattr("routers.accounts.unit_svc.get_application", fake_get_application)
    monkeypatch.setattr("routers.accounts.unit_svc.create_deposit_account", fake_create_deposit_account)

    resp = client.post("/accounts/activate-sandbox", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["account_active"] is True
    assert body["unit_account_id"] == "acc_fake456"

    # Idempotent: calling again with an already-active account is a no-op, not an error
    second = client.post("/accounts/activate-sandbox", headers=_auth(token))
    assert second.status_code == 200
    assert second.json()["unit_account_id"] == "acc_fake456"


def test_activate_sandbox_refuses_outside_sandbox(client, monkeypatch):
    import config
    monkeypatch.setattr(config.settings, "unit_base_url", "https://api.unit.co")

    user_id = _create_pending_user(f"prodguard_{uuid.uuid4().hex[:8]}@example.com")
    resp = client.post("/accounts/activate-sandbox", headers=_auth(_token_for(user_id)))
    assert resp.status_code == 403
