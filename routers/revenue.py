"""
Revenue Intelligence Dashboard - Admin only
Tracks where ALL transaction revenue goes. Complete financial transparency.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func, text
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
import json
from database import get_db
from dependencies import get_current_user
from models import User, UserAuditLog

router = APIRouter(prefix="/admin/revenue", tags=["admin-revenue"])


# ── MODELS ──

class RevenueSummary(BaseModel):
    period: str  # "today", "week", "month", "all_time"
    total_revenue_cents: int
    revenue_breakdown: dict
    top_users: list
    timestamp: datetime


# ── ADMIN AUTH ──

async def verify_admin(current_user: User = Depends(get_current_user)):
    """Verify user is admin. Only founder (@founder) can access revenue."""
    if current_user.username != "founder":
        raise HTTPException(status_code=403, detail="Revenue dashboard is admin-only")
    return current_user


# ── REVENUE ENDPOINTS ──

@router.get("/summary")
async def get_revenue_summary(
    admin: User = Depends(verify_admin),
    db: Session = Depends(get_db),
    period: str = "month",
):
    """Get complete revenue summary for period."""
    now = datetime.now(tz=timezone.utc)

    # Calculate period
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = now - timedelta(days=7)
    elif period == "month":
        start = now - timedelta(days=30)
    else:
        start = None

    # Query transfers for fees
    query = """
    SELECT
        SUM(CAST(platform_fee_cents AS INTEGER)) as total_fees,
        COUNT(*) as transaction_count,
        COUNT(DISTINCT user_id) as unique_users
    FROM crypto_trades
    WHERE status = 'completed'
    """
    if start:
        query += f" AND completed_at >= '{start.isoformat()}'"

    result = db.query(text(query)).first()

    total_fees = result[0] if result and result[0] else 0
    transaction_count = result[1] if result else 0
    unique_users = result[2] if result else 0

    # Revenue breakdown
    breakdown_query = """
    SELECT
        CASE
            WHEN to_token = 'USDC' THEN 'USDC Swap'
            WHEN to_token = 'ETH' THEN 'ETH Swap'
            WHEN to_token = 'MATIC' THEN 'MATIC Swap'
            ELSE 'Other'
        END as category,
        SUM(CAST(platform_fee_cents AS INTEGER)) as revenue,
        COUNT(*) as count
    FROM crypto_trades
    WHERE status = 'completed'
    """
    if start:
        breakdown_query += f" AND completed_at >= '{start.isoformat()}'"
    breakdown_query += " GROUP BY category"

    breakdown_results = db.query(text(breakdown_query)).all()

    revenue_breakdown = {
        row[0]: {
            "revenue_cents": row[1],
            "transaction_count": row[2],
            "avg_fee_cents": row[1] // max(row[2], 1),
        }
        for row in breakdown_results
    }

    # Top revenue-generating users
    top_users_query = """
    SELECT
        user_id,
        COUNT(*) as transaction_count,
        SUM(CAST(platform_fee_cents AS INTEGER)) as total_fees_generated
    FROM crypto_trades
    WHERE status = 'completed'
    """
    if start:
        top_users_query += f" AND completed_at >= '{start.isoformat()}'"
    top_users_query += " GROUP BY user_id ORDER BY total_fees_generated DESC LIMIT 10"

    top_users_results = db.query(text(top_users_query)).all()

    top_users = [
        {
            "user_id": row[0],
            "transaction_count": row[1],
            "revenue_generated_cents": row[2],
            "revenue_generated_usd": f"${row[2]/100:.2f}",
        }
        for row in top_users_results
    ]

    return {
        "period": period,
        "start_date": start.isoformat() if start else None,
        "end_date": now.isoformat(),
        "total_revenue_cents": total_fees,
        "total_revenue_usd": f"${total_fees/100:.2f}",
        "metrics": {
            "total_transactions": transaction_count,
            "unique_users": unique_users,
            "avg_fee_per_transaction": f"${total_fees/max(transaction_count, 1)/100:.4f}",
        },
        "revenue_breakdown": revenue_breakdown,
        "top_users": top_users,
        "timestamp": now.isoformat(),
    }


@router.get("/daily")
async def get_daily_revenue(
    admin: User = Depends(verify_admin),
    db: Session = Depends(get_db),
    days: int = 30,
):
    """Get daily revenue trend for last N days."""
    now = datetime.now(tz=timezone.utc)
    start = now - timedelta(days=days)

    query = """
    SELECT
        DATE(completed_at) as date,
        SUM(CAST(platform_fee_cents AS INTEGER)) as daily_revenue,
        COUNT(*) as transaction_count
    FROM crypto_trades
    WHERE status = 'completed' AND completed_at >= :start_date
    GROUP BY DATE(completed_at)
    ORDER BY date DESC
    """

    results = db.query(text(query)).params(start_date=start).all()

    daily_data = [
        {
            "date": str(row[0]),
            "revenue_cents": row[1],
            "revenue_usd": f"${row[1]/100:.2f}",
            "transaction_count": row[2],
            "avg_fee": f"${row[1]/max(row[2], 1)/100:.4f}",
        }
        for row in results
    ]

    return {"days": days, "daily_revenue": daily_data}


@router.get("/distribution")
async def get_revenue_distribution(
    admin: User = Depends(verify_admin),
    db: Session = Depends(get_db),
):
    """Where does revenue go? (FAWN treasury, partnerships, etc)"""
    now = datetime.now(tz=timezone.utc)

    # Assume: 100% FAWN treasury for now (no partnerships)
    # This can be expanded as FAWN adds partnerships

    total_query = """
    SELECT SUM(CAST(platform_fee_cents AS INTEGER)) as total
    FROM crypto_trades WHERE status = 'completed'
    """

    total = db.query(text(total_query)).first()[0] or 0

    distribution = {
        "FAWN Treasury": {
            "amount_cents": total,
            "amount_usd": f"${total/100:.2f}",
            "percentage": 100,
            "description": "All transaction fees go to FAWN treasury for operations & development",
        },
        "Partner Revenue": {
            "amount_cents": 0,
            "amount_usd": "$0.00",
            "percentage": 0,
            "description": "Reserved for future partnerships (Alchemy, Ramp, etc)",
        },
    }

    return {
        "distribution": distribution,
        "total_revenue_cents": total,
        "total_revenue_usd": f"${total/100:.2f}",
        "timestamp": now.isoformat(),
        "note": "100% of revenue currently goes to FAWN treasury",
    }


@router.get("/reconciliation")
async def get_revenue_reconciliation(
    admin: User = Depends(verify_admin),
    db: Session = Depends(get_db),
):
    """Reconciliation: Revenue collected vs. Fees paid out."""
    # Revenue collected from trades
    revenue_query = """
    SELECT SUM(CAST(platform_fee_cents AS INTEGER)) as total
    FROM crypto_trades WHERE status = 'completed'
    """
    revenue_collected = db.query(text(revenue_query)).first()[0] or 0

    # Fees paid to users (if any refunds/adjustments)
    # For now, FAWN doesn't pay out refunds, so this is 0
    fees_paid_out = 0

    # Net revenue
    net_revenue = revenue_collected - fees_paid_out

    return {
        "revenue_collected_cents": revenue_collected,
        "revenue_collected_usd": f"${revenue_collected/100:.2f}",
        "fees_paid_out_cents": fees_paid_out,
        "fees_paid_out_usd": f"${fees_paid_out/100:.2f}",
        "net_revenue_cents": net_revenue,
        "net_revenue_usd": f"${net_revenue/100:.2f}",
        "reconciliation_status": "BALANCED" if revenue_collected == net_revenue else "MISMATCH",
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
    }


@router.get("/projections")
async def get_revenue_projections(
    admin: User = Depends(verify_admin),
    db: Session = Depends(get_db),
):
    """Project revenue based on historical trends."""
    # Get last 7 days of data
    seven_days_ago = datetime.now(tz=timezone.utc) - timedelta(days=7)

    query = """
    SELECT SUM(CAST(platform_fee_cents AS INTEGER)) as weekly_revenue
    FROM crypto_trades
    WHERE status = 'completed' AND completed_at >= :start_date
    """

    weekly_revenue = db.query(text(query)).params(start_date=seven_days_ago).first()[0] or 0
    daily_average = weekly_revenue / 7

    projections = {
        "daily_average_cents": int(daily_average),
        "daily_average_usd": f"${daily_average/100:.2f}",
        "weekly_projection_cents": int(daily_average * 7),
        "weekly_projection_usd": f"${daily_average * 7 / 100:.2f}",
        "monthly_projection_cents": int(daily_average * 30),
        "monthly_projection_usd": f"${daily_average * 30 / 100:.2f}",
        "yearly_projection_cents": int(daily_average * 365),
        "yearly_projection_usd": f"${daily_average * 365 / 100:.2f}",
        "confidence": "Medium (based on last 7 days)",
    }

    return projections
