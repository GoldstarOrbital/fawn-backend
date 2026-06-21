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
from models import FoundingMember, WaitlistEntry

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
