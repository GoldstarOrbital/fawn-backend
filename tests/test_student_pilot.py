"""End-to-end regression for the intended 25–50 student pilot cohort."""

from models import CryptoWallet, User
from database import SessionLocal


def _pilot_payload(index: int) -> dict:
    return {
        "email": f"pilot.student.{index}@example.com",
        "password": "safe_password_123!",
        "full_name": f"Pilot Student {index}",
        "username": f"student_{index}",
        "phone": f"555000{index:04d}",
        "is_student": True,
        "school": "berkeley",
        "location": "Berkeley, CA",
        "military_status": "none",
    }


def test_fifty_student_pilot_onboarding_and_p2p_balances(client):
    """Every pilot signup gets a wallet, then an internal payment settles once.

    This deliberately exercises the HTTP signup path for all 50 accounts and
    verifies both the canonical user ledger and the wallet mirror after a
    duplicate-safe send/receive.
    """
    accounts = []
    for index in range(50):
        registered = client.post("/auth/register", json=_pilot_payload(index))
        assert registered.status_code == 201, registered.text
        token = registered.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        me = client.get("/auth/me", headers=headers)
        assert me.status_code == 200, me.text
        body = me.json()
        assert body["wallet_initialized"] is True
        assert body["wallet_type"] == "fawn_custodial"
        assert body["crypto_wallet_address"].startswith("0x")
        accounts.append({"token": token, "me": body})

    db = SessionLocal()
    try:
        sender = db.query(User).filter(User.id == accounts[0]["me"]["id"]).one()
        sender_wallet = db.query(CryptoWallet).filter(CryptoWallet.user_id == sender.id).one()
        sender.usdc_balance_cents = 10_000
        sender_wallet.usdc_balance_cents = 10_000
        db.commit()
    finally:
        db.close()

    sender_headers = {
        "Authorization": f"Bearer {accounts[0]['token']}",
        "Idempotency-Key": "pilot-50-student-send-1",
    }
    payload = {"recipient": "@student_1", "amount_cents": 1_000, "memo": "pilot test"}
    first = client.post("/transfers/send-unified", headers=sender_headers, json=payload)
    assert first.status_code == 201, first.text
    retry = client.post("/transfers/send-unified", headers=sender_headers, json=payload)
    assert retry.status_code == 201, retry.text
    assert retry.json()["transfer_id"] == first.json()["transfer_id"]

    sender_balance = client.get("/wallet/balance", headers={"Authorization": f"Bearer {accounts[0]['token']}"})
    recipient_balance = client.get("/wallet/balance", headers={"Authorization": f"Bearer {accounts[1]['token']}"})
    assert sender_balance.json()["usdc_balance_cents"] == 8_999
    assert recipient_balance.json()["usdc_balance_cents"] == 1_000

    received = client.get("/transfers/history?limit=10", headers={"Authorization": f"Bearer {accounts[1]['token']}"})
    assert received.status_code == 200, received.text
    assert any(item["type"] == "receive" and item["amount"] == 10.0 for item in received.json())
