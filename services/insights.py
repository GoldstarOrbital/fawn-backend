"""Spending / cashflow insights for FAWN.

Pure, READ-ONLY analytics over the existing ledger tables. Nothing here
writes to the database, touches balances, creates transfers, or calls any
external / on-chain code — it only reads CryptoDeposit (money in) and
CryptoTransfer (money out) and returns plain dicts, which makes every
function unit-testable without HTTP.

Money-in  = CryptoDeposit rows that were actually credited to the ledger
            (credited_to_ledger=True). Backfilled/uncredited history is
            excluded so it isn't shown as fresh cashflow, mirroring how
            GET /transfers/history treats it.
Money-out = CryptoTransfer rows that actually completed (status="completed").
            Pending / failed / rejected / held sends never moved money, so
            they are not counted as spend. Outflow includes the platform
            fee (amount_cents + fee_cents) since that also left the balance.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from models import CryptoDeposit, CryptoTransfer

# A completed send is the only state where USDC actually left the sender.
_COMPLETED = "completed"


def _naive(dt: Optional[datetime]) -> Optional[datetime]:
    """Drop tzinfo so arithmetic/sorting never mixes aware and naive values.

    SQLite may hand DateTime(timezone=True) columns back as naive while
    Postgres returns aware ones; normalizing avoids "can't subtract
    offset-naive and offset-aware datetimes" at runtime.
    """
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _month_key(dt: datetime) -> str:
    return f"{dt.year:04d}-{dt.month:02d}"


def _recent_month_keys(now: datetime, months: int) -> list[str]:
    """Ordered oldest->newest list of the last `months` YYYY-MM keys."""
    y, m = now.year, now.month
    keys: list[str] = []
    for _ in range(months):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    keys.reverse()
    return keys


def monthly_cashflow(db: Session, user_id: str, months: int = 6) -> list[dict]:
    """Per-month inflow / outflow / net for the last `months` months.

    Returns a contiguous, chronological (oldest first) series — months with
    no activity are included as zeros so the caller gets a predictable,
    chart-ready shape. Amounts are integer cents.
    """
    months = max(1, int(months))
    now = datetime.now(tz=timezone.utc)
    keys = _recent_month_keys(now, months)
    key_set = set(keys)

    inflow: dict[str, int] = {k: 0 for k in keys}
    outflow: dict[str, int] = {k: 0 for k in keys}

    deposits = (
        db.query(CryptoDeposit)
        .filter(
            CryptoDeposit.user_id == user_id,
            CryptoDeposit.credited_to_ledger.is_(True),
        )
        .all()
    )
    for d in deposits:
        created = _naive(d.created_at)
        if created is None:
            continue
        k = _month_key(created)
        if k in key_set:
            inflow[k] += int(d.amount_cents or 0)

    transfers = (
        db.query(CryptoTransfer)
        .filter(
            CryptoTransfer.sender_id == user_id,
            CryptoTransfer.status == _COMPLETED,
        )
        .all()
    )
    for t in transfers:
        created = _naive(t.created_at)
        if created is None:
            continue
        k = _month_key(created)
        if k in key_set:
            outflow[k] += int(t.amount_cents or 0) + int(t.fee_cents or 0)

    return [
        {
            "month": k,
            "inflow_cents": inflow[k],
            "outflow_cents": outflow[k],
            "net_cents": inflow[k] - outflow[k],
        }
        for k in keys
    ]


def top_counterparties(db: Session, user_id: str, limit: int = 5) -> list[dict]:
    """The recipients this user has sent the most USDC to (completed sends).

    total_cents is the sum of amounts sent to that recipient (excluding the
    platform fee, which goes to FAWN, not the counterparty). Sorted by
    total sent desc, then send count desc, then address for stable ties.
    """
    limit = max(1, int(limit))
    transfers = (
        db.query(CryptoTransfer)
        .filter(
            CryptoTransfer.sender_id == user_id,
            CryptoTransfer.status == _COMPLETED,
        )
        .all()
    )

    totals: dict[str, int] = defaultdict(int)
    counts: dict[str, int] = defaultdict(int)
    for t in transfers:
        addr = t.recipient_address
        if not addr:
            continue
        totals[addr] += int(t.amount_cents or 0)
        counts[addr] += 1

    ranked = sorted(
        totals.keys(),
        key=lambda a: (-totals[a], -counts[a], a),
    )
    return [
        {"counterparty": a, "total_cents": totals[a], "count": counts[a]}
        for a in ranked[:limit]
    ]


def detect_recurring(db: Session, user_id: str) -> list[dict]:
    """Detect likely recurring payments (subscriptions, allowances, rent).

    A recipient qualifies when the user has >=3 completed sends to it whose
    consecutive time gaps are roughly regular. `cadence_days` is the rounded
    average gap; `amount_cents` is the most common send amount to that
    recipient (subscriptions repeat the same charge); `occurrences` is the
    number of completed sends. Sorted by occurrences desc, then recipient.
    """
    transfers = (
        db.query(CryptoTransfer)
        .filter(
            CryptoTransfer.sender_id == user_id,
            CryptoTransfer.status == _COMPLETED,
        )
        .all()
    )

    by_recipient: dict[str, list[CryptoTransfer]] = defaultdict(list)
    for t in transfers:
        if t.recipient_address and _naive(t.created_at) is not None:
            by_recipient[t.recipient_address].append(t)

    results: list[dict] = []
    for recipient, sends in by_recipient.items():
        if len(sends) < 3:
            continue

        sends.sort(key=lambda s: _naive(s.created_at))
        times = [_naive(s.created_at) for s in sends]
        gaps = [
            (times[i + 1] - times[i]).total_seconds() / 86400.0
            for i in range(len(times) - 1)
        ]
        avg_gap = sum(gaps) / len(gaps)
        # Need a real cadence (not several sends bunched into one day).
        if avg_gap < 1.0:
            continue

        # "Roughly regular": every gap within 25% of the average, or within
        # 3 days for short (e.g. weekly) cadences — whichever is more lenient.
        tolerance = max(3.0, avg_gap * 0.25)
        if any(abs(g - avg_gap) > tolerance for g in gaps):
            continue

        amounts = [int(s.amount_cents or 0) for s in sends]
        common_amount = Counter(amounts).most_common(1)[0][0]

        results.append(
            {
                "recipient": recipient,
                "amount_cents": common_amount,
                "cadence_days": int(round(avg_gap)),
                "occurrences": len(sends),
            }
        )

    results.sort(key=lambda r: (-r["occurrences"], r["recipient"]))
    return results
