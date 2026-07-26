"""Unit tests for services.insights — the spending/cashflow analytics.

These test the SERVICE functions directly (the router isn't registered yet),
creating User / CryptoTransfer / CryptoDeposit rows in a throwaway SQLite DB
via SessionLocal, exactly like the other data-level tests in this suite.

The insights service is pure read-only: no external HTTP is involved, so
nothing needs mocking. A dedicated test also asserts that calling every
function leaves balances and row counts untouched (no money movement).
"""
import uuid
from datetime import datetime, timedelta, timezone

from database import SessionLocal
from models import User, CryptoTransfer, CryptoDeposit
from services import insights

NOW = datetime.now(tz=timezone.utc)


def _make_user(db, balance_cents=100_000):
    user = User(
        email=f"insights_{uuid.uuid4().hex[:10]}@example.com",
        hashed_password="x",
        full_name="Insights Tester",
        is_student=True,
        crypto_wallet_address="0x" + uuid.uuid4().hex[:40].ljust(40, "0"),
        wallet_initialized=True,
        usdc_balance_cents=balance_cents,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _add_transfer(db, sender_id, recipient, amount_cents, created_at,
                  fee_cents=100, status="completed"):
    t = CryptoTransfer(
        sender_id=sender_id,
        recipient_address=recipient,
        amount_cents=amount_cents,
        fee_cents=fee_cents,
        status=status,
        created_at=created_at,
    )
    db.add(t)
    db.commit()
    return t


def _add_deposit(db, user_id, to_address, amount_cents, created_at,
                 credited=True):
    d = CryptoDeposit(
        user_id=user_id,
        chain="base",
        contract_address="0x833589fCD6eDb6E08f4c7C32D4f71b54bda02913",
        from_address="0x" + uuid.uuid4().hex[:40].ljust(40, "0"),
        to_address=to_address,
        amount_cents=amount_cents,
        tx_hash="0x" + uuid.uuid4().hex,
        block_number=1000,
        credited_to_ledger=credited,
        created_at=created_at,
    )
    db.add(d)
    db.commit()
    return d


# ── monthly_cashflow ──

def test_monthly_cashflow_buckets_inflow_outflow_and_net():
    db = SessionLocal()
    try:
        user = _make_user(db)
        # Current month: 5000 in, one completed send of 1000 + 100 fee out.
        _add_deposit(db, user.id, user.crypto_wallet_address, 5000, NOW)
        _add_transfer(db, user.id, "0xrent", 1000, NOW, fee_cents=100)
        # Excluded: a failed send moved no money.
        _add_transfer(db, user.id, "0xrent", 9999, NOW, status="failed")
        # Excluded: an uncredited (backfilled) deposit isn't fresh cashflow.
        _add_deposit(db, user.id, user.crypto_wallet_address, 7777, NOW,
                     credited=False)
        # Excluded: a deposit far outside the 3-month window.
        _add_deposit(db, user.id, user.crypto_wallet_address, 4242,
                     NOW - timedelta(days=400))

        series = insights.monthly_cashflow(db, user.id, months=3)

        assert len(series) == 3
        # Oldest-first ordering: last bucket is the current month.
        assert [row["month"] for row in series] == sorted(r["month"] for r in series)
        current = series[-1]
        assert current["inflow_cents"] == 5000
        assert current["outflow_cents"] == 1100  # 1000 + 100 fee
        assert current["net_cents"] == 3900
        # A quiet earlier month is present as zeros.
        assert series[0]["inflow_cents"] == 0
        assert series[0]["outflow_cents"] == 0
        assert series[0]["net_cents"] == 0
    finally:
        db.close()


def test_monthly_cashflow_empty_user_is_all_zero():
    db = SessionLocal()
    try:
        user = _make_user(db)
        series = insights.monthly_cashflow(db, user.id, months=6)
        assert len(series) == 6
        assert all(r["inflow_cents"] == 0 and r["outflow_cents"] == 0
                   and r["net_cents"] == 0 for r in series)
    finally:
        db.close()


# ── top_counterparties ──

def test_top_counterparties_ranked_by_total_sent():
    db = SessionLocal()
    try:
        user = _make_user(db)
        _add_transfer(db, user.id, "0xaaa", 1000, NOW)
        _add_transfer(db, user.id, "0xaaa", 2000, NOW)
        _add_transfer(db, user.id, "0xaaa", 500, NOW)   # 0xaaa total 3500, count 3
        _add_transfer(db, user.id, "0xbbb", 3000, NOW)  # 0xbbb total 3000, count 1
        _add_transfer(db, user.id, "0xccc", 9999, NOW, status="failed")  # excluded

        top = insights.top_counterparties(db, user.id, limit=5)
        assert [c["counterparty"] for c in top] == ["0xaaa", "0xbbb"]
        assert top[0] == {"counterparty": "0xaaa", "total_cents": 3500, "count": 3}
        assert top[1] == {"counterparty": "0xbbb", "total_cents": 3000, "count": 1}
        assert all(c["counterparty"] != "0xccc" for c in top)

        # limit is honored.
        assert len(insights.top_counterparties(db, user.id, limit=1)) == 1
    finally:
        db.close()


# ── detect_recurring ──

def test_detect_recurring_finds_regular_monthly_subscription():
    db = SessionLocal()
    try:
        user = _make_user(db)
        # 3 sends ~30 days apart, same amount => recurring.
        _add_transfer(db, user.id, "0xsub", 999, NOW - timedelta(days=60))
        _add_transfer(db, user.id, "0xsub", 999, NOW - timedelta(days=30))
        _add_transfer(db, user.id, "0xsub", 999, NOW)
        # Only 2 sends => not enough to be recurring.
        _add_transfer(db, user.id, "0xtwice", 500, NOW - timedelta(days=30))
        _add_transfer(db, user.id, "0xtwice", 500, NOW)
        # 3 sends but wildly irregular gaps => not recurring.
        _add_transfer(db, user.id, "0xrandom", 100, NOW - timedelta(days=61))
        _add_transfer(db, user.id, "0xrandom", 100, NOW - timedelta(days=60))
        _add_transfer(db, user.id, "0xrandom", 100, NOW)

        recurring = insights.detect_recurring(db, user.id)
        recipients = {r["recipient"] for r in recurring}
        assert recipients == {"0xsub"}

        sub = next(r for r in recurring if r["recipient"] == "0xsub")
        assert sub["amount_cents"] == 999
        assert sub["occurrences"] == 3
        assert 27 <= sub["cadence_days"] <= 33
    finally:
        db.close()


# ── safety: no money movement, purely read-only ──

def test_insights_never_move_money_or_write_rows():
    db = SessionLocal()
    try:
        user = _make_user(db, balance_cents=42_000)
        _add_deposit(db, user.id, user.crypto_wallet_address, 5000, NOW)
        _add_transfer(db, user.id, "0xsub", 999, NOW - timedelta(days=60))
        _add_transfer(db, user.id, "0xsub", 999, NOW - timedelta(days=30))
        _add_transfer(db, user.id, "0xsub", 999, NOW)

        transfers_before = db.query(CryptoTransfer).count()
        deposits_before = db.query(CryptoDeposit).count()
        balance_before = user.usdc_balance_cents

        insights.monthly_cashflow(db, user.id, months=6)
        insights.top_counterparties(db, user.id, limit=5)
        insights.detect_recurring(db, user.id)

        db.refresh(user)
        assert user.usdc_balance_cents == balance_before == 42_000
        assert db.query(CryptoTransfer).count() == transfers_before
        assert db.query(CryptoDeposit).count() == deposits_before
    finally:
        db.close()
