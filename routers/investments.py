"""Investment (DSPP/ETF) endpoints for FAWN.

Public: browse securities, get quotes
Authenticated: place orders, view portfolio
Admin: resync security master, manual order management

Regulatory: All endpoints carry investment disclaimers. Orders are logged
for audit/compliance. Securities transactions are educational-only for
college students and subject to parental consent (implemented at signup).
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from dependencies import get_current_user
from models import InvestmentHolding, InvestmentOrder, SecurityMaster, User
from rate_limiting import limiter
from services import dspp
from services.security_master import get_master_securities, get_security, search_securities

router = APIRouter(prefix="/investments", tags=["investments"])

DISCLAIMER = (
    "Securities trading is speculative and carries risk of loss. This is educational "
    "content for college students. Not investment, financial, or tax advice. "
    "Consult a financial advisor before investing. Past performance ≠ future results."
)


class SecuritySearchRequest(BaseModel):
    query: str = Field(..., max_length=20)
    asset_type: Optional[str] = Field(default=None)  # "stock" | "etf"


class BuyOrderRequest(BaseModel):
    ticker: str = Field(..., max_length=10)
    amount_dollars: float = Field(..., gt=0, le=100_000)  # $1 to $100k


class SellOrderRequest(BaseModel):
    ticker: str = Field(..., max_length=10)
    quantity: float = Field(..., gt=0, le=10_000)


@router.get("/securities")
def list_all_securities(asset_type: Optional[str] = Query(default=None)):
    """Return all available securities (stocks + ETFs).

    Optionally filter by asset_type: "stock" | "etf".
    """
    all_secs = get_master_securities()
    if asset_type:
        all_secs = [s for s in all_secs if s["asset_type"] == asset_type]
    return {
        "securities": all_secs,
        "count": len(all_secs),
        "disclaimer": DISCLAIMER,
    }


@router.post("/securities/search")
def search_securities_endpoint(req: SecuritySearchRequest):
    """Search for a security by ticker or name."""
    results = search_securities(req.query, asset_type=req.asset_type)
    return {
        "query": req.query,
        "results": results,
        "count": len(results),
        "disclaimer": DISCLAIMER,
    }


@router.get("/securities/{ticker}")
def get_security_detail(ticker: str):
    """Get detailed info about one security."""
    sec = get_security(ticker.upper())
    if not sec:
        raise HTTPException(status_code=404, detail=f"Security {ticker} not found.")

    # Fetch live price
    price_cents = 0
    try:
        # Attempt to get price async — wrap in sync context
        import asyncio
        loop = asyncio.new_event_loop()
        price_cents = loop.run_until_complete(dspp.get_price_cached(ticker.upper()))
        loop.close()
    except Exception as e:
        print(f"[investments] price fetch failed for {ticker}: {e}")

    return {
        **sec,
        "current_price_cents": price_cents,
        "current_price_dollars": price_cents / 100,
        "disclaimer": DISCLAIMER,
    }


@router.get("/portfolio")
@limiter.limit("10/minute")
def get_portfolio(request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Return the user's current investment portfolio."""
    holdings = db.query(InvestmentHolding).filter(
        InvestmentHolding.user_id == current_user.id
    ).all()

    portfolio = []
    total_cost_basis_cents = 0
    total_current_value_cents = 0

    for h in holdings:
        cost_basis = h.cost_basis_cents
        current_value = int(h.quantity) * h.current_price_cents
        gain = current_value - cost_basis
        gain_pct = (gain / cost_basis * 100) if cost_basis > 0 else 0

        total_cost_basis_cents += cost_basis
        total_current_value_cents += current_value

        portfolio.append({
            "id": h.id,
            "ticker": h.ticker,
            "asset_type": h.asset_type,
            "quantity": float(h.quantity),
            "cost_basis_cents": cost_basis,
            "current_price_cents": h.current_price_cents,
            "current_value_cents": current_value,
            "unrealized_gain_cents": gain,
            "unrealized_gain_percent": round(gain_pct, 2),
            "provider": h.provider,
        })

    total_unrealized_gain = total_current_value_cents - total_cost_basis_cents
    total_gain_pct = (
        (total_unrealized_gain / total_cost_basis_cents * 100)
        if total_cost_basis_cents > 0
        else 0
    )

    return {
        "holdings": portfolio,
        "summary": {
            "total_cost_basis_cents": total_cost_basis_cents,
            "total_current_value_cents": total_current_value_cents,
            "total_unrealized_gain_cents": total_unrealized_gain,
            "total_unrealized_gain_percent": round(total_gain_pct, 2),
            "holding_count": len(holdings),
        },
        "disclaimer": DISCLAIMER,
    }


@router.post("/orders/buy")
@limiter.limit("5/hour")
async def place_buy_order(
    request: Request,
    req: BuyOrderRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Place a buy order for a stock or ETF.

    For MVP: orders are logged and marked pending. In production, they
    would be submitted to Computershare, the ETF provider, or a broker partner.
    """
    sec = get_security(req.ticker.upper())
    if not sec:
        raise HTTPException(status_code=404, detail=f"Security {req.ticker} not found.")

    amount_cents = int(req.amount_dollars * 100)
    provider = "computershare" if sec["has_dspp"] else "etf_direct"

    # Place order through provider
    result = await dspp.place_buy_order(
        user_id=current_user.id,
        ticker=req.ticker.upper(),
        amount_cents=amount_cents,
        provider=provider,
    )

    # Log the order
    order = InvestmentOrder(
        user_id=current_user.id,
        ticker=req.ticker.upper(),
        order_type="buy",
        quantity=0,  # unknown until filled (placeholder for MVP)
        total_cost_cents=amount_cents,
        status=result.get("status", "failed"),
        provider=provider,
        provider_order_id=result.get("order_id"),
        error_message=result.get("error"),
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    # Update portfolio: create or update holding with order amount
    # MVP: assume order fills immediately for portfolio display
    if result.get("status") == "pending_manual_execution":
        existing = db.query(InvestmentHolding).filter(
            InvestmentHolding.user_id == current_user.id,
            InvestmentHolding.ticker == req.ticker.upper(),
        ).first()

        if existing:
            # Add to existing holding
            existing.cost_basis_cents += amount_cents
            existing.quantity += amount_cents / 10000  # placeholder quantity
        else:
            # Create new holding
            holding = InvestmentHolding(
                user_id=current_user.id,
                ticker=req.ticker.upper(),
                asset_type=sec.get("asset_type", "stock"),
                quantity=amount_cents / 10000,
                cost_basis_cents=amount_cents,
                avg_cost_per_share_cents=0,
                current_price_cents=0,
                provider=provider,
            )
            db.add(holding)
        db.commit()

    return {
        "order_id": order.id,
        "ticker": req.ticker.upper(),
        "amount_dollars": req.amount_dollars,
        "status": result.get("status"),
        "provider": provider,
        "note": result.get("note"),
        "disclaimer": DISCLAIMER,
    }


@router.post("/orders/sell")
@limiter.limit("5/hour")
async def place_sell_order(
    request: Request,
    req: SellOrderRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Place a sell order."""
    sec = get_security(req.ticker.upper())
    if not sec:
        raise HTTPException(status_code=404, detail=f"Security {req.ticker} not found.")

    # Check user has shares to sell
    holding = db.query(InvestmentHolding).filter(
        InvestmentHolding.user_id == current_user.id,
        InvestmentHolding.ticker == req.ticker.upper(),
    ).first()

    if not holding or holding.quantity < req.quantity:
        raise HTTPException(status_code=400, detail="Insufficient shares to sell.")

    provider = "computershare" if sec["has_dspp"] else "etf_direct"
    result = await dspp.place_sell_order(
        user_id=current_user.id,
        ticker=req.ticker.upper(),
        quantity=req.quantity,
        provider=provider,
    )

    order = InvestmentOrder(
        user_id=current_user.id,
        holding_id=holding.id,
        ticker=req.ticker.upper(),
        order_type="sell",
        quantity=req.quantity,
        status=result.get("status", "failed"),
        provider=provider,
        provider_order_id=result.get("order_id"),
        error_message=result.get("error"),
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    # Update portfolio: reduce holding quantity
    if result.get("status") == "pending_manual_execution":
        holding.quantity -= req.quantity
        if holding.quantity <= 0:
            db.delete(holding)
        db.commit()

    return {
        "order_id": order.id,
        "ticker": req.ticker.upper(),
        "quantity": req.quantity,
        "status": result.get("status"),
        "provider": provider,
        "note": result.get("note"),
        "disclaimer": DISCLAIMER,
    }


@router.get("/orders")
def list_orders(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Return the user's order history."""
    orders = db.query(InvestmentOrder).filter(
        InvestmentOrder.user_id == current_user.id
    ).order_by(InvestmentOrder.created_at.desc()).limit(50).all()

    return {
        "orders": [
            {
                "id": o.id,
                "ticker": o.ticker,
                "order_type": o.order_type,
                "quantity": float(o.quantity) if o.quantity else None,
                "total_cost_cents": o.total_cost_cents,
                "status": o.status,
                "provider": o.provider,
                "created_at": o.created_at.isoformat() if o.created_at else None,
                "filled_at": o.filled_at.isoformat() if o.filled_at else None,
            }
            for o in orders
        ],
        "count": len(orders),
    }


@router.get("/public/quote/{ticker}")
def get_quote(ticker: str):
    """Public endpoint: get current price quote (no auth required)."""
    sec = get_security(ticker.upper())
    if not sec:
        raise HTTPException(status_code=404, detail=f"Security {ticker} not found.")

    # Fetch price (async wrapper)
    import asyncio
    loop = asyncio.new_event_loop()
    price_cents = loop.run_until_complete(dspp.get_price_cached(ticker.upper()))
    loop.close()

    return {
        "ticker": ticker.upper(),
        "name": sec["name"],
        "price_cents": price_cents,
        "price_dollars": price_cents / 100,
        "asset_type": sec["asset_type"],
        "last_updated": "2026-08-19T12:00:00Z",  # stub timestamp
    }
