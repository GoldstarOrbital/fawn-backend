"""Private, low-pressure shared-expense settlement requests for FAWN Tab."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from dependencies import get_current_user
from models import RepaymentRequest, User, UserAuditLog
from services import crypto_wallet, onchain_send
from services.sanctions_screening import RecipientSanctioned


router = APIRouter(prefix="/repayments", tags=["repayments"])
AUDIT_RETENTION = timedelta(days=365 * 7)
REMINDER_COOLDOWN = timedelta(hours=24)


class CreateRepaymentRequest(BaseModel):
    recipient: str = Field(min_length=2, max_length=32)
    amount_cents: int = Field(ge=100, le=100_000)
    note: str | None = Field(default=None, max_length=140)
    due_at: datetime | None = None


def _audit(db: Session, user_id: str, action: str, details: dict) -> None:
    db.add(UserAuditLog(
        user_id=user_id,
        action=action,
        details=json.dumps(details),
        retention_expires_at=datetime.now(timezone.utc) + AUDIT_RETENTION,
    ))


def _utc(value: datetime) -> datetime:
    """Normalize timestamps returned by SQLite and Postgres before comparison."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _serialize(row: RepaymentRequest, current_user: User, counterpart: User) -> dict:
    direction = "incoming" if row.payer_id == current_user.id else "outgoing"
    reminder_available_at = None
    if row.last_reminded_at:
        reminder_available_at = (_utc(row.last_reminded_at) + REMINDER_COOLDOWN).isoformat()
    return {
        "id": row.id,
        "direction": direction,
        "counterparty": f"@{counterpart.username}" if counterpart.username else counterpart.full_name,
        "amount": row.amount_cents / 100,
        "amount_cents": row.amount_cents,
        "note": row.note,
        "due_at": row.due_at.isoformat() if row.due_at else None,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "paid_at": row.paid_at.isoformat() if row.paid_at else None,
        "reminder_available_at": reminder_available_at,
    }


@router.post("/requests", status_code=201)
def create_request(req: CreateRepaymentRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    handle = req.recipient.strip().lstrip("@").lower()
    if not handle:
        raise HTTPException(status_code=400, detail="Enter your friend's FAWN username.")
    payer = db.query(User).filter(User.username.ilike(handle)).first()
    if not payer:
        raise HTTPException(status_code=404, detail=f"No FAWN user found with username @{handle}.")
    if payer.id == current_user.id:
        raise HTTPException(status_code=400, detail="You can't request money from yourself.")
    if not payer.wallet_initialized or not payer.crypto_wallet_address:
        raise HTTPException(status_code=409, detail=f"@{handle} needs to finish setting up their FAWN wallet first.")
    if req.due_at and _utc(req.due_at) < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Choose a due date in the future, or leave it blank.")

    row = RepaymentRequest(
        requester_id=current_user.id,
        payer_id=payer.id,
        amount_cents=req.amount_cents,
        note=(req.note or "").strip() or None,
        due_at=req.due_at,
    )
    db.add(row)
    db.flush()  # allocate the request id before recording its immutable audit entry
    _audit(db, current_user.id, "repayment_request_created", {"request_id": row.id, "payer_id": payer.id, "amount_cents": req.amount_cents})
    db.commit()
    db.refresh(row)
    return {"request": _serialize(row, current_user, payer), "message": "Your private settle-up request is ready. No money moves until they choose to pay."}


@router.get("/requests")
def list_requests(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.query(RepaymentRequest).filter(
        (RepaymentRequest.requester_id == current_user.id) | (RepaymentRequest.payer_id == current_user.id)
    ).order_by(RepaymentRequest.created_at.desc()).limit(100).all()
    users = {user.id: user for user in db.query(User).filter(User.id.in_({r.requester_id for r in rows} | {r.payer_id for r in rows})).all()} if rows else {}
    return {"requests": [
        _serialize(row, current_user, users[row.requester_id if row.payer_id == current_user.id else row.payer_id]) for row in rows
    ]}


@router.post("/requests/{request_id}/remind")
def prepare_reminder(request_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.query(RepaymentRequest).filter(RepaymentRequest.id == request_id, RepaymentRequest.requester_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Repayment request not found.")
    if row.status != "pending":
        raise HTTPException(status_code=409, detail="Only pending requests can be nudged.")
    now = datetime.now(timezone.utc)
    if row.last_reminded_at and _utc(row.last_reminded_at) + REMINDER_COOLDOWN > now:
        raise HTTPException(status_code=429, detail="A friendly nudge is available once every 24 hours.")
    payer = db.query(User).filter(User.id == row.payer_id).first()
    row.last_reminded_at = now
    _audit(db, current_user.id, "repayment_reminder_prepared", {"request_id": row.id, "payer_id": row.payer_id})
    db.commit()
    label = f" for {row.note}" if row.note else ""
    return {
        "share_text": f"Hey {('@' + payer.username) if payer.username else payer.full_name} — tiny FAWN nudge for ${row.amount_cents / 100:.2f}{label}. Whenever you're ready, you can settle it in FAWN. No stress.",
        "message": "Your nudge is ready to copy. FAWN will never send it for you without your action.",
    }


@router.post("/requests/{request_id}/pay")
async def pay_request(request_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.query(RepaymentRequest).filter(RepaymentRequest.id == request_id, RepaymentRequest.payer_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Repayment request not found.")
    if row.status == "paid":
        return {"status": "paid", "message": "This tab is already settled."}
    if row.status != "pending":
        raise HTTPException(status_code=409, detail="This request is not available to pay right now.")
    recipient = db.query(User).filter(User.id == row.requester_id).first()
    if not recipient or not recipient.wallet_initialized or not recipient.crypto_wallet_address:
        raise HTTPException(status_code=409, detail="The recipient's FAWN wallet is unavailable.")

    # Claim the request before invoking settlement, preventing double approval from duplicate clicks.
    row.status = "processing"
    db.commit()
    try:
        result = await crypto_wallet.send_usdc(
            sender_id=current_user.id,
            recipient_address=recipient.crypto_wallet_address,
            amount_cents=row.amount_cents,
            db=db,
            memo=f"FAWN Tab repayment {row.id}",
            is_internal=True,
        )
    except (crypto_wallet.WalletNotInitialized, crypto_wallet.InvalidAddress, crypto_wallet.InsufficientBalance,
            onchain_send.CannotSignTransaction, onchain_send.SendLimitExceeded, onchain_send.VelocityLimitExceeded,
            onchain_send.NoChainHasSufficientBalance, onchain_send.GasStationLimitExceeded,
            onchain_send.OnchainSendFailed, RecipientSanctioned) as exc:
        row = db.query(RepaymentRequest).filter(RepaymentRequest.id == request_id).first()
        if row and row.status == "processing":
            row.status = "pending"
            db.commit()
        status = 402 if isinstance(exc, (crypto_wallet.InsufficientBalance, onchain_send.NoChainHasSufficientBalance)) else 422
        raise HTTPException(status_code=status, detail=str(exc))

    row = db.query(RepaymentRequest).filter(RepaymentRequest.id == request_id).first()
    row.status = "paid"
    row.paid_at = datetime.now(timezone.utc)
    _audit(db, current_user.id, "repayment_request_paid", {"request_id": row.id, "requester_id": recipient.id, "amount_cents": row.amount_cents})
    db.commit()
    return {"status": "paid", "transfer": result, "message": "Tab settled. Nice work keeping it easy."}
