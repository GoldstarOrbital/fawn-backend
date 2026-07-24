"""
Automation APIs for FAWN - recurring transfers, price alerts, savings goals, etc.

Services:
1. Recurring Transfers - Auto-send USDC on schedule
2. Price Alerts - Notify on token price thresholds
3. Savings Goals - Auto-allocate to savings wallets
4. DCA (Dollar Cost Averaging) - Auto-buy tokens on schedule
5. Portfolio Metrics - Real-time portfolio stats
"""
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from datetime import datetime, timedelta, timezone
import json
from database import get_db
from dependencies import get_current_user
from models import User, UserAuditLog
from sqlalchemy import Column, String, Integer, DateTime, Boolean, Float, ForeignKey, text

router = APIRouter(prefix="/automation", tags=["automation"])


# ── MODELS ──

class RecurringTransfer(BaseModel):
    recipient_address: str = Field(min_length=3, max_length=120)  # 0x... or @username
    amount_cents: int = Field(ge=100, le=100_000)
    frequency: str = Field(pattern=r"^(weekly|biweekly|monthly)$")
    start_date: datetime
    end_date: datetime = None  # None = indefinite


class PriceAlert(BaseModel):
    token: str = Field(min_length=2, max_length=12, pattern=r"^[A-Za-z0-9]+$")  # "USDC", "USDT", "ETH", etc
    threshold_usd: float = Field(gt=0, le=1_000_000)
    direction: str = Field(pattern=r"^(above|below)$")
    notify_on_change: bool = True


class SavingsGoal(BaseModel):
    name: str = Field(min_length=2, max_length=60)  # "Emergency Fund", "Vacation", etc
    target_amount_cents: int = Field(ge=100, le=10_000_000)
    target_date: datetime
    auto_allocate_percent: float = Field(ge=0, le=50)


class DCAPlan(BaseModel):
    token: str = Field(min_length=1, max_length=12, pattern=r"^[A-Za-z0-9]+$")  # Token to buy
    amount_usd: float = Field(ge=1, le=1000)  # Amount per cycle
    frequency: str = Field(pattern=r"^(weekly|biweekly|monthly)$")
    start_date: datetime
    end_date: datetime = None


class PortfolioSnapshot(BaseModel):
    total_usdc_cents: int
    holdings: dict  # {token: amount_cents}
    updated_at: datetime


# ── RECURRING TRANSFERS ──

@router.post("/recurring-transfers")
async def create_recurring_transfer(
    req: RecurringTransfer,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = None,
):
    """Create automatic recurring transfer."""
    if not current_user.usdc_balance_cents:
        raise HTTPException(status_code=400, detail="No USDC balance to transfer")

    if req.amount_cents > current_user.usdc_balance_cents:
        raise HTTPException(status_code=400, detail="Amount exceeds balance")

    # Store recurring transfer config
    config = {
        "recipient": req.recipient_address,
        "amount_cents": req.amount_cents,
        "frequency": req.frequency,
        "start_date": req.start_date.isoformat(),
        "end_date": req.end_date.isoformat() if req.end_date else None,
        "next_execution": req.start_date.isoformat(),
        "status": "active",
        "created_at": datetime.utcnow().isoformat(),
    }

    # Audit log
    audit = UserAuditLog(
        user_id=current_user.id,
        action="recurring_transfer_created",
        details=json.dumps(config),
        retention_expires_at=datetime.now(tz=timezone.utc) + timedelta(days=365*7),
    )
    db.add(audit)
    db.commit()

    return {
        "status": "created",
        "config": config,
        "message": f"Recurring transfer scheduled: ${req.amount_cents/100:.2f} {req.frequency}",
    }


@router.get("/recurring-transfers")
async def list_recurring_transfers(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all active recurring transfers for user."""
    # Query audit logs for this user's recurring transfers
    recurring = db.query(UserAuditLog).filter(
        UserAuditLog.user_id == current_user.id,
        UserAuditLog.action == "recurring_transfer_created",
    ).all()

    transfers = []
    for log in recurring:
        try:
            config = json.loads(log.details)
            if config.get("status") == "active":
                transfers.append(config)
        except:
            pass

    return {"recurring_transfers": transfers, "count": len(transfers)}


# ── PRICE ALERTS ──

@router.post("/price-alerts")
async def create_price_alert(
    req: PriceAlert,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Set up price alert for token."""
    alert_config = {
        "token": req.token,
        "threshold_usd": req.threshold_usd,
        "direction": req.direction,
        "notify_on_change": req.notify_on_change,
        "active": True,
        "created_at": datetime.utcnow().isoformat(),
    }

    audit = UserAuditLog(
        user_id=current_user.id,
        action="price_alert_created",
        details=json.dumps(alert_config),
        retention_expires_at=datetime.now(tz=timezone.utc) + timedelta(days=365*7),
    )
    db.add(audit)
    db.commit()

    return {
        "status": "created",
        "alert": alert_config,
        "message": f"Alert: notify if {req.token} goes {req.direction} ${req.threshold_usd}",
    }


@router.get("/price-alerts")
async def list_price_alerts(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all active price alerts."""
    alerts = db.query(UserAuditLog).filter(
        UserAuditLog.user_id == current_user.id,
        UserAuditLog.action == "price_alert_created",
    ).all()

    active_alerts = []
    for log in alerts:
        try:
            alert = json.loads(log.details)
            if alert.get("active"):
                active_alerts.append(alert)
        except:
            pass

    return {"price_alerts": active_alerts, "count": len(active_alerts)}


# ── SAVINGS GOALS ──

@router.post("/savings-goals")
async def create_savings_goal(
    req: SavingsGoal,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create automated savings goal."""
    goal_config = {
        "name": req.name,
        "target_amount_cents": req.target_amount_cents,
        "target_date": req.target_date.isoformat(),
        "auto_allocate_percent": req.auto_allocate_percent,
        "current_balance_cents": 0,
        "progress_percent": 0,
        "status": "active",
        "created_at": datetime.utcnow().isoformat(),
    }

    audit = UserAuditLog(
        user_id=current_user.id,
        action="savings_goal_created",
        details=json.dumps(goal_config),
        retention_expires_at=datetime.now(tz=timezone.utc) + timedelta(days=365*7),
    )
    db.add(audit)
    db.commit()

    return {
        "status": "created",
        "goal": goal_config,
        "message": f"Savings goal: ${req.target_amount_cents/100:.2f} by {req.target_date.date()} ({req.auto_allocate_percent}% auto-allocation)",
    }


@router.get("/savings-goals")
async def list_savings_goals(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all savings goals."""
    goals = db.query(UserAuditLog).filter(
        UserAuditLog.user_id == current_user.id,
        UserAuditLog.action == "savings_goal_created",
    ).all()

    active_goals = []
    for log in goals:
        try:
            goal = json.loads(log.details)
            if goal.get("status") == "active":
                active_goals.append(goal)
        except:
            pass

    return {"savings_goals": active_goals, "count": len(active_goals)}


# ── DCA (DOLLAR COST AVERAGING) ──

@router.post("/dca-plans")
async def create_dca_plan(
    req: DCAPlan,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create DCA (Dollar Cost Averaging) plan to auto-buy tokens."""
    dca_config = {
        "token": req.token,
        "amount_usd": req.amount_usd,
        "frequency": req.frequency,
        "start_date": req.start_date.isoformat(),
        "end_date": req.end_date.isoformat() if req.end_date else None,
        "next_buy": req.start_date.isoformat(),
        "total_bought_usd": 0,
        "num_purchases": 0,
        "status": "active",
        "created_at": datetime.utcnow().isoformat(),
    }

    audit = UserAuditLog(
        user_id=current_user.id,
        action="dca_plan_created",
        details=json.dumps(dca_config),
        retention_expires_at=datetime.now(tz=timezone.utc) + timedelta(days=365*7),
    )
    db.add(audit)
    db.commit()

    return {
        "status": "created",
        "plan": dca_config,
        "message": f"DCA plan: auto-buy ${req.amount_usd:.2f} of {req.token} {req.frequency}",
    }


@router.get("/dca-plans")
async def list_dca_plans(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all active DCA plans."""
    plans = db.query(UserAuditLog).filter(
        UserAuditLog.user_id == current_user.id,
        UserAuditLog.action == "dca_plan_created",
    ).all()

    active_plans = []
    for log in plans:
        try:
            plan = json.loads(log.details)
            if plan.get("status") == "active":
                active_plans.append(plan)
        except:
            pass

    return {"dca_plans": active_plans, "count": len(active_plans)}


# ── PORTFOLIO METRICS ──

@router.get("/portfolio/metrics")
async def get_portfolio_metrics(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get real-time portfolio metrics."""
    # USDC balance
    usdc_balance = current_user.usdc_balance_cents / 100

    # Get trading history for P&L
    trades = db.query(text(
        "SELECT SUM(CAST(gain_loss_cents AS INTEGER)) as total_pnl FROM crypto_trades WHERE user_id = :user_id AND status = 'completed'"
    )).params(user_id=current_user.id).first()

    total_pnl_cents = trades[0] if trades and trades[0] else 0
    total_pnl_usd = total_pnl_cents / 100

    # Calculate metrics
    total_portfolio = usdc_balance + total_pnl_usd
    pnl_percent = (total_pnl_usd / max(usdc_balance, 1)) * 100 if usdc_balance > 0 else 0

    return {
        "usdc_balance_usd": usdc_balance,
        "trading_pnl_usd": total_pnl_usd,
        "pnl_percent": round(pnl_percent, 2),
        "total_portfolio_usd": round(total_portfolio, 2),
        "snapshot_time": datetime.utcnow().isoformat(),
    }


@router.get("/portfolio/summary")
async def get_portfolio_summary(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get complete portfolio summary with all automation status."""
    # Get automation counts
    recurring_transfers = db.query(UserAuditLog).filter(
        UserAuditLog.user_id == current_user.id,
        UserAuditLog.action == "recurring_transfer_created",
    ).count()

    price_alerts = db.query(UserAuditLog).filter(
        UserAuditLog.user_id == current_user.id,
        UserAuditLog.action == "price_alert_created",
    ).count()

    savings_goals = db.query(UserAuditLog).filter(
        UserAuditLog.user_id == current_user.id,
        UserAuditLog.action == "savings_goal_created",
    ).count()

    dca_plans = db.query(UserAuditLog).filter(
        UserAuditLog.user_id == current_user.id,
        UserAuditLog.action == "dca_plan_created",
    ).count()

    return {
        "username": current_user.username,
        "email": current_user.email,
        "usdc_balance_usd": current_user.usdc_balance_cents / 100,
        "automations": {
            "recurring_transfers": recurring_transfers,
            "price_alerts": price_alerts,
            "savings_goals": savings_goals,
            "dca_plans": dca_plans,
            "total_active": recurring_transfers + price_alerts + savings_goals + dca_plans,
        },
        "updated_at": datetime.utcnow().isoformat(),
    }
