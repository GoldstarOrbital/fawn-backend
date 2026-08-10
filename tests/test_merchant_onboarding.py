"""Tests for merchant onboarding: KYB gates, API keys, settlement config.

The cannabis-relevant invariants are the point of this file:
  - a high-risk merchant cannot submit without a valid, unexpired license
  - an expired license blocks submission, verification, and live keys
  - high-risk merchants are never auto-approved
  - API key plaintext is returned once and only the hash is stored
"""
import uuid
from datetime import datetime, timedelta, timezone

from database import SessionLocal
from models import CryptoWallet, MerchantAccount, User
from models_merchant import MerchantApiKey, MerchantKyb, hash_secret
from routers.merchant_onboarding import license_expiry_sweep

ADMIN = {"X-Admin-Key": "test-admin-key-12345"}


def _register(client, name="Green Leaf Owner"):
    email = f"m_{uuid.uuid4().hex[:10]}@example.com"
    r = client.post("/auth/register", json={
        "full_name": name, "email": email, "password": "Zx9-quantum-River-42", "is_student": False,
    })
    assert r.status_code in (200, 201), r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _merchant(client, hdrs, name="Green Leaf Dispensary"):
    """Create an active merchant with the custodial wallet the flow requires."""
    client.post("/auth/wallets/create", headers=hdrs)
    r = client.post("/closed-loop/merchants", headers=hdrs, json={
        "business_name": name, "display_name": name, "support_email": "ops@example.com",
    })
    assert r.status_code in (200, 201), r.text
    return r.json()


def _activate(merchant_id):
    db = SessionLocal()
    try:
        row = db.query(MerchantAccount).filter(MerchantAccount.id == merchant_id).first()
        row.status = "active"
        db.commit()
    finally:
        db.close()


def _kyb_body(**over):
    body = {
        "legal_business_name": "Green Leaf LLC",
        "entity_type": "llc",
        "ein": "123456789",
        "address_line1": "100 Main St",
        "city": "Denver",
        "state": "CO",
        "postal_code": "80202",
        "vertical": "cannabis",
        "state_license_number": "402R-00123",
        "state_license_state": "CO",
        "state_license_expires_on": (datetime.now(timezone.utc) + timedelta(days=200)).isoformat(),
        "beneficial_owners": [{"name": "Jane Roe", "title": "CEO", "ownership_percent": 100}],
        "attested_accurate": True,
        "attested_compliance": True,
    }
    body.update(over)
    return body


def test_cannabis_kyb_is_flagged_high_risk(client):
    h = _register(client); _merchant(client, h)
    r = client.post("/merchant/kyb", headers=h, json=_kyb_body())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["is_high_risk"] is True
    assert body["vertical"] == "cannabis"
    assert body["ein_last4"] == "6789"
    assert body["can_transact"] is False


def test_ein_is_never_stored_in_plaintext(client):
    h = _register(client); m = _merchant(client, h)
    client.post("/merchant/kyb", headers=h, json=_kyb_body())
    db = SessionLocal()
    try:
        row = db.query(MerchantKyb).filter(MerchantKyb.merchant_id == m["id"]).first()
        assert row.ein_hash == hash_secret("123456789")
        assert row.ein_last4 == "6789"
        # the full EIN must appear nowhere on the record
        assert "123456789" not in json_dump(row)
    finally:
        db.close()


def json_dump(row) -> str:
    return " ".join(str(getattr(row, c.name)) for c in row.__table__.columns)


def test_cannabis_cannot_submit_without_license(client):
    h = _register(client); _merchant(client, h)
    client.post("/merchant/kyb", headers=h, json=_kyb_body(
        state_license_number=None, state_license_state=None, state_license_expires_on=None))
    r = client.post("/merchant/kyb/submit", headers=h)
    assert r.status_code == 422
    assert "license" in r.json()["detail"].lower()


def test_expired_license_blocks_submission(client):
    h = _register(client); _merchant(client, h)
    client.post("/merchant/kyb", headers=h, json=_kyb_body(
        state_license_expires_on=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat()))
    r = client.post("/merchant/kyb/submit", headers=h)
    assert r.status_code == 422
    assert "expired" in r.json()["detail"].lower()


def test_submit_locks_record_from_edits(client):
    h = _register(client); _merchant(client, h)
    client.post("/merchant/kyb", headers=h, json=_kyb_body())
    assert client.post("/merchant/kyb/submit", headers=h).status_code == 200
    r = client.post("/merchant/kyb", headers=h, json=_kyb_body(legal_business_name="Sneaky Rename LLC"))
    assert r.status_code == 409


def test_high_risk_requires_human_decision_and_reaches_verified(client):
    h = _register(client); m = _merchant(client, h)
    client.post("/merchant/kyb", headers=h, json=_kyb_body())
    client.post("/merchant/kyb/submit", headers=h)

    q = client.get("/merchant/admin/review-queue", headers=ADMIN)
    assert q.status_code == 200
    assert any(row["merchant_id"] == m["id"] for row in q.json()["queue"])

    kyb_id = client.get("/merchant/kyb", headers=h).json()["id"]
    r = client.post(f"/merchant/admin/kyb/{kyb_id}/decide", headers=ADMIN,
                    json={"decision": "verified", "reviewer": "alex", "notes": "License checked on CO MED portal"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "verified"
    assert r.json()["can_transact"] is True


def test_review_queue_requires_admin_key(client):
    assert client.get("/merchant/admin/review-queue").status_code == 403


def test_live_key_requires_verified_kyb(client):
    h = _register(client); m = _merchant(client, h)
    _activate(m["id"])
    client.post("/merchant/kyb", headers=h, json=_kyb_body())
    r = client.post("/merchant/api-keys?mode=live", headers=h)
    assert r.status_code == 409
    # a test key is still available so integration work isn't blocked
    assert client.post("/merchant/api-keys?mode=test", headers=h).status_code == 201


def test_api_key_returned_once_and_only_hash_stored(client):
    h = _register(client); m = _merchant(client, h)
    r = client.post("/merchant/api-keys?mode=test&label=pos", headers=h)
    assert r.status_code == 201, r.text
    raw = r.json()["api_key"]
    assert raw.startswith("fawn_sk_test_")

    listed = client.get("/merchant/api-keys", headers=h).json()["keys"]
    assert all("api_key" not in k for k in listed)

    db = SessionLocal()
    try:
        row = db.query(MerchantApiKey).filter(MerchantApiKey.merchant_id == m["id"]).first()
        assert row.key_hash == hash_secret(raw)
        assert row.key_hash != raw
    finally:
        db.close()


def test_revoked_key_is_marked(client):
    h = _register(client); _merchant(client, h)
    key_id = client.post("/merchant/api-keys?mode=test", headers=h).json()["id"]
    assert client.delete(f"/merchant/api-keys/{key_id}", headers=h).status_code == 200
    listed = client.get("/merchant/api-keys", headers=h).json()["keys"]
    assert [k for k in listed if k["id"] == key_id][0]["revoked"] is True


def test_settlement_defaults_and_validation(client):
    h = _register(client); _merchant(client, h)
    d = client.get("/merchant/settlement", headers=h).json()
    assert d["method"] == "hold_usdc"

    bad = client.put("/merchant/settlement", headers=h,
                     json={"method": "auto_withdraw_usdc", "payout_address": "nope"})
    assert bad.status_code == 422

    ok = client.put("/merchant/settlement", headers=h, json={
        "method": "auto_withdraw_usdc", "payout_address": "0x" + "a" * 40,
        "payout_chain": "base", "min_payout_cents": 50_000})
    assert ok.status_code == 200
    assert ok.json()["payout_chain"] == "base"


def test_license_expiry_sweep_expires_verified_merchant(client):
    h = _register(client); m = _merchant(client, h)
    client.post("/merchant/kyb", headers=h, json=_kyb_body())
    client.post("/merchant/kyb/submit", headers=h)
    kyb_id = client.get("/merchant/kyb", headers=h).json()["id"]
    client.post(f"/merchant/admin/kyb/{kyb_id}/decide", headers=ADMIN,
                json={"decision": "verified", "reviewer": "alex"})

    db = SessionLocal()
    try:
        row = db.query(MerchantKyb).filter(MerchantKyb.id == kyb_id).first()
        row.state_license_expires_on = datetime.now(timezone.utc) - timedelta(days=2)
        db.commit()
        result = license_expiry_sweep(db)
        assert result["expired"] >= 1
        db.refresh(row)
        assert row.status == "expired"
    finally:
        db.close()

    assert client.get("/merchant/kyb", headers=h).json()["can_transact"] is False
