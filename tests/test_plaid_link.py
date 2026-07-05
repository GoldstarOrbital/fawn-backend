"""Tests for /plaid link-token + public-token exchange (Plaid-backed)."""
import uuid

from datetime import datetime, timedelta
from jose import jwt

from database import SessionLocal
from models import User, PlaidItem
from config import settings


def _create_user(email):
    db = SessionLocal()
    try:
        user = User(email=email.lower(), hashed_password="x", full_name="Plaid Tester", is_student=True)
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


def test_link_token_happy_path(client, monkeypatch):
    async def fake_link(user_id):
        return {"link_token": "link-sandbox-abc", "expiration": "2026-01-01T00:00:00Z"}

    monkeypatch.setattr("routers.plaid_link.plaid_svc.create_link_token", fake_link)
    user_id = _create_user(f"pl_{uuid.uuid4().hex[:8]}@example.com")
    resp = client.post("/plaid/link-token", headers=_auth(_token_for(user_id)))
    assert resp.status_code == 200, resp.text
    assert resp.json()["link_token"] == "link-sandbox-abc"


def test_exchange_persists_item(client, monkeypatch):
    item_id = f"item_{uuid.uuid4().hex[:8]}"

    async def fake_exchange(public_token):
        return {"access_token": "access-secret-xyz", "item_id": item_id}

    async def fake_auth(access_token):
        return {"mask": "4321", "account_name": "Test Checking", "account_number": "000004321"}

    monkeypatch.setattr("routers.plaid_link.plaid_svc.exchange_public_token", fake_exchange)
    monkeypatch.setattr("routers.plaid_link.plaid_svc.get_auth", fake_auth)

    user_id = _create_user(f"pl_{uuid.uuid4().hex[:8]}@example.com")
    resp = client.post("/plaid/exchange", headers=_auth(_token_for(user_id)),
                       json={"public_token": "public-sandbox-tok"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["account_mask"] == "4321"

    db = SessionLocal()
    try:
        row = db.query(PlaidItem).filter(PlaidItem.item_id == item_id).first()
        assert row is not None
        assert row.access_token == "access-secret-xyz"  # stored server-side only
    finally:
        db.close()


def test_unconfigured_provider_returns_503(client):
    user_id = _create_user(f"pl_{uuid.uuid4().hex[:8]}@example.com")
    resp = client.post("/plaid/link-token", headers=_auth(_token_for(user_id)))
    assert resp.status_code == 503
