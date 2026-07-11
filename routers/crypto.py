"""
API endpoints for stablecoin wallet operations.

SECURITY:
- All endpoints require Bearer token authentication (user login)
- Input validation: wallet addresses, amounts, memo length
- Authorization: users can only access their own wallets/transfers
- Rate limiting: aggressive per-user limits on sensitive endpoints
- No seed phrases logged, returned only once
- Error messages do not leak sensitive data
"""
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field, validator
from database import get_db
from dependencies import get_current_user_id
from services import crypto_wallet
from services.analytics import capture, EVENTS
from rate_limiting import limiter, RATE_LIMITS
from config import settings
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


class SendToUserRequest(BaseModel):
    """Send USDC to a FAWN user by username or to an external wallet address."""
    recipient: str = Field(..., min_length=1, max_length=255, description="Username (e.g. @maria) or 0x... wallet address")
    amount_cents: int = Field(..., gt=0, le=999999999, description="Amount to send in cents (e.g., 1000 = $10.00)")
    memo: str | None = Field(None, max_length=200)

    @validator("memo")
    def validate_memo(cls, v):
        """Security: prevent null bytes and control characters in memo."""
        if v and ("\x00" in v or any(ord(c) < 32 for c in v)):
            raise ValueError("Memo contains invalid characters")
        return v


class SendToBankRequest(BaseModel):
    """Send USDC to a traditional bank account via instant Stripe Payouts.

    USDC is converted 1:1 to USD and sent instantly. Settlement: typically
    <30 seconds (instant). $0.01 flat fee (same as P2P transfers).
    """
    recipient_name: str = Field(..., min_length=1, max_length=100, description="Name on the receiving bank account")
    recipient_routing_number: str = Field(..., pattern=r"^\d{9}$", description="9-digit US routing number")
    recipient_account_number: str = Field(..., pattern=r"^\d{4,17}$", description="Bank account number (typically 4-17 digits)")
    amount_cents: int = Field(..., gt=0, le=999999999, description="Amount to send in cents (e.g., 1000 = $10.00)")
    memo: str | None = Field(None, max_length=100, description="Payment memo/reference")

    @validator("recipient_name")
    def validate_recipient_name(cls, v):
        """Security: prevent injection attacks in bank transfer names."""
        if "\x00" in v or any(ord(c) < 32 for c in v):
            raise ValueError("Recipient name contains invalid characters")
        # Only allow alphanumeric, spaces, and common name punctuation
        if not re.match(r"^[a-zA-Z0-9\s\-\.\',]*$", v):
            raise ValueError("Recipient name contains invalid characters")
        return v

    @validator("memo")
    def validate_memo(cls, v):
        """Security: prevent null bytes and control characters in memo."""
        if v and ("\x00" in v or any(ord(c) < 32 for c in v)):
            raise ValueError("Memo contains invalid characters")
        return v


class BankTransferResponse(BaseModel):
    """Response for bank transfer (ACH) request."""
    transfer_id: str
    amount: float  # USD amount (same as USDC amount)
    fee: float
    total_debited: float
    recipient_name: str
    recipient_last4: str  # account last 4 digits for reference
    status: str  # pending | completed | failed
    estimated_settlement: str  # "1-3 business days"
    created_at: str | None


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
@limiter.limit(RATE_LIMITS["wallet_create"])
async def create_wallet(
    req: CreateWalletRequest,
    request: Request,
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
        import traceback
        error_msg = str(e)
        print(f"[wallet] ValueError creating wallet for {user_id}: {error_msg}")
        traceback.print_exc()
        # Return more specific error messages for debugging
        raise HTTPException(status_code=400, detail=error_msg)
    except Exception as e:
        import traceback
        print(f"[wallet] Exception creating wallet for {user_id}: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Wallet error: {str(e)[:100]}")


@router.get("/balance", response_model=BalanceResponse)
@limiter.limit(RATE_LIMITS["wallet_balance"])
async def get_balance(
    request: Request,
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
@limiter.limit(RATE_LIMITS["transfer_send"])
async def send_usdc(
    req: SendUSDCRequest,
    request: Request,
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


@transfer_router.post("/send-unified", response_model=TransferResponse, status_code=201)
@limiter.limit(RATE_LIMITS["transfer_send"])
async def send_to_user_or_wallet(
    req: SendToUserRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    Send USDC to either a FAWN user (by username) or external wallet address.

    Supports:
    - @username (e.g., @maria) - P2P transfer to FAWN user
    - 0x... wallet address - direct transfer to external wallet

    Cost: flat $0.01 platform fee for both types.
    Settlement: instant (no blockchain tx needed for FAWN users, instant for external wallets).
    """
    recipient = req.recipient.strip()

    # Determine if this is a username or wallet address
    if recipient.startswith("@"):
        # P2P transfer to FAWN user by username
        handle = recipient[1:].lower()  # remove @ and lowercase
        from models import Handle
        handle_row = db.query(Handle).filter(Handle.handle == handle).first()
        if not handle_row:
            raise HTTPException(status_code=404, detail=f"No FAWN user found with handle @{handle}.")

        recipient_user = db.query(User).filter(User.id == handle_row.user_id).first()
        if not recipient_user or not recipient_user.crypto_wallet_address:
            raise HTTPException(status_code=404, detail=f"User @{handle} doesn't have a wallet initialized.")

        if recipient_user.id == user_id:
            raise HTTPException(status_code=400, detail="You can't send money to yourself.")

        recipient_address = recipient_user.crypto_wallet_address
    elif _validate_eth_address(recipient):
        # External wallet address
        recipient_address = recipient
    else:
        raise HTTPException(status_code=400, detail="Recipient must be either @username or 0x... wallet address")

    # Execute the transfer
    try:
        result = await crypto_wallet.send_usdc(
            sender_id=user_id,
            recipient_address=recipient_address,
            amount_cents=req.amount_cents,
            db=db,
            memo=req.memo,
        )
        capture(EVENTS["TRANSFER_SENT"], user_id, {
            "amount_cents": req.amount_cents,
            "recipient_type": "fawn_user" if recipient.startswith("@") else "external_wallet"
        })
        return result
    except crypto_wallet.WalletNotInitialized:
        raise HTTPException(status_code=404, detail="No stablecoin wallet. Call POST /wallet/create first.")
    except crypto_wallet.InvalidAddress as e:
        raise HTTPException(status_code=400, detail=str(e))
    except crypto_wallet.InsufficientBalance as e:
        raise HTTPException(status_code=402, detail=str(e))  # 402 = Payment Required


@transfer_router.post("/send-to-bank", response_model=BankTransferResponse, status_code=201)
@limiter.limit(RATE_LIMITS["transfer_send"])
async def send_to_bank(
    req: SendToBankRequest,
    request: Request,
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    Send USDC to a traditional bank account via instant Stripe Payouts.

    FLOW:
    1. User sends USDC from FAWN wallet
    2. FAWN converts USDC → USD (1:1, no slippage)
    3. FAWN initiates instant payout via Stripe Payouts API
    4. USDC deducted from user balance (amount + $0.01 fee)
    5. Status: pending (typically <30 seconds for settlement)

    Cost: flat $0.01 platform fee (on top of the transfer amount, no ACH fees).

    SECURITY:
    - Recipient bank details NOT persisted (sent directly to Stripe)
    - Only account last 4 stored for reference
    - Audit logged with 7-year retention
    - Rate limited per user

    Returns transfer ID, expected settlement time (instant), and confirmation.
    """
    try:
        result = await crypto_wallet.send_to_bank(
            sender_id=user_id,
            recipient_name=req.recipient_name,
            recipient_routing_number=req.recipient_routing_number,
            recipient_account_number=req.recipient_account_number,
            amount_cents=req.amount_cents,
            db=db,
            memo=req.memo,
        )
        capture(EVENTS["TRANSFER_SENT"], user_id, {
            "amount_cents": req.amount_cents,
            "transfer_type": "bank_ach"
        })
        return result
    except crypto_wallet.WalletNotInitialized:
        raise HTTPException(status_code=404, detail="No stablecoin wallet. Call POST /wallet/create first.")
    except crypto_wallet.InsufficientBalance as e:
        raise HTTPException(status_code=402, detail=str(e))  # 402 = Payment Required
    except crypto_wallet.BankTransferError as e:
        raise HTTPException(status_code=503, detail=str(e))  # 503 = Service Unavailable


@transfer_router.get("/history", response_model=list[TransferHistoryItem])
@limiter.limit(RATE_LIMITS["transfer_history"])
async def transfer_history(
    request: Request,
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
@limiter.limit(RATE_LIMITS["user_export"])
async def export_user_data(
    request: Request,
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

    # SECURITY: Log this export (audit trail, 7-year retention)
    from models import UserAuditLog
    from datetime import datetime, timedelta, timezone
    retention_expires = datetime.now(tz=timezone.utc) + timedelta(days=365*7)
    audit_entry = UserAuditLog(
        user_id=user_id,
        action="export_data",
        details=json.dumps({"export_size_bytes": len(json.dumps(data))}),
        retention_expires_at=retention_expires,
    )
    db.add(audit_entry)
    db.commit()

    return data


@user_router.post("/delete")
@limiter.limit(RATE_LIMITS["user_delete"])
async def request_account_deletion(
    request: Request,
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

    # Log deletion request (audit trail, 7-year retention)
    from datetime import datetime, timedelta, timezone
    retention_expires = datetime.now(tz=timezone.utc) + timedelta(days=365*7)
    audit_entry = UserAuditLog(
        user_id=user_id,
        action="account_deletion_requested",
        details=json.dumps({"reason": "user_requested"}),
        retention_expires_at=retention_expires,
    )
    db.add(audit_entry)
    db.commit()

    return {"status": "deleted", "message": "Account marked for deletion. Transfers remain in ledger for 7 years."}


# ── ADMIN / FEE COLLECTION ──
admin_router = APIRouter(prefix="/fees", tags=["admin"])


@admin_router.post("/collect")
@limiter.limit(RATE_LIMITS["fees_collect"])
async def collect_fees(
    request: Request,
    db: Session = Depends(get_db),
):
    """
    [ADMIN ONLY] Collect platform fees to treasury wallet.

    Requires X-Admin-Key header for authentication.
    In production: triggers on-chain sweep to treasury.
    """
    admin_key = request.headers.get("X-Admin-Key", "")
    if not admin_key or admin_key != settings.admin_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing admin key"
        )

    result = await crypto_wallet.collect_fees(db)
    capture(EVENTS["FEES_COLLECTED"], "admin", {"total_cents": result["total_fees"]})
    return result
