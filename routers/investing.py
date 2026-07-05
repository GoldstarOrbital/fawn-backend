"""Investing endpoints backed by Alpaca (Broker API).

Scope (MVP): open a brokerage account, view it, place a market order
(dollar-notional or share qty), and list positions. Money movement into the
brokerage account is out of scope here — that rides the ACH funding rails.

Every Alpaca call is guarded in services/alpaca.py: if Alpaca isn't
configured the endpoints return 503 rather than 500, so the feature is
safely dormant until ALPACA_API_KEY/SECRET are set.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from models import User, InvestingOrder
from dependencies import get_current_user
from services import alpaca as alpaca_svc

router = APIRouter(prefix="/investing", tags=["investing"])


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
    symbol: str
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
async def place_order(request: Request, req: OrderRequest,
                      current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not current_user.alpaca_account_id:
        raise HTTPException(status_code=400, detail="Open an investing account first.")
    req.validate_amount()

    idem = f"order:{current_user.id}:{req.symbol}:{req.side}:{req.notional or req.qty}"
    existing = db.query(InvestingOrder).filter(InvestingOrder.idempotency_key == idem).first()
    if existing:
        raise HTTPException(status_code=409, detail="Duplicate order.")

    order_row = InvestingOrder(
        user_id=current_user.id, symbol=req.symbol.upper(), side=req.side,
        notional_cents=int(req.notional * 100) if req.notional is not None else None,
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
        raise _svc_error(e)

    order_row.alpaca_order_id = result.get("id")
    order_row.status = result.get("status", "accepted")
    db.commit()
    return {"order_id": order_row.alpaca_order_id, "status": order_row.status,
            "symbol": order_row.symbol, "side": order_row.side}


@router.get("/positions")
async def positions(current_user: User = Depends(get_current_user)):
    if not current_user.alpaca_account_id:
        raise HTTPException(status_code=404, detail="No investing account yet.")
    try:
        return {"positions": await alpaca_svc.list_positions(current_user.alpaca_account_id)}
    except Exception as e:
        raise _svc_error(e)
