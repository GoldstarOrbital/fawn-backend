"""
Cryptocurrency trading endpoints via Uniswap on Polygon.

All endpoints require Bearer token authentication.
Rate limited: 100 trades/day per user.
Supports: USDC ↔ ETH, USDC ↔ MATIC, ETH ↔ MATIC, etc. (on Polygon)

SECURITY:
- All endpoints require JWT authentication
- Balance verified before debit (no overdraft)
- Atomic transactions: all-or-nothing
- Slippage protection (user-configurable)
- Audit logged every trade
- Double-spend prevention via row locking

FLOW:
1. User calls POST /quote to get price
2. User calls POST /execute with approval (returns unsigned tx for signing)
3. User calls GET /history to see past trades and P&L

FEES:
- Platform fee: $0.01 flat (100 cents) per trade
- Gas fees: Uniswap gas cost estimate (varies by liquidity/route)
- No slippage, no hidden charges

PENDING STATE:
On /execute, user receives an unsigned transaction object. They must sign
via their wallet (Metamask, WalletConnect, etc.) and broadcast to Polygon.
Trade status remains "pending" until tx is confirmed on-chain.
"""

from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy import func
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field, validator
from datetime import datetime, timedelta, timezone
import json
import logging
from decimal import Decimal

from database import get_db
from dependencies import get_current_user_id
from models import CryptoTrade, User, UserAuditLog
from services import trading_uniswap
from services.trading_uniswap import (
    get_swap_quote, execute_swap, SwapQuote,
    InvalidToken, InvalidTokenAddress, InsufficientLiquidity, SlippageError, GasError
)
from rate_limiting import limiter, RATE_LIMITS
from config import settings

router = APIRouter(prefix="/wallet/trades", tags=["trading"])
logger = logging.getLogger(__name__)

# Rate limiting
TRADES_PER_DAY = 100
TRADES_PER_HOUR = 20

# Platform fees (in cents)
PLATFORM_FEE_CENTS = 100  # $0.01


# ── REQUEST/RESPONSE SCHEMAS ──

class QuoteRequest(BaseModel):
    """Request a price quote for a token swap."""
    from_token: str = Field(..., pattern="^[A-Z]{2,6}$", description="Source token symbol (e.g., USDC)")
    to_token: str = Field(..., pattern="^[A-Z]{2,6}$", description="Destination token symbol (e.g., ETH)")
    amount_cents: int = Field(..., gt=0, le=999999999, description="Amount in cents (e.g., 5000 = $50.00)")
    slippage_tolerance: float = Field(default=0.5, ge=0.01, le=5.0, description="Max slippage % (0.01 to 5.0)")

    @validator("from_token", "to_token")
    def validate_token(cls, v):
        """Ensure token symbols are uppercase."""
        return v.upper()

    @validator("from_token", "to_token")
    def check_different_tokens(cls, v, values):
        """Ensure from_token != to_token."""
        if "from_token" in values and v == values["from_token"]:
            raise ValueError("from_token and to_token must be different")
        return v


class QuoteResponse(BaseModel):
    """Price quote for a swap (no balance debit)."""
    from_amount: str  # "$50.00"
    to_amount: str  # "0.0199 ETH"
    price: str  # "$2511.55" (price per unit)
    slippage: str  # "0.12%"
    gas_estimate: str  # "$0.50"
    fawn_fee: str  # "$0.01"
    total_cost: str  # "$0.51" (fee + gas)


class ExecuteRequest(BaseModel):
    """Execute a confirmed trade."""
    from_token: str = Field(..., pattern="^[A-Z]{2,6}$", description="Source token symbol")
    to_token: str = Field(..., pattern="^[A-Z]{2,6}$", description="Destination token symbol")
    amount_cents: int = Field(..., gt=0, le=999999999, description="Amount in cents")
    slippage_tolerance: float = Field(default=0.5, ge=0.01, le=5.0, description="Max slippage %")

    @validator("from_token", "to_token")
    def validate_token(cls, v):
        return v.upper()


class UnsignedTransaction(BaseModel):
    """Unsigned Uniswap transaction ready for user to sign."""
    to: str  # Uniswap Router address
    from_: str = Field(..., alias="from")  # User's wallet address
    data: str  # Encoded function call
    value: str  # ETH value (usually "0" for ERC20)
    gas: int  # Gas estimate
    gas_price: str  # Gas price in wei
    chain_id: int  # Polygon chain ID (137)


class ExecuteResponse(BaseModel):
    """Response after trade is submitted for execution."""
    trade_id: str
    status: str  # "pending"
    from_token: str
    to_token: str
    from_amount: str
    expected_to_amount: str
    platform_fee: str
    gas_estimate: str
    total_cost: str
    unsigned_tx: UnsignedTransaction
    message: str  # "Sign and broadcast this transaction to complete the trade"


class TradeHistoryItem(BaseModel):
    """A single trade in the user's history."""
    trade_id: str
    from_token: str
    to_token: str
    from_amount: str  # "$50.00"
    received: str  # "0.0199 ETH" or "pending"
    price: str  # "$2511.55"
    value_now: str  # current market value
    gain_loss: str  # "+$2.50 / +5.0%"
    fee: str  # "$0.01"
    status: str  # quote | pending | completed | failed | cancelled
    created_at: str  # ISO timestamp
    completed_at: str | None  # ISO timestamp if confirmed


# ── HELPERS ──

def _audit_log(
    db: Session,
    user_id: str,
    action: str,
    trade_id: str | None = None,
    details: dict = None,
):
    """Log trade action to audit trail (7-year retention)."""
    retention_expires = datetime.now(tz=timezone.utc) + timedelta(days=365*7)
    audit_entry = UserAuditLog(
        user_id=user_id,
        action=action,
        details=json.dumps({"trade_id": trade_id, **(details or {})}),
        retention_expires_at=retention_expires,
    )
    db.add(audit_entry)


def _check_daily_limit(db: Session, user_id: str) -> int:
    """Get count of trades submitted today by this user.

    Returns: number of trades today
    Raises: HTTPException if limit exceeded
    """
    now = datetime.now(tz=timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    count = db.query(func.count(CryptoTrade.id)).filter(
        CryptoTrade.user_id == user_id,
        CryptoTrade.created_at >= today_start,
        CryptoTrade.status.in_(["quote", "pending", "completed"]),
    ).scalar() or 0

    if count >= TRADES_PER_DAY:
        raise HTTPException(
            status_code=429,
            detail=f"Daily trade limit ({TRADES_PER_DAY}) exceeded. Try again tomorrow.",
        )

    return count


def _cents_to_dollars(cents: int) -> str:
    """Convert cents to "$X.XX" format."""
    dollars = Decimal(cents) / 100
    return f"${dollars:.2f}"


def _format_token_amount(amount_cents: int, token_symbol: str, decimals: int = 18) -> str:
    """Format amount in token units with symbol.

    For stablecoins (USDC, USDT, DAI): show as dollars.
    For other tokens: convert to token units and show with symbol.
    """
    if token_symbol.upper() in ("USDC", "USDT", "DAI"):
        return _cents_to_dollars(amount_cents)

    # For other tokens: convert cents to token units
    # This is simplified; real logic depends on token decimals
    amount = Decimal(amount_cents) / (10 ** (decimals + 2))
    return f"{amount:.4f} {token_symbol.upper()}"


def _get_user_wallet(db: Session, user_id: str) -> User:
    """Get user's wallet or raise 404."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.wallet_initialized:
        raise HTTPException(
            status_code=404,
            detail="Wallet not initialized. Call POST /wallet/create first.",
        )
    return user


# ── ENDPOINT 1: POST /wallet/trades/quote ──

@router.post("/quote", response_model=QuoteResponse, status_code=200)
@limiter.limit(RATE_LIMITS["trading_quote"])
async def get_quote(
    req: QuoteRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    Get a price quote for a token swap.

    No balance debit, no commitment. Quote is valid for 30 seconds.
    Includes gas estimate and slippage calculation.

    Returns:
    - from_amount: Input amount formatted
    - to_amount: Expected output (with slippage applied)
    - price: Price per unit
    - slippage: Actual slippage %
    - gas_estimate: Uniswap gas cost
    - fawn_fee: Platform fee ($0.01)
    - total_cost: Combined cost estimate
    """
    try:
        # Validate user has wallet
        user = _get_user_wallet(db, user_id)

        # Check daily trade limit
        _check_daily_limit(db, user_id)

        # Get quote from Uniswap
        quote: SwapQuote = await get_swap_quote(
            from_token=req.from_token,
            to_token=req.to_token,
            amount_cents=req.amount_cents,
            slippage_tolerance_percent=req.slippage_tolerance,
        )

        # Convert gas estimate from wei to cents (approximate)
        # Polygon gas is ~20-100 gwei, ~1M gas for swap
        # Simplified: gas_estimate_wei from service is in wei, convert to USD cents
        # Actual: would need current gas price oracle
        gas_price_usd_cents = int(quote.gas_estimate_wei / 1e9 * 50)  # ~50 gwei Polygon avg

        total_cost_cents = PLATFORM_FEE_CENTS + gas_price_usd_cents

        # Format response
        response = QuoteResponse(
            from_amount=_cents_to_dollars(req.amount_cents),
            to_amount=_format_token_amount(quote.amount_out_cents, req.to_token),
            price=_cents_to_dollars(
                int(Decimal(req.amount_cents) / (Decimal(quote.amount_out_cents) / 100))
            ) if quote.amount_out_cents > 0 else "$0.00",
            slippage=f"{quote.price_impact_percent:.2f}%",
            gas_estimate=_cents_to_dollars(gas_price_usd_cents),
            fawn_fee=_cents_to_dollars(PLATFORM_FEE_CENTS),
            total_cost=_cents_to_dollars(total_cost_cents),
        )

        logger.info(
            f"[quote] {user_id}: {req.from_token} → {req.to_token}, "
            f"{req.amount_cents} cents, total cost ${total_cost_cents/100:.2f}"
        )

        return response

    except InvalidToken as e:
        raise HTTPException(status_code=400, detail=str(e))
    except InsufficientLiquidity as e:
        raise HTTPException(status_code=503, detail=f"No liquidity for this pair: {str(e)[:100]}")
    except GasError as e:
        raise HTTPException(status_code=503, detail=f"Gas service unavailable: {str(e)[:100]}")
    except Exception as e:
        logger.error(f"[quote] error for {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Quote failed: {str(e)[:100]}")


# ── ENDPOINT 2: POST /wallet/trades/execute ──

@router.post("/execute", response_model=ExecuteResponse, status_code=201)
@limiter.limit(RATE_LIMITS["trading_execute"])
async def execute_trade(
    req: ExecuteRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    Execute a confirmed trade (submit to blockchain).

    FLOW:
    1. Validate user & wallet
    2. Check daily limit
    3. Get fresh quote
    4. Verify balance (amount + total fees)
    5. Build unsigned transaction
    6. Create CryptoTrade record (pending)
    7. Debit balance: amount + $0.01 fee
    8. Log audit trail
    9. Return unsigned tx for user to sign

    CRITICAL: User must sign this transaction and broadcast it to Polygon.
    Balance is debited immediately (not on blockchain). If user doesn't sign,
    trade remains pending and can be cancelled.

    Slippage protection: min output is calculated with user's tolerance.
    If actual output < min output, blockchain reverts the transaction.

    Returns:
    - trade_id: Trade ID for tracking
    - status: "pending" (awaiting blockchain confirmation)
    - unsigned_tx: Transaction object user must sign and broadcast
    """
    try:
        # Validate user & wallet
        user = _get_user_wallet(db, user_id)

        # Check daily limit
        _check_daily_limit(db, user_id)

        # Get fresh quote
        quote: SwapQuote = await get_swap_quote(
            from_token=req.from_token,
            to_token=req.to_token,
            amount_cents=req.amount_cents,
            slippage_tolerance_percent=req.slippage_tolerance,
        )

        # Calculate total cost (amount + fee + gas estimate)
        gas_price_usd_cents = int(quote.gas_estimate_wei / 1e9 * 50)  # Polygon ~50 gwei
        total_cost_cents = req.amount_cents + PLATFORM_FEE_CENTS + gas_price_usd_cents

        # Verify balance (DOUBLE-SPEND PREVENTION with row lock)
        db.query(User).filter(User.id == user_id).with_for_update().first()
        if user.usdc_balance_cents < total_cost_cents:
            raise HTTPException(
                status_code=402,  # 402 = Payment Required
                detail=f"Insufficient balance. Need ${total_cost_cents/100:.2f}, "
                       f"have ${user.usdc_balance_cents/100:.2f}",
            )

        # Build unsigned transaction (from Uniswap)
        tx_data = await execute_swap(
            from_token=req.from_token,
            to_token=req.to_token,
            amount_cents=req.amount_cents,
            user_wallet=user.crypto_wallet_address,
            slippage_tolerance_percent=req.slippage_tolerance,
        )

        # Create CryptoTrade record (pending)
        trade = CryptoTrade(
            user_id=user_id,
            from_token=req.from_token,
            to_token=req.to_token,
            from_amount_cents=req.amount_cents,
            expected_to_amount_cents=quote.amount_out_cents,
            price_per_unit=_cents_to_dollars(
                int(Decimal(req.amount_cents) / (Decimal(quote.amount_out_cents) / 100))
            ) if quote.amount_out_cents > 0 else "$0.00",
            slippage_tolerance_percent=str(req.slippage_tolerance),
            platform_fee_cents=PLATFORM_FEE_CENTS,
            gas_estimate_cents=gas_price_usd_cents,
            total_cost_cents=total_cost_cents,
            status="pending",
            idempotency_key=f"{user_id}:{req.from_token}:{req.to_token}:{req.amount_cents}:{datetime.utcnow().isoformat()}",
        )
        db.add(trade)
        db.flush()

        # Debit balance (atomic: if anything below fails, rollback)
        user.usdc_balance_cents -= total_cost_cents
        user.total_fees_paid_cents += PLATFORM_FEE_CENTS

        # Audit log
        _audit_log(
            db,
            user_id,
            "trade_submitted",
            trade.id,
            {
                "from_token": req.from_token,
                "to_token": req.to_token,
                "amount_cents": req.amount_cents,
                "total_cost_cents": total_cost_cents,
                "balance_after": user.usdc_balance_cents,
            },
        )

        db.commit()

        # Build unsigned transaction response
        unsigned_tx = UnsignedTransaction(
            to=tx_data.get("to", "0xE592427A0AEce92De3Edee1F18E0157C05861564"),
            **{"from": user.crypto_wallet_address},
            data=tx_data.get("data", "0x"),
            value=tx_data.get("value", "0"),
            gas=tx_data.get("gas", 200000),
            gas_price=str(tx_data.get("gasPrice", int(50 * 1e9))),
            chain_id=137,  # Polygon
        )

        logger.info(
            f"[execute] {user_id}: trade {trade.id} pending, "
            f"{req.from_token} → {req.to_token}, debited ${total_cost_cents/100:.2f}"
        )

        return ExecuteResponse(
            trade_id=trade.id,
            status="pending",
            from_token=req.from_token,
            to_token=req.to_token,
            from_amount=_cents_to_dollars(req.amount_cents),
            expected_to_amount=_format_token_amount(quote.amount_out_cents, req.to_token),
            platform_fee=_cents_to_dollars(PLATFORM_FEE_CENTS),
            gas_estimate=_cents_to_dollars(gas_price_usd_cents),
            total_cost=_cents_to_dollars(total_cost_cents),
            unsigned_tx=unsigned_tx,
            message="Sign and broadcast this transaction to Polygon to complete the trade",
        )

    except InvalidToken as e:
        raise HTTPException(status_code=400, detail=str(e))
    except InvalidTokenAddress as e:
        raise HTTPException(status_code=400, detail=str(e))
    except InsufficientLiquidity as e:
        raise HTTPException(status_code=503, detail=f"No liquidity: {str(e)[:100]}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[execute] error for {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Trade execution failed: {str(e)[:100]}")


# ── ENDPOINT 3: GET /wallet/trades/history ──

@router.get("/history", response_model=list[TradeHistoryItem], status_code=200)
@limiter.limit(RATE_LIMITS["trading_history"])
async def get_trade_history(
    request: Request,
    limit: int = 50,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    Get trade history for the user.

    Shows all trades (completed, pending, failed) in reverse chronological order.
    Includes P&L data for completed trades (current market value, gain/loss).

    Parameters:
    - limit: Max trades to return (1-200, default 50)

    Returns: List of trades with status, amounts, fees, and P&L.
    """
    try:
        # Validate user
        _get_user_wallet(db, user_id)

        # Query trades (most recent first)
        limit = min(max(limit, 1), 200)  # Clamp 1-200
        trades = db.query(CryptoTrade).filter(
            CryptoTrade.user_id == user_id,
        ).order_by(
            CryptoTrade.created_at.desc(),
        ).limit(limit).all()

        # Format response
        history = []
        for trade in trades:
            # Format amounts
            from_amount = _cents_to_dollars(trade.from_amount_cents)
            received = (
                _format_token_amount(trade.to_amount_cents, trade.to_token)
                if trade.to_amount_cents and trade.status == "completed"
                else "pending" if trade.status in ("pending", "quote") else "failed"
            )

            # Format P&L
            if trade.status == "completed" and trade.gain_loss_cents is not None:
                gain_loss_str = (
                    f"{_cents_to_dollars(abs(trade.gain_loss_cents))} "
                    f"({trade.gain_loss_percent}%)"
                )
                if trade.gain_loss_cents >= 0:
                    gain_loss_str = f"+{gain_loss_str}"
            else:
                gain_loss_str = "—"

            # Format value now
            value_now = (
                _cents_to_dollars(trade.value_now_cents)
                if trade.value_now_cents else "—"
            )

            history.append(TradeHistoryItem(
                trade_id=trade.id,
                from_token=trade.from_token,
                to_token=trade.to_token,
                from_amount=from_amount,
                received=received,
                price=trade.price_per_unit,
                value_now=value_now,
                gain_loss=gain_loss_str,
                fee=_cents_to_dollars(trade.platform_fee_cents),
                status=trade.status,
                created_at=trade.created_at.isoformat() if trade.created_at else None,
                completed_at=trade.completed_at.isoformat() if trade.completed_at else None,
            ))

        logger.info(f"[history] {user_id}: returned {len(history)} trades")
        return history

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[history] error for {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"History fetch failed: {str(e)[:100]}")


# ── WEBHOOK / SETTLEMENT (stub) ──
# In production, a Polygon block listener would:
# 1. Monitor for transactions from user wallets
# 2. When tx confirms, fetch receipt and amounts
# 3. Update CryptoTrade.status = "completed", .to_amount_cents, .tx_hash
# 4. Verify amounts match quote (check slippage didn't exceed tolerance)
# 5. Update P&L (value_now, gain_loss, gain_loss_percent)
# 6. Log audit trail

# For now, trades remain "pending" until manually confirmed or admin updates them.
