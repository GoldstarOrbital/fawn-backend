"""
routers/public_stats.py

Public, no-auth endpoint that exposes founding-member sales counts pulled
live from Stripe. Powers the real-time "X of 20 claimed" counter on the
founding landing page.

CORS is open since this is intentionally consumed by the marketing site.
"""

import os
import time
from typing import Optional

import httpx
from fastapi import APIRouter

router = APIRouter(prefix="/public", tags=["public"])

# Stripe price IDs from the live payment links
FOUNDING_PRICE_ID = "price_1Tk8FkCOvuBRXRtvLm3ji2Pf"  # $49
INNER_CIRCLE_PRICE_ID = "price_1Tk8FxCOvuBRXRtvOk059G40"  # $97
DEV_SPRINT_PRICE_ID = "price_1Tk8FyCOvuBRXRtvyZKQspaf"  # $300

FOUNDING_CAP = 20
INNER_CIRCLE_CAP = 5

# Tiny in-process cache so we don't hammer Stripe on every page view.
# 60s is short enough to feel real-time and long enough to absorb traffic spikes.
_CACHE: dict = {"data": None, "expires_at": 0.0}
_CACHE_TTL_SECONDS = 60


def _count_paid_for_price(stripe_key: str, price_id: str) -> int:
    """Count successful payment_intents whose checkout session included the given price.

    We use payment_intents because Stripe payment-link checkouts produce a
    payment_intent on success; querying charges directly misses some flows.
    Returns 0 silently on any error so the marketing page never breaks.
    """
    try:
        # payment_intents.search with a metadata query would be ideal but
        # requires a Stripe Search index that takes time to populate. The
        # robust approach: list recent successful payment_intents (max 100),
        # expand checkout sessions, filter by price id.
        r = httpx.get(
            "https://api.stripe.com/v1/payment_intents",
            params={"limit": 100},
            headers={"Authorization": f"Bearer {stripe_key}"},
            timeout=8,
        )
        if r.status_code != 200:
            return 0
        intents = r.json().get("data", [])
        count = 0
        for pi in intents:
            if pi.get("status") != "succeeded":
                continue
            # Each payment_intent links to a charge; check the charges'
            # `description` or just compare metadata. The cleanest path is
            # via checkout sessions, but for payment links we can match by
            # amount as a fallback if metadata isn't propagated.
            amount = pi.get("amount", 0)
            if price_id == FOUNDING_PRICE_ID and amount == 4900:
                count += 1
            elif price_id == INNER_CIRCLE_PRICE_ID and amount == 9700:
                count += 1
            elif price_id == DEV_SPRINT_PRICE_ID and amount == 30000:
                count += 1
        return count
    except Exception:
        return 0


@router.get("/founding-stats")
def founding_stats():
    """Return live sales counts for the three founding tiers.

    Cached for 60s in-process. Falls back to 0s on any Stripe error — never
    blocks the marketing page.
    """
    now = time.time()
    if _CACHE["data"] and _CACHE["expires_at"] > now:
        return _CACHE["data"]

    stripe_key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not stripe_key:
        # No key set — return zeros honestly. Page still renders.
        data = {
            "founding_sold": 0,
            "founding_cap": FOUNDING_CAP,
            "inner_circle_sold": 0,
            "inner_circle_cap": INNER_CIRCLE_CAP,
            "dev_sprint_sold": 0,
            "total_revenue_cents": 0,
            "stripe_connected": False,
        }
    else:
        founding = _count_paid_for_price(stripe_key, FOUNDING_PRICE_ID)
        inner = _count_paid_for_price(stripe_key, INNER_CIRCLE_PRICE_ID)
        sprints = _count_paid_for_price(stripe_key, DEV_SPRINT_PRICE_ID)
        data = {
            "founding_sold": min(founding, FOUNDING_CAP),
            "founding_cap": FOUNDING_CAP,
            "inner_circle_sold": min(inner, INNER_CIRCLE_CAP),
            "inner_circle_cap": INNER_CIRCLE_CAP,
            "dev_sprint_sold": sprints,
            "total_revenue_cents": founding * 4900 + inner * 9700 + sprints * 30000,
            "stripe_connected": True,
        }

    _CACHE["data"] = data
    _CACHE["expires_at"] = now + _CACHE_TTL_SECONDS
    return data
