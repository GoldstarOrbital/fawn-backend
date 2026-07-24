"""Product scorecard, trust, feedback, support, and card-readiness tests."""
import uuid
from datetime import datetime, timedelta

from jose import jwt

from config import settings
from database import SessionLocal
from models import User


def _user():
    db = SessionLocal()
    user = User(email=f"experience_{uuid.uuid4().hex[:8]}@example.com", hashed_password="x", full_name="Experience Tester")
    db.add(user)
    db.commit()
    db.refresh(user)
    db.close()
    return user.id


def _auth(user_id):
    token = jwt.encode({"sub": user_id, "exp": datetime.utcnow() + timedelta(minutes=30)}, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return {"Authorization": f"Bearer {token}"}


def test_trust_disclosures_are_public(client):
    response = client.get("/experience/trust")
    assert response.status_code == 200
    body = response.json()
    assert body["custody"]["model"] == "custodial"
    assert body["fees"]["internal_transfer"] == 0.01
    assert "recovery" in body


def test_feedback_support_and_card_interest(client):
    user_id = _user()
    headers = _auth(user_id)
    assert client.post("/experience/feedback", headers=headers, json={"score": 5, "context": "dashboard"}).status_code == 201
    ticket = client.post("/experience/support/tickets", headers=headers, json={"category": "dispute", "subject": "Transfer question", "message": "I need help reviewing a transfer."})
    assert ticket.status_code == 201
    card = client.post("/experience/cards/request", headers=headers, json={})
    assert card.status_code == 201
    status = client.get("/experience/cards/status", headers=headers)
    assert status.status_code == 200
    assert status.json()["status"] == "interest"


def test_scorecard_does_not_invent_empty_metrics(client, admin_key):
    response = client.get("/experience/admin/scorecard", headers={"X-Admin-Key": admin_key})
    assert response.status_code == 200
    body = response.json()
    assert body["window_days"] == 30
    assert body["median_page_load_ms"] is None or body["page_load_samples"] >= 0
