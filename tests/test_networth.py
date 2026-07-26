"""Unit tests for services/networth.py (the Unified Net Worth service).

These test the SERVICE functions directly (the router is not registered yet),
following the tests/test_referral.py + tests/test_transfer_history.py style:
create User / PlaidItem rows directly via SessionLocal, assert on returned
dicts. All Alpaca (external HTTP) access is mocked — no real network calls.
The suite also proves the service moves no money.
"""
import json
import uuid

import pytest

from database import SessionLocal
from models import User, PlaidItem, UserAuditLog, CryptoTransfer
from services import networth as networth_svc
from services import alpaca as alpaca_mod


def _make_user(db, *, usdc_cents=0, alpaca_account_id=None):
    user = User(
        email=f"nw_{uuid.uuid4().hex[:10]}@example.com",
        hashed_password="x",
        full_name="Net Worth Tester",
        is_student=True,
        usdc_balance_cents=usdc_cents,
        alpaca_account_id=alpaca_account_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _fake_get_account(equity, status="ACTIVE"):
    async def _inner(account_id):
        return {
            "account_id": account_id,
            "status": status,
            "cash": 0.0,
            "equity": equity,
            "buying_power": 0.0,
            "currency": "USD",
        }
    return _inner


async def _boom_get_account(account_id):
    raise RuntimeError("alpaca is down")


# ── CASH (USDC) ──

def test_compute_net_worth_cash_only():
    db = SessionLocal()
    try:
        user = _make_user(db, usdc_cents=25_000)  # $250.00, no brokerage, no bank
        result = networth_svc.compute_net_worth(db, user.id)

        assert result["breakdown"]["cash_usdc_cents"] == 25_000
        assert result["breakdown"]["investments_cents"] == 0
        assert result["breakdown"]["bank_linked_cents"] == 0
        assert result["total_cents"] == 25_000
        assert "as_of" in result

        cash = next(c for c in result["components"] if c["key"] == "cash_usdc")
        assert cash["amount_cents"] == 25_000
        assert cash["available"] is True
    finally:
        db.close()


# ── INVESTMENTS (Alpaca, guarded) ──

def test_investments_included_when_alpaca_returns_equity(monkeypatch):
    monkeypatch.setattr(alpaca_mod, "get_account", _fake_get_account(123.45))
    db = SessionLocal()
    try:
        user = _make_user(db, usdc_cents=10_000, alpaca_account_id="acct_123")
        result = networth_svc.compute_net_worth(db, user.id)

        assert result["breakdown"]["investments_cents"] == 12_345  # 123.45 * 100
        assert result["breakdown"]["cash_usdc_cents"] == 10_000
        assert result["total_cents"] == 22_345

        inv = next(c for c in result["components"] if c["key"] == "investments")
        assert inv["available"] is True
        assert inv["equity_usd"] == 123.45
    finally:
        db.close()


def test_investments_guarded_to_zero_when_alpaca_raises(monkeypatch):
    monkeypatch.setattr(alpaca_mod, "get_account", _boom_get_account)
    db = SessionLocal()
    try:
        user = _make_user(db, usdc_cents=5_000, alpaca_account_id="acct_bad")
        result = networth_svc.compute_net_worth(db, user.id)

        # Net worth must not fail because Alpaca is unavailable.
        assert result["breakdown"]["investments_cents"] == 0
        assert result["total_cents"] == 5_000

        inv = next(c for c in result["components"] if c["key"] == "investments")
        assert inv["available"] is False
        assert "note" in inv
    finally:
        db.close()


def test_investments_zero_when_no_brokerage_account():
    # No alpaca_account_id => get_account is never called (no network at all).
    db = SessionLocal()
    try:
        user = _make_user(db, usdc_cents=7_000, alpaca_account_id=None)
        result = networth_svc.compute_net_worth(db, user.id)

        assert result["breakdown"]["investments_cents"] == 0
        inv = next(c for c in result["components"] if c["key"] == "investments")
        assert inv["available"] is False
    finally:
        db.close()


# ── BANK LINKED (Plaid — not stored, reported 0 with note) ──

def test_bank_linked_reports_zero_with_note_and_count():
    db = SessionLocal()
    try:
        user = _make_user(db, usdc_cents=1_000)
        for _ in range(2):
            db.add(PlaidItem(
                user_id=user.id,
                item_id=f"item_{uuid.uuid4().hex[:12]}",
                access_token="secret-token",
                status="active",
            ))
        # A removed item should NOT be counted.
        db.add(PlaidItem(
            user_id=user.id,
            item_id=f"item_{uuid.uuid4().hex[:12]}",
            access_token="secret-token",
            status="removed",
        ))
        db.commit()

        result = networth_svc.compute_net_worth(db, user.id)
        bank = next(c for c in result["components"] if c["key"] == "bank_linked")

        assert result["breakdown"]["bank_linked_cents"] == 0
        assert bank["amount_cents"] == 0
        assert bank["linked_account_count"] == 2
        assert "note" in bank
    finally:
        db.close()


# ── SNAPSHOT PERSISTENCE (tracking-only via UserAuditLog) + NO MONEY MOVEMENT ──

def test_record_snapshot_persists_audit_log_and_moves_no_money(monkeypatch):
    monkeypatch.setattr(alpaca_mod, "get_account", _fake_get_account(50.00))
    db = SessionLocal()
    try:
        user = _make_user(db, usdc_cents=8_000, alpaca_account_id="acct_ok")
        balance_before = user.usdc_balance_cents

        result = networth_svc.record_net_worth_snapshot(db, user.id)
        assert result["snapshot_recorded"] is True
        assert result["total_cents"] == 8_000 + 5_000  # cash + $50 equity

        logs = db.query(UserAuditLog).filter(
            UserAuditLog.user_id == user.id,
            UserAuditLog.action == networth_svc.NET_WORTH_SNAPSHOT_ACTION,
        ).all()
        assert len(logs) == 1
        stored = json.loads(logs[0].details)
        assert stored["total_cents"] == 13_000
        assert stored["breakdown"]["cash_usdc_cents"] == 8_000
        assert logs[0].retention_expires_at is not None

        # HARD SAFETY: no balance mutated, no transfer created.
        db.refresh(user)
        assert user.usdc_balance_cents == balance_before
        assert db.query(CryptoTransfer).filter(
            CryptoTransfer.sender_id == user.id
        ).count() == 0
    finally:
        db.close()


def test_get_history_reads_back_snapshots_newest_first(monkeypatch):
    monkeypatch.setattr(alpaca_mod, "get_account", _fake_get_account(0.0))
    db = SessionLocal()
    try:
        user = _make_user(db, usdc_cents=1_000, alpaca_account_id="acct_hist")

        networth_svc.record_net_worth_snapshot(db, user.id)
        networth_svc.record_net_worth_snapshot(db, user.id)

        history = networth_svc.get_net_worth_history(db, user.id, limit=10)
        assert history["count"] == 2
        assert len(history["snapshots"]) == 2
        for snap in history["snapshots"]:
            assert snap["total_cents"] == 1_000
            assert "recorded_at" in snap
    finally:
        db.close()


# ── ERROR HANDLING ──

def test_compute_net_worth_unknown_user_raises():
    db = SessionLocal()
    try:
        with pytest.raises(ValueError):
            networth_svc.compute_net_worth(db, "nonexistent-user-id")
    finally:
        db.close()
