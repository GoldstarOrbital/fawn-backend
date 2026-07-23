"""Tests for /ai/money-review — the 10-prompts-in-one-button feature.

FAWN has no linked bank transaction history, so this endpoint only supports
the pasted-data mode. All
Anthropic calls are mocked.
"""
import uuid
from datetime import datetime, timedelta

from jose import jwt

from database import SessionLocal
from models import User
from config import settings


def _make_user(email):
    db = SessionLocal()
    try:
        user = User(email=email.lower(), hashed_password="x", full_name="Review User",
                    is_student=True)
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


FAKE_REVIEW = "BUDGET CHECK\nYou spent 76 dollars this period.\n\nTHREE ADJUSTMENTS\n1. Example."


def _mock_review(monkeypatch, review=FAKE_REVIEW):
    async def fake_review(**kwargs):
        return review
    monkeypatch.setattr("routers.money_review.claude_svc.generate_money_review", fake_review)


def test_review_pasted_data_mode(client, monkeypatch):
    _mock_review(monkeypatch)
    user_id = _make_user(f"rev_{uuid.uuid4().hex[:8]}@example.com")
    resp = client.post("/ai/money-review", json={"pasted_data": "rent 800, food 300, income 1400"},
                       headers=_auth_for(user_id))
    assert resp.status_code == 200
    assert resp.json()["used_pasted_data"] is True
    assert resp.json()["transaction_count"] == 0


def test_review_400_with_no_data_at_all(client, monkeypatch):
    _mock_review(monkeypatch)
    user_id = _make_user(f"rev_{uuid.uuid4().hex[:8]}@example.com")
    resp = client.post("/ai/money-review", json={}, headers=_auth_for(user_id))
    assert resp.status_code == 400


def test_review_503_when_model_unavailable(client, monkeypatch):
    _mock_review(monkeypatch, review=None)
    user_id = _make_user(f"rev_{uuid.uuid4().hex[:8]}@example.com")
    resp = client.post("/ai/money-review", json={"pasted_data": "rent 800, food 300"},
                       headers=_auth_for(user_id))
    assert resp.status_code == 503


def test_review_requires_auth(client):
    resp = client.post("/ai/money-review", json={})
    assert resp.status_code in (401, 403)
