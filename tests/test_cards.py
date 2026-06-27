"""Tests for /cards — create/list/freeze/unfreeze, ownership checks."""
import uuid

from datetime import datetime, timedelta
from jose import jwt

from database import SessionLocal
from models import User
from config import settings


def _create_active_user(email, unit_account_id="acc_test123"):
    db = SessionLocal()
    try:
        user = User(
            email=email.lower(), hashed_password="x", full_name="Card Tester",
            is_student=True, unit_account_id=unit_account_id,
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


def _mock_unit_card_calls(monkeypatch, card_id="card_fake1"):
    async def fake_create(account_id, idempotency_key):
        return {"id": card_id, "attributes": {"last4Digits": "1234", "expirationDate": "0930", "status": "Active", "createdAt": "2026-01-01T00:00:00Z"}}

    async def fake_get(unit_card_id):
        return {"id": unit_card_id, "last4Digits": "1234", "expirationDate": "0930", "status": "Active", "createdAt": "2026-01-01T00:00:00Z"}

    async def fake_freeze(unit_card_id, reason="userRequested"):
        return {"id": unit_card_id, "last4Digits": "1234", "expirationDate": "0930", "status": "Frozen", "createdAt": "2026-01-01T00:00:00Z"}

    async def fake_unfreeze(unit_card_id):
        return {"id": unit_card_id, "last4Digits": "1234", "expirationDate": "0930", "status": "Active", "createdAt": "2026-01-01T00:00:00Z"}

    monkeypatch.setattr("routers.cards.unit_svc.create_virtual_card", fake_create)
    monkeypatch.setattr("routers.cards.unit_svc.get_card", fake_get)
    monkeypatch.setattr("routers.cards.unit_svc.freeze_card", fake_freeze)
    monkeypatch.setattr("routers.cards.unit_svc.unfreeze_card", fake_unfreeze)


def test_create_card_without_active_account_400(client):
    user_id = _create_active_user(f"noacct_{uuid.uuid4().hex[:8]}@example.com", unit_account_id=None)
    resp = client.post("/cards", headers=_auth(_token_for(user_id)))
    assert resp.status_code == 400


def test_create_card_happy_path_then_duplicate_409(client, monkeypatch):
    _mock_unit_card_calls(monkeypatch, card_id=f"card_{uuid.uuid4().hex[:8]}")
    user_id = _create_active_user(f"cardholder_{uuid.uuid4().hex[:8]}@example.com")
    token = _token_for(user_id)

    first = client.post("/cards", headers=_auth(token))
    assert first.status_code == 201, first.text
    assert first.json()["last4_digits"] == "1234"

    second = client.post("/cards", headers=_auth(token))
    assert second.status_code == 409


def test_list_then_freeze_then_unfreeze(client, monkeypatch):
    card_id = f"card_{uuid.uuid4().hex[:8]}"
    _mock_unit_card_calls(monkeypatch, card_id=card_id)
    user_id = _create_active_user(f"freezer_{uuid.uuid4().hex[:8]}@example.com")
    token = _token_for(user_id)

    create = client.post("/cards", headers=_auth(token))
    assert create.status_code == 201

    listed = client.get("/cards", headers=_auth(token))
    assert listed.status_code == 200
    assert len(listed.json()["cards"]) == 1

    frozen = client.post(f"/cards/{card_id}/freeze", json={"reason": "lost"}, headers=_auth(token))
    assert frozen.status_code == 200
    assert frozen.json()["status"] == "Frozen"

    unfrozen = client.post(f"/cards/{card_id}/unfreeze", json={}, headers=_auth(token))
    assert unfrozen.status_code == 200
    assert unfrozen.json()["status"] == "Active"


def test_list_skips_card_on_unit_error_and_logs(client, monkeypatch, capsys):
    card_id = f"card_{uuid.uuid4().hex[:8]}"
    _mock_unit_card_calls(monkeypatch, card_id=card_id)
    user_id = _create_active_user(f"flaky_{uuid.uuid4().hex[:8]}@example.com")
    token = _token_for(user_id)

    create = client.post("/cards", headers=_auth(token))
    assert create.status_code == 201

    async def fake_get_failing(unit_card_id):
        raise RuntimeError("Unit API timeout")

    monkeypatch.setattr("routers.cards.unit_svc.get_card", fake_get_failing)

    listed = client.get("/cards", headers=_auth(token))
    assert listed.status_code == 200
    assert listed.json()["cards"] == []

    captured = capsys.readouterr()
    assert card_id in captured.out
    assert "Unit API timeout" in captured.out


def test_freeze_someone_elses_card_404(client, monkeypatch):
    card_id = f"card_{uuid.uuid4().hex[:8]}"
    _mock_unit_card_calls(monkeypatch, card_id=card_id)

    owner_id = _create_active_user(f"owner_{uuid.uuid4().hex[:8]}@example.com")
    client.post("/cards", headers=_auth(_token_for(owner_id)))

    other_id = _create_active_user(f"other_{uuid.uuid4().hex[:8]}@example.com")
    resp = client.post(f"/cards/{card_id}/freeze", json={"reason": "lost"}, headers=_auth(_token_for(other_id)))
    assert resp.status_code == 404
