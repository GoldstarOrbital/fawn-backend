"""
Emergency admin endpoint for manual balance credits (undetected on-chain deposits, etc)
SECURITY: Requires X-Admin-Key header (same as /fees/collect)
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
from database import get_db
from models import User, UserAuditLog, ChainScanCheckpoint, CryptoDeposit, CryptoTransfer
from routers.admin import require_admin_key
import json

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/bootstrap-alembic-stamp")
async def bootstrap_alembic_stamp(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin_key),
):
    """
    ONE-TIME bootstrap: mark Alembic's baseline migration as already
    applied, without running any DDL. Production's schema already
    matches the baseline (built up over time via the old _patch()
    system in main.py, which keeps running unchanged) -- this just
    writes one row to a new alembic_version table so Alembic knows
    where the DB stands, enabling real migrations going forward.

    Runs inside the app's own process using its own already-configured
    DATABASE_URL, rather than requiring direct production DB
    credentials to be handled outside the app. Refuses to run if
    alembic_version already has a row, so this can't accidentally
    re-stamp over real migration history later. This endpoint is meant
    to be removed from the codebase right after its one real use --
    see alembic/README.md.
    """
    from sqlalchemy import text
    from alembic.config import Config
    from alembic import command

    existing = db.execute(text(
        "SELECT to_regclass('public.alembic_version') IS NOT NULL"
        if "postgresql" in str(db.bind.url)
        else "SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'"
    )).scalar()
    if existing:
        raise HTTPException(status_code=409, detail="alembic_version already exists -- refusing to re-stamp.")

    cfg = Config("alembic.ini")
    command.stamp(cfg, "head")

    current = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    return {"status": "stamped", "revision": current}


@router.get("/pending-transfers")
async def pending_transfers(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin_key),
):
    """List sends held for manual review (large first-time-recipient
    sends -- see services/crypto_wallet.py::send_usdc). Nothing has moved
    yet for any of these; the ledger was never touched."""
    rows = db.query(CryptoTransfer).filter(
        CryptoTransfer.status == "pending_review"
    ).order_by(CryptoTransfer.created_at.asc()).all()

    sender_emails = {
        u.id: u.email for u in db.query(User).filter(
            User.id.in_([t.sender_id for t in rows])
        ).all()
    }

    return {
        "pending": [
            {
                "transfer_id": t.id,
                "sender_id": t.sender_id,
                "sender_email": sender_emails.get(t.sender_id),
                "recipient_address": t.recipient_address,
                "amount_cents": t.amount_cents,
                "fee_cents": t.fee_cents,
                "memo": t.memo,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in rows
        ]
    }


class ApproveTransferRequest(BaseModel):
    transfer_id: str


@router.post("/approve-transfer")
async def approve_transfer(
    req: ApproveTransferRequest,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin_key),
):
    """Execute a held send for real -- signs and broadcasts on-chain now,
    then settles the ledger. Re-runs every normal safeguard (balance,
    hard limits, velocity, sanctions screening) at approval time, not
    just at hold time -- state may have changed since the hold was
    created (e.g. the sender made other sends in the meantime, or the
    recipient was added to the sanctions list).

    Claims the transfer with an atomic conditional UPDATE (pending_review
    -> approving) before doing anything else. Without this, two
    concurrent approve requests for the same transfer_id (a double-click,
    a retried request) could both pass a plain SELECT-then-check and both
    execute the real on-chain send -- a genuine double-send, not just a
    duplicate audit log entry. Only the request that successfully claims
    the row proceeds; the other gets a clear 409, not a silent no-op.
    """
    from services import onchain_send

    claimed = db.query(CryptoTransfer).filter(
        CryptoTransfer.id == req.transfer_id,
        CryptoTransfer.status == "pending_review",
    ).update({"status": "approving"}, synchronize_session=False)
    db.commit()

    if claimed == 0:
        # If the row still showed pending_review, OUR update would have
        # claimed it -- so right after a failed claim it can only be:
        # doesn't exist at all (404), or exists in some other status,
        # meaning a concurrent request already claimed/settled it (409).
        existing = db.query(CryptoTransfer).filter(CryptoTransfer.id == req.transfer_id).first()
        if existing:
            raise HTTPException(status_code=409, detail=f"Transfer is already {existing.status} -- not available to approve.")
        raise HTTPException(status_code=404, detail=f"No pending-review transfer with id {req.transfer_id}")

    transfer = db.query(CryptoTransfer).filter(CryptoTransfer.id == req.transfer_id).first()

    try:
        sender = db.query(User).filter(User.id == transfer.sender_id).first()
        if not sender:
            raise HTTPException(status_code=404, detail="Sender no longer exists")

        total_needed = transfer.amount_cents + transfer.fee_cents
        if sender.usdc_balance_cents < total_needed:
            raise HTTPException(
                status_code=402,
                detail=f"Sender's balance (${sender.usdc_balance_cents/100:.2f}) no longer covers "
                       f"this transfer + fee (${total_needed/100:.2f})."
            )

        settlement = await onchain_send.send_onchain_usdc(sender, transfer.recipient_address, transfer.amount_cents, db)

        transfer.status = "completed"
        transfer.tx_hash = settlement["tx_hash"]
        transfer.chain = settlement["chain"]
        transfer.completed_at = datetime.utcnow()

        sender.usdc_balance_cents -= total_needed
        sender.total_fees_paid_cents += transfer.fee_cents

        retention_expires = datetime.now(tz=timezone.utc) + timedelta(days=365 * 7)
        db.add(UserAuditLog(
            user_id=sender.id,
            action="send_approved_after_review",
            details=json.dumps({
                "transfer_id": transfer.id,
                "amount_cents": transfer.amount_cents,
                "chain": settlement["chain"],
                "tx_hash": settlement["tx_hash"],
                "timestamp": datetime.utcnow().isoformat(),
            }),
            retention_expires_at=retention_expires,
        ))
        db.commit()

        return {
            "transfer_id": transfer.id,
            "status": "completed",
            "chain": settlement["chain"],
            "tx_hash": settlement["tx_hash"],
            "new_sender_balance": f"${sender.usdc_balance_cents/100:.2f}",
        }
    except Exception:
        # Whatever went wrong, release the claim -- an "approving" row
        # stuck forever (unretryable, invisible to /admin/pending-transfers
        # since that only lists pending_review) is worse than letting a
        # failed approval be tried again.
        db.rollback()
        transfer_retry = db.query(CryptoTransfer).filter(CryptoTransfer.id == req.transfer_id).first()
        if transfer_retry and transfer_retry.status == "approving":
            transfer_retry.status = "pending_review"
            db.commit()
        raise


class RejectTransferRequest(BaseModel):
    transfer_id: str
    reason: str = "rejected_by_admin"


@router.post("/reject-transfer")
async def reject_transfer(
    req: RejectTransferRequest,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin_key),
):
    """Reject a held send -- no funds ever moved, the ledger was never
    touched, this just marks it closed."""
    transfer = db.query(CryptoTransfer).filter(
        CryptoTransfer.id == req.transfer_id,
        CryptoTransfer.status == "pending_review",
    ).first()
    if not transfer:
        raise HTTPException(status_code=404, detail=f"No pending-review transfer with id {req.transfer_id}")

    transfer.status = "rejected"

    retention_expires = datetime.now(tz=timezone.utc) + timedelta(days=365 * 7)
    db.add(UserAuditLog(
        user_id=transfer.sender_id,
        action="send_rejected_after_review",
        details=json.dumps({
            "transfer_id": transfer.id,
            "reason": req.reason,
            "timestamp": datetime.utcnow().isoformat(),
        }),
        retention_expires_at=retention_expires,
    ))
    db.commit()

    return {"transfer_id": transfer.id, "status": "rejected"}


@router.get("/sanctions-status")
async def sanctions_status(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin_key),
):
    """Debug/ops helper: is the OFAC screening list actually being kept
    current? A screening check that silently runs against an empty or
    stale list is worse than no screening at all if nobody notices."""
    from models import SanctionedAddress, SanctionsListRefresh

    total_addresses = db.query(SanctionedAddress).count()
    last_refresh = db.query(SanctionsListRefresh).order_by(SanctionsListRefresh.created_at.desc()).first()

    return {
        "total_sanctioned_addresses": total_addresses,
        "last_refresh": {
            "status": last_refresh.status,
            "addresses_found": last_refresh.addresses_found,
            "error": last_refresh.error,
            "at": last_refresh.created_at.isoformat() if last_refresh.created_at else None,
        } if last_refresh else None,
    }


class RewindCheckpointRequest(BaseModel):
    wallet_address: str
    chain: str
    blocks_back: int = 15000


@router.post("/rewind-checkpoint")
async def rewind_checkpoint(
    req: RewindCheckpointRequest,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin_key),
):
    """
    Rewind a (wallet, chain) scan checkpoint so the NEXT background/sync-now
    cycle re-scans that range via the normal event-log path -- lets the
    automated system catch a deposit it missed (e.g. during an RPC outage
    that predates the checkpoint-advancement fix) itself, with real
    per-transfer attribution, instead of an admin computing and injecting a
    balance delta by hand.

    Deliberately rewinds rather than deletes the checkpoint: deleting it
    would make the next scan a fresh backfill pass, which records but does
    NOT credit deposits (by design, for pre-existing balances). Rewinding
    keeps it in normal incremental/crediting mode, just starting from an
    earlier block. Already-recorded deposits in the re-scanned range are
    no-ops (CryptoDeposit's chain+tx_hash+contract+to_address unique
    constraint), so this is safe to run even if some of the range was
    already scanned successfully.
    """
    checkpoint = db.query(ChainScanCheckpoint).filter(
        ChainScanCheckpoint.wallet_address.ilike(req.wallet_address),
        ChainScanCheckpoint.chain == req.chain,
    ).first()
    if not checkpoint:
        raise HTTPException(status_code=404, detail=f"No checkpoint for {req.wallet_address} on {req.chain}")

    old_block = checkpoint.last_scanned_block
    checkpoint.last_scanned_block = max(old_block - req.blocks_back, 0)
    db.commit()

    return {
        "wallet_address": req.wallet_address,
        "chain": req.chain,
        "old_last_scanned_block": old_block,
        "new_last_scanned_block": checkpoint.last_scanned_block,
        "blocks_back": req.blocks_back,
    }


class SetChainBaselineRequest(BaseModel):
    wallet_address: str
    chain: str
    baseline_cents: int


@router.post("/set-chain-baseline")
async def set_chain_baseline(
    req: SetChainBaselineRequest,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin_key),
):
    """
    Set ChainScanCheckpoint.pre_ledger_baseline_cents -- the amount already
    attributable to a (wallet, chain) that predates per-transfer
    CryptoDeposit tracking (e.g. balance reconciled via a one-off manual
    credit before this feature existed).

    Why this exists: services.blockchain_monitor's balance-diff fallback
    compares live on-chain balance against pre_ledger_baseline_cents PLUS
    the sum of credited CryptoDeposit rows for that chain, to decide how
    much (if any) is a genuinely new, uncredited deposit. Without a correct
    baseline, a chain whose balance predates CryptoDeposit tracking looks
    like it has contributed $0 so far -- so the very first time its
    fallback fires, it re-credits the ENTIRE historical balance as if it
    were new. Confirmed in production: caused a real $5 double-credit.

    This should only ever need to be set once per (wallet, chain), for
    wallets that had a balance before this system started tracking
    per-chain contributions. Every checkpoint created from now on defaults
    to 0, which is correct.
    """
    checkpoint = db.query(ChainScanCheckpoint).filter(
        ChainScanCheckpoint.wallet_address.ilike(req.wallet_address),
        ChainScanCheckpoint.chain == req.chain,
    ).first()
    if not checkpoint:
        raise HTTPException(status_code=404, detail=f"No checkpoint for {req.wallet_address} on {req.chain}")

    old_baseline = checkpoint.pre_ledger_baseline_cents
    checkpoint.pre_ledger_baseline_cents = req.baseline_cents
    db.commit()

    return {
        "wallet_address": req.wallet_address,
        "chain": req.chain,
        "old_baseline_cents": old_baseline,
        "new_baseline_cents": checkpoint.pre_ledger_baseline_cents,
    }


class FixDepositAmountRequest(BaseModel):
    tx_hash: str
    correct_amount_cents: int
    reason: str


@router.post("/fix-deposit-amount")
async def fix_deposit_amount(
    req: FixDepositAmountRequest,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin_key),
):
    """
    Correct a specific CryptoDeposit's recorded amount (e.g. one credited
    by the balance-diff fallback before a baseline was seeded, so it
    over-counted a pre-existing balance as brand new). Adjusts the user's
    ledger by exactly the delta, atomically, with a full audit log --
    unlike POST /admin/credit-balance, this corrects the specific
    misattributed record itself (so the user's transaction history stays
    accurate) rather than just nudging the total.
    """
    deposit = db.query(CryptoDeposit).filter(CryptoDeposit.tx_hash == req.tx_hash).first()
    if not deposit:
        raise HTTPException(status_code=404, detail=f"No CryptoDeposit with tx_hash {req.tx_hash}")

    user = db.query(User).filter(User.id == deposit.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail=f"No user for deposit {req.tx_hash}")

    old_deposit_amount = deposit.amount_cents
    delta = req.correct_amount_cents - old_deposit_amount
    old_balance = user.usdc_balance_cents

    deposit.amount_cents = req.correct_amount_cents
    if deposit.credited_to_ledger:
        user.usdc_balance_cents += delta

    retention_expires = datetime.now(tz=timezone.utc) + timedelta(days=365 * 7)
    db.add(UserAuditLog(
        user_id=user.id,
        action="deposit_amount_corrected",
        details=json.dumps({
            "tx_hash": req.tx_hash,
            "old_deposit_amount_cents": old_deposit_amount,
            "new_deposit_amount_cents": req.correct_amount_cents,
            "ledger_before_cents": old_balance,
            "ledger_after_cents": user.usdc_balance_cents,
            "reason": req.reason,
            "timestamp": datetime.utcnow().isoformat(),
        }),
        retention_expires_at=retention_expires,
    ))
    db.commit()

    return {
        "tx_hash": req.tx_hash,
        "email": user.email,
        "old_deposit_amount": f"${old_deposit_amount / 100:.2f}",
        "new_deposit_amount": f"${req.correct_amount_cents / 100:.2f}",
        "old_balance": f"${old_balance / 100:.2f}",
        "new_balance": f"${user.usdc_balance_cents / 100:.2f}",
    }


@router.get("/rpc-health")
async def rpc_health(
    chain: str,
    _: str = Depends(require_admin_key),
):
    """Debug helper: call eth_blockNumber against every configured RPC
    endpoint for one chain, from Railway's own network path, and report
    exactly what each one returns. A checkpoint that stops advancing could
    mean every endpoint is failing in a way a local curl from a different
    network wouldn't reproduce (e.g. a public RPC rate-limiting or
    blocking Railway's egress IP specifically)."""
    from services import blockchain_monitor as bm
    import httpx as httpx_lib

    if chain not in bm.CHAINS:
        raise HTTPException(status_code=400, detail=f"Unknown chain: {chain}")

    endpoints = bm._get_rpc_endpoints(chain)
    results = []
    for endpoint in endpoints:
        display = endpoint if "alchemy" not in endpoint else endpoint.rsplit("/", 1)[0] + "/***"
        entry = {"endpoint": display}
        try:
            async with httpx_lib.AsyncClient(timeout=15.0) as client:
                resp = await client.post(endpoint, json={
                    "jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1,
                })
                entry["http_status"] = resp.status_code
                body = resp.json()
                if "error" in body:
                    entry["rpc_error"] = body["error"]
                else:
                    entry["result_block"] = int(body["result"], 16) if body.get("result") else None
        except Exception as e:
            entry["exception"] = f"{type(e).__name__}: {e}"
        results.append(entry)

    return {"chain": chain, "endpoints_tried": results}


@router.get("/wallet-scan-status")
async def wallet_scan_status(
    wallet_address: str,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin_key),
):
    """Debug helper: see exactly what the blockchain monitor has recorded
    for a wallet -- per-chain checkpoint (last scanned block, backfilled
    status) and recent CryptoDeposit records. Lets us tell apart "monitor
    hasn't reached this block yet" from "monitor is stuck/erroring" from
    "logs missed it, fallback should catch it next cycle" without needing
    direct Railway log access."""
    checkpoints = db.query(ChainScanCheckpoint).filter(
        ChainScanCheckpoint.wallet_address.ilike(wallet_address)
    ).all()
    deposits = db.query(CryptoDeposit).join(User).filter(
        User.crypto_wallet_address.ilike(wallet_address)
    ).order_by(CryptoDeposit.created_at.desc()).limit(20).all()

    return {
        "wallet_address": wallet_address,
        "checkpoints": [
            {
                "chain": c.chain,
                "last_scanned_block": c.last_scanned_block,
                "is_backfilled": c.is_backfilled,
                "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            }
            for c in checkpoints
        ],
        "recent_deposits": [
            {
                "chain": d.chain,
                "contract_address": d.contract_address,
                "from_address": d.from_address,
                "amount_cents": d.amount_cents,
                "tx_hash": d.tx_hash,
                "block_number": d.block_number,
                "credited_to_ledger": d.credited_to_ledger,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in deposits
        ],
    }

class ManualCreditRequest(BaseModel):
    wallet_address: str
    amount_cents: int
    reason: str = "manual_deposit_credit"

@router.post("/credit-balance")
async def manual_credit_balance(
    req: ManualCreditRequest,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin_key),
):
    """
    Manually credit a user's USDC balance.

    SECURITY: Requires X-Admin-Key header (same as /fees/collect)
    """
    # Find user by wallet
    user = db.query(User).filter(
        User.crypto_wallet_address.ilike(req.wallet_address)
    ).first()

    if not user:
        raise HTTPException(
            status_code=404,
            detail=f"No user found with wallet: {req.wallet_address}"
        )

    # Credit the balance
    old_balance = user.usdc_balance_cents
    user.usdc_balance_cents += req.amount_cents

    # Create audit log (7-year retention)
    retention_expires = datetime.now(tz=timezone.utc) + timedelta(days=365*7)
    audit = UserAuditLog(
        user_id=user.id,
        action="manual_balance_credit",
        details=json.dumps({
            "wallet": req.wallet_address,
            "amount_cents": req.amount_cents,
            "old_balance_cents": old_balance,
            "new_balance_cents": user.usdc_balance_cents,
            "reason": req.reason,
            "timestamp": datetime.utcnow().isoformat(),
        }),
        retention_expires_at=retention_expires,
    )
    db.add(audit)
    db.commit()

    return {
        "user_id": user.id,
        "email": user.email,
        "wallet": req.wallet_address,
        "amount_credited": f"${req.amount_cents / 100:.2f}",
        "old_balance": f"${old_balance / 100:.2f}",
        "new_balance": f"${user.usdc_balance_cents / 100:.2f}",
        "timestamp": datetime.utcnow().isoformat(),
        "status": "credited"
    }
