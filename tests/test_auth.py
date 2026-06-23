"""Tests for the most security-critical auth paths."""


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
