"""Safety and persistence checks for user-configured automation rules."""
import uuid
import asyncio
from datetime import datetime, timedelta

from jose import jwt

from config import settings
from database import SessionLocal
from models import User
from services import automation_runner


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


def test_webhook_returns_secret_once_and_hides_it_on_list(client):
    headers = _auth(_user_with_balance())
    response = client.post(
        "/webhooks/subscribe",
        headers=headers,
        json={"url": "https://example.com/fawn", "events": ["test.ping", "price_alert.triggered"]},
    )
    assert response.status_code == 200
    assert response.json()["signing_secret"].startswith("whsec_")
    listed = client.get("/webhooks/subscriptions", headers=headers)
    assert listed.status_code == 200
    assert "signing_secret" not in listed.json()["webhooks"][0]


def test_batch_requires_multiple_capped_recipients(client):
    headers = _auth(_user_with_balance())
    one_recipient = client.post(
        "/webhooks/batch/transfers",
        headers=headers,
        json={"recipients": [{"address": "@friend", "amount_cents": 1000}]},
    )
    assert one_recipient.status_code == 422
    oversized = client.post(
        "/webhooks/batch/transfers",
        headers=headers,
        json={"recipients": [{"address": "@a", "amount_cents": 300_000}, {"address": "@b", "amount_cents": 300_000}]},
    )
    assert oversized.status_code == 422


def test_price_alert_runner_records_threshold_crossing(client, monkeypatch):
    headers = _auth(_user_with_balance())
    created = client.post(
        "/automation/price-alerts",
        headers=headers,
        json={"token": "ETH", "threshold_usd": 2500, "direction": "above"},
    )
    assert created.status_code == 200

    async def fake_prices(_tokens):
        return {"ETH": 3000.0}

    monkeypatch.setattr(automation_runner, "_prices", fake_prices)
    db = SessionLocal()
    try:
        result = asyncio.run(automation_runner.run_price_alert_checks(db))
    finally:
        db.close()
    assert result["triggered"] >= 1
