"""Savings Goals API for FAWN — TRACKING ONLY (no fund movement).

Endpoints let a user create personal savings goals and record *tracking*
contributions toward them to watch their progress. IMPORTANT SAFETY NOTE:

    Contributions recorded here are TRACKING ENTRIES ONLY. Nothing in this
    router moves, holds, reserves, or escrows real money. It never touches
    User.usdc_balance_cents, never creates a CryptoTransfer/CryptoDeposit, and
    never calls any send / settlement / on-chain code. The user's spendable
    USDC balance is completely unaffected by anything here.

State is persisted append-only in the existing UserAuditLog table as JSON (see
services/goals.py) — no new tables or migrations. This router is a thin wrapper;
all logic lives in services/goals.py so it can be unit-tested without HTTP.

Endpoints (prefix /goals):
    POST   /goals                     create a goal
    GET    /goals                     list goals (optionally include archived)
    POST   /goals/{goal_id}/contribute  record a tracking contribution
    DELETE /goals/{goal_id}           archive (soft-delete) a goal
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from dependencies import get_current_user
from models import User
from services import goals as goals_service

router = APIRouter(prefix="/goals", tags=["savings-goals"])


# ── Request bodies ──

class CreateGoalRequest(BaseModel):
    name: str = Field(min_length=2, max_length=80, description="Goal name, e.g. 'Emergency Fund'")
    target_cents: int = Field(ge=1, le=1_000_000_000, description="Target amount in cents")
    deadline: Optional[str] = Field(
        default=None,
        description="Optional target date as an ISO-8601 string (informational only)",
    )


class ContributeRequest(BaseModel):
    amount_cents: int = Field(
        ge=1,
        le=1_000_000_000,
        description="Tracking-only amount in cents. Does NOT move real funds.",
    )


# ── Endpoints ──

@router.post("")
def create_goal(
    req: CreateGoalRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new savings goal (tracking only). Returns the goal with a stable id."""
    try:
        return goals_service.create_goal(
            db, current_user.id, req.name, req.target_cents, req.deadline
        )
    except goals_service.GoalError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("")
def list_goals(
    include_archived: bool = Query(False, description="Include soft-archived goals"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List the current user's savings goals with computed progress."""
    return goals_service.list_goals(db, current_user.id, include_archived=include_archived)


@router.post("/{goal_id}/contribute")
def contribute(
    goal_id: str,
    req: ContributeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Record a TRACKING contribution toward a goal. No real funds are moved."""
    try:
        return goals_service.log_contribution(db, current_user.id, goal_id, req.amount_cents)
    except goals_service.GoalNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except goals_service.GoalError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/{goal_id}")
def delete_goal(
    goal_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Archive (soft-delete) a savings goal. No funds are affected."""
    try:
        return goals_service.archive_goal(db, current_user.id, goal_id)
    except goals_service.GoalNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except goals_service.GoalError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
