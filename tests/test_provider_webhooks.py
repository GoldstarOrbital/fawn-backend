"""Tests for the Column and Lithic webhook receivers.

Covers the unsigned-guard (503 when no secret and not explicitly allowed),
the allow-unsigned dev path, and idempotent dedupe on event id.
"""
import hashlib
import hmac
import json
import uuid

from config import settings


def test_column_webhook_rejects_when_unconfigured(client):
    resp = client.post("/column/webhook", json={"id": "evt_1", "type": "ach.credit.completed"})
    assert resp.status_code == 503


def test_column_webhook_allow_unsigned_and_dedupe(client, monkeypatch):
    monkeypatch.setattr(settings, "allow_unsigned_column_webhooks", True)
    evt_id = f"evt_{uuid.uuid4().hex[:8]}"
    body = {"id": evt_id, "type": "book.transfer.completed"}

    first = client.post("/column/webhook", json=body)
    assert first.status_code == 200, first.text
    assert first.json()["duplicate"] is False

    second = client.post("/column/webhook", json=body)
    assert second.status_code == 200
    assert second.json()["duplicate"] is True


def test_column_webhook_valid_signature(client, monkeypatch):
    secret = "col_whsec_test"
    monkeypatch.setattr(settings, "column_webhook_secret", secret)
    evt_id = f"evt_{uuid.uuid4().hex[:8]}"
    payload = json.dumps({"id": evt_id, "type": "ach.credit.completed"}).encode()
    sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    resp = client.post("/column/webhook", content=payload,
                       headers={"Content-Type": "application/json", "column-signature": f"sha256={sig}"})
    assert resp.status_code == 200, resp.text


def test_column_webhook_bad_signature_400(client, monkeypatch):
    monkeypatch.setattr(settings, "column_webhook_secret", "col_whsec_test")
    resp = client.post("/column/webhook", json={"id": "x", "type": "y"},
                       headers={"column-signature": "sha256=deadbeef"})
    assert resp.status_code == 400


def test_lithic_webhook_rejects_when_unconfigured(client):
    resp = client.post("/lithic/webhook", json={"token": "tok_1", "type": "card.created"})
    assert resp.status_code == 503


def test_lithic_webhook_allow_unsigned_and_dedupe(client, monkeypatch):
    monkeypatch.setattr(settings, "allow_unsigned_lithic_webhooks", True)
    evt_id = f"tok_{uuid.uuid4().hex[:8]}"
    body = {"token": evt_id, "type": "authorization.request"}

    first = client.post("/lithic/webhook", json=body)
    assert first.status_code == 200, first.text
    assert first.json()["duplicate"] is False

    second = client.post("/lithic/webhook", json=body)
    assert second.json()["duplicate"] is True
