from tests.test_auth import _register_payload


def test_unit_application_form_prefill_requires_auth(client):
    resp = client.get("/unit/application-form-prefill")
    assert resp.status_code == 401


def test_unit_application_form_prefill_uses_current_user(client):
    payload = _register_payload(email="unitprefill@example.com")
    payload.pop("ssn")
    payload.pop("date_of_birth")
    payload.pop("address")
    payload["full_name"] = "Maria Student"
    payload["phone"] = "(555) 777-1212"
    payload["school"] = "stanford"
    payload["military_status"] = "none"

    register_resp = client.post("/auth/register", json=payload)
    assert register_resp.status_code == 201
    token = register_resp.json()["access_token"]

    resp = client.get(
        "/unit/application-form-prefill",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    attrs = data["attributes"]

    assert data["type"] == "whiteLabelAppEndUserConfig"
    assert attrs["applicationFormPrefill"]["fullName"] == {
        "first": "Maria",
        "last": "Student",
    }
    assert attrs["applicationFormPrefill"]["email"] == "unitprefill@example.com"
    assert attrs["applicationFormPrefill"]["phone"] == {
        "countryCode": "1",
        "number": "5557771212",
    }
    assert attrs["applicationFormPrefill"]["occupation"] == "Student"
    assert attrs["applicationFormSettingsOverride"]["idempotencyKey"].startswith("fawn-user-")
    assert attrs["applicationFormSettingsOverride"]["tags"]["school"] == "stanford"


def test_create_application_form_returns_unit_url(client, monkeypatch):
    monkeypatch.setattr("routers.unit_onboarding.settings.unit_api_token", "unit-test-token")

    payload = _register_payload(email="unitform@example.com")
    payload.pop("ssn")
    payload.pop("date_of_birth")
    payload.pop("address")
    register_resp = client.post("/auth/register", json=payload)
    assert register_resp.status_code == 201
    token = register_resp.json()["access_token"]

    seen = {}

    async def fake_create_application_form(**kwargs):
        seen.update(kwargs)
        return {
            "id": "form_123",
            "links": {"related": {"href": "https://unit.test/application-form/form_123"}},
        }

    monkeypatch.setattr("routers.unit_onboarding.unit_svc.create_application_form", fake_create_application_form)

    resp = client.post("/unit/application-form", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    assert resp.json() == {
        "application_form_id": "form_123",
        "application_form_url": "https://unit.test/application-form/form_123",
    }
    assert seen["email"] == "unitform@example.com"
    assert seen["school"] == "berkeley"


def test_create_application_form_requires_unit_token(client, monkeypatch):
    monkeypatch.setattr("routers.unit_onboarding.settings.unit_api_token", "UNIT_TOKEN_NOT_SET")

    payload = _register_payload(email="unitformmissingtoken@example.com")
    payload.pop("ssn")
    payload.pop("date_of_birth")
    payload.pop("address")
    register_resp = client.post("/auth/register", json=payload)
    assert register_resp.status_code == 201
    token = register_resp.json()["access_token"]

    resp = client.post("/unit/application-form", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 503
