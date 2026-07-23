"""Tests for the most security-critical auth paths."""

import httpx

import routers.auth as auth_router


def _register_payload(email="Test@Example.COM", password="supersecret1"):
    return {
        "email": email,
        "password": password,
        "full_name": "Test Student",
        "phone": "5551234567",
        "is_student": True,
        "school": "berkeley",
        "location": "Berkeley, CA",
        "military_status": "none",
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


def test_register_collects_no_sensitive_kyc_fields(client):
    """Registration never asks for or stores SSN, date of birth, or address."""
    payload = _register_payload(email="nokyc@example.com")

    resp = client.post("/auth/register", json=payload)
    assert resp.status_code == 201
    token = resp.json()["access_token"]

    me_resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_resp.status_code == 200
    body = me_resp.json()
    assert body["wallet_initialized"] is False
    assert "ssn" not in body
    assert "date_of_birth" not in body
    assert "address" not in body


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
