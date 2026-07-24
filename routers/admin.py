import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Security, status
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel
from sqlalchemy import func, cast, Date
from sqlalchemy.orm import Session

from database import get_db
from models import FoundingMember, WaitlistEntry, User, DealSuggestion

router = APIRouter(prefix="/admin", tags=["admin"])

API_KEY_HEADER = APIKeyHeader(name="X-Admin-Key", auto_error=False)


def require_admin_key(api_key: Optional[str] = Security(API_KEY_HEADER)) -> str:
    expected = os.environ.get("ADMIN_API_KEY", "")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ADMIN_API_KEY environment variable is not configured.",
        )
    if not api_key or not hmac.compare_digest(api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing X-Admin-Key header.",
        )
    return api_key


class WaitlistEntryOut(BaseModel):
    id: str
    email: str
    referral_code: Optional[str]
    signup_timestamp: datetime
    position: int

    class Config:
        from_attributes = True


class ReferralCodeCount(BaseModel):
    code: str
    count: int


class StatsOut(BaseModel):
    total_signups: int
    signups_today: int
    signups_this_week: int
    top_referral_codes: List[ReferralCodeCount]
    referral_conversion_rate: float


class DayCount(BaseModel):
    date: str
    count: int


class MemberOut(BaseModel):
    id: str
    email: str
    member_number: int
    tier: str
    amount_cents: int
    joined_at: str
    refunded: bool
    stripe_session_id: Optional[str]

    class Config:
        from_attributes = True


class TierSummary(BaseModel):
    founding: int
    inner_circle: int
    dev_sprint: int


class MemberSummary(BaseModel):
    total_members: int
    total_revenue_cents: int
    by_tier: TierSummary


class MembersOut(BaseModel):
    members: List[MemberOut]
    summary: MemberSummary


class WaitlistCountOut(BaseModel):
    count: int


@router.get("/waitlist", response_model=List[WaitlistEntryOut])
def get_waitlist(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin_key),
) -> List[WaitlistEntryOut]:
    entries = (
        db.query(WaitlistEntry)
        .order_by(WaitlistEntry.created_at.asc())
        .all()
    )
    result = []
    for position, entry in enumerate(entries, start=1):
        result.append(
            WaitlistEntryOut(
                id=entry.id,
                email=entry.email,
                referral_code=entry.referral_code,
                signup_timestamp=entry.created_at,
                position=position,
            )
        )
    return result


@router.get("/waitlist-count", response_model=WaitlistCountOut)
def get_waitlist_count(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin_key),
) -> WaitlistCountOut:
    count: int = db.query(func.count(WaitlistEntry.id)).scalar() or 0
    return WaitlistCountOut(count=count)


@router.get("/stats", response_model=StatsOut)
def get_stats(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin_key),
) -> StatsOut:
    now = datetime.now(tz=timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=now.weekday())

    total_signups: int = db.query(func.count(WaitlistEntry.id)).scalar() or 0

    signups_today: int = (
        db.query(func.count(WaitlistEntry.id))
        .filter(WaitlistEntry.created_at >= today_start)
        .scalar()
        or 0
    )

    signups_this_week: int = (
        db.query(func.count(WaitlistEntry.id))
        .filter(WaitlistEntry.created_at >= week_start)
        .scalar()
        or 0
    )

    top_codes_rows = (
        db.query(
            WaitlistEntry.referral_code,
            func.count(WaitlistEntry.id).label("cnt"),
        )
        .filter(WaitlistEntry.referral_code.isnot(None))
        .group_by(WaitlistEntry.referral_code)
        .order_by(func.count(WaitlistEntry.id).desc())
        .limit(10)
        .all()
    )
    top_referral_codes = [
        ReferralCodeCount(code=row.referral_code, count=row.cnt)
        for row in top_codes_rows
    ]

    signups_with_referral: int = (
        db.query(func.count(WaitlistEntry.id))
        .filter(WaitlistEntry.referral_code.isnot(None))
        .scalar()
        or 0
    )
    referral_conversion_rate: float = (
        round(signups_with_referral / total_signups, 4) if total_signups > 0 else 0.0
    )

    return StatsOut(
        total_signups=total_signups,
        signups_today=signups_today,
        signups_this_week=signups_this_week,
        top_referral_codes=top_referral_codes,
        referral_conversion_rate=referral_conversion_rate,
    )


@router.get("/signups-by-day", response_model=List[DayCount])
def get_signups_by_day(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin_key),
) -> List[DayCount]:
    now = datetime.now(tz=timezone.utc)
    thirty_days_ago = (now - timedelta(days=29)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    rows = (
        db.query(
            cast(WaitlistEntry.created_at, Date).label("day"),
            func.count(WaitlistEntry.id).label("cnt"),
        )
        .filter(WaitlistEntry.created_at >= thirty_days_ago)
        .group_by(cast(WaitlistEntry.created_at, Date))
        .order_by(cast(WaitlistEntry.created_at, Date).asc())
        .all()
    )

    counts_by_day = {str(row.day): row.cnt for row in rows}
    result = []
    for i in range(30):
        day = (thirty_days_ago + timedelta(days=i)).date()
        day_str = str(day)
        result.append(DayCount(date=day_str, count=counts_by_day.get(day_str, 0)))

    return result


@router.get("/members", response_model=MembersOut)
def get_members(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin_key),
    tier: Optional[str] = Query(default=None, description="Filter by tier: founding, inner_circle, dev_sprint"),
    limit: int = Query(default=50, ge=1, le=200, description="Max results to return"),
) -> MembersOut:
    valid_tiers = {"founding", "inner_circle", "dev_sprint"}
    if tier is not None and tier not in valid_tiers:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid tier. Must be one of: {', '.join(sorted(valid_tiers))}",
        )

    query = db.query(FoundingMember).order_by(FoundingMember.member_number.asc())
    if tier is not None:
        query = query.filter(FoundingMember.tier == tier)

    rows = query.limit(limit).all()

    members = [
        MemberOut(
            id=m.id,
            email=m.email,
            member_number=m.member_number,
            tier=m.tier,
            amount_cents=m.amount_cents,
            joined_at=m.joined_at.isoformat() if m.joined_at else "",
            refunded=m.refunded,
            stripe_session_id=m.stripe_session_id,
        )
        for m in rows
    ]

    # Summary always covers all members (ignores tier/limit filter)
    summary_rows = (
        db.query(
            FoundingMember.tier,
            func.count(FoundingMember.id).label("cnt"),
            func.sum(FoundingMember.amount_cents).label("rev"),
        )
        .group_by(FoundingMember.tier)
        .all()
    )

    by_tier_counts: Dict[str, int] = {"founding": 0, "inner_circle": 0, "dev_sprint": 0}
    total_members = 0
    total_revenue_cents = 0

    for row in summary_rows:
        t = row.tier
        cnt = row.cnt or 0
        rev = row.rev or 0
        total_members += cnt
        total_revenue_cents += rev
        if t in by_tier_counts:
            by_tier_counts[t] = cnt

    summary = MemberSummary(
        total_members=total_members,
        total_revenue_cents=total_revenue_cents,
        by_tier=TierSummary(
            founding=by_tier_counts["founding"],
            inner_circle=by_tier_counts["inner_circle"],
            dev_sprint=by_tier_counts["dev_sprint"],
        ),
    )

    return MembersOut(members=members, summary=summary)


@router.get("/stats/overview")
def get_stats_overview(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin_key),
) -> Dict:
    waitlist_count: int = db.query(func.count(WaitlistEntry.id)).scalar() or 0

    founding_members_count: int = db.query(func.count(FoundingMember.id)).scalar() or 0

    # Revenue: amount_cents on FoundingMember is the price paid per tier via
    # Stripe checkout (see model docstring) — sum across all members is the
    # genuine total revenue collected.
    tier_rows = (
        db.query(
            FoundingMember.tier,
            func.count(FoundingMember.id).label("cnt"),
            func.sum(FoundingMember.amount_cents).label("rev"),
        )
        .group_by(FoundingMember.tier)
        .all()
    )
    founding_members_by_tier: Dict[str, int] = {
        "founding": 0,
        "inner_circle": 0,
        "dev_sprint": 0,
    }
    total_revenue_cents = 0
    for row in tier_rows:
        if row.tier in founding_members_by_tier:
            founding_members_by_tier[row.tier] = row.cnt or 0
        total_revenue_cents += row.rev or 0

    registered_users_count: int = db.query(func.count(User.id)).scalar() or 0

    # "Active account" = a USDC wallet created.
    users_with_active_account_count: int = (
        db.query(func.count(User.id))
        .filter(User.wallet_initialized.is_(True))
        .scalar()
        or 0
    )

    school_rows = (
        db.query(User.school, func.count(User.id).label("cnt"))
        .filter(User.school.isnot(None))
        .group_by(User.school)
        .all()
    )
    users_by_school: Dict[str, int] = {row.school: row.cnt for row in school_rows}

    deal_suggestions_pending_count: int = (
        db.query(func.count(DealSuggestion.id))
        .filter(DealSuggestion.status == "pending")
        .scalar()
        or 0
    )

    return {
        "waitlist_count": waitlist_count,
        "founding_members_count": founding_members_count,
        "founding_members_by_tier": founding_members_by_tier,
        "total_revenue_cents": total_revenue_cents,
        "registered_users_count": registered_users_count,
        "users_with_active_account_count": users_with_active_account_count,
        "users_by_school": users_by_school,
        "deal_suggestions_pending_count": deal_suggestions_pending_count,
    }


@router.get("/users")
def list_users(
    db: Session = Depends(get_db),
    _: str = Depends(require_admin_key),
    limit: int = Query(default=500, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> Dict:
    """List registered user emails with basic signup/wallet status --
    matches the existing /admin/waitlist pattern (which already exposes
    waitlist emails the same way), just for full User accounts. No
    password hashes or other sensitive fields returned."""
    total = db.query(func.count(User.id)).scalar() or 0
    rows = (
        db.query(User)
        .order_by(User.created_at.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "users": [
            {
                "email": u.email,
                "created_at": u.created_at.isoformat() if u.created_at else None,
                "wallet_initialized": bool(u.wallet_initialized),
                "school": u.school,
            }
            for u in rows
        ],
    }


@router.get("/users/lookup")
def lookup_user_status(
    email: str = Query(..., description="Account email to look up"),
    db: Session = Depends(get_db),
    _: str = Depends(require_admin_key),
) -> Dict:
    """Debug helper: see exactly where a user's wallet/account setup stands,
    without needing direct DB access. No password/SSN/secrets exposed."""
    from models import CryptoWallet

    user = db.query(User).filter(func.lower(User.email) == email.lower()).first()
    if not user:
        raise HTTPException(status_code=404, detail="No user with that email.")

    crypto_wallet_row = None
    if user.crypto_wallet_address:
        crypto_wallet_row = db.query(CryptoWallet).filter(
            CryptoWallet.wallet_address.ilike(user.crypto_wallet_address)
        ).first()

    return {
        "email": user.email,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "wallet_initialized": bool(user.wallet_initialized),
        "crypto_wallet_address": user.crypto_wallet_address,
        "wallet_type": user.wallet_type,
        "alpaca_account_id": user.alpaca_account_id,
        "crypto_wallet_row_exists": crypto_wallet_row is not None,
        "has_encrypted_private_key": bool(crypto_wallet_row and crypto_wallet_row.encrypted_private_key),
    }


class ReissueWalletResult(BaseModel):
    email: str
    action: str  # "reissued" | "already_ok" | "flagged_has_funds" | "dry_run"
    old_wallet_address: Optional[str] = None
    new_wallet_address: Optional[str] = None
    detail: str


@router.post("/reissue-stranded-wallet", response_model=ReissueWalletResult)
async def reissue_stranded_wallet(
    email: str = Query(..., description="Account whose stranded custodial wallet to reissue."),
    confirm: bool = Query(default=False, description="Must be true to actually reissue; otherwise dry-run."),
    _admin: str = Depends(require_admin_key),
    db: Session = Depends(get_db),
):
    """Reissue a fresh custodial wallet for an account whose current wallet has
    NO usable signing key (created before the encrypted-key fix — FAWN can't
    sign for it, so the user is stuck).

    Safety: refuses to reissue if the old wallet holds any USDC on-chain, so
    real funds are never abandoned. The old ledger balance is NOT preserved —
    the new wallet starts at $0. Idempotent: a wallet that already has a usable
    key is left untouched.
    """
    import json
    from eth_account import Account
    from models import CryptoWallet, UserAuditLog
    from services import crypto_wallet
    from services import blockchain_monitor as bm

    user = db.query(User).filter(func.lower(User.email) == email.lower()).first()
    if not user:
        raise HTTPException(status_code=404, detail="No user with that email.")

    wallet = db.query(CryptoWallet).filter(CryptoWallet.user_id == user.id).first()

    def _key_usable(w) -> bool:
        if not w or not w.encrypted_private_key:
            return False
        try:
            pk = crypto_wallet._decrypt_private_key(
                w.encrypted_private_key, key_version=w.key_version, wrapped_dek=w.wrapped_dek
            )
            return Account.from_key(pk).address.lower() == (w.wallet_address or "").lower()
        except Exception:
            return False

    old_addr = user.crypto_wallet_address or (wallet.wallet_address if wallet else None)

    if _key_usable(wallet):
        return ReissueWalletResult(
            email=user.email, action="already_ok", old_wallet_address=old_addr,
            new_wallet_address=old_addr, detail="Wallet already has a usable signing key; nothing to do.",
        )

    # Never abandon on-chain funds. Confirm the old address is empty on every chain.
    if old_addr:
        for chain in bm.CHAINS:
            try:
                onchain = await bm._get_combined_balance(chain, old_addr)
            except Exception:
                onchain = None
            if onchain is None:
                return ReissueWalletResult(
                    email=user.email, action="flagged_has_funds", old_wallet_address=old_addr,
                    detail=f"On-chain balance on {chain} could not be confirmed — refusing to reissue. Retry, or investigate manually.",
                )
            if onchain > 0:
                return ReissueWalletResult(
                    email=user.email, action="flagged_has_funds", old_wallet_address=old_addr,
                    detail=f"Old wallet holds {onchain} cents of USDC on {chain}. Refusing to reissue so funds aren't abandoned — manual reconciliation required.",
                )

    if not confirm:
        return ReissueWalletResult(
            email=user.email, action="dry_run", old_wallet_address=old_addr,
            detail="Stranded wallet with no usable key and no on-chain funds. Re-run with confirm=true to reissue a fresh custodial wallet (ledger balance will reset to $0).",
        )

    # Safe to reissue: drop the stranded row + linkage, then create a fresh
    # custodial wallet (encrypted, round-trip-verified) with prod's real KEK.
    if wallet is not None:
        db.delete(wallet)
    user.crypto_wallet_address = None
    user.wallet_initialized = False
    user.wallet_type = None
    user.usdc_balance_cents = 0  # per decision: old ledger balance is not carried over
    db.flush()

    result = await crypto_wallet.create_wallet(user.id, db, wallet_type="fawn_custodial")

    db.add(UserAuditLog(
        user_id=user.id,
        action="stranded_wallet_reissued",
        details=json.dumps({"old_wallet_address": old_addr, "new_wallet_address": result["wallet_address"]}),
        retention_expires_at=datetime.now(tz=timezone.utc) + timedelta(days=365 * 7),
    ))
    db.commit()

    return ReissueWalletResult(
        email=user.email, action="reissued", old_wallet_address=old_addr,
        new_wallet_address=result["wallet_address"],
        detail="Reissued a fresh custodial wallet with an encrypted signing key. Ledger balance reset to $0.",
    )
