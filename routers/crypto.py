"""
API endpoints for stablecoin wallet operations.

All endpoints require Bearer token authentication (user login).
No more banking workflows — just USDC transfers with flat $0.01 fees.
"""
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from database import get_db
from middleware.auth import get_current_user_id
from services import crypto_wallet
from services.analytics import capture, EVENTS

router = APIRouter(prefix="/wallet", tags=["crypto"])


class CreateWalletRequest(BaseModel):
    wallet_type: str = Field(..., pattern="^(non_custodial|fawn_custodial)$")


class CreateWalletResponse(BaseModel):
    wallet_address: str
    wallet_type: str
    usdc_balance: float
    chain: str
    seed_phrase: str | None = None  # only for non_custodial


class BalanceResponse(BaseModel):
    wallet_address: str
    usdc_balance: float
    usdc_balance_cents: int
    total_fees_paid: float


class SendUSDCRequest(BaseModel):
    recipient_address: str = Field(..., description="Recipient's 0x... wallet address")
    amount_cents: int = Field(..., gt=0, description="Amount to send in cents (e.g., 1000 = $10.00)")
    memo: str | None = Field(None, max_length=200)


class TransferResponse(BaseModel):
    transfer_id: str
    amount: float
    fee: float
    total_debited: float
    status: str
    tx_hash: str | None
    created_at: str | None


class TransferHistoryItem(BaseModel):
    transfer_id: str
    type: str  # "send"
    amount: float
    fee: float
    counterparty: str
    status: str
    memo: str | None
    created_at: str | None


@router.post("/create", response_model=CreateWalletResponse, status_code=201)
async def create_wallet(
    req: CreateWalletRequest,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    Create a new stablecoin wallet for the logged-in user.

    Two wallet types:
    - non_custodial: User manages private key (seed phrase returned — must save)
    - fawn_custodial: FAWN holds encrypted key (user accesses via PIN — MVP: just balance)

    Returns wallet address and (for non-custodial) the seed phrase.
    Seed phrase is shown ONCE and cannot be recovered if lost.
    """
    try:
        result = await crypto_wallet.create_wallet(user_id, db, wallet_type=req.wallet_type)
        capture(EVENTS["WALLET_CREATED"], user_id, {"wallet_type": req.wallet_type})
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/balance", response_model=BalanceResponse)
async def get_balance(
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    Get current USDC balance for the user's stablecoin wallet.

    Balance is tracked in our internal ledger (instant updates).
    """
    try:
        result = await crypto_wallet.get_wallet_balance(user_id, db)
        return result
    except crypto_wallet.WalletNotInitialized:
        raise HTTPException(status_code=404, detail="No stablecoin wallet initialized. Call POST /wallet/create first.")


# ── TRANSFERS ROUTER ──
transfer_router = APIRouter(prefix="/transfers", tags=["transfers"])


@transfer_router.post("/send", response_model=TransferResponse, status_code=201)
async def send_usdc(
    req: SendUSDCRequest,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    Send USDC from user's wallet to a recipient.

    Cost: flat $0.01 platform fee (on top of the transfer amount, no gas fees).
    Settlement: instant (internal ledger, no blockchain tx needed).

    Returns transfer details including fee breakdown.
    """
    try:
        result = await crypto_wallet.send_usdc(
            sender_id=user_id,
            recipient_address=req.recipient_address,
            amount_cents=req.amount_cents,
            db=db,
            memo=req.memo,
        )
        capture(EVENTS["TRANSFER_SENT"], user_id, {"amount_cents": req.amount_cents})
        return result
    except crypto_wallet.WalletNotInitialized:
        raise HTTPException(status_code=404, detail="No stablecoin wallet. Call POST /wallet/create first.")
    except crypto_wallet.InvalidAddress as e:
        raise HTTPException(status_code=400, detail=str(e))
    except crypto_wallet.InsufficientBalance as e:
        raise HTTPException(status_code=402, detail=str(e))  # 402 = Payment Required


@transfer_router.get("/history", response_model=list[TransferHistoryItem])
async def transfer_history(
    limit: int = 50,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    Get transaction history for the user (all sends).

    Shows transfers sent from this wallet (recipient address, amount, fee, status).
    """
    try:
        history = await crypto_wallet.get_transfer_history(user_id, db, limit=min(limit, 200))
        return history
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── ADMIN / FEE COLLECTION ──
admin_router = APIRouter(prefix="/fees", tags=["admin"])


@admin_router.post("/collect")
async def collect_fees(
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    [ADMIN ONLY] Collect platform fees to treasury wallet.

    In production: requires admin key + triggers on-chain sweep.
    For MVP: just aggregates and logs fees.
    """
    # TODO: add admin key check
    result = await crypto_wallet.collect_fees(db)
    capture(EVENTS["FEES_COLLECTED"], "admin", {"total_cents": result["total_fees"]})
    return result
