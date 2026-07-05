"""Tests for the most security-critical auth paths."""

import httpx

import routers.auth as auth_router


def _register_payload(email="Test@Example.COM", password="supersecret1"):
    return {
        "email": email,
        "password": password,
        "full_name": "Test Student",
        "phone": "5551234567",
        "date_of_birth": "2000-01-01",
        "ssn": "123456789",
        "address": {
            "street": "1 Main St",
            "city": "Berkeley",
            "state": "CA",
            "postal_code": "94720",
            "country": "US",
        },
        "is_student": True,
        "occupation": "Student",
        "school": "berkeley",
        "location": "Berkeley, CA",
        "military_status": "none",
        "is_us_citizen": True,
    }


def test_register_then_login_with_different_email_case(client):
    """Guards a real bug: case-sensitive email matching broke login when the
    user registered with a different case than they later logged in with."""
    register_resp = client.post("/auth/register", json=_register_payload(email="Test@Example.COM"))
    assert register_resp.status_code == 201

    login_resp = client.post(
        "/auth/login",
        json={"email": "test@example.com", "password": "supersecret1"},
    )
    assert login_resp.status_code == 200
    assert "access_token" in login_resp.json()


def test_register_with_short_password_fails_422(client):
    payload = _register_payload(email="shortpw@example.com", password="short1")
    resp = client.post("/auth/register", json=payload)
    assert resp.status_code == 422


def test_duplicate_email_registration_fails_400(client):
    payload = _register_payload(email="dupe@example.com")
    first = client.post("/auth/register", json=payload)
    assert first.status_code == 201

    second = client.post("/auth/register", json=_register_payload(email="DUPE@example.com"))
    assert second.status_code == 400


def test_register_with_ssn_but_not_us_citizen_is_rejected_403(client):
    """FAWN banking is U.S.-citizens-only: submitting an SSN/KYC payload without
    attesting citizenship must be refused before any SSN could reach Stripe, and
    no user row may be created."""
    payload = _register_payload(email="noncitizen@example.com")
    payload["is_us_citizen"] = False

    resp = client.post("/auth/register", json=payload)
    assert resp.status_code == 403
    assert "citizen" in resp.json()["detail"].lower()

    # The rejected registration must not have created an account.
    login_resp = client.post(
        "/auth/login",
        json={"email": "noncitizen@example.com", "password": "supersecret1"},
    )
    assert login_resp.status_code == 401


def test_register_with_ssn_defaults_to_non_citizen_and_is_rejected_403(client):
    """Omitting is_us_citizen entirely defaults to False — a KYC payload must not
    slip through the citizenship gate just because the flag was left off."""
    payload = _register_payload(email="missingflag@example.com")
    payload.pop("is_us_citizen")

    resp = client.post("/auth/register", json=payload)
    assert resp.status_code == 403


def test_register_citizen_with_non_us_address_is_rejected_403(client):
    """A citizenship attestation with a non-U.S. address is contradictory and
    can't open a Stripe Treasury account — reject before submitting KYC."""
    payload = _register_payload(email="foreignaddr@example.com")
    payload["is_us_citizen"] = True
    payload["address"]["country"] = "CA"

    resp = client.post("/auth/register", json=payload)
    assert resp.status_code == 403
    assert "u.s. address" in resp.json()["detail"].lower()


def test_register_us_citizen_stores_attestation(client):
    """A valid U.S.-citizen KYC registration succeeds and records the
    attestation on the user for the compliance audit trail."""
    from database import SessionLocal
    from models import User

    payload = _register_payload(email="citizen@example.com")
    resp = client.post("/auth/register", json=payload)
    assert resp.status_code == 201

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == "citizen@example.com").first()
        assert user.is_us_citizen is True
    finally:
        db.close()


def test_register_without_sensitive_kyc_fields_for_hosted_stripe_onboarding(client):
    payload = _register_payload(email="hostedkyc@example.com")
    payload.pop("ssn")
    payload.pop("date_of_birth")
    payload.pop("address")

    resp = client.post("/auth/register", json=payload)
    assert resp.status_code == 201
    token = resp.json()["access_token"]

    me_resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_resp.status_code == 200
    body = me_resp.json()
    assert body["account_active"] is False
    assert body["application_pending"] is False
    assert body["stripe_onboarding_ready"] is True


def test_patch_me_updates_school(client):
    payload = _register_payload(email="patchme@example.com")
    register_resp = client.post("/auth/register", json=payload)
    assert register_resp.status_code == 201
    token = register_resp.json()["access_token"]

    headers = {"Authorization": f"Bearer {token}"}
    patch_resp = client.patch("/auth/me", json={"school": "stanford"}, headers=headers)
    assert patch_resp.status_code == 200
    assert patch_resp.json()["school"] == "stanford"

    get_resp = client.get("/auth/me", headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["school"] == "stanford"


def test_register_and_patch_me_preserves_personalization_fields(client):
    payload = _register_payload(email="personalized@example.com")
    payload["military_status"] = "military_veteran_or_rotc"
    register_resp = client.post("/auth/register", json=payload)
    assert register_resp.status_code == 201
    token = register_resp.json()["access_token"]

    headers = {"Authorization": f"Bearer {token}"}
    me_resp = client.get("/auth/me", headers=headers)
    assert me_resp.status_code == 200
    assert me_resp.json()["school"] == "berkeley"
    assert me_resp.json()["location"] == "Berkeley, CA"
    assert me_resp.json()["military_status"] == "military_veteran_or_rotc"

    patch_resp = client.patch(
        "/auth/me",
        json={
            "school": "stanford",
            "location": "Palo Alto, CA",
            "military_status": "none",
        },
        headers=headers,
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["school"] == "stanford"
    assert patch_resp.json()["location"] == "Palo Alto, CA"
    assert patch_resp.json()["military_status"] == "none"


def test_send_reset_email_logs_on_non_2xx_status(monkeypatch, capsys):
    """Regression guard: a non-2xx response from Resend for the password-reset
    email must be logged with the status code and response body, not silently
    swallowed. This is the same silent-failure pattern as the waitlist bug,
    but here it hides failures in account recovery."""
    monkeypatch.setattr(auth_router.settings, "resend_api_key", "test-key")
    monkeypatch.setenv("RESEND_API_KEY", "test-key")

    class FakeResponse:
        status_code = 422
        text = "from_email not verified"

    def fake_post(*args, **kwargs):
        return FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)

    result = auth_router._send_reset_email("student@example.com", "raw-token-123")

    assert result is False
    captured = capsys.readouterr()
    assert "422" in captured.out
    assert "student@example.com" in captured.out


def test_send_reset_email_logs_on_exception(monkeypatch, capsys):
    """Regression guard: an exception raised while calling Resend for the
    password-reset email must be logged, not silently swallowed."""
    monkeypatch.setattr(auth_router.settings, "resend_api_key", "test-key")
    monkeypatch.setenv("RESEND_API_KEY", "test-key")

    def fake_post(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", fake_post)

    result = auth_router._send_reset_email("student@example.com", "raw-token-123")

    assert result is False
    captured = capsys.readouterr()
    assert "student@example.com" in captured.out
    assert "connection refused" in captured.out
