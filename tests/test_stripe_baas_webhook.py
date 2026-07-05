"""Tests for /stripe/baas-webhook — signature verification, idempotency,
account.updated handling (Stripe Connect + Treasury activation)."""
import hashlib
import hmac
import json
import time
import uuid

from database import SessionLocal
from models import User


def _create_pending_user(email, account_id):
    db = SessionLocal()
    try:
        user = User(
            email=email.lower(), hashed_password="x", full_name="Webhook Tester",
            is_student=True, stripe_account_id=account_id,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user.id
    finally:
        db.close()


def _account_updated_event(event_id, account_id, active=True):
    return {
        "id": event_id,
        "type": "account.updated",
        "data": {
            "object": {
                "id": account_id,
                "capabilities": {"treasury": "active" if active else "pending", "card_issuing": "active" if active else "pending"},
            }
        },
    }


def _sign(secret, body_bytes, timestamp=None):
    timestamp = timestamp or int(time.time())
    signed_payload = f"{timestamp}.{body_bytes.decode('utf-8')}".encode("utf-8")
    signature = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={signature}"


def _mock_create_financial_account(monkeypatch, financial_account_id="fa_from_webhook"):
    async def fake(account_id):
        return {"id": financial_account_id}
    monkeypatch.setattr("routers.stripe_baas_webhook.stripe_svc.create_financial_account", fake)


def test_webhook_without_secret_configured_processes_unverified(client, monkeypatch):
    import config
    monkeypatch.setattr(config.settings, "allow_unsigned_baas_webhooks", True)
    account_id = f"acct_{uuid.uuid4().hex[:8]}"
    user_id = _create_pending_user(f"hook1_{uuid.uuid4().hex[:8]}@example.com", account_id)

    _mock_create_financial_account(monkeypatch, "fa_from_webhook")

    event_id = f"evt_{uuid.uuid4().hex[:8]}"
    body = _account_updated_event(event_id, account_id)
    resp = client.post("/stripe/baas-webhook", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["duplicate"] is False

    db = SessionLocal()
    user = db.query(User).filter(User.id == user_id).first()
    db.close()
    assert user.stripe_financial_account_id == "fa_from_webhook"


def test_webhook_without_secret_rejects_by_default(client, monkeypatch):
    import config
    monkeypatch.setattr(config.settings, "stripe_baas_webhook_secret", "")
    monkeypatch.setattr(config.settings, "allow_unsigned_baas_webhooks", False)

    body = _account_updated_event(f"evt_{uuid.uuid4().hex[:8]}", "acct_x")
    resp = client.post("/stripe/baas-webhook", json=body)
    assert resp.status_code == 503


def test_webhook_duplicate_event_id_not_reprocessed(client, monkeypatch):
    import config
    monkeypatch.setattr(config.settings, "allow_unsigned_baas_webhooks", True)
    account_id = f"acct_{uuid.uuid4().hex[:8]}"
    _create_pending_user(f"hook2_{uuid.uuid4().hex[:8]}@example.com", account_id)

    call_count = {"n": 0}

    async def fake_create_financial_account(acct_id):
        call_count["n"] += 1
        return {"id": "fa_dup_test"}
    monkeypatch.setattr("routers.stripe_baas_webhook.stripe_svc.create_financial_account", fake_create_financial_account)

    event_id = f"evt_{uuid.uuid4().hex[:8]}"
    body = _account_updated_event(event_id, account_id)

    first = client.post("/stripe/baas-webhook", json=body)
    second = client.post("/stripe/baas-webhook", json=body)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert call_count["n"] == 1  # never double-processed


def test_webhook_with_secret_rejects_bad_signature(client, monkeypatch):
    import config
    monkeypatch.setattr(config.settings, "stripe_baas_webhook_secret", "test_baas_webhook_secret")

    body = _account_updated_event(f"evt_{uuid.uuid4().hex[:8]}", "acct_x")
    resp = client.post(
        "/stripe/baas-webhook",
        data=json.dumps(body),
        headers={"Content-Type": "application/json", "stripe-signature": "t=1,v1=totally-wrong-signature"},
    )
    assert resp.status_code == 400


def test_webhook_skips_if_capability_not_actually_active(client, monkeypatch):
    """Even if the event claims account.updated, the handler must not
    finish account setup unless capabilities.treasury is actually active."""
    import config
    monkeypatch.setattr(config.settings, "allow_unsigned_baas_webhooks", True)
    account_id = f"acct_{uuid.uuid4().hex[:8]}"
    user_id = _create_pending_user(f"hooknotreally_{uuid.uuid4().hex[:8]}@example.com", account_id)

    async def fake_create_financial_account(acct_id):
        raise AssertionError("should never be called")
    monkeypatch.setattr("routers.stripe_baas_webhook.stripe_svc.create_financial_account", fake_create_financial_account)

    body = _account_updated_event(f"evt_{uuid.uuid4().hex[:8]}", account_id, active=False)
    resp = client.post("/stripe/baas-webhook", json=body)
    assert resp.status_code == 200

    db = SessionLocal()
    user = db.query(User).filter(User.id == user_id).first()
    db.close()
    assert user.stripe_financial_account_id is None


def test_webhook_with_secret_accepts_correct_signature(client, monkeypatch):
    import config
    secret = "test_baas_webhook_secret_2"
    monkeypatch.setattr(config.settings, "stripe_baas_webhook_secret", secret)

    _mock_create_financial_account(monkeypatch, "fa_sig_ok")

    account_id = f"acct_{uuid.uuid4().hex[:8]}"
    _create_pending_user(f"hook3_{uuid.uuid4().hex[:8]}@example.com", account_id)

    body_bytes = json.dumps(_account_updated_event(f"evt_{uuid.uuid4().hex[:8]}", account_id)).encode()
    signature = _sign(secret, body_bytes)

    resp = client.post(
        "/stripe/baas-webhook",
        data=body_bytes,
        headers={"Content-Type": "application/json", "stripe-signature": signature},
    )
    assert resp.status_code == 200, resp.text
