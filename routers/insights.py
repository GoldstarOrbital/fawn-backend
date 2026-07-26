"""Spending / cashflow insight endpoints.

Thin, READ-ONLY wrappers over services.insights. No writes, no balance
changes, no transfers, no on-chain calls — these endpoints only summarize a
user's existing deposit and transfer history.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database import get_db
from dependencies import get_current_user
from models import User
from services import insights

router = APIRouter(prefix="/insights", tags=["insights"])


@router.get("/cashflow")
def get_cashflow(
    months: int = Query(6, ge=1, le=24, description="How many recent months to include."),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Per-month money-in / money-out / net for the last `months` months."""
    series = insights.monthly_cashflow(db, current_user.id, months=months)
    return {"months": months, "cashflow": series}


@router.get("/top-counterparties")
def get_top_counterparties(
    limit: int = Query(5, ge=1, le=50, description="Max recipients to return."),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The recipients this user has sent the most USDC to (completed sends)."""
    counterparties = insights.top_counterparties(db, current_user.id, limit=limit)
    return {"counterparties": counterparties, "count": len(counterparties)}


@router.get("/recurring")
def get_recurring(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Likely recurring payments (>=3 roughly-regular sends to a recipient)."""
    recurring = insights.detect_recurring(db, current_user.id)
    return {"recurring": recurring, "count": len(recurring)}
