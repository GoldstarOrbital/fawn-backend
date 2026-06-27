"""
routers/public_stats.py

Public, no-auth endpoint that exposes founding-member sales counts.
Powers the real-time "X of 20 claimed" counter on the founding landing page.

Reads directly from our own FoundingMember table (populated by the Stripe
webhook on checkout.session.completed) rather than calling Stripe's API.
This is both more reliable (no dependency on STRIPE_SECRET_KEY being set
on this service, no guessing tier from payment amount) and always
accurate — it's the same table the admin dashboard and member portal
already trust as the source of truth.

CORS is open since this is intentionally consumed by the marketing site.
"""
import time

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from models import FoundingMember

router = APIRouter(prefix="/public", tags=["public"])

FOUNDING_CAP = 20
INNER_CIRCLE_CAP = 5

# Tiny in-process cache so we don't re-query on every page view.
_CACHE: dict = {"data": None, "expires_at": 0.0}
_CACHE_TTL_SECONDS = 30


@router.get("/founding-stats")
def founding_stats(db: Session = Depends(get_db)):
    """Live sales counts for the three founding tiers, from our own DB.

    Cached in-process for 30s. Refunded members are excluded from counts
    and revenue so the page never overstates real, current sales.
    """
    now = time.time()
    if _CACHE["data"] and _CACHE["expires_at"] > now:
        return _CACHE["data"]

    rows = (
        db.query(FoundingMember.tier, func.count(FoundingMember.id), func.coalesce(func.sum(FoundingMember.amount_cents), 0))
        .filter(FoundingMember.refunded == False)  # noqa: E712
        .group_by(FoundingMember.tier)
        .all()
    )
    counts = {tier: (cnt, revenue) for tier, cnt, revenue in rows}

    founding_cnt, founding_rev = counts.get("founding", (0, 0))
    inner_cnt, inner_rev = counts.get("inner_circle", (0, 0))
    sprint_cnt, sprint_rev = counts.get("dev_sprint", (0, 0))

    data = {
        "founding_sold": min(founding_cnt, FOUNDING_CAP),
        "founding_cap": FOUNDING_CAP,
        "inner_circle_sold": min(inner_cnt, INNER_CIRCLE_CAP),
        "inner_circle_cap": INNER_CIRCLE_CAP,
        "dev_sprint_sold": sprint_cnt,
        "total_revenue_cents": founding_rev + inner_rev + sprint_rev,
    }

    _CACHE["data"] = data
    _CACHE["expires_at"] = now + _CACHE_TTL_SECONDS
    return data
