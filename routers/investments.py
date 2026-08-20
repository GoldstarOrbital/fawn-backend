"""Investment (DSPP/ETF) endpoints for FAWN.

Public: browse securities, get quotes
Authenticated: place orders, view portfolio
Admin: resync security master, manual order management

Regulatory: All endpoints carry investment disclaimers. Orders are logged
for audit/compliance. Securities transactions are educational-only for
college students and subject to parental consent (implemented at signup).
"""
from decimal import Decimal
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
    """Get detailed info about one security with full performance data."""
    sec = get_security(ticker.upper())
    if not sec:
        raise HTTPException(status_code=404, detail=f"Security {ticker} not found.")

    # Fetch live price
    price_cents = 0
    try:
        import asyncio
        loop = asyncio.new_event_loop()
        price_cents = loop.run_until_complete(dspp.get_price_cached(ticker.upper()))
        loop.close()
    except Exception as e:
        print(f"[investments] price fetch failed for {ticker}: {e}")

    # Return all security fields plus live price
    return {
        **sec,
        "current_price_cents": price_cents,
        "current_price_dollars": price_cents / 100,
        "disclaimer": DISCLAIMER,
    }


@router.get("/portfolio")
@limiter.limit("10/minute")
async def get_portfolio(request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Return the user's current investment portfolio."""
    holdings = db.query(InvestmentHolding).filter(
        InvestmentHolding.user_id == current_user.id
    ).all()

    portfolio = []
    total_cost_basis_cents = 0
    total_current_value_cents = 0

    for h in holdings:
        # Refresh live price for each holding
        try:
            live_price_cents = await dspp.get_price_cached(h.ticker)
            if live_price_cents > 0:
                h.current_price_cents = live_price_cents
        except Exception as e:
            print(f"[investments] portfolio price refresh failed for {h.ticker}: {e}")

        cost_basis = h.cost_basis_cents
        quantity = float(h.quantity)
        current_value = round(quantity * h.current_price_cents)
        gain = current_value - cost_basis
        gain_pct = (gain / cost_basis * 100) if cost_basis > 0 else 0

        total_cost_basis_cents += cost_basis
        total_current_value_cents += current_value

        portfolio.append({
            "id": h.id,
            "ticker": h.ticker,
            "asset_type": h.asset_type,
            "quantity": quantity,
            "avg_cost_per_share_cents": h.avg_cost_per_share_cents,
            "cost_basis_cents": cost_basis,
            "current_price_cents": h.current_price_cents,
            "current_value_cents": current_value,
            "unrealized_gain_cents": gain,
            "unrealized_gain_percent": round(gain_pct, 4),
            "provider": h.provider,
        })

    db.commit()

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
            "total_unrealized_gain_percent": round(total_gain_pct, 4),
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

    # Fetch live price to compute actual share quantity
    price_per_share_cents = await dspp.get_price_cached(req.ticker.upper())
    if price_per_share_cents <= 0:
        raise HTTPException(
            status_code=503,
            detail=f"Unable to fetch a live price for {req.ticker}. Please try again shortly.",
        )

    quantity = Decimal(str(amount_cents)) / Decimal(str(price_per_share_cents))

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
        quantity=quantity,
        price_per_share_cents=price_per_share_cents,
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
            # Add to existing holding, recompute weighted-average cost per share
            new_cost_basis = existing.cost_basis_cents + amount_cents
            new_quantity = Decimal(str(existing.quantity)) + quantity
            existing.cost_basis_cents = new_cost_basis
            existing.quantity = new_quantity
            existing.avg_cost_per_share_cents = int(new_cost_basis / float(new_quantity)) if new_quantity > 0 else 0
            existing.current_price_cents = price_per_share_cents
            order.holding_id = existing.id
        else:
            # Create new holding
            holding = InvestmentHolding(
                user_id=current_user.id,
                ticker=req.ticker.upper(),
                asset_type=sec.get("asset_type", "stock"),
                quantity=quantity,
                cost_basis_cents=amount_cents,
                avg_cost_per_share_cents=price_per_share_cents,
                current_price_cents=price_per_share_cents,
                provider=provider,
            )
            db.add(holding)
            db.flush()
            order.holding_id = holding.id
        db.commit()

    return {
        "order_id": str(order.id),
        "ticker": req.ticker.upper(),
        "amount_dollars": req.amount_dollars,
        "quantity": float(quantity),
        "price_per_share_cents": price_per_share_cents,
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

    if not holding:
        raise HTTPException(status_code=400, detail="No holdings of this security.")

    # Round to the same precision as the stored column (8 decimals) to avoid
    # floating-point dust causing spurious "insufficient shares" failures.
    sell_qty = Decimal(str(req.quantity)).quantize(Decimal("0.00000001"))
    held_qty = Decimal(str(holding.quantity)).quantize(Decimal("0.00000001"))

    if held_qty < sell_qty:
        raise HTTPException(status_code=400, detail="Insufficient shares to sell.")

    # Fetch live price to value the sale
    price_per_share_cents = await dspp.get_price_cached(req.ticker.upper())

    provider = "computershare" if sec["has_dspp"] else "etf_direct"
    result = await dspp.place_sell_order(
        user_id=current_user.id,
        ticker=req.ticker.upper(),
        quantity=float(sell_qty),
        provider=provider,
    )

    proceeds_cents = round(float(sell_qty) * price_per_share_cents) if price_per_share_cents > 0 else None

    order = InvestmentOrder(
        user_id=current_user.id,
        holding_id=holding.id,
        ticker=req.ticker.upper(),
        order_type="sell",
        quantity=sell_qty,
        price_per_share_cents=price_per_share_cents if price_per_share_cents > 0 else None,
        total_cost_cents=proceeds_cents,
        status=result.get("status", "failed"),
        provider=provider,
        provider_order_id=result.get("order_id"),
        error_message=result.get("error"),
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    # Update portfolio: reduce holding quantity, proportionally reduce cost basis
    if result.get("status") == "pending_manual_execution":
        remaining_qty = held_qty - sell_qty
        if remaining_qty <= 0:
            db.delete(holding)
        else:
            # Reduce cost basis proportionally so avg cost per share is preserved
            fraction_sold = float(sell_qty) / float(held_qty)
            holding.cost_basis_cents = round(holding.cost_basis_cents * (1 - fraction_sold))
            holding.quantity = remaining_qty
            if price_per_share_cents > 0:
                holding.current_price_cents = price_per_share_cents
        db.commit()

    return {
        "order_id": str(order.id),
        "ticker": req.ticker.upper(),
        "quantity": float(sell_qty),
        "price_per_share_cents": price_per_share_cents if price_per_share_cents > 0 else None,
        "proceeds_cents": proceeds_cents,
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
                "price_per_share_cents": o.price_per_share_cents,
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
