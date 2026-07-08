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


def _create_hosted_form_user(email):
    db = SessionLocal()
    try:
        user = User(
            email=email.lower(),
            hashed_password="not-a-real-hash",
            full_name="Hosted Student",
            is_student=True,
            unit_application_form_id=f"form_{uuid.uuid4().hex[:10]}",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user.id, user.unit_application_form_id
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
    assert body["wallet_initialized"] is True
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


def test_refresh_application_status_completes_hosted_form_application(client, monkeypatch):
    user_id, form_id = _create_hosted_form_user(f"hostedrefresh_{uuid.uuid4().hex[:8]}@example.com")
    token = _token_for(user_id)

    async def fake_get_application_form(application_form_id):
        assert application_form_id == form_id
        return {
            "data": {
                "id": application_form_id,
                "relationships": {
                    "application": {"data": {"type": "application", "id": "app_hosted123"}}
                },
            },
            "included": [],
        }

    async def fake_get_application(application_id):
        assert application_id == "app_hosted123"
        return {
            "id": application_id,
            "attributes": {"status": "Approved"},
            "relationships": {"customer": {"data": {"id": "cust_hosted123"}}},
        }

    async def fake_create_deposit_account(customer_id):
        assert customer_id == "cust_hosted123"
        return {"id": "acc_hosted456"}

    monkeypatch.setattr("routers.accounts.unit_svc.get_application_form", fake_get_application_form)
    monkeypatch.setattr("routers.accounts.unit_svc.get_application", fake_get_application)
    monkeypatch.setattr("routers.accounts.unit_svc.create_deposit_account", fake_create_deposit_account)

    resp = client.post("/accounts/refresh-application-status", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["wallet_initialized"] is True
    assert resp.json()["unit_account_id"] == "acc_hosted456"

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        assert user.unit_application_id == "app_hosted123"
        assert user.unit_customer_id == "cust_hosted123"
        assert user.unit_account_id == "acc_hosted456"
    finally:
        db.close()
