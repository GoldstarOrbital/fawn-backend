"""Tests for /onramp — the multi-provider Buy Crypto config + Coinbase session token."""
import uuid

from datetime import datetime, timedelta
from jose import jwt

from database import SessionLocal
from models import User
from config import settings


def _create_user(email, wallet_address=None):
    db = SessionLocal()
    try:
        user = User(email=email.lower(), hashed_password="x", full_name="Onramp Tester",
                    is_student=True, crypto_wallet_address=wallet_address)
        db.add(user)
        db.commit()
        db.refresh(user)
        return user.id
    finally:
        db.close()


def _auth(user_id):
    expire = datetime.utcnow() + timedelta(minutes=settings.jwt_expire_minutes)
    token = jwt.encode({"sub": user_id, "exp": expire}, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return {"Authorization": f"Bearer {token}"}


def test_config_reports_all_providers_disabled_by_default(client):
    resp = client.get("/onramp/config")
    assert resp.status_code == 200
    body = resp.json()
    for provider in ("ramp", "coinbase", "moonpay", "transak"):
        assert body[provider]["enabled"] is False


def test_config_reports_provider_enabled_when_key_set(client, monkeypatch):
    monkeypatch.setattr(settings, "ramp_host_app_id", "ramp_host_test")
    resp = client.get("/onramp/config")
    assert resp.status_code == 200
    assert resp.json()["ramp"]["enabled"] is True
    assert resp.json()["ramp"]["host_app_id"] == "ramp_host_test"


def test_coinbase_session_token_503_when_unconfigured(client):
    user_id = _create_user(f"onr_{uuid.uuid4().hex[:8]}@example.com", wallet_address="0x" + "1" * 40)
    resp = client.post("/onramp/coinbase/session-token", headers=_auth(user_id), json={})
    assert resp.status_code == 503


def test_coinbase_session_token_404_without_wallet(client):
    user_id = _create_user(f"onr_{uuid.uuid4().hex[:8]}@example.com", wallet_address=None)
    resp = client.post("/onramp/coinbase/session-token", headers=_auth(user_id), json={})
    assert resp.status_code == 404


def test_coinbase_session_token_happy_path(client, monkeypatch):
    async def fake_create_session_token(destination_address, assets=None):
        return {"session_token": "tok_abc", "onramp_url": "https://pay.coinbase.com/buy/select-asset?sessionToken=tok_abc"}

    monkeypatch.setattr("routers.onramp.coinbase_svc.create_session_token", fake_create_session_token)
    user_id = _create_user(f"onr_{uuid.uuid4().hex[:8]}@example.com", wallet_address="0x" + "2" * 40)
    resp = client.post("/onramp/coinbase/session-token", headers=_auth(user_id), json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["session_token"] == "tok_abc"


def test_onramp_requires_auth(client):
    resp = client.post("/onramp/coinbase/session-token", json={})
    assert resp.status_code in (401, 403)
