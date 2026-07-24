import uuid
from datetime import datetime, timedelta

from jose import jwt

from config import settings
from database import SessionLocal
from models import User


def _user(wallet=True):
    db = SessionLocal()
    suffix = uuid.uuid4().hex[:10]
    user = User(
        email=f"tab_{suffix}@example.com", username=f"tab{suffix}", hashed_password="x", full_name="Tab Tester",
        wallet_initialized=wallet, crypto_wallet_address=f"0x{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    db.close()
    return user


def _auth(user_id):
    token = jwt.encode({"sub": user_id, "exp": datetime.utcnow() + timedelta(minutes=30)}, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return {"Authorization": f"Bearer {token}"}


def test_private_repayment_request_and_nudge_are_payer_controlled(client):
    requester, payer = _user(), _user()
    response = client.post("/repayments/requests", headers=_auth(requester.id), json={
        "recipient": f"@{payer.username}", "amount_cents": 1875, "note": "dinner split",
    })
    assert response.status_code == 201
    request_id = response.json()["request"]["id"]
    assert "No money moves" in response.json()["message"]

    incoming = client.get("/repayments/requests", headers=_auth(payer.id)).json()["requests"]
    assert incoming[0]["direction"] == "incoming"
    assert incoming[0]["amount_cents"] == 1875

    reminder = client.post(f"/repayments/requests/{request_id}/remind", headers=_auth(requester.id))
    assert reminder.status_code == 200
    assert "never send it for you" in reminder.json()["message"]
    assert client.post(f"/repayments/requests/{request_id}/remind", headers=_auth(requester.id)).status_code == 429


def test_repayment_cannot_target_self_or_uninitialized_wallet(client):
    requester, unready = _user(), _user(wallet=False)
    self_request = client.post("/repayments/requests", headers=_auth(requester.id), json={"recipient": f"@{requester.username}", "amount_cents": 100})
    assert self_request.status_code == 400
    unavailable = client.post("/repayments/requests", headers=_auth(requester.id), json={"recipient": f"@{unready.username}", "amount_cents": 100})
    assert unavailable.status_code == 409


def test_payer_must_explicitly_settle_request(client, monkeypatch):
    requester, payer = _user(), _user()
    created = client.post("/repayments/requests", headers=_auth(requester.id), json={"recipient": f"@{payer.username}", "amount_cents": 500})
    request_id = created.json()["request"]["id"]

    async def fake_send(**kwargs):
        assert kwargs["sender_id"] == payer.id
        assert kwargs["recipient_address"] == requester.crypto_wallet_address
        assert kwargs["amount_cents"] == 500
        assert kwargs["is_internal"] is True
        return {"transfer_id": "settled-test", "fee": 0.01}

    monkeypatch.setattr("routers.repayments.crypto_wallet.send_usdc", fake_send)
    settled = client.post(f"/repayments/requests/{request_id}/pay", headers=_auth(payer.id))
    assert settled.status_code == 200
    assert settled.json()["status"] == "paid"
    assert client.get("/repayments/requests", headers=_auth(payer.id)).json()["requests"][0]["status"] == "paid"
