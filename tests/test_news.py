"""Tests for /news — headlines categories, AI digest, and saved alerts.

All RSS/Anthropic network calls are monkeypatched — no test touches the
network. Users are created directly via the ORM (mirrors test_p2p.py's
pattern) to avoid the rate-limited /auth/register endpoint.
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
        user = User(email=email.lower(), hashed_password="x", full_name="News Reader", is_student=True)
        db.add(user)
        db.commit()
        db.refresh(user)
        return user.id
    finally:
        db.close()


def _token_for(user_id):
    expire = datetime.utcnow() + timedelta(minutes=settings.jwt_expire_minutes)
    return jwt.encode({"sub": user_id, "exp": expire}, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _auth(email=None):
    user_id = _make_user(email or f"news_{uuid.uuid4().hex[:8]}@example.com")
    return {"Authorization": f"Bearer {_token_for(user_id)}"}


FAKE_ARTICLES = [
    {"title": "Fed holds rates steady", "summary": "The Federal Reserve kept rates unchanged.", "source": "TestWire", "pub_date": "7/1 · 9:00 AM"},
    {"title": "Student loan servicers fined", "summary": "Regulators fined two loan servicers.", "source": "TestWire", "pub_date": "7/1 · 8:00 AM"},
]


def _mock_feeds(monkeypatch):
    async def fake_fetch(keywords=None, limit=30, category=None):
        return FAKE_ARTICLES[:limit]
    monkeypatch.setattr("services.claude.fetch_headlines", fake_fetch)
    # routers.news imports the module, calls via claude_svc.fetch_headlines
    monkeypatch.setattr("routers.news.claude_svc.fetch_headlines", fake_fetch)


# --- headlines / categories ---

def test_public_headlines_accepts_category(client, monkeypatch):
    _mock_feeds(monkeypatch)
    resp = client.get("/news/public-headlines", params={"category": "world"})
    assert resp.status_code == 200
    assert resp.json()["category"] == "world"


def test_invalid_category_rejected_422(client, monkeypatch):
    _mock_feeds(monkeypatch)
    resp = client.get("/news/public-headlines", params={"category": "sports"})
    assert resp.status_code == 422


def test_headlines_requires_auth(client):
    resp = client.get("/news/headlines")
    assert resp.status_code in (401, 403)


# --- AI digest ---

def test_digest_unavailable_without_anthropic_key(client, monkeypatch):
    _mock_feeds(monkeypatch)
    monkeypatch.setattr(settings, "anthropic_api_key", "ANTHROPIC_KEY_NOT_SET")
    resp = client.get("/news/digest", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["digest"] is None


def test_digest_returns_text_when_model_responds(client, monkeypatch):
    _mock_feeds(monkeypatch)

    async def fake_digest(articles, focus=None):
        return "- Rates unchanged means loan costs hold steady."
    monkeypatch.setattr("routers.news.claude_svc.generate_news_digest", fake_digest)

    resp = client.get("/news/digest", params={"q": "rates"}, headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is True
    assert "loan costs" in body["digest"]
    assert body["query"] == "rates"


# --- alerts ---

def test_alert_crud_lifecycle(client, monkeypatch):
    _mock_feeds(monkeypatch)
    headers = _auth()

    created = client.post("/news/alerts", json={"query": "student loans"}, headers=headers)
    assert created.status_code == 201, created.text
    alert_id = created.json()["id"]
    assert created.json()["query"] == "student loans"

    listed = client.get("/news/alerts", headers=headers)
    assert listed.status_code == 200
    assert len(listed.json()["alerts"]) == 1

    # Duplicate save returns the existing alert instead of stacking copies
    dupe = client.post("/news/alerts", json={"query": "student loans"}, headers=headers)
    assert dupe.json()["id"] == alert_id
    assert len(client.get("/news/alerts", headers=headers).json()["alerts"]) == 1

    deleted = client.delete(f"/news/alerts/{alert_id}", headers=headers)
    assert deleted.status_code == 204
    assert client.get("/news/alerts", headers=headers).json()["alerts"] == []


def test_alert_invalid_category_rejected(client):
    resp = client.post("/news/alerts", json={"query": "rates", "category": "sports"}, headers=_auth())
    assert resp.status_code == 422


def test_alert_cap_enforced(client):
    headers = _auth()
    for i in range(10):
        r = client.post("/news/alerts", json={"query": f"topic number {i}"}, headers=headers)
        assert r.status_code == 201
    over = client.post("/news/alerts", json={"query": "one too many"}, headers=headers)
    assert over.status_code == 400


def test_cannot_delete_someone_elses_alert(client):
    owner_headers = _auth()
    created = client.post("/news/alerts", json={"query": "tuition"}, headers=owner_headers)
    alert_id = created.json()["id"]

    other_headers = _auth()
    resp = client.delete(f"/news/alerts/{alert_id}", headers=other_headers)
    assert resp.status_code == 404


def test_check_alerts_returns_matches_and_stamps_time(client, monkeypatch):
    _mock_feeds(monkeypatch)
    headers = _auth()
    client.post("/news/alerts", json={"query": "student loans"}, headers=headers)

    resp = client.get("/news/alerts/check", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["alerts"]) == 1
    assert body["alerts"][0]["match_count"] == len(FAKE_ARTICLES)
    assert body["alerts"][0]["matches"][0]["title"] == "Fed holds rates steady"

    listed = client.get("/news/alerts", headers=headers)
    assert listed.json()["alerts"][0]["last_checked_at"] != ""
