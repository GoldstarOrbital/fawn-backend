"""Tests for /ai/money-review — the 10-prompts-in-one-button feature.
All Anthropic/Unit calls are mocked."""
import uuid
from datetime import datetime, timedelta

from jose import jwt

from database import SessionLocal
from models import User
from config import settings


def _make_user(email, unit_account_id=None):
    db = SessionLocal()
    try:
        user = User(email=email.lower(), hashed_password="x", full_name="Review User",
                    is_student=True, unit_account_id=unit_account_id)
        db.add(user)
        db.commit()
        db.refresh(user)
        return user.id
    finally:
        db.close()


def _auth_for(user_id):
    expire = datetime.utcnow() + timedelta(minutes=settings.jwt_expire_minutes)
    token = jwt.encode({"sub": user_id, "exp": expire}, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return {"Authorization": f"Bearer {token}"}


FAKE_TXNS = [
    {"date": "2026-06-28", "description": "STARBUCKS #123", "amount": -6.50},
    {"date": "2026-06-27", "description": "TRADER JOE'S", "amount": -54.20},
    {"date": "2026-06-25", "description": "NETFLIX.COM", "amount": -15.49},
    {"date": "2026-06-24", "description": "PAYROLL DEPOSIT", "amount": 800.00},
]

FAKE_REVIEW = "BUDGET CHECK\nYou spent 76 dollars this period.\n\nTHREE ADJUSTMENTS\n1. Example."


def _mock_review(monkeypatch, review=FAKE_REVIEW):
    async def fake_review(**kwargs):
        return review
    monkeypatch.setattr("routers.money_review.claude_svc.generate_money_review", fake_review)


def _mock_unit_txns(monkeypatch, txns=FAKE_TXNS):
    async def fake_list(account_id, limit=100):
        return txns
    monkeypatch.setattr("routers.money_review.unit_svc.list_transactions", fake_list)


def test_review_with_account_data(client, monkeypatch):
    _mock_review(monkeypatch)
    _mock_unit_txns(monkeypatch)
    user_id = _make_user(f"rev_{uuid.uuid4().hex[:8]}@example.com", unit_account_id="acc_x")

    resp = client.post("/ai/money-review", json={"monthly_income_dollars": 1200, "goals": "save for a laptop"},
                       headers=_auth_for(user_id))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["review"].startswith("BUDGET CHECK")
    assert body["transaction_count"] == len(FAKE_TXNS)
    assert body["ai_generated"] is True
    assert "not financial" in body["disclaimer"].lower()
    # Only spending is categorized — the payroll credit must not appear.
    assert "Income" not in body["category_totals"]
    assert body["category_totals"]["Coffee"] == 6.50
    assert body["category_totals"]["Groceries"] == 54.20


def test_review_pasted_data_mode_without_account(client, monkeypatch):
    _mock_review(monkeypatch)
    user_id = _make_user(f"rev_{uuid.uuid4().hex[:8]}@example.com", unit_account_id=None)
    resp = client.post("/ai/money-review", json={"pasted_data": "rent 800, food 300, income 1400"},
                       headers=_auth_for(user_id))
    assert resp.status_code == 200
    assert resp.json()["used_pasted_data"] is True
    assert resp.json()["transaction_count"] == 0


def test_review_400_with_no_data_at_all(client, monkeypatch):
    _mock_review(monkeypatch)
    user_id = _make_user(f"rev_{uuid.uuid4().hex[:8]}@example.com", unit_account_id=None)
    resp = client.post("/ai/money-review", json={}, headers=_auth_for(user_id))
    assert resp.status_code == 400


def test_review_503_when_model_unavailable(client, monkeypatch):
    _mock_review(monkeypatch, review=None)
    _mock_unit_txns(monkeypatch)
    user_id = _make_user(f"rev_{uuid.uuid4().hex[:8]}@example.com", unit_account_id="acc_y")
    resp = client.post("/ai/money-review", json={}, headers=_auth_for(user_id))
    assert resp.status_code == 503


def test_review_requires_auth(client):
    resp = client.post("/ai/money-review", json={})
    assert resp.status_code in (401, 403)
