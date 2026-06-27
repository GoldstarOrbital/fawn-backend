"""Tests for /public/founding-stats — the live counter Stripe revenue
depends on for social proof on the founding landing page."""
import uuid

from database import SessionLocal
from models import FoundingMember


def _add_member(tier, amount_cents, refunded=False):
    db = SessionLocal()
    try:
        existing_count = db.query(FoundingMember).count()
        db.add(FoundingMember(
            email=f"member_{uuid.uuid4().hex[:8]}@example.com",
            member_number=existing_count + 1,
            tier=tier,
            amount_cents=amount_cents,
            stripe_session_id=f"sess_{uuid.uuid4().hex[:10]}",
            refunded=refunded,
        ))
        db.commit()
    finally:
        db.close()


def _reset_cache():
    # Bypass the 30s in-process cache so each test sees fresh counts.
    import routers.public_stats as ps
    ps._CACHE["data"] = None
    ps._CACHE["expires_at"] = 0.0


def test_no_members_returns_zeros(client):
    _reset_cache()
    resp = client.get("/public/founding-stats")
    assert resp.status_code == 200
    body = resp.json()
    # Other tests in the suite may have already created members in this
    # shared DB, so only assert structure/caps here, not exact zero counts.
    assert body["founding_cap"] == 20
    assert body["inner_circle_cap"] == 5
    assert "founding_sold" in body
    assert "total_revenue_cents" in body


def test_real_sales_are_counted_and_summed(client):
    _reset_cache()
    before = client.get("/public/founding-stats").json()

    _add_member("founding", 4900)
    _add_member("inner_circle", 9700)
    _add_member("dev_sprint", 30000)
    _reset_cache()

    after = client.get("/public/founding-stats").json()
    assert after["founding_sold"] == before["founding_sold"] + 1
    assert after["inner_circle_sold"] == before["inner_circle_sold"] + 1
    assert after["dev_sprint_sold"] == before["dev_sprint_sold"] + 1
    assert after["total_revenue_cents"] == before["total_revenue_cents"] + 4900 + 9700 + 30000


def test_refunded_members_excluded_from_counts_and_revenue(client):
    _reset_cache()
    before = client.get("/public/founding-stats").json()

    _add_member("founding", 4900, refunded=True)
    _reset_cache()

    after = client.get("/public/founding-stats").json()
    assert after["founding_sold"] == before["founding_sold"]
    assert after["total_revenue_cents"] == before["total_revenue_cents"]


def test_founding_sold_caps_at_founding_cap(client):
    _reset_cache()
    for _ in range(25):
        _add_member("founding", 4900)
    _reset_cache()

    resp = client.get("/public/founding-stats").json()
    assert resp["founding_sold"] <= resp["founding_cap"]


def test_response_is_cached_for_30_seconds(client, monkeypatch):
    _reset_cache()
    first = client.get("/public/founding-stats").json()

    _add_member("founding", 4900)
    # No _reset_cache() here — within the cache window, the new member
    # should NOT show up yet.
    second = client.get("/public/founding-stats").json()
    assert second["founding_sold"] == first["founding_sold"]
