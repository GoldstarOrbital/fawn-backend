"""Tier 1 P2P payments: handles, instant FAWN-to-FAWN sends, requests, splits.

All sends move through Unit Book Payments — both accounts live at the same
sponsor bank, so the transfer is a ledger move (sub-second, no external
network). Every send is created in a non-final state and only touches Unit
once the sender calls /confirm, which is what forces the irreversible-action
confirmation screen client-side for every transfer, not just risky ones.

Risk controls (limits, step-up, scam warnings, idempotency, audit log,
disputes) are designed in from the start per product spec — see the
constants and _check_limits/_scam_warning helpers below.
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from models import User, Handle, P2PTransfer, P2PDispute, P2PAuditLog, new_id
from schemas import (
    HandleClaimRequest, HandleOut, HandleLookupOut,
    P2PSendRequest, P2PRequestRequest, P2PSplitRequest, P2PConfirmRequest,
    P2PTransferOut, P2PTransferList, P2PDisputeRequest, P2PDisputeOut,
)
from dependencies import get_current_user
from services import unit as unit_svc
from services.external_send import get_external_send_provider
from routers.admin import require_admin_key
from rate_limiting import limiter

router = APIRouter(prefix="/p2p", tags=["p2p"])

# --- Risk controls (MVP constants; promote to a per-user DB table once we
#     have real usage data to tune against) ---
PER_TX_LIMIT_CENTS = 50_000          # $500 per single send
DAILY_LIMIT_CENTS = 100_000          # $1,000 rolling 24h
ROLLING_7DAY_LIMIT_CENTS = 300_000   # $3,000 rolling 7 days
NEW_RECIPIENT_STEP_UP_THRESHOLD_CENTS = 20_000  # $200+ to a never-paid recipient requires step-up

_SCAM_KEYWORDS = [
    "ticket", "tickets", "concert", "reselling", "resale", "resell",
    "crypto", "bitcoin", "investment", "invest", "double your money",
    "tuition refund", "overpayment", "gift card", "wire me back",
    "send back the difference", "refund the extra",
]


# --- Internal helpers ---

def _audit(db: Session, transfer_id: str, user_id: str, event_type: str, metadata: Optional[dict] = None):
    db.add(P2PAuditLog(
        transfer_id=transfer_id, user_id=user_id, event_type=event_type,
        metadata_json=json.dumps(metadata or {}),
    ))


def _get_handle_row(db: Session, user_id: str) -> Optional[Handle]:
    return db.query(Handle).filter(Handle.user_id == user_id).first()


def _resolve_handle(db: Session, handle: str) -> Optional[User]:
    row = db.query(Handle).filter(Handle.handle == handle).first()
    if not row:
        return None
    return db.query(User).filter(User.id == row.user_id).first()


def _require_active_account(user: User):
    if not user.unit_account_id:
        raise HTTPException(status_code=400, detail="That wallet isn't initialized yet.")


def _scam_warning(note: Optional[str], is_new_recipient: bool) -> Optional[str]:
    text = (note or "").lower()
    if any(k in text for k in _SCAM_KEYWORDS):
        return ("This looks like it could match a common scam pattern (event tickets, crypto, "
                "'refund the difference'). Only send money to people you actually know — FAWN can't reverse this.")
    if is_new_recipient:
        return "You haven't sent to this person before. Double-check the handle — this can't be undone."
    return None


def _rolling_total(db: Session, user_id: str, since: datetime) -> int:
    total = db.query(func.coalesce(func.sum(P2PTransfer.amount_cents), 0)).filter(
        P2PTransfer.from_user_id == user_id,
        P2PTransfer.status == "completed",
        P2PTransfer.created_at >= since,
    ).scalar()
    return total or 0


def _check_limits(db: Session, from_user_id: str, amount_cents: int):
    if amount_cents > PER_TX_LIMIT_CENTS:
        raise HTTPException(status_code=400, detail=f"Single transfers are capped at ${PER_TX_LIMIT_CENTS / 100:.2f}.")
    now = datetime.now(timezone.utc)
    day_total = _rolling_total(db, from_user_id, now - timedelta(hours=24))
    if day_total + amount_cents > DAILY_LIMIT_CENTS:
        raise HTTPException(status_code=400, detail=f"This would exceed your 24-hour sending limit of ${DAILY_LIMIT_CENTS / 100:.2f}.")
    week_total = _rolling_total(db, from_user_id, now - timedelta(days=7))
    if week_total + amount_cents > ROLLING_7DAY_LIMIT_CENTS:
        raise HTTPException(status_code=400, detail=f"This would exceed your 7-day sending limit of ${ROLLING_7DAY_LIMIT_CENTS / 100:.2f}.")


def _is_new_recipient(db: Session, from_user_id: str, to_user_id: str) -> bool:
    prior = db.query(P2PTransfer.id).filter(
        P2PTransfer.from_user_id == from_user_id,
        P2PTransfer.to_user_id == to_user_id,
        P2PTransfer.status == "completed",
    ).first()
    return prior is None


def _is_first_send_ever(db: Session, from_user_id: str) -> bool:
    prior = db.query(P2PTransfer.id).filter(
        P2PTransfer.from_user_id == from_user_id,
        P2PTransfer.status == "completed",
    ).first()
    return prior is None


def _to_out(t: P2PTransfer, viewer_id: str) -> P2PTransferOut:
    if t.type == "send":
        direction = "sent" if t.from_user_id == viewer_id else "received"
        counterparty = t.to_handle if direction == "sent" else t.from_handle
    else:  # request
        direction = "request_outgoing" if t.to_user_id == viewer_id else "request_incoming"
        counterparty = t.from_handle if direction == "request_outgoing" else t.to_handle
    return P2PTransferOut(
        id=t.id, type=t.type, status=t.status, direction=direction,
        counterparty_handle=counterparty, amount_cents=t.amount_cents,
        note=t.note, warning=t.warning, group_id=t.group_id,
        step_up_required=t.step_up_required,
        created_at=t.created_at.isoformat() if t.created_at else "",
        completed_at=t.completed_at.isoformat() if t.completed_at else None,
        error_message=t.error_message,
    )


# --- Handles ---

@router.post("/handles", response_model=HandleOut, status_code=201)
@limiter.limit("10/minute")
def claim_handle(request: Request, req: HandleClaimRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    existing = db.query(Handle).filter(Handle.handle == req.handle).first()
    if existing and existing.user_id != current_user.id:
        raise HTTPException(status_code=409, detail="That handle is already taken.")

    mine = _get_handle_row(db, current_user.id)
    if mine:
        mine.handle = req.handle
    else:
        mine = Handle(user_id=current_user.id, handle=req.handle)
        db.add(mine)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="That handle is already taken.")
    return HandleOut(handle=mine.handle)


@router.get("/handles/me")
def my_handle(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = _get_handle_row(db, current_user.id)
    return {"handle": row.handle if row else None}


@router.get("/handles/lookup", response_model=HandleLookupOut)
def lookup_handle(handle: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    h = handle.strip().lstrip("@").lower()
    row = db.query(Handle).filter(Handle.handle == h).first()
    if not row:
        return HandleLookupOut(handle=h, claimable=True, display_name=None)
    user = db.query(User).filter(User.id == row.user_id).first()
    display = None
    if user:
        parts = user.full_name.split()
        display = f"{parts[0]} {parts[-1][0]}." if len(parts) > 1 else parts[0]
    return HandleLookupOut(handle=h, claimable=False, display_name=display)


# --- Sends ---

@router.post("/transfers", response_model=P2PTransferOut, status_code=201)
@limiter.limit("20/minute")
async def create_send(request: Request, req: P2PSendRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    existing = db.query(P2PTransfer).filter(P2PTransfer.idempotency_key == req.idempotency_key).first()
    if existing:
        if existing.from_user_id != current_user.id:
            raise HTTPException(status_code=404, detail="Transfer not found.")
        return _to_out(existing, current_user.id)

    _require_active_account(current_user)
    my_handle = _get_handle_row(db, current_user.id)
    if not my_handle:
        raise HTTPException(status_code=400, detail="Claim a handle before sending money.")

    recipient = _resolve_handle(db, req.to_handle)
    if not recipient:
        raise HTTPException(status_code=404, detail=f"No FAWN user found for @{req.to_handle}.")
    if recipient.id == current_user.id:
        raise HTTPException(status_code=400, detail="You can't send money to yourself.")
    _require_active_account(recipient)
    _check_limits(db, current_user.id, req.amount_cents)

    new_recipient = _is_new_recipient(db, current_user.id, recipient.id)
    first_ever = _is_first_send_ever(db, current_user.id)
    step_up = first_ever or (new_recipient and req.amount_cents >= NEW_RECIPIENT_STEP_UP_THRESHOLD_CENTS)
    warning = _scam_warning(req.note, new_recipient)

    transfer = P2PTransfer(
        type="send", status="requires_step_up" if step_up else "pending",
        from_user_id=current_user.id, to_user_id=recipient.id,
        from_handle=my_handle.handle, to_handle=req.to_handle,
        amount_cents=req.amount_cents, note=req.note, warning=warning,
        idempotency_key=req.idempotency_key, step_up_required=step_up,
    )
    db.add(transfer)
    db.flush()
    _audit(db, transfer.id, current_user.id, "created", {"amount_cents": req.amount_cents, "step_up_required": step_up})
    db.commit()
    return _to_out(transfer, current_user.id)


@router.post("/transfers/{transfer_id}/confirm", response_model=P2PTransferOut)
@limiter.limit("20/minute")
async def confirm_transfer(request: Request, transfer_id: str, req: P2PConfirmRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    transfer = db.query(P2PTransfer).filter(P2PTransfer.id == transfer_id).first()
    if not transfer:
        raise HTTPException(status_code=404, detail="Transfer not found.")
    if transfer.from_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the sender can confirm this transfer.")
    if transfer.status == "completed":
        return _to_out(transfer, current_user.id)  # idempotent replay
    if transfer.status not in ("pending", "requires_step_up"):
        raise HTTPException(status_code=400, detail=f"This transfer can't be confirmed (status: {transfer.status}).")

    # Lock the sender's user row for the remainder of this transaction so
    # concurrent /confirm calls for the same sender (different transfers)
    # serialize here instead of racing the rolling-limit check below — each
    # call would otherwise only see previously-completed transfers, not
    # transfers concurrently in flight through this same code path, letting
    # the sum of simultaneously-confirmed transfers exceed the daily/weekly
    # caps even though each one's own check passed. On Postgres this is a
    # real row lock (SELECT ... FOR UPDATE) held until commit/rollback; on
    # SQLite (tests) it's a harmless no-op since SQLite already serializes
    # writers at the connection/file level.
    db.query(User).filter(User.id == transfer.from_user_id).with_for_update().first()

    if transfer.step_up_required and not req.step_up_acknowledged:
        _audit(db, transfer.id, current_user.id, "step_up_required", {})
        db.commit()
        raise HTTPException(
            status_code=428,
            detail="Step-up confirmation required — show the warning, then resubmit with step_up_acknowledged=true.",
        )
    if transfer.step_up_required:
        transfer.step_up_acknowledged = True

    _check_limits(db, transfer.from_user_id, transfer.amount_cents)

    sender = db.query(User).filter(User.id == transfer.from_user_id).first()
    recipient = db.query(User).filter(User.id == transfer.to_user_id).first()
    _require_active_account(sender)
    _require_active_account(recipient)

    try:
        payment = await unit_svc.create_book_payment(
            sender_account_id=sender.unit_account_id,
            recipient_account_id=recipient.unit_account_id,
            amount_cents=transfer.amount_cents,
            description=transfer.note or f"FAWN P2P from @{transfer.from_handle}",
            idempotency_key=transfer.idempotency_key,
        )
        transfer.unit_book_payment_id = payment["id"]
        transfer.status = "completed"
        transfer.completed_at = datetime.now(timezone.utc)
        _audit(db, transfer.id, current_user.id, "confirmed", {"unit_book_payment_id": payment["id"]})

        if transfer.source_request_id:
            src = db.query(P2PTransfer).filter(P2PTransfer.id == transfer.source_request_id).first()
            if src:
                src.status = "completed"
                src.completed_at = transfer.completed_at
    except Exception as e:
        transfer.status = "failed"
        transfer.error_message = str(e)[:500]
        _audit(db, transfer.id, current_user.id, "failed", {"error": str(e)[:500]})
        db.commit()
        raise HTTPException(status_code=502, detail="The transfer couldn't be completed. No money was moved — try again.")

    db.commit()
    return _to_out(transfer, current_user.id)


# --- Requests ---

@router.post("/requests", response_model=P2PTransferOut, status_code=201)
@limiter.limit("20/minute")
def create_request(request: Request, req: P2PRequestRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    existing = db.query(P2PTransfer).filter(P2PTransfer.idempotency_key == req.idempotency_key).first()
    if existing:
        if existing.to_user_id != current_user.id:
            raise HTTPException(status_code=404, detail="Request not found.")
        return _to_out(existing, current_user.id)

    my_handle = _get_handle_row(db, current_user.id)
    if not my_handle:
        raise HTTPException(status_code=400, detail="Claim a handle before requesting money.")

    payer = _resolve_handle(db, req.from_handle)
    if not payer:
        raise HTTPException(status_code=404, detail=f"No FAWN user found for @{req.from_handle}.")
    if payer.id == current_user.id:
        raise HTTPException(status_code=400, detail="You can't request money from yourself.")

    transfer = P2PTransfer(
        type="request", status="requested",
        from_user_id=payer.id, to_user_id=current_user.id,
        from_handle=req.from_handle, to_handle=my_handle.handle,
        amount_cents=req.amount_cents, note=req.note,
        idempotency_key=req.idempotency_key,
    )
    db.add(transfer)
    db.flush()
    _audit(db, transfer.id, current_user.id, "created", {"amount_cents": req.amount_cents, "request": True})
    db.commit()
    return _to_out(transfer, current_user.id)


@router.post("/requests/{request_id}/pay", response_model=P2PTransferOut, status_code=201)
@limiter.limit("20/minute")
def pay_request(request: Request, request_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    req_row = db.query(P2PTransfer).filter(P2PTransfer.id == request_id, P2PTransfer.type == "request").first()
    if not req_row:
        raise HTTPException(status_code=404, detail="Request not found.")
    if req_row.from_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="This request isn't addressed to you.")
    if req_row.status != "requested":
        raise HTTPException(status_code=400, detail=f"This request can't be paid (status: {req_row.status}).")

    _require_active_account(current_user)
    payee = db.query(User).filter(User.id == req_row.to_user_id).first()
    if not payee:
        raise HTTPException(status_code=404, detail="Requester not found.")
    _require_active_account(payee)
    _check_limits(db, current_user.id, req_row.amount_cents)

    new_recipient = _is_new_recipient(db, current_user.id, payee.id)
    first_ever = _is_first_send_ever(db, current_user.id)
    step_up = first_ever or (new_recipient and req_row.amount_cents >= NEW_RECIPIENT_STEP_UP_THRESHOLD_CENTS)
    warning = _scam_warning(req_row.note, new_recipient)

    linked = P2PTransfer(
        type="send", status="requires_step_up" if step_up else "pending",
        from_user_id=current_user.id, to_user_id=payee.id,
        from_handle=req_row.from_handle, to_handle=req_row.to_handle,
        amount_cents=req_row.amount_cents, note=req_row.note, warning=warning,
        source_request_id=req_row.id,
        idempotency_key=f"{req_row.idempotency_key}:pay",
        step_up_required=step_up,
    )
    db.add(linked)
    req_row.status = "fulfilling"
    db.flush()
    _audit(db, linked.id, current_user.id, "created", {"source_request_id": req_row.id})
    db.commit()
    return _to_out(linked, current_user.id)


# --- Split the bill ---

@router.post("/splits", response_model=P2PTransferList, status_code=201)
@limiter.limit("10/minute")
def create_split(request: Request, req: P2PSplitRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    existing = db.query(P2PTransfer).filter(P2PTransfer.idempotency_key.like(f"{req.idempotency_key}:%")).all()
    if existing:
        if any(t.to_user_id != current_user.id for t in existing):
            raise HTTPException(status_code=404, detail="Split not found.")
        return P2PTransferList(transfers=[_to_out(t, current_user.id) for t in existing])

    my_handle = _get_handle_row(db, current_user.id)
    if not my_handle:
        raise HTTPException(status_code=400, detail="Claim a handle before splitting a bill.")

    n = len(req.recipient_handles)
    base = req.total_amount_cents // n
    remainder = req.total_amount_cents - base * n
    if base <= 0:
        raise HTTPException(status_code=400, detail="Total amount is too small to split across that many people.")

    group_id = new_id()
    rows = []
    for i, handle in enumerate(req.recipient_handles):
        payer = _resolve_handle(db, handle)
        if not payer:
            raise HTTPException(status_code=404, detail=f"No FAWN user found for @{handle}.")
        if payer.id == current_user.id:
            raise HTTPException(status_code=400, detail="You can't split a bill with yourself.")
        amount = base + (remainder if i == 0 else 0)  # first row absorbs the leftover cent(s)
        row = P2PTransfer(
            type="request", status="requested",
            from_user_id=payer.id, to_user_id=current_user.id,
            from_handle=handle, to_handle=my_handle.handle,
            amount_cents=amount, note=req.note, group_id=group_id,
            idempotency_key=f"{req.idempotency_key}:{i}",
        )
        db.add(row)
        rows.append(row)

    db.flush()
    for row in rows:
        _audit(db, row.id, current_user.id, "created", {"group_id": group_id, "split": True})
    db.commit()
    return P2PTransferList(transfers=[_to_out(r, current_user.id) for r in rows])


# --- Feed ---

@router.get("/transfers", response_model=P2PTransferList)
def list_transfers(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.query(P2PTransfer).filter(
        or_(P2PTransfer.from_user_id == current_user.id, P2PTransfer.to_user_id == current_user.id)
    ).order_by(P2PTransfer.created_at.desc()).limit(100).all()
    return P2PTransferList(transfers=[_to_out(r, current_user.id) for r in rows])


@router.get("/limits")
def get_my_limits(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Remaining send headroom for the current user, so the UI can show
    "you can send up to $X today" before an attempt instead of only
    erroring after. Computed from the same rolling totals _check_limits
    enforces — this is advisory display data; the authoritative check
    still happens (under a row lock) at confirm time.
    """
    now = datetime.now(timezone.utc)
    day_total = _rolling_total(db, current_user.id, now - timedelta(hours=24))
    week_total = _rolling_total(db, current_user.id, now - timedelta(days=7))
    day_remaining = max(0, DAILY_LIMIT_CENTS - day_total)
    week_remaining = max(0, ROLLING_7DAY_LIMIT_CENTS - week_total)
    return {
        "per_transaction_limit_cents": PER_TX_LIMIT_CENTS,
        "daily_limit_cents": DAILY_LIMIT_CENTS,
        "weekly_limit_cents": ROLLING_7DAY_LIMIT_CENTS,
        "daily_remaining_cents": day_remaining,
        "weekly_remaining_cents": week_remaining,
        # The most a single new send could be right now, all caps considered.
        "max_send_cents": min(PER_TX_LIMIT_CENTS, day_remaining, week_remaining),
    }


# --- Disputes (Reg E–style claims layer) ---

def _canonical_payment_id(db: Session, transfer: P2PTransfer) -> str:
    """A 'request' that's been paid and its linked 'send' both represent the
    same real movement of funds (see pay_request/confirm_transfer above).
    Resolve either row to the id of the underlying 'send' transfer so that
    disputing the request and disputing its linked send are treated as
    disputing the same payment, not two separate ones.
    """
    if transfer.type == "request":
        linked_send = db.query(P2PTransfer).filter(
            P2PTransfer.source_request_id == transfer.id,
            P2PTransfer.type == "send",
        ).first()
        if linked_send:
            return linked_send.id
    return transfer.id


@router.post("/transfers/{transfer_id}/dispute", response_model=P2PDisputeOut, status_code=201)
@limiter.limit("10/minute")
def dispute_transfer(request: Request, transfer_id: str, req: P2PDisputeRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    transfer = db.query(P2PTransfer).filter(P2PTransfer.id == transfer_id).first()
    if not transfer:
        raise HTTPException(status_code=404, detail="Transfer not found.")
    if current_user.id not in (transfer.from_user_id, transfer.to_user_id):
        raise HTTPException(status_code=403, detail="You weren't a party to this transfer.")
    if transfer.status != "completed":
        raise HTTPException(status_code=400, detail="Only completed transfers can be disputed.")

    payment_id = _canonical_payment_id(db, transfer)
    # Also fold in the legacy per-row check (transfer_id == this row, or the
    # row on the other side of the request/send link) for disputes filed
    # before payment_id existed and was always populated.
    sibling_id = transfer.source_request_id if transfer.type == "send" else payment_id
    open_existing = db.query(P2PDispute).filter(
        P2PDispute.status == "open",
        or_(
            P2PDispute.payment_id == payment_id,
            P2PDispute.transfer_id == transfer_id,
            P2PDispute.transfer_id == sibling_id,
        ),
    ).first()
    if open_existing:
        raise HTTPException(status_code=409, detail="A dispute is already open for this transfer.")

    dispute = P2PDispute(transfer_id=transfer_id, payment_id=payment_id, filer_user_id=current_user.id, reason=req.reason)
    transfer.status = "disputed"
    db.add(dispute)
    db.flush()
    _audit(db, transfer_id, current_user.id, "disputed", {"dispute_id": dispute.id, "payment_id": payment_id})
    db.commit()
    return P2PDisputeOut(id=dispute.id, transfer_id=transfer_id, status=dispute.status, reason=dispute.reason, created_at=dispute.created_at.isoformat())


# --- Tier 2: external sends (stubbed) ---

@router.post("/external-transfers")
async def create_external_transfer(current_user: User = Depends(get_current_user)):
    """Send to a non-FAWN wallet/address. Not live — see services/external_send.py."""
    provider = get_external_send_provider()
    try:
        await provider.send(
            sender_account_id=current_user.unit_account_id or "",
            destination_account_number="",
            destination_routing_number="",
            amount_cents=0,
            idempotency_key="",
        )
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))


@router.get("/admin/disputes", dependencies=[Depends(require_admin_key)])
def list_disputes(db: Session = Depends(get_db)):
    rows = db.query(P2PDispute).order_by(P2PDispute.created_at.desc()).all()
    return [
        P2PDisputeOut(id=d.id, transfer_id=d.transfer_id, status=d.status, reason=d.reason, created_at=d.created_at.isoformat())
        for d in rows
    ]


@router.post("/admin/disputes/{dispute_id}/resolve", dependencies=[Depends(require_admin_key)])
async def resolve_dispute(dispute_id: str, action: str, note: Optional[str] = None, db: Session = Depends(get_db)):
    """action: 'refund' (issues a reverse Book Payment) or 'deny'."""
    dispute = db.query(P2PDispute).filter(P2PDispute.id == dispute_id).first()
    if not dispute:
        raise HTTPException(status_code=404, detail="Dispute not found.")
    if dispute.status != "open":
        raise HTTPException(status_code=400, detail="Dispute already resolved.")

    transfer = db.query(P2PTransfer).filter(P2PTransfer.id == dispute.transfer_id).first()
    if not transfer:
        raise HTTPException(status_code=404, detail="Underlying transfer not found.")

    if action == "refund":
        sender = db.query(User).filter(User.id == transfer.from_user_id).first()
        recipient = db.query(User).filter(User.id == transfer.to_user_id).first()
        # Use the canonical underlying-payment id (not this dispute's own
        # transfer row id) so that a refund already issued via the linked
        # request/send row's dispute is recognized here too, and vice versa.
        payment_id = dispute.payment_id or _canonical_payment_id(db, transfer)
        refund_key = f"refund:{payment_id}"
        already = db.query(P2PTransfer).filter(P2PTransfer.idempotency_key == refund_key).first()
        if not already:
            payment = await unit_svc.create_book_payment(
                sender_account_id=recipient.unit_account_id,
                recipient_account_id=sender.unit_account_id,
                amount_cents=transfer.amount_cents,
                description=f"Dispute refund for transfer {transfer.id}",
                idempotency_key=refund_key,
            )
            db.add(P2PTransfer(
                type="send", status="completed",
                from_user_id=recipient.id, to_user_id=sender.id,
                from_handle=transfer.to_handle, to_handle=transfer.from_handle,
                amount_cents=transfer.amount_cents, note="Dispute refund",
                related_transfer_id=transfer.id, unit_book_payment_id=payment["id"],
                idempotency_key=refund_key, completed_at=datetime.now(timezone.utc),
            ))
        dispute.status = "refunded"
    elif action == "deny":
        dispute.status = "denied"
    else:
        raise HTTPException(status_code=400, detail="action must be 'refund' or 'deny'")

    dispute.resolution_note = note
    dispute.resolved_at = datetime.now(timezone.utc)
    _audit(db, transfer.id, "admin", "dispute_resolved", {"action": action, "dispute_id": dispute.id})
    db.commit()
    return {"dispute_id": dispute.id, "status": dispute.status}
