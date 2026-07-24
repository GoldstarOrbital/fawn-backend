"""Safety and persistence checks for user-configured automation rules."""
import uuid
from datetime import datetime, timedelta

from jose import jwt

from config import settings
from database import SessionLocal
from models import User


def _user_with_balance():
    db = SessionLocal()
    user = User(
        email=f"automation_{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name="Automation Tester",
        usdc_balance_cents=50_000,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    db.close()
    return user.id


def _auth(user_id):
    token = jwt.encode(
        {"sub": user_id, "exp": datetime.utcnow() + timedelta(minutes=30)},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    return {"Authorization": f"Bearer {token}"}


def test_price_alert_can_be_created_and_listed(client):
    headers = _auth(_user_with_balance())
    response = client.post(
        "/automation/price-alerts",
        headers=headers,
        json={"token": "ETH", "threshold_usd": 2500, "direction": "above"},
    )
    assert response.status_code == 200
    listed = client.get("/automation/price-alerts", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["count"] == 1


def test_money_rule_has_server_side_cap(client):
    headers = _auth(_user_with_balance())
    response = client.post(
        "/automation/recurring-transfers",
        headers=headers,
        json={
            "recipient_address": "@friend",
            "amount_cents": 100_001,
            "frequency": "weekly",
            "start_date": (datetime.utcnow() + timedelta(days=1)).isoformat(),
        },
    )
    assert response.status_code == 422
