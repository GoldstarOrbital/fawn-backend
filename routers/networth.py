"""Unified Net Worth API for FAWN.

Two read endpoints that aggregate a user's total net worth across cash (USDC
ledger balance), investments (Alpaca brokerage equity, guarded), and linked
bank accounts (Plaid — currently reported as 0, see the service for why):

    GET /networth            -- current net worth (also records a snapshot)
    GET /networth/breakdown  -- detailed breakdown, components, and history

This router is a THIN wrapper: all logic lives in services/networth.py as pure,
directly-unit-testable functions. It is strictly read/tracking-only and moves
no money. Handlers are sync ``def`` (run in FastAPI's threadpool) so the
service's guarded async Alpaca call runs without event-loop re-entrancy.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database import get_db
from dependencies import get_current_user
from models import User
from services import networth as networth_svc

router = APIRouter(prefix="/networth", tags=["networth"])


@router.get("")
def get_net_worth(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the user's current unified net worth and record a snapshot.

    The snapshot is an append-only UserAuditLog row (tracking-only); no funds
    move and no balance is modified.
    """
    try:
        return networth_svc.record_net_worth_snapshot(db, current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/breakdown")
def get_net_worth_breakdown(
    history_limit: int = Query(30, ge=1, le=365),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the detailed net-worth breakdown, per-source components, and the
    recent snapshot history. Pure read — records nothing."""
    try:
        current = networth_svc.compute_net_worth(db, current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    history = networth_svc.get_net_worth_history(db, current_user.id, limit=history_limit)
    return {**current, "history": history}
