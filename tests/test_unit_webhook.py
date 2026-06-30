"""Tests for /unit/webhook — signature verification, idempotency,
application.approved handling."""
import base64
import hashlib
import hmac
import json
import uuid

from database import SessionLocal
from models import User


def _create_pending_user(email, application_id):
    db = SessionLocal()
    try:
        user = User(
            email=email.lower(), hashed_password="x", full_name="Webhook Tester",
            is_student=True, unit_application_id=application_id,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user.id
    finally:
        db.close()


def _approved_event(event_id, application_id, customer_id):
    return {
        "data": [{
            "id": event_id,
            "type": "application.approved",
            "attributes": {"createdAt": "2026-01-01T00:00:00Z"},
            "relationships": {
                "application": {"data": {"type": "individualApplication", "id": application_id}},
                "customer": {"data": {"type": "individualCustomer", "id": customer_id}},
            },
        }]
    }


def _sign(secret, body_bytes):
    return base64.b64encode(hmac.new(secret.encode(), body_bytes, hashlib.sha1).digest()).decode()


def _mock_get_application(monkeypatch, customer_id, status="approved"):
    """The handler re-fetches the application from Unit directly rather than
    trusting the webhook body's customer_id — see routers/unit_webhook.py."""
    async def fake_get_application(application_id):
        return {
            "id": application_id,
            "attributes": {"status": status},
            "relationships": {"customer": {"data": {"id": customer_id}}},
        }
    monkeypatch.setattr("routers.unit_webhook.unit_svc.get_application", fake_get_application)


def test_webhook_without_secret_configured_processes_unverified(client, monkeypatch):
    import config
    monkeypatch.setattr(config.settings, "allow_unsigned_unit_webhooks", True)
    application_id = f"app_{uuid.uuid4().hex[:8]}"
    customer_id = f"cust_{uuid.uuid4().hex[:8]}"
    user_id = _create_pending_user(f"hook1_{uuid.uuid4().hex[:8]}@example.com", application_id)

    _mock_get_application(monkeypatch, customer_id)

    async def fake_create_deposit_account(cust_id):
        return {"id": "acc_from_webhook"}
    monkeypatch.setattr("routers.unit_webhook.unit_svc.create_deposit_account", fake_create_deposit_account)

    event_id = f"evt_{uuid.uuid4().hex[:8]}"
    body = _approved_event(event_id, application_id, customer_id)
    resp = client.post("/unit/webhook", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.json()["events"][0]["duplicate"] is False

    db = SessionLocal()
    user = db.query(User).filter(User.id == user_id).first()
    db.close()
    assert user.unit_account_id == "acc_from_webhook"
    assert user.unit_customer_id == customer_id


def test_webhook_without_secret_rejects_by_default(client, monkeypatch):
    import config
    monkeypatch.setattr(config.settings, "unit_webhook_secret", "")
    monkeypatch.setattr(config.settings, "allow_unsigned_unit_webhooks", False)

    body = _approved_event(f"evt_{uuid.uuid4().hex[:8]}", "app_x", "cust_x")
    resp = client.post("/unit/webhook", json=body)
    assert resp.status_code == 503


def test_webhook_duplicate_event_id_not_reprocessed(client, monkeypatch):
    import config
    monkeypatch.setattr(config.settings, "allow_unsigned_unit_webhooks", True)
    application_id = f"app_{uuid.uuid4().hex[:8]}"
    customer_id = f"cust_{uuid.uuid4().hex[:8]}"
    _create_pending_user(f"hook2_{uuid.uuid4().hex[:8]}@example.com", application_id)
    _mock_get_application(monkeypatch, customer_id)

    call_count = {"n": 0}

    async def fake_create_deposit_account(cust_id):
        call_count["n"] += 1
        return {"id": "acc_dup_test"}
    monkeypatch.setattr("routers.unit_webhook.unit_svc.create_deposit_account", fake_create_deposit_account)

    event_id = f"evt_{uuid.uuid4().hex[:8]}"
    body = _approved_event(event_id, application_id, customer_id)

    first = client.post("/unit/webhook", json=body)
    second = client.post("/unit/webhook", json=body)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["events"][0]["duplicate"] is True
    assert call_count["n"] == 1  # never double-processed


def test_webhook_with_secret_rejects_bad_signature(client, monkeypatch):
    import config
    monkeypatch.setattr(config.settings, "unit_webhook_secret", "test_webhook_secret")

    body = _approved_event(f"evt_{uuid.uuid4().hex[:8]}", "app_x", "cust_x")
    resp = client.post(
        "/unit/webhook",
        data=json.dumps(body),
        headers={"Content-Type": "application/json", "x-unit-signature": "totally-wrong-signature"},
    )
    assert resp.status_code == 400


def test_webhook_ignores_forged_customer_id_in_body(client, monkeypatch):
    """The handler must use the customer_id from Unit's own API response,
    not whatever the request body claims — otherwise an unauthenticated
    caller (when unsigned local webhook testing is explicitly enabled) could link an arbitrary
    customer/account id to a victim's FAWN account."""
    import config
    monkeypatch.setattr(config.settings, "allow_unsigned_unit_webhooks", True)
    application_id = f"app_{uuid.uuid4().hex[:8]}"
    real_customer_id = f"cust_real_{uuid.uuid4().hex[:8]}"
    forged_customer_id = f"cust_FORGED_{uuid.uuid4().hex[:8]}"
    user_id = _create_pending_user(f"hookforge_{uuid.uuid4().hex[:8]}@example.com", application_id)

    _mock_get_application(monkeypatch, real_customer_id)

    seen_customer_ids = []

    async def fake_create_deposit_account(cust_id):
        seen_customer_ids.append(cust_id)
        return {"id": "acc_forged_test"}
    monkeypatch.setattr("routers.unit_webhook.unit_svc.create_deposit_account", fake_create_deposit_account)

    body = _approved_event(f"evt_{uuid.uuid4().hex[:8]}", application_id, forged_customer_id)
    resp = client.post("/unit/webhook", json=body)
    assert resp.status_code == 200, resp.text

    assert seen_customer_ids == [real_customer_id]  # forged id never reached create_deposit_account

    db = SessionLocal()
    user = db.query(User).filter(User.id == user_id).first()
    db.close()
    assert user.unit_customer_id == real_customer_id


def test_webhook_skips_if_unit_says_not_actually_approved(client, monkeypatch):
    """Even if the request body claims type=application.approved, the
    handler must not finish account setup unless Unit's own API confirms
    the application status is actually approved."""
    import config
    monkeypatch.setattr(config.settings, "allow_unsigned_unit_webhooks", True)
    application_id = f"app_{uuid.uuid4().hex[:8]}"
    customer_id = f"cust_{uuid.uuid4().hex[:8]}"
    user_id = _create_pending_user(f"hooknotreally_{uuid.uuid4().hex[:8]}@example.com", application_id)

    _mock_get_application(monkeypatch, customer_id, status="pendingReview")

    async def fake_create_deposit_account(cust_id):
        raise AssertionError("should never be called")
    monkeypatch.setattr("routers.unit_webhook.unit_svc.create_deposit_account", fake_create_deposit_account)

    body = _approved_event(f"evt_{uuid.uuid4().hex[:8]}", application_id, customer_id)
    resp = client.post("/unit/webhook", json=body)
    assert resp.status_code == 200

    db = SessionLocal()
    user = db.query(User).filter(User.id == user_id).first()
    db.close()
    assert user.unit_account_id is None


def test_webhook_with_secret_accepts_correct_signature(client, monkeypatch):
    import config
    secret = "test_webhook_secret_2"
    monkeypatch.setattr(config.settings, "unit_webhook_secret", secret)

    async def fake_create_deposit_account(cust_id):
        return {"id": "acc_sig_ok"}
    monkeypatch.setattr("routers.unit_webhook.unit_svc.create_deposit_account", fake_create_deposit_account)

    application_id = f"app_{uuid.uuid4().hex[:8]}"
    customer_id = f"cust_{uuid.uuid4().hex[:8]}"
    _create_pending_user(f"hook3_{uuid.uuid4().hex[:8]}@example.com", application_id)
    _mock_get_application(monkeypatch, customer_id)

    body_bytes = json.dumps(_approved_event(f"evt_{uuid.uuid4().hex[:8]}", application_id, customer_id)).encode()
    signature = _sign(secret, body_bytes)

    resp = client.post(
        "/unit/webhook",
        data=body_bytes,
        headers={"Content-Type": "application/json", "x-unit-signature": signature},
    )
    assert resp.status_code == 200, resp.text
