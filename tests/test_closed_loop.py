"""Closed-loop card, merchant enrollment, checkout, and tap settlement tests."""
from datetime import datetime, timedelta
import base64
import io
import json
from types import SimpleNamespace
from urllib.parse import urlsplit
import zipfile

import jwt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from config import settings
from database import SessionLocal
from models import ClosedLoopCheckout, ClosedLoopNfcChallenge, CryptoWallet, MerchantAccount, User
from routers import closed_loop


def _auth(user_id: str) -> dict[str, str]:
    token = jwt.encode(
        {"sub": user_id, "exp": datetime.utcnow() + timedelta(minutes=30)},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    return {"Authorization": f"Bearer {token}"}


def _user(email: str, username: str, balance_cents: int) -> str:
    db = SessionLocal()
    user = User(
        email=email,
        username=username,
        hashed_password="test",
        full_name=username.title(),
        wallet_initialized=True,
        wallet_type="fawn_custodial",
        crypto_wallet_address=f"0x{username.encode().hex():0<40}"[:42],
        usdc_balance_cents=balance_cents,
    )
    db.add(user)
    db.flush()
    db.add(CryptoWallet(
        user_id=user.id,
        wallet_address=user.crypto_wallet_address,
        wallet_type="fawn_custodial",
        chain="polygon",
        status="active",
        usdc_balance_cents=balance_cents,
    ))
    db.commit()
    user_id = user.id
    db.close()
    return user_id


def _approve(client, merchant_id: str):
    response = client.post(
        f"/closed-loop/admin/merchants/{merchant_id}/approve",
        headers={"X-Admin-Key": "test-admin-key-12345"},
    )
    assert response.status_code == 200, response.text


def test_closed_loop_purchase_charges_exact_one_cent_to_each_side(client):
    payer_id = _user("closed-payer@example.com", "closedpayer", 10_000)
    merchant_owner_id = _user("closed-merchant@example.com", "closedmerchant", 1_000)
    payer_headers = _auth(payer_id)
    merchant_headers = _auth(merchant_owner_id)

    card = client.post("/closed-loop/cards", headers=payer_headers)
    assert card.status_code == 201, card.text
    assert card.json()["network"] == "FAWN"
    assert card.json()["phone_wallet"]["dynamic_tap_token"] is True
    assert card.json()["phone_wallet"]["google_wallet_pass_available"] is False
    assert card.json()["phone_wallet"]["apple_wallet_pass_available"] is False
    unavailable_pass = client.post("/closed-loop/cards/me/google-wallet", headers=payer_headers)
    assert unavailable_pass.status_code == 503
    unavailable_apple_pass = client.post("/closed-loop/cards/me/apple-wallet", headers=payer_headers)
    assert unavailable_apple_pass.status_code == 503

    merchant = client.post("/closed-loop/merchants", headers=merchant_headers, json={
        "business_name": "Campus Coffee LLC",
        "display_name": "Campus Coffee",
        "support_email": "help@campuscoffee.example",
    })
    assert merchant.status_code == 201, merchant.text
    merchant_id = merchant.json()["id"]
    assert merchant.json()["status"] == "active"

    checkout_headers = {**merchant_headers, "Idempotency-Key": "latte-order-001"}
    checkout = client.post("/closed-loop/merchant/checkouts", headers=checkout_headers, json={
        "amount_cents": 500,
        "order_reference": "latte-001",
    })
    assert checkout.status_code == 201, checkout.text
    body = checkout.json()
    assert body["user_fee_cents"] == 1
    assert body["merchant_fee_cents"] == 1
    assert body["payer_total_cents"] == 501
    checkout_replay = client.post("/closed-loop/merchant/checkouts", headers=checkout_headers, json={
        "amount_cents": 500,
        "order_reference": "latte-001",
    })
    assert checkout_replay.status_code == 201
    assert checkout_replay.json()["checkout_token"] == body["checkout_token"]
    assert checkout_replay.json()["idempotent_replay"] is True

    paid = client.post(f"/closed-loop/checkouts/{body['checkout_token']}/authorize", headers=payer_headers)
    assert paid.status_code == 200, paid.text
    result = paid.json()
    assert result["status"] == "completed"
    assert result["merchant_net_cents"] == 499
    assert result["payer_balance_cents"] == 9_499
    assert result["merchant_balance_cents"] == 1_499

    payer_activity = client.get("/closed-loop/activity", headers=payer_headers).json()["activity"]
    merchant_activity = client.get("/closed-loop/activity", headers=merchant_headers).json()["activity"]
    assert payer_activity[0]["type"] == "purchase"
    assert payer_activity[0]["amount_cents"] == -501
    assert merchant_activity[0]["type"] == "merchant_sale"
    assert merchant_activity[0]["amount_cents"] == 499

    replay = client.post(f"/closed-loop/checkouts/{body['checkout_token']}/authorize", headers=payer_headers)
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True

    db = SessionLocal()
    payer = db.query(User).filter(User.id == payer_id).first()
    merchant_owner = db.query(User).filter(User.id == merchant_owner_id).first()
    merchant_row = db.query(MerchantAccount).filter(MerchantAccount.id == merchant_id).first()
    payer_wallet = db.query(CryptoWallet).filter(CryptoWallet.user_id == payer_id).first()
    purchase_count = db.query(ClosedLoopCheckout).filter(ClosedLoopCheckout.checkout_token == body["checkout_token"]).count()
    assert payer.usdc_balance_cents == 9_499
    assert payer.total_fees_paid_cents == 1
    assert merchant_owner.usdc_balance_cents == 1_499
    assert merchant_owner.total_fees_paid_cents == 1
    assert merchant_row.total_volume_cents == 500
    assert merchant_row.total_fees_paid_cents == 1
    assert payer_wallet.pending_fee_cents == 2
    assert purchase_count == 1
    db.close()


def test_card_issuance_requires_active_custodial_wallet(client):
    user_id = _user("legacy-card@example.com", "legacycard", 1_000)
    db = SessionLocal()
    wallet = db.query(CryptoWallet).filter(CryptoWallet.user_id == user_id).first()
    wallet.wallet_type = "non_custodial"
    db.commit()
    db.close()

    response = client.post("/closed-loop/cards", headers=_auth(user_id))
    assert response.status_code == 409
    assert "active custodial wallet" in response.json()["detail"].lower()


def test_google_wallet_pass_uses_fawn_owned_signed_generic_pass(client, monkeypatch):
    user_id = _user("wallet-pass@example.com", "walletpass", 1_000)
    headers = _auth(user_id)
    issued = client.post("/closed-loop/cards", headers=headers)
    assert issued.status_code == 201

    captured = {}

    def fake_encode(claims, key, algorithm, headers):
        captured.update({"claims": claims, "key": key, "algorithm": algorithm, "headers": headers})
        return "signed-wallet-jwt"

    monkeypatch.setattr(settings, "google_wallet_issuer_id", "123456789")
    monkeypatch.setattr(settings, "google_wallet_service_account_email", "wallet@fawn.example")
    monkeypatch.setattr(settings, "google_wallet_private_key", "FAWN-PRIVATE-KEY\\nLINE-2")
    monkeypatch.setattr(settings, "google_wallet_private_key_id", "key-1")
    monkeypatch.setattr(closed_loop.jwt, "encode", fake_encode)

    response = client.post("/closed-loop/cards/me/google-wallet", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["add_url"] == "https://pay.google.com/gp/v/save/signed-wallet-jwt"
    assert body["pass_type"] == "generic"
    assert body["smart_tap_enabled"] is False
    assert body["payment_card"] is False
    assert captured["algorithm"] == "RS256"
    assert captured["key"] == "FAWN-PRIVATE-KEY\nLINE-2"
    assert captured["claims"]["iss"] == "wallet@fawn.example"
    generic = captured["claims"]["payload"]["genericObjects"][0]
    assert generic["classId"].startswith("123456789.fawn_balance_")
    assert generic["barcode"]["value"].startswith("fawn://card?card=")


def test_apple_wallet_pass_uses_short_lived_signed_download(client, monkeypatch):
    user_id = _user("apple-pass@example.com", "applepass", 1_000)
    headers = _auth(user_id)
    issued = client.post("/closed-loop/cards", headers=headers)
    assert issued.status_code == 201

    monkeypatch.setattr(settings, "apple_wallet_pass_type_identifier", "pass.com.fawn.balance")
    monkeypatch.setattr(settings, "apple_wallet_team_identifier", "FAWNTEAM01")
    monkeypatch.setattr(settings, "apple_wallet_certificate_pem", "certificate")
    monkeypatch.setattr(settings, "apple_wallet_private_key_pem", "private-key")
    monkeypatch.setattr(settings, "apple_wallet_wwdr_certificate_pem", "wwdr")
    monkeypatch.setattr(settings, "public_api_base_url", "https://api.fawn.example")
    monkeypatch.setattr(closed_loop, "_build_apple_wallet_pass", lambda card, user: b"signed-pkpass")

    response = client.post("/closed-loop/cards/me/apple-wallet", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["wallet"] == "apple_wallet"
    assert body["pass_type"] == "generic"
    assert body["nfc_enabled"] is False
    assert body["payment_card"] is False
    assert body["expires_in_seconds"] == 300
    assert body["add_url"].startswith("https://api.fawn.example/closed-loop/wallet-passes/apple/")

    download = client.get(urlsplit(body["add_url"]).path)
    assert download.status_code == 200, download.text
    assert download.content == b"signed-pkpass"
    assert download.headers["content-type"] == "application/vnd.apple.pkpass"
    assert download.headers["cache-control"] == "private, no-store, max-age=0"
    replay = client.get(urlsplit(body["add_url"]).path)
    assert replay.status_code == 410


def test_apple_wallet_package_has_signed_manifest_and_no_unapproved_nfc(monkeypatch):
    monkeypatch.setattr(settings, "apple_wallet_pass_type_identifier", "pass.com.fawn.balance")
    monkeypatch.setattr(settings, "apple_wallet_team_identifier", "FAWNTEAM01")
    monkeypatch.setattr(closed_loop, "_sign_apple_manifest", lambda manifest: b"detached-signature")
    card = SimpleNamespace(public_id="fawn-card-public-id", last_four="2046")
    user = SimpleNamespace(full_name="Avery Fawn", username="avery")

    package = closed_loop._build_apple_wallet_pass(card, user)
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        names = set(archive.namelist())
        assert {"pass.json", "manifest.json", "signature", "icon.png", "icon@2x.png", "icon@3x.png"} <= names
        pass_json = json.loads(archive.read("pass.json"))
        manifest = json.loads(archive.read("manifest.json"))
        assert archive.read("signature") == b"detached-signature"
        assert pass_json["passTypeIdentifier"] == "pass.com.fawn.balance"
        assert pass_json["barcodes"][0]["message"] == "fawn://card?card=fawn-card-public-id"
        assert "nfc" not in pass_json
        assert set(manifest) == {"pass.json", "icon.png", "icon@2x.png", "icon@3x.png"}


def test_dynamic_tap_token_is_short_lived_and_single_use(client):
    payer_id = _user("tap-payer@example.com", "tappayer", 5_000)
    merchant_owner_id = _user("tap-merchant@example.com", "tapmerchant", 500)
    payer_headers = _auth(payer_id)
    merchant_headers = _auth(merchant_owner_id)
    client.post("/closed-loop/cards", headers=payer_headers)
    merchant = client.post("/closed-loop/merchants", headers=merchant_headers, json={
        "business_name": "Tap Shop LLC",
        "display_name": "Tap Shop",
        "support_email": "help@tapshop.example",
    }).json()
    _approve(client, merchant["id"])

    checkout = client.post("/closed-loop/merchant/checkouts", headers=merchant_headers, json={"amount_cents": 250}).json()
    tap = client.post("/closed-loop/cards/me/tap-token", headers=payer_headers, json={"checkout_token": checkout["checkout_token"]})
    assert tap.status_code == 201, tap.text
    assert tap.json()["single_use"] is True

    accepted = client.post(
        f"/closed-loop/merchant/checkouts/{checkout['checkout_token']}/tap",
        headers=merchant_headers,
        json={"tap_token": tap.json()["tap_token"]},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["acceptance_method"] == "dynamic_tap_token"

    second_checkout = client.post("/closed-loop/merchant/checkouts", headers=merchant_headers, json={"amount_cents": 200}).json()
    replay = client.post(
        f"/closed-loop/merchant/checkouts/{second_checkout['checkout_token']}/tap",
        headers=merchant_headers,
        json={"tap_token": tap.json()["tap_token"]},
    )
    assert replay.status_code == 401

    fresh_tap = client.post("/closed-loop/cards/me/tap-token", headers=payer_headers, json={"checkout_token": second_checkout["checkout_token"]}).json()
    third_checkout = client.post("/closed-loop/merchant/checkouts", headers=merchant_headers, json={"amount_cents": 175}).json()
    wrong_checkout = client.post(
        f"/closed-loop/merchant/checkouts/{third_checkout['checkout_token']}/tap",
        headers=merchant_headers,
        json={"tap_token": fresh_tap["tap_token"]},
    )
    assert wrong_checkout.status_code == 403


def test_android_hce_challenge_is_checkout_bound_signed_and_single_use(client):
    payer_id = _user("hce-payer@example.com", "hcepayer", 5_000)
    merchant_owner_id = _user("hce-merchant@example.com", "hcemerchant", 500)
    payer_headers = _auth(payer_id)
    merchant_headers = _auth(merchant_owner_id)
    card = client.post("/closed-loop/cards", headers=payer_headers).json()

    private_key = ec.generate_private_key(ec.SECP256R1())
    public_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_b64 = base64.urlsafe_b64encode(public_der).decode().rstrip("=")
    registered = client.post("/closed-loop/cards/me/nfc-devices", headers=payer_headers, json={
        "device_name": "Avery's Android",
        "public_key_spki_b64": public_b64,
    })
    assert registered.status_code == 201, registered.text
    device = registered.json()
    assert device["card_id"] == card["id"]
    assert device["hce_aid"] == "F04641574E0101"
    assert device["requires_device_unlock"] is True
    assert device["attestation_status"] == "unverified"

    merchant = client.post("/closed-loop/merchants", headers=merchant_headers, json={
        "business_name": "HCE Shop LLC",
        "display_name": "HCE Shop",
        "support_email": "help@hceshop.example",
    }).json()
    _approve(client, merchant["id"])
    checkout_one = client.post("/closed-loop/merchant/checkouts", headers=merchant_headers, json={
        "amount_cents": 500,
        "order_reference": "hce-one",
    }).json()
    checkout_two = client.post("/closed-loop/merchant/checkouts", headers=merchant_headers, json={
        "amount_cents": 600,
        "order_reference": "hce-two",
    }).json()
    challenge_response = client.post(
        f"/closed-loop/merchant/checkouts/{checkout_one['checkout_token']}/nfc-challenge",
        headers=merchant_headers,
    )
    assert challenge_response.status_code == 201, challenge_response.text
    challenge = challenge_response.json()["challenge_b64"]
    challenge_bytes = base64.urlsafe_b64decode(challenge + "=" * (-len(challenge) % 4))
    signature = private_key.sign(b"FAWN-NFC-v1\x00" + challenge_bytes, ec.ECDSA(hashes.SHA256()))
    signature_b64 = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    payload = {"device_id": device["id"], "challenge_b64": challenge, "signature_b64": signature_b64}

    wrong_checkout = client.post(
        f"/closed-loop/merchant/checkouts/{checkout_two['checkout_token']}/nfc-authorize",
        headers=merchant_headers,
        json=payload,
    )
    assert wrong_checkout.status_code == 401

    wrong_key = ec.generate_private_key(ec.SECP256R1())
    wrong_signature = wrong_key.sign(b"FAWN-NFC-v1\x00" + challenge_bytes, ec.ECDSA(hashes.SHA256()))
    invalid_signature = client.post(
        f"/closed-loop/merchant/checkouts/{checkout_one['checkout_token']}/nfc-authorize",
        headers=merchant_headers,
        json={**payload, "signature_b64": base64.urlsafe_b64encode(wrong_signature).decode().rstrip("=")},
    )
    assert invalid_signature.status_code == 401

    accepted = client.post(
        f"/closed-loop/merchant/checkouts/{checkout_one['checkout_token']}/nfc-authorize",
        headers=merchant_headers,
        json=payload,
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["acceptance_method"] == "android_hce"
    assert accepted.json()["payer_balance_cents"] == 4_499
    assert accepted.json()["merchant_balance_cents"] == 999

    db = SessionLocal()
    try:
        challenge_row = db.query(ClosedLoopNfcChallenge).filter(
            ClosedLoopNfcChallenge.checkout_id == checkout_one["id"]
        ).one()
        assert challenge_row.used_at is not None
    finally:
        db.close()

    replay = client.post(
        f"/closed-loop/merchant/checkouts/{checkout_one['checkout_token']}/nfc-authorize",
        headers=merchant_headers,
        json=payload,
    )
    assert replay.status_code == 409

    revoked = client.delete(f"/closed-loop/cards/me/nfc-devices/{device['id']}", headers=payer_headers)
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"


def test_public_merchant_application_does_not_enable_payments(client):
    response = client.post("/closed-loop/merchant-applications", json={
        "email": "owner@bookshop.example",
        "contact_name": "Morgan Lee",
        "business_name": "Campus Bookshop",
        "website": "https://bookshop.example",
        "category": "retail",
    })
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "received"
