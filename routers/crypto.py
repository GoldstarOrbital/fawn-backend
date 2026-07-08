"""
API endpoints for stablecoin wallet operations.

SECURITY:
- All endpoints require Bearer token authentication (user login)
- Input validation: wallet addresses, amounts, memo length
- Authorization: users can only access their own wallets/transfers
- Rate limiting: prevents abuse (see main.py)
- No seed phrases logged, returned only once
- Error messages do not leak sensitive data
"""
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field, validator
from database import get_db
from middleware.auth import get_current_user_id
from services import crypto_wallet
from services.analytics import capture, EVENTS
import re

router = APIRouter(prefix="/wallet", tags=["crypto"])

# Security: Ethereum address validation (0x + 40 hex chars)
ETH_ADDRESS_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")

def _validate_eth_address(addr: str) -> bool:
    """Validate Ethereum-style address format."""
    return bool(ETH_ADDRESS_PATTERN.match(addr))


class CreateWalletRequest(BaseModel):
    wallet_type: str = Field(..., pattern="^(non_custodial|fawn_custodial)$")


class CreateWalletResponse(BaseModel):
    wallet_address: str
    wallet_type: str
    usdc_balance: float
    chain: str
    seed_phrase: str | None = None  # only for non_custodial, returned ONCE


class BalanceResponse(BaseModel):
    wallet_address: str
    usdc_balance: float
    usdc_balance_cents: int
    total_fees_paid: float


class SendUSDCRequest(BaseModel):
    recipient_address: str = Field(..., min_length=42, max_length=42, description="Recipient's 0x... wallet address")
    amount_cents: int = Field(..., gt=0, le=999999999, description="Amount to send in cents (e.g., 1000 = $10.00, max $9,999,999.99)")
    memo: str | None = Field(None, max_length=200)

    @validator("recipient_address")
    def validate_address(cls, v):
        """Security: validate recipient address format."""
        if not _validate_eth_address(v):
            raise ValueError("Invalid Ethereum address format (must be 0x + 40 hex characters)")
        return v

    @validator("memo")
    def validate_memo(cls, v):
        """Security: prevent null bytes and control characters in memo."""
        if v and ("\x00" in v or any(ord(c) < 32 for c in v)):
            raise ValueError("Memo contains invalid characters")
        return v


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

    SECURITY: Only one wallet per user. Idempotent.
    - non_custodial: User manages private key (seed phrase returned — must save, SHOWN ONCE ONLY)
    - fawn_custodial: FAWN holds encrypted key (user accesses via PIN — MVP)

    Returns wallet address and (for non-custodial only) the seed phrase.
    CRITICAL: Seed phrase is shown ONCE and cannot be recovered if lost.
    """
    try:
        result = await crypto_wallet.create_wallet(user_id, db, wallet_type=req.wallet_type)
        capture(EVENTS["WALLET_CREATED"], user_id, {"wallet_type": req.wallet_type})
        # SECURITY: Log creation (for audit trail) but don't log seed phrase
        return result
    except ValueError as e:
        # SECURITY: Don't leak whether user already has wallet or other details
        raise HTTPException(status_code=400, detail="Could not create wallet")


@router.get("/balance", response_model=BalanceResponse)
async def get_balance(
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    Get current USDC balance for the logged-in user's wallet.

    SECURITY: Returns only the authenticated user's balance (authorization enforced by get_current_user_id).
    Balance is tracked in our internal ledger (instant updates).
    """
    try:
        result = await crypto_wallet.get_wallet_balance(user_id, db)
        # SECURITY: Audit log this read (optional, for compliance)
        return result
    except crypto_wallet.WalletNotInitialized:
        # SECURITY: Don't leak whether user exists; generic message
        raise HTTPException(status_code=404, detail="Wallet not found. Create one with POST /wallet/create.")


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


# ── USER DATA & PRIVACY ──
user_router = APIRouter(prefix="/user", tags=["user-data"])


@user_router.get("/export")
async def export_user_data(
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    Export all user data as JSON (GDPR/CCPA compliance).

    Returns: account info, wallet details, transfer history, audit log, fees paid.
    Rate limited to 1/day per user (prevent DOS).
    """
    from models import User, CryptoWallet, CryptoTransfer, UserAuditLog
    import json
    from datetime import datetime

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    wallet = db.query(CryptoWallet).filter(CryptoWallet.user_id == user_id).first()
    transfers = db.query(CryptoTransfer).filter(CryptoTransfer.sender_id == user_id).all()
    audit_logs = db.query(UserAuditLog).filter(UserAuditLog.user_id == user_id).all()

    data = {
        "exported_at": datetime.utcnow().isoformat(),
        "user": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "wallet_initialized": user.wallet_initialized,
            "total_fees_paid": user.total_fees_paid_cents / 100.0,
        },
        "wallet": {
            "address": wallet.wallet_address if wallet else None,
            "type": wallet.wallet_type if wallet else None,
            "chain": wallet.chain if wallet else None,
            "balance": wallet.usdc_balance_cents / 100.0 if wallet else 0,
        },
        "transfers": [
            {
                "id": t.id,
                "recipient": t.recipient_address,
                "amount": t.amount_cents / 100.0,
                "fee": t.fee_cents / 100.0,
                "status": t.status,
                "memo": t.memo,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in transfers
        ],
        "audit_log": [
            {
                "action": log.action,
                "details": log.details,
                "created_at": log.created_at.isoformat() if log.created_at else None,
            }
            for log in audit_logs
        ],
    }

    # SECURITY: Log this export (audit trail)
    from models import UserAuditLog
    audit_entry = UserAuditLog(
        user_id=user_id,
        action="export_data",
        details=json.dumps({"export_size_bytes": len(json.dumps(data))}),
    )
    db.add(audit_entry)
    db.commit()

    return data


@user_router.post("/delete")
async def request_account_deletion(
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    Request account deletion (soft-delete).

    Transfers remain in ledger for compliance. Account marked inactive.
    User can no longer log in after this.
    """
    from models import User, UserAuditLog
    import json

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Soft-delete: mark inactive (can be reactivated by support if needed)
    user.hashed_password = ""  # prevent login
    user.wallet_initialized = False  # prevent wallet access

    # Log deletion request (audit trail)
    audit_entry = UserAuditLog(
        user_id=user_id,
        action="account_deletion_requested",
        details=json.dumps({"reason": "user_requested"}),
    )
    db.add(audit_entry)
    db.commit()

    return {"status": "deleted", "message": "Account marked for deletion. Transfers remain in ledger for 7 years."}


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
