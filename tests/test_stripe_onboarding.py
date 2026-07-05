"""Tests for POST /stripe/onboarding — hosted Connect + Account Link KYC flow."""
from database import SessionLocal
from models import User
from tests.test_auth import _register_payload


def test_stripe_onboarding_requires_auth(client):
    resp = client.post("/stripe/onboarding")
    assert resp.status_code in (401, 403)


def test_create_onboarding_returns_stripe_url(client, monkeypatch):
    payload = _register_payload(email="stripeform@example.com")
    payload.pop("ssn")
    payload.pop("date_of_birth")
    payload.pop("address")
    register_resp = client.post("/auth/register", json=payload)
    assert register_resp.status_code == 201
    token = register_resp.json()["access_token"]

    import config
    monkeypatch.setattr(config.settings, "stripe_secret_key", "sk_test_fake")

    seen = {}

    async def fake_create_connect_account_stub(**kwargs):
        seen.update(kwargs)
        return {"id": "acct_form_123"}

    async def fake_create_account_onboarding_link(account_id, refresh_url, return_url):
        return {"url": f"https://connect.stripe.com/setup/e/{account_id}/fake"}

    monkeypatch.setattr("routers.stripe_onboarding.stripe_svc.create_connect_account_stub", fake_create_connect_account_stub)
    monkeypatch.setattr("routers.stripe_onboarding.stripe_svc.create_account_onboarding_link", fake_create_account_onboarding_link)

    resp = client.post("/stripe/onboarding", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["stripe_account_id"] == "acct_form_123"
    assert body["onboarding_url"] == "https://connect.stripe.com/setup/e/acct_form_123/fake"
    assert seen["email"] == "stripeform@example.com"

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == "stripeform@example.com").first()
        assert user.stripe_account_id == "acct_form_123"
    finally:
        db.close()


def test_create_onboarding_requires_stripe_key(client, monkeypatch):
    import config
    monkeypatch.setattr(config.settings, "stripe_secret_key", "")

    payload = _register_payload(email="stripeformmissingkey@example.com")
    payload.pop("ssn")
    payload.pop("date_of_birth")
    payload.pop("address")
    register_resp = client.post("/auth/register", json=payload)
    assert register_resp.status_code == 201
    token = register_resp.json()["access_token"]

    resp = client.post("/stripe/onboarding", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 503


def test_create_onboarding_rejects_already_active_account(client, monkeypatch):
    import config
    monkeypatch.setattr(config.settings, "stripe_secret_key", "sk_test_fake")

    payload = _register_payload(email="alreadyactive@example.com")
    payload.pop("ssn")
    payload.pop("date_of_birth")
    payload.pop("address")
    register_resp = client.post("/auth/register", json=payload)
    token = register_resp.json()["access_token"]

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == "alreadyactive@example.com").first()
        user.stripe_financial_account_id = "fa_already_active"
        db.commit()
    finally:
        db.close()

    resp = client.post("/stripe/onboarding", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 409
