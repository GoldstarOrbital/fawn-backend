"""Savings Goals — TRACKING ONLY. No fund movement, ever.

This module lets a user define personal savings goals (e.g. "Emergency Fund",
"Spring Break") and record *tracking* contributions toward them so they can
watch their progress. It is deliberately a bookkeeping/aspiration feature:

    * It NEVER touches User.usdc_balance_cents.
    * It NEVER creates a CryptoTransfer / CryptoDeposit or calls any
      send / settlement / on-chain code.
    * A "contribution" here is a self-reported tracking entry only. No real
      money is moved, held, escrowed, or reserved. The user's spendable USDC
      balance is completely unaffected.

Everything is persisted append-only in the existing UserAuditLog table as JSON
(the same pattern as routers/automation.py), so no new tables/models/migrations
are introduced. Three action types are used:

    savings_goal_v2            -> one row per goal definition (canonical record)
    savings_goal_contribution  -> one row per tracking contribution
    savings_goal_archived      -> tombstone row that soft-archives a goal

Goals are addressed by a stable uuid `id` generated at creation time. Because
every query is filtered by user_id, a goal id only ever resolves within its
owner's own audit rows — there is no cross-user access.

All public functions are pure-ish helpers of the form (db, user_id, ...) -> dict
so they can be unit-tested directly without HTTP registration.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from models import UserAuditLog

# ── Audit-log action names (namespaced so they never collide with other features) ──
GOAL_ACTION = "savings_goal_v2"
CONTRIBUTION_ACTION = "savings_goal_contribution"
ARCHIVE_ACTION = "savings_goal_archived"

# Compliance retention window, matching the rest of the codebase (7 years).
_RETENTION = timedelta(days=365 * 7)

# Guardrails on tracked amounts (in cents). Purely input validation — these are
# tracking numbers, not real balances, so no balance check is performed.
_MAX_TARGET_CENTS = 1_000_000_000   # $10,000,000 sanity cap
_MAX_CONTRIB_CENTS = 1_000_000_000


class GoalError(ValueError):
    """Bad input for a savings-goal operation (maps to HTTP 400)."""


class GoalNotFound(GoalError):
    """Referenced goal does not exist for this user (maps to HTTP 404)."""


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _retention_expiry() -> datetime:
    return datetime.now(tz=timezone.utc) + _RETENTION


def _load_json(log: UserAuditLog) -> Optional[dict]:
    try:
        data = json.loads(log.details)
        return data if isinstance(data, dict) else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _goal_records(db: Session, user_id: str) -> dict[str, dict]:
    """Return {goal_id: canonical goal record} for every goal this user created."""
    rows = db.query(UserAuditLog).filter(
        UserAuditLog.user_id == user_id,
        UserAuditLog.action == GOAL_ACTION,
    ).all()
    records: dict[str, dict] = {}
    for row in rows:
        data = _load_json(row)
        if data and data.get("id"):
            records[data["id"]] = data
    return records


def _archived_ids(db: Session, user_id: str) -> set[str]:
    """Set of goal ids this user has archived (soft-deleted via tombstone rows)."""
    rows = db.query(UserAuditLog).filter(
        UserAuditLog.user_id == user_id,
        UserAuditLog.action == ARCHIVE_ACTION,
    ).all()
    ids: set[str] = set()
    for row in rows:
        data = _load_json(row)
        if data and data.get("goal_id"):
            ids.add(data["goal_id"])
    return ids


def _saved_by_goal(db: Session, user_id: str) -> dict[str, int]:
    """Sum of tracked contribution amounts (cents) per goal id."""
    rows = db.query(UserAuditLog).filter(
        UserAuditLog.user_id == user_id,
        UserAuditLog.action == CONTRIBUTION_ACTION,
    ).all()
    totals: dict[str, int] = {}
    for row in rows:
        data = _load_json(row)
        if not data:
            continue
        gid = data.get("goal_id")
        try:
            amount = int(data.get("amount_cents", 0))
        except (TypeError, ValueError):
            continue
        if gid and amount > 0:
            totals[gid] = totals.get(gid, 0) + amount
    return totals


def _view(record: dict, saved_cents: int, archived: bool = False) -> dict:
    """Enrich a stored goal record with computed progress fields."""
    target = int(record.get("target_cents", 0) or 0)
    saved = max(0, int(saved_cents))
    if target > 0:
        progress_pct = round(min(100.0, saved / target * 100), 2)
    else:
        progress_pct = 0.0
    remaining = max(0, target - saved)
    return {
        "id": record.get("id"),
        "name": record.get("name"),
        "target_cents": target,
        "deadline": record.get("deadline"),
        "saved_cents": saved,
        "progress_pct": progress_pct,
        "remaining_cents": remaining,
        "is_complete": target > 0 and saved >= target,
        "status": "archived" if archived else "active",
        "created_at": record.get("created_at"),
        # Loud reminder to every consumer that this is bookkeeping only.
        "tracking_only": True,
    }


def _require_goal(db: Session, user_id: str, goal_id: str) -> dict:
    """Return the canonical record for an active goal, or raise GoalNotFound."""
    record = _goal_records(db, user_id).get(goal_id)
    if record is None or goal_id in _archived_ids(db, user_id):
        raise GoalNotFound(f"Savings goal {goal_id!r} not found.")
    return record


# ── Public API ──

def create_goal(
    db: Session,
    user_id: str,
    name: str,
    target_cents: int,
    deadline: Optional[str] = None,
) -> dict:
    """Create a new savings goal (tracking only) and persist it in UserAuditLog.

    `deadline` is an optional ISO-8601 string (stored verbatim). Returns the new
    goal as a view dict with a stable uuid `id` and zeroed progress. No funds
    are moved or reserved.
    """
    name = (name or "").strip()
    if not (2 <= len(name) <= 80):
        raise GoalError("Goal name must be between 2 and 80 characters.")
    try:
        target_cents = int(target_cents)
    except (TypeError, ValueError):
        raise GoalError("target_cents must be an integer number of cents.")
    if target_cents <= 0:
        raise GoalError("target_cents must be a positive number of cents.")
    if target_cents > _MAX_TARGET_CENTS:
        raise GoalError("target_cents exceeds the maximum allowed value.")

    record = {
        "id": str(uuid.uuid4()),
        "name": name,
        "target_cents": target_cents,
        "deadline": deadline,
        "created_at": _now_iso(),
    }

    db.add(UserAuditLog(
        user_id=user_id,
        action=GOAL_ACTION,
        details=json.dumps(record),
        retention_expires_at=_retention_expiry(),
    ))
    db.commit()

    return _view(record, saved_cents=0)


def list_goals(db: Session, user_id: str, include_archived: bool = False) -> dict:
    """List this user's savings goals with computed progress.

    By default only active goals are returned. Pass include_archived=True to also
    include soft-archived goals (flagged status="archived").
    """
    records = _goal_records(db, user_id)
    archived = _archived_ids(db, user_id)
    saved = _saved_by_goal(db, user_id)

    goals = []
    for gid, record in records.items():
        is_archived = gid in archived
        if is_archived and not include_archived:
            continue
        goals.append(_view(record, saved_cents=saved.get(gid, 0), archived=is_archived))

    # Stable, newest-first ordering by creation time.
    goals.sort(key=lambda g: (g.get("created_at") or ""), reverse=True)

    return {
        "goals": goals,
        "count": len(goals),
        "tracking_only": True,
        "note": "Savings goals are tracking-only; contributions never move real funds.",
    }


def get_goal(db: Session, user_id: str, goal_id: str) -> dict:
    """Return a single active goal view, or raise GoalNotFound."""
    record = _require_goal(db, user_id, goal_id)
    saved = _saved_by_goal(db, user_id).get(goal_id, 0)
    return _view(record, saved_cents=saved)


def log_contribution(db: Session, user_id: str, goal_id: str, amount_cents: int) -> dict:
    """Record a TRACKING contribution toward a goal.

    This does NOT move, hold, or reserve any real money. It only appends a
    tracking entry to UserAuditLog and recomputes progress. User.usdc_balance_cents
    is never read or written here.
    """
    record = _require_goal(db, user_id, goal_id)

    try:
        amount_cents = int(amount_cents)
    except (TypeError, ValueError):
        raise GoalError("amount_cents must be an integer number of cents.")
    if amount_cents <= 0:
        raise GoalError("amount_cents must be a positive number of cents.")
    if amount_cents > _MAX_CONTRIB_CENTS:
        raise GoalError("amount_cents exceeds the maximum allowed value.")

    db.add(UserAuditLog(
        user_id=user_id,
        action=CONTRIBUTION_ACTION,
        details=json.dumps({
            "goal_id": goal_id,
            "amount_cents": amount_cents,
            "created_at": _now_iso(),
            "tracking_only": True,  # explicit: no real balance changed
        }),
        retention_expires_at=_retention_expiry(),
    ))
    db.commit()

    saved = _saved_by_goal(db, user_id).get(goal_id, 0)
    view = _view(record, saved_cents=saved)
    view["contributed_cents"] = amount_cents
    view["message"] = (
        f"Tracked ${amount_cents / 100:.2f} toward '{record.get('name')}'. "
        "This is a tracking entry only — no funds were moved."
    )
    return view


def archive_goal(db: Session, user_id: str, goal_id: str) -> dict:
    """Soft-archive (delete) a goal.

    Because UserAuditLog is an append-only compliance trail, deletion is modeled
    as an appended tombstone row rather than a physical delete. The goal then
    disappears from the default list. No funds are affected.
    """
    _require_goal(db, user_id, goal_id)  # 404 if unknown / already archived

    db.add(UserAuditLog(
        user_id=user_id,
        action=ARCHIVE_ACTION,
        details=json.dumps({
            "goal_id": goal_id,
            "archived_at": _now_iso(),
        }),
        retention_expires_at=_retention_expiry(),
    ))
    db.commit()

    return {
        "id": goal_id,
        "status": "archived",
        "tracking_only": True,
        "message": "Savings goal archived. No funds were moved.",
    }
