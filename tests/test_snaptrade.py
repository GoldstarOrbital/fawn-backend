"""SnapTrade connection lifecycle tests (provider calls are mocked)."""
import uuid
from datetime import datetime, timedelta

from jose import jwt

from config import settings
from database import SessionLocal
from models import User, SnapTradeUser


def _user():
    db = SessionLocal()
    user = User(email=f"snap_{uuid.uuid4().hex[:8]}@example.com", hashed_password="x", full_name="Snap Tester")
    db.add(user)
    db.commit()
    db.refresh(user)
    db.close()
    return user.id


def _auth(user_id):
    token = jwt.encode({"sub": user_id, "exp": datetime.utcnow() + timedelta(minutes=30)}, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return {"Authorization": f"Bearer {token}"}


def test_snaptrade_is_dormant_without_credentials(client):
    user_id = _user()
    response = client.post("/brokerage/connect", headers=_auth(user_id), json={})
    assert response.status_code == 503
    assert "not configured" in response.json()["detail"].lower()


def test_snaptrade_secret_is_encrypted_and_never_returned(client, monkeypatch):
    async def register(snaptrade_user_id):
        return {"userId": snaptrade_user_id, "userSecret": "secret-not-for-client"}

    async def portal(user_id, user_secret, redirect_uri=None):
        assert user_secret == "secret-not-for-client"
        return {"redirectURI": "https://app.snaptrade.com/portal", "sessionId": "session-1"}

    monkeypatch.setattr("routers.snaptrade.snaptrade_svc.register_user", register)
    monkeypatch.setattr("routers.snaptrade.snaptrade_svc.create_portal", portal)
    monkeypatch.setattr(settings, "snaptrade_client_id", "client")
    monkeypatch.setattr(settings, "snaptrade_consumer_key", "consumer")

    user_id = _user()
    response = client.post("/brokerage/connect", headers=_auth(user_id), json={"redirect_uri": "https://goldstarorbital.github.io/fawn-frontend/"})
    assert response.status_code == 200, response.text
    assert response.json() == {"redirect_uri": "https://app.snaptrade.com/portal", "session_id": "session-1"}

    db = SessionLocal()
    row = db.query(SnapTradeUser).filter(SnapTradeUser.user_id == user_id).one()
    assert row.status == "active"
    assert b"secret-not-for-client" not in row.encrypted_user_secret
    db.close()
