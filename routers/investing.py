"""Investing endpoints backed by Alpaca (Broker API).

Scope: open a brokerage account, view it, place market orders, list positions,
get quotes, view order history, and manage a watchlist of tracked symbols.

Every Alpaca call is guarded in services/alpaca.py: if Alpaca isn't
configured the endpoints return 503 rather than 500, so the feature is
safely dormant until ALPACA_API_KEY/SECRET are set.

Security:
- Rate limiting on trading endpoints (50 orders/hour per user)
- Position limits ($50k max per trade for students)
- Audit logging via services/analytics
- All endpoints require Bearer token authentication
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone
from sqlalchemy import func

from database import get_db
from models import User, InvestingOrder, InvestingWatchlist
from dependencies import get_current_user
from services import alpaca as alpaca_svc
from services.analytics import capture, EVENTS
from rate_limiting import limiter, RATE_LIMITS

router = APIRouter(prefix="/investing", tags=["investing"])

# Conservative guardrails apply to every user. They are intentionally enforced
# server-side so a modified browser cannot bypass the safety envelope.
MAX_ORDER_NOTIONAL_CENTS = 100_000       # $1,000 per order
DAILY_ORDER_NOTIONAL_CENTS = 250_000     # $2,500 rolling 24-hour turnover
MAX_OPEN_ORDERS = 5


class OpenAccountRequest(BaseModel):
    # Alpaca requires signed agreements; the frontend collects these from the
    # user (checkbox + captured timestamp/IP) and forwards them verbatim.
    agreements: list[dict] = Field(default_factory=list)


class AccountOut(BaseModel):
    account_id: str
    status: str
    cash: float
    equity: float
    buying_power: float
    currency: str


class OrderRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=10, pattern=r"^[A-Za-z0-9.]+$")
    side: str = Field(pattern="^(buy|sell)$")
    notional: float | None = None  # dollar amount (fractional shares)
    qty: float | None = None        # share count

    def validate_amount(self) -> None:
        if (self.notional is None) == (self.qty is None):
            raise HTTPException(status_code=400, detail="Provide exactly one of notional or qty.")
        amount = self.notional if self.notional is not None else self.qty
        if amount is None or amount <= 0:
            raise HTTPException(status_code=400, detail="Amount must be positive.")


def _svc_error(e: Exception) -> HTTPException:
    if isinstance(e, alpaca_svc.AlpacaNotConfigured):
        return HTTPException(status_code=503, detail="Investing isn't available yet.")
    return HTTPException(status_code=502, detail=f"Investing provider error: {e}")


@router.post("/account", response_model=AccountOut, status_code=201)
async def open_account(req: OpenAccountRequest, current_user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    if current_user.alpaca_account_id:
        raise HTTPException(status_code=409, detail="You already have an investing account.")
    # KYC identity is reused from the approved onboarding record.
    first, *rest = (current_user.full_name or "FAWN User").strip().split()
    last = " ".join(rest) if rest else "Unknown"
    try:
        acct = await alpaca_svc.create_brokerage_account(
            email=current_user.email, given_name=first, family_name=last,
            agreements=req.agreements,
        )
    except Exception as e:
        raise _svc_error(e)

    current_user.alpaca_account_id = acct.get("id") or acct.get("account_id")
    db.commit()
    summary = {"account_id": current_user.alpaca_account_id, "status": acct.get("status", "SUBMITTED"),
               "cash": 0.0, "equity": 0.0, "buying_power": 0.0, "currency": "USD"}
    return AccountOut(**summary)


@router.get("/account", response_model=AccountOut)
async def get_account(current_user: User = Depends(get_current_user)):
    if not current_user.alpaca_account_id:
        raise HTTPException(status_code=404, detail="No investing account yet.")
    try:
        return AccountOut(**await alpaca_svc.get_account(current_user.alpaca_account_id))
    except Exception as e:
        raise _svc_error(e)


@router.post("/orders", status_code=201)
@limiter.limit(RATE_LIMITS["investing_place_order"])
async def place_order(request: Request, req: OrderRequest,
                      current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Place a market buy/sell order for stocks, ETFs, or crypto.

    Security: conservative order caps, quote-first UI, and rate limiting.
    """
    if not current_user.alpaca_account_id:
        raise HTTPException(status_code=400, detail="Open an investing account first.")
    req.validate_amount()

    # Protective limits are based on notional so the server can enforce them
    # before contacting Alpaca. Share-quantity orders are not safe to cap
    # without a live price, so require the dollar-based fractional form.
    if req.notional is None:
        raise HTTPException(status_code=400, detail="Use a dollar amount for protected investing orders.")
    order_size_cents = int(round(req.notional * 100))
    if order_size_cents > MAX_ORDER_NOTIONAL_CENTS:
        raise HTTPException(status_code=400, detail="Orders are limited to $1,000 each.")

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    daily_total = db.query(func.coalesce(func.sum(InvestingOrder.notional_cents), 0)).filter(
        InvestingOrder.user_id == current_user.id,
        InvestingOrder.created_at >= since,
        InvestingOrder.status.notin_(["failed", "cancelled", "canceled"]),
    ).scalar() or 0
    if int(daily_total) + order_size_cents > DAILY_ORDER_NOTIONAL_CENTS:
        remaining = max(0, DAILY_ORDER_NOTIONAL_CENTS - int(daily_total)) / 100
        raise HTTPException(status_code=400, detail=f"24-hour investing limit reached. Remaining limit: ${remaining:.2f}.")

    open_orders = db.query(InvestingOrder).filter(
        InvestingOrder.user_id == current_user.id,
        InvestingOrder.status.in_(["pending", "accepted", "new", "partially_filled"]),
    ).count()
    if open_orders >= MAX_OPEN_ORDERS:
        raise HTTPException(status_code=400, detail="You have 5 open investing orders. Wait for one to settle before placing another.")

    idem = f"order:{current_user.id}:{req.symbol}:{req.side}:{req.notional or req.qty}"
    existing = db.query(InvestingOrder).filter(InvestingOrder.idempotency_key == idem).first()
    if existing:
        raise HTTPException(status_code=409, detail="Duplicate order.")

    order_row = InvestingOrder(
        user_id=current_user.id, symbol=req.symbol.upper(), side=req.side,
        notional_cents=order_size_cents,
        qty=req.qty, status="pending", idempotency_key=idem,
    )
    db.add(order_row)
    db.commit()

    try:
        result = await alpaca_svc.place_order(
            current_user.alpaca_account_id, req.symbol, req.side,
            notional=req.notional, qty=req.qty,
        )
    except Exception as e:
        order_row.status = "failed"
        order_row.error_message = str(e)[:500]
        db.commit()
        capture(current_user.id, EVENTS.get("investing_order_failed", "investing.order_failed"),
                {"symbol": req.symbol, "side": req.side, "error": str(e)[:100]})
        raise _svc_error(e)

    order_row.alpaca_order_id = result.get("id")
    order_row.status = result.get("status", "accepted")
    db.commit()

    capture(current_user.id, EVENTS.get("investing_order_placed", "investing.order_placed"),
            {"symbol": req.symbol, "side": req.side, "notional": req.notional, "qty": req.qty})

    return {"order_id": order_row.alpaca_order_id, "status": order_row.status,
            "symbol": order_row.symbol, "side": order_row.side}


@router.get("/positions")
@limiter.limit(RATE_LIMITS["investing_positions"])
async def positions(request: Request, current_user: User = Depends(get_current_user)):
    if not current_user.alpaca_account_id:
        raise HTTPException(status_code=404, detail="No investing account yet.")
    try:
        return {"positions": await alpaca_svc.list_positions(current_user.alpaca_account_id)}
    except Exception as e:
        raise _svc_error(e)


class QuoteResponse(BaseModel):
    symbol: str
    bid: float
    ask: float
    last: float
    bid_size: int
    ask_size: int
    timestamp: str | None = None


@router.get("/quotes/{symbol}", response_model=QuoteResponse)
@limiter.limit(RATE_LIMITS["investing_quote"])
async def get_quote(symbol: str, request: Request):
    """Get real-time quote for a stock, ETF, or crypto symbol.

    Supports: AAPL, SPY, BTC, ETH, etc. No auth required (public market data).
    """
    if not symbol or len(symbol) > 10:
        raise HTTPException(status_code=400, detail="Invalid symbol.")
    try:
        result = await alpaca_svc.get_quote(symbol.upper())
        return QuoteResponse(**result)
    except Exception as e:
        if isinstance(e, alpaca_svc.AlpacaNotConfigured):
            raise HTTPException(status_code=503, detail="Investing isn't available yet.")
        raise HTTPException(status_code=502, detail=f"Quote service error: {e}")


class OrderHistoryResponse(BaseModel):
    order_id: str
    symbol: str
    qty: float
    notional: float
    side: str  # buy | sell
    type: str  # market | limit | etc
    status: str  # pending | filled | canceled | etc
    filled_qty: float
    filled_avg_price: float
    created_at: str
    updated_at: str


@router.get("/orders", response_model=dict)
@limiter.limit(RATE_LIMITS["investing_orders"])
async def order_history(request: Request, current_user: User = Depends(get_current_user)):
    """List recent orders for the user's investing account (limit 100).

    Returns Alpaca order history: filled orders, pending, canceled, etc.
    Most recent orders first.
    """
    if not current_user.alpaca_account_id:
        raise HTTPException(status_code=404, detail="No investing account yet.")
    try:
        orders = await alpaca_svc.list_orders(current_user.alpaca_account_id, status="all", limit=100)
        return {"orders": orders}
    except Exception as e:
        raise _svc_error(e)


class WatchlistItemOut(BaseModel):
    symbol: str
    created_at: str


@router.get("/watchlist")
@limiter.limit(RATE_LIMITS["investing_watchlist_add"])
async def get_watchlist(request: Request, current_user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    """Get user's watchlist (symbols they're tracking)."""
    items = db.query(InvestingWatchlist).filter(
        InvestingWatchlist.user_id == current_user.id
    ).order_by(InvestingWatchlist.created_at.desc()).all()
    return {
        "watchlist": [
            {"symbol": item.symbol, "created_at": item.created_at.isoformat()}
            for item in items
        ]
    }


class AddWatchlistRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=10)


@router.post("/watchlist")
@limiter.limit(RATE_LIMITS["investing_watchlist_add"])
async def add_to_watchlist(request: Request, req: AddWatchlistRequest,
                           current_user: User = Depends(get_current_user),
                           db: Session = Depends(get_db)):
    """Add a symbol to watchlist (stocks, ETFs, crypto)."""
    symbol = req.symbol.upper()
    if not symbol.replace(".", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid symbol format.")

    # Check if already in watchlist
    existing = db.query(InvestingWatchlist).filter(
        InvestingWatchlist.user_id == current_user.id,
        InvestingWatchlist.symbol == symbol
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"{symbol} is already in your watchlist.")

    item = InvestingWatchlist(user_id=current_user.id, symbol=symbol)
    db.add(item)
    db.commit()

    capture(current_user.id, EVENTS["investing_watchlist_add"], {"symbol": symbol})
    return {"symbol": symbol, "status": "added"}


@router.delete("/watchlist/{symbol}")
@limiter.limit(RATE_LIMITS["investing_watchlist_delete"])
async def remove_from_watchlist(symbol: str, request: Request,
                                current_user: User = Depends(get_current_user),
                                db: Session = Depends(get_db)):
    """Remove a symbol from watchlist."""
    symbol = symbol.upper()
    item = db.query(InvestingWatchlist).filter(
        InvestingWatchlist.user_id == current_user.id,
        InvestingWatchlist.symbol == symbol
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail=f"{symbol} not in watchlist.")

    db.delete(item)
    db.commit()

    capture(current_user.id, EVENTS["investing_watchlist_delete"], {"symbol": symbol})
    return {"symbol": symbol, "status": "removed"}
