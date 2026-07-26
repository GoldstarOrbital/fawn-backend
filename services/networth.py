"""Unified Net Worth service for FAWN.

Aggregates a user's total net worth across every asset surface FAWN can see:

  1. Cash (USDC)     -- User.usdc_balance_cents (internal ledger, source of truth)
  2. Investments     -- Alpaca brokerage equity (guarded; 0 if unconfigured/absent)
  3. Linked banks    -- external bank balances via Plaid (NOT persisted today, so
                        reported as 0 with an explanatory note; the number of
                        linked institutions is still surfaced)

This module is strictly READ-ONLY / TRACKING-ONLY. It never modifies a balance,
never creates a transfer, and never touches send/settlement/on-chain code. The
only write it performs is an append-only UserAuditLog snapshot (opt-in via
``record_net_worth_snapshot``) so net-worth-over-time can be reconstructed —
exactly the UserAuditLog-as-JSON pattern used by routers/automation.py.

All logic lives here as pure functions taking (db, user_id, ...) and returning
plain dicts, so it is unit-testable without registering the router.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from models import User, PlaidItem, UserAuditLog
from services import alpaca

logger = logging.getLogger(__name__)

# Action string used to persist net-worth snapshots into UserAuditLog. Reads
# filter on exactly this value, mirroring routers/automation.py.
NET_WORTH_SNAPSHOT_ACTION = "networth_snapshot"

# 7-year compliance retention, matching every other UserAuditLog writer.
_RETENTION = timedelta(days=365 * 7)


def _run_async(coro):
    """Drive an awaitable to completion from synchronous code.

    ``alpaca.get_account`` is async, but this service (and the router that wraps
    it as a sync ``def`` handler) is synchronous. When there is no running event
    loop we can use ``asyncio.run`` directly; if a loop is already running in
    this thread we hand the coroutine to a throwaway loop on a worker thread to
    avoid re-entrancy errors.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro)).result()


def _cash_component(user: User) -> dict:
    """Cash held as USDC on the FAWN internal ledger (always available)."""
    cents = int(user.usdc_balance_cents or 0)
    return {
        "key": "cash_usdc",
        "label": "USDC Cash",
        "amount_cents": cents,
        "available": True,
        "source": "fawn_ledger",
    }


def _investments_component(user: User) -> dict:
    """Brokerage equity from Alpaca, guarded.

    Only attempts a call when the user actually has a brokerage account, and
    swallows every failure (unconfigured Alpaca, network error, bad payload)
    down to ``amount_cents=0`` with an explanatory note — a net-worth read must
    never fail because a third party is unavailable.
    """
    account_id = getattr(user, "alpaca_account_id", None)
    if not account_id:
        return {
            "key": "investments",
            "label": "Investments",
            "amount_cents": 0,
            "available": False,
            "source": "alpaca",
            "note": "No brokerage account linked.",
        }
    try:
        acct = _run_async(alpaca.get_account(account_id))
        equity_usd = float(acct.get("equity", 0) or 0)
        cents = int(round(equity_usd * 100))
        return {
            "key": "investments",
            "label": "Investments",
            "amount_cents": cents,
            "available": True,
            "source": "alpaca",
            "equity_usd": equity_usd,
            "account_status": acct.get("status", ""),
        }
    except Exception as e:  # noqa: BLE001 -- guard: never let Alpaca break net worth
        logger.warning("[networth] Alpaca equity unavailable for user %s: %s", user.id, e)
        return {
            "key": "investments",
            "label": "Investments",
            "amount_cents": 0,
            "available": False,
            "source": "alpaca",
            "note": f"Brokerage equity temporarily unavailable ({type(e).__name__}).",
        }


def _bank_component(db: Session, user_id: str) -> dict:
    """Linked external bank balances.

    FAWN does not persist Plaid account balances today (PlaidItem stores only
    the access token + display mask, and services/plaid.py exposes no balance
    fetch), so this contributes 0 to net worth with an explicit note. The count
    of linked, active institutions is still surfaced so the UI can prompt the
    user, and so this stays correct automatically if balances become stored.
    """
    linked_count = db.query(PlaidItem).filter(
        PlaidItem.user_id == user_id,
        PlaidItem.status == "active",
    ).count()
    return {
        "key": "bank_linked",
        "label": "Linked Bank Accounts",
        "amount_cents": 0,
        "available": False,
        "source": "plaid",
        "linked_account_count": linked_count,
        "note": (
            "Bank balances are not stored; linked accounts are shown for "
            "reference but do not contribute to net worth yet."
        ),
    }


def compute_net_worth(db: Session, user_id: str) -> dict:
    """Compute a user's unified net worth. Pure read — performs no writes.

    Returns:
        {
          "total_cents": int,
          "breakdown": {
             "cash_usdc_cents": int,
             "investments_cents": int,
             "bank_linked_cents": int,
          },
          "components": [ {key, label, amount_cents, available, ...}, ... ],
          "as_of": ISO-8601 UTC timestamp,
        }

    Raises:
        ValueError: if the user does not exist.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise ValueError(f"User {user_id} not found")

    cash = _cash_component(user)
    investments = _investments_component(user)
    bank = _bank_component(db, user_id)

    breakdown = {
        "cash_usdc_cents": cash["amount_cents"],
        "investments_cents": investments["amount_cents"],
        "bank_linked_cents": bank["amount_cents"],
    }
    total_cents = (
        breakdown["cash_usdc_cents"]
        + breakdown["investments_cents"]
        + breakdown["bank_linked_cents"]
    )

    return {
        "total_cents": total_cents,
        "breakdown": breakdown,
        "components": [cash, investments, bank],
        "as_of": datetime.now(tz=timezone.utc).isoformat(),
    }


def record_net_worth_snapshot(db: Session, user_id: str) -> dict:
    """Compute net worth and persist a snapshot to UserAuditLog (tracking-only).

    The snapshot is an append-only JSON audit row (action
    ``networth_snapshot``). This is the ONLY write this module performs; it
    moves no money and mutates no balance. Returns the freshly computed net
    worth dict (identical shape to ``compute_net_worth``) with an added
    ``snapshot_recorded: True`` flag.
    """
    result = compute_net_worth(db, user_id)

    snapshot = {
        "total_cents": result["total_cents"],
        "breakdown": result["breakdown"],
        "as_of": result["as_of"],
    }
    db.add(UserAuditLog(
        user_id=user_id,
        action=NET_WORTH_SNAPSHOT_ACTION,
        details=json.dumps(snapshot),
        retention_expires_at=datetime.now(tz=timezone.utc) + _RETENTION,
    ))
    db.commit()

    return {**result, "snapshot_recorded": True}


def get_net_worth_history(db: Session, user_id: str, limit: int = 30) -> dict:
    """Read back persisted net-worth snapshots, newest first.

    Returns ``{"snapshots": [...], "count": int}`` where each snapshot is the
    JSON previously written by ``record_net_worth_snapshot`` (plus the row's
    ``recorded_at``). Malformed rows are skipped defensively.
    """
    if limit < 1:
        limit = 1
    logs = db.query(UserAuditLog).filter(
        UserAuditLog.user_id == user_id,
        UserAuditLog.action == NET_WORTH_SNAPSHOT_ACTION,
    ).order_by(UserAuditLog.created_at.desc()).limit(limit).all()

    snapshots = []
    for log in logs:
        try:
            data = json.loads(log.details) if log.details else {}
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        if log.created_at is not None:
            data["recorded_at"] = log.created_at.isoformat()
        snapshots.append(data)

    return {"snapshots": snapshots, "count": len(snapshots)}
