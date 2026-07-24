"""Tests for /investing (Alpaca-backed) — account open, order, positions.

Alpaca is never actually called; services.alpaca functions are monkeypatched.
Also verifies the unconfigured-provider path returns 503, not 500.
"""
import uuid

from datetime import datetime, timedelta
from jose import jwt

from database import SessionLocal
from models import User, InvestingOrder
from config import settings


def _create_user(email, alpaca_account_id=None):
    db = SessionLocal()
    try:
        user = User(
            email=email.lower(), hashed_password="x", full_name="Invest Tester",
            is_student=True, alpaca_account_id=alpaca_account_id,
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


def test_open_account_happy_path(client, monkeypatch):
    async def fake_create(email, given_name, family_name, agreements):
        return {"id": "alp_123", "status": "SUBMITTED"}

    monkeypatch.setattr("routers.investing.alpaca_svc.create_brokerage_account", fake_create)
    user_id = _create_user(f"inv_{uuid.uuid4().hex[:8]}@example.com")
    resp = client.post("/investing/account", headers=_auth(_token_for(user_id)),
                       json={"agreements": [{"agreement": "customer_agreement"}]})
    assert resp.status_code == 201, resp.text
    assert resp.json()["account_id"] == "alp_123"


def test_open_account_conflict_if_exists(client):
    user_id = _create_user(f"inv_{uuid.uuid4().hex[:8]}@example.com", alpaca_account_id="alp_existing")
    resp = client.post("/investing/account", headers=_auth(_token_for(user_id)), json={"agreements": []})
    assert resp.status_code == 409


def test_place_order_requires_account(client):
    user_id = _create_user(f"inv_{uuid.uuid4().hex[:8]}@example.com")
    resp = client.post("/investing/orders", headers=_auth(_token_for(user_id)),
                       json={"symbol": "AAPL", "side": "buy", "notional": 10})
    assert resp.status_code == 400


def test_place_order_rejects_both_amounts(client):
    user_id = _create_user(f"inv_{uuid.uuid4().hex[:8]}@example.com", alpaca_account_id="alp_x")
    resp = client.post("/investing/orders", headers=_auth(_token_for(user_id)),
                       json={"symbol": "AAPL", "side": "buy", "notional": 10, "qty": 1})
    assert resp.status_code == 400


def test_place_order_happy_path(client, monkeypatch):
    async def fake_order(account_id, symbol, side, notional=None, qty=None):
        return {"id": "ord_1", "status": "accepted"}

    monkeypatch.setattr("routers.investing.alpaca_svc.place_order", fake_order)
    user_id = _create_user(f"inv_{uuid.uuid4().hex[:8]}@example.com", alpaca_account_id="alp_y")
    resp = client.post("/investing/orders", headers=_auth(_token_for(user_id)),
                       json={"symbol": "aapl", "side": "buy", "notional": 25})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["order_id"] == "ord_1"
    assert body["symbol"] == "AAPL"


def test_place_order_enforces_protective_per_order_limit(client):
    user_id = _create_user(f"inv_{uuid.uuid4().hex[:8]}@example.com", alpaca_account_id="alp_limit")
    resp = client.post("/investing/orders", headers=_auth(_token_for(user_id)),
                       json={"symbol": "AAPL", "side": "buy", "notional": 1000.01})
    assert resp.status_code == 400
    assert "1,000" in resp.json()["detail"]


def test_place_order_enforces_rolling_daily_limit(client):
    user_id = _create_user(f"inv_{uuid.uuid4().hex[:8]}@example.com", alpaca_account_id="alp_daily")
    db = SessionLocal()
    try:
        db.add(InvestingOrder(
            user_id=user_id, symbol="SPY", side="buy", notional_cents=200_000,
            status="accepted", idempotency_key=f"existing:{uuid.uuid4().hex}",
        ))
        db.commit()
    finally:
        db.close()
    resp = client.post("/investing/orders", headers=_auth(_token_for(user_id)),
                       json={"symbol": "AAPL", "side": "buy", "notional": 501})
    assert resp.status_code == 400
    assert "24-hour" in resp.json()["detail"]


def test_unconfigured_provider_returns_503(client):
    # No monkeypatch: real services.alpaca runs, sees empty key, raises
    # AlpacaNotConfigured -> router maps to 503 (feature dormant, not a crash).
    user_id = _create_user(f"inv_{uuid.uuid4().hex[:8]}@example.com")
    resp = client.post("/investing/account", headers=_auth(_token_for(user_id)), json={"agreements": []})
    assert resp.status_code == 503
