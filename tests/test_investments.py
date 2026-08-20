"""Tests for /investments — DSPP/ETF buying and portfolio management."""
import uuid
from datetime import datetime, timedelta

from jose import jwt

from database import SessionLocal
from models import User, InvestmentHolding, InvestmentOrder
from config import settings


def _make_user(email):
    db = SessionLocal()
    try:
        user = User(email=email.lower(), hashed_password="x", full_name="Investor",
                    is_student=True)
        db.add(user)
        db.commit()
        db.refresh(user)
        return user.id
    finally:
        db.close()


def _auth_for(user_id):
    expire = datetime.utcnow() + timedelta(minutes=settings.jwt_expire_minutes)
    token = jwt.encode({"sub": user_id, "exp": expire}, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return {"Authorization": f"Bearer {token}"}


def test_list_securities(client):
    """Get full list of available securities."""
    resp = client.get("/investments/securities")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] > 50  # Should have stocks + ETFs
    assert any(s["asset_type"] == "stock" for s in body["securities"])
    assert any(s["asset_type"] == "etf" for s in body["securities"])


def test_filter_securities_by_type(client):
    """Filter securities by asset type."""
    resp = client.get("/investments/securities?asset_type=etf")
    assert resp.status_code == 200
    body = resp.json()
    assert all(s["asset_type"] == "etf" for s in body["securities"])


def test_search_securities(client):
    """Search for a specific security."""
    resp = client.post("/investments/securities/search", json={"query": "AAPL"})
    assert resp.status_code == 200
    body = resp.json()
    assert any(s["ticker"] == "AAPL" for s in body["results"])


def test_get_security_detail(client):
    """Get detail on one security including live price."""
    resp = client.get("/investments/securities/VTI")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ticker"] == "VTI"
    assert body["asset_type"] == "etf"
    assert "current_price_cents" in body


def test_security_not_found(client):
    """404 on unknown ticker."""
    resp = client.get("/investments/securities/FAKE_TICKER_XYZ")
    assert resp.status_code == 404


def test_get_portfolio_empty(client):
    """Empty portfolio for new user."""
    user_id = _make_user(f"inv_{uuid.uuid4().hex[:8]}@example.com")
    resp = client.get("/investments/portfolio", headers=_auth_for(user_id))
    assert resp.status_code == 200
    body = resp.json()
    assert body["holdings"] == []
    assert body["summary"]["holding_count"] == 0


def test_get_portfolio_with_holdings(client, monkeypatch):
    """Portfolio shows holdings with gain/loss calculation."""
    user_id = _make_user(f"inv_{uuid.uuid4().hex[:8]}@example.com")
    db = SessionLocal()
    try:
        # Add a mock holding
        db.add(InvestmentHolding(
            user_id=user_id,
            ticker="AAPL",
            asset_type="stock",
            quantity=10,
            cost_basis_cents=150_000,  # $1500
            avg_cost_per_share_cents=15_000,  # $150/share
            current_price_cents=17_000,  # $170/share now
            provider="computershare",
        ))
        db.commit()
    finally:
        db.close()

    # Pin the "live" price so the gain assertion below is deterministic —
    # get_portfolio() refreshes current_price_cents from the price service.
    async def mock_price(ticker):
        return 17_000

    monkeypatch.setattr("routers.investments.dspp.get_price_cached", mock_price)

    resp = client.get("/investments/portfolio", headers=_auth_for(user_id))
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["holdings"]) == 1
    h = body["holdings"][0]
    assert h["ticker"] == "AAPL"
    assert h["quantity"] == 10
    # Gain: 10 * 17000 - 150000 = 20000 cents = $200
    assert h["unrealized_gain_cents"] == 20_000


def test_place_buy_order(client, monkeypatch):
    """Place a buy order."""
    user_id = _make_user(f"inv_{uuid.uuid4().hex[:8]}@example.com")

    # Mock the DSPP provider and the live price lookup used to compute
    # actual share quantity from the dollar amount.
    async def mock_buy(user_id, ticker, amount_cents, provider):
        return {"status": "pending_manual_execution", "order_id": "order_123", "error": None, "note": "queued"}

    async def mock_price(ticker):
        return 10_000  # $100/share

    monkeypatch.setattr("routers.investments.dspp.place_buy_order", mock_buy)
    monkeypatch.setattr("routers.investments.dspp.get_price_cached", mock_price)

    resp = client.post(
        "/investments/orders/buy",
        json={"ticker": "AAPL", "amount_dollars": 500.00},
        headers=_auth_for(user_id),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ticker"] == "AAPL"
    assert body["amount_dollars"] == 500.0
    assert "pending" in body["status"].lower()
    # $500 at $100/share = 5 shares
    assert body["quantity"] == 5.0

    # Holding should be created with the correct fractional quantity and
    # average cost per share.
    db = SessionLocal()
    try:
        holding = db.query(InvestmentHolding).filter(
            InvestmentHolding.user_id == user_id, InvestmentHolding.ticker == "AAPL"
        ).first()
        assert holding is not None
        assert float(holding.quantity) == 5.0
        assert holding.avg_cost_per_share_cents == 10_000
        assert holding.cost_basis_cents == 50_000
    finally:
        db.close()


def test_place_buy_order_price_unavailable(client, monkeypatch):
    """Buy order fails cleanly (503) when no live price can be fetched."""
    user_id = _make_user(f"inv_{uuid.uuid4().hex[:8]}@example.com")

    async def mock_price(ticker):
        return 0

    monkeypatch.setattr("routers.investments.dspp.get_price_cached", mock_price)

    resp = client.post(
        "/investments/orders/buy",
        json={"ticker": "AAPL", "amount_dollars": 100.00},
        headers=_auth_for(user_id),
    )
    assert resp.status_code == 503


def test_place_sell_order(client, monkeypatch):
    """Sell part of a holding; portfolio quantity and cost basis update proportionally."""
    user_id = _make_user(f"inv_{uuid.uuid4().hex[:8]}@example.com")
    db = SessionLocal()
    try:
        db.add(InvestmentHolding(
            user_id=user_id,
            ticker="AAPL",
            asset_type="stock",
            quantity=10,
            cost_basis_cents=100_000,  # $1000 for 10 shares -> $100/share
            avg_cost_per_share_cents=10_000,
            current_price_cents=10_000,
            provider="computershare",
        ))
        db.commit()
    finally:
        db.close()

    async def mock_sell(user_id, ticker, quantity, provider):
        return {"status": "pending_manual_execution", "order_id": "sell_123", "error": None, "note": "queued"}

    async def mock_price(ticker):
        return 12_000  # $120/share now

    monkeypatch.setattr("routers.investments.dspp.place_sell_order", mock_sell)
    monkeypatch.setattr("routers.investments.dspp.get_price_cached", mock_price)

    resp = client.post(
        "/investments/orders/sell",
        json={"ticker": "AAPL", "quantity": 4},
        headers=_auth_for(user_id),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["quantity"] == 4.0
    assert body["proceeds_cents"] == 48_000  # 4 * 12000

    db = SessionLocal()
    try:
        holding = db.query(InvestmentHolding).filter(
            InvestmentHolding.user_id == user_id, InvestmentHolding.ticker == "AAPL"
        ).first()
        assert holding is not None
        assert float(holding.quantity) == 6.0
        # 6/10 of the original cost basis remains
        assert holding.cost_basis_cents == 60_000
    finally:
        db.close()


def test_place_sell_order_full_position_deletes_holding(client, monkeypatch):
    """Selling the entire position removes the holding."""
    user_id = _make_user(f"inv_{uuid.uuid4().hex[:8]}@example.com")
    db = SessionLocal()
    try:
        db.add(InvestmentHolding(
            user_id=user_id,
            ticker="MSFT",
            asset_type="stock",
            quantity=2,
            cost_basis_cents=40_000,
            avg_cost_per_share_cents=20_000,
            current_price_cents=20_000,
            provider="computershare",
        ))
        db.commit()
    finally:
        db.close()

    async def mock_sell(user_id, ticker, quantity, provider):
        return {"status": "pending_manual_execution", "order_id": "sell_456", "error": None, "note": "queued"}

    async def mock_price(ticker):
        return 21_000

    monkeypatch.setattr("routers.investments.dspp.place_sell_order", mock_sell)
    monkeypatch.setattr("routers.investments.dspp.get_price_cached", mock_price)

    resp = client.post(
        "/investments/orders/sell",
        json={"ticker": "MSFT", "quantity": 2},
        headers=_auth_for(user_id),
    )
    assert resp.status_code == 200

    db = SessionLocal()
    try:
        holding = db.query(InvestmentHolding).filter(
            InvestmentHolding.user_id == user_id, InvestmentHolding.ticker == "MSFT"
        ).first()
        assert holding is None
    finally:
        db.close()


def test_place_buy_order_invalid_ticker(client):
    """Buy order with nonexistent ticker."""
    user_id = _make_user(f"inv_{uuid.uuid4().hex[:8]}@example.com")
    resp = client.post(
        "/investments/orders/buy",
        json={"ticker": "FAKE_XYZ", "amount_dollars": 100},
        headers=_auth_for(user_id),
    )
    assert resp.status_code == 404


def test_place_sell_order_insufficient_shares(client):
    """Sell more shares than you own."""
    user_id = _make_user(f"inv_{uuid.uuid4().hex[:8]}@example.com")
    resp = client.post(
        "/investments/orders/sell",
        json={"ticker": "AAPL", "quantity": 10},
        headers=_auth_for(user_id),
    )
    assert resp.status_code == 400  # No holdings


def test_order_history(client):
    """List user's orders."""
    user_id = _make_user(f"inv_{uuid.uuid4().hex[:8]}@example.com")
    resp = client.get("/investments/orders", headers=_auth_for(user_id))
    assert resp.status_code == 200
    body = resp.json()
    assert body["orders"] == []


def test_public_quote(client):
    """Public quote endpoint (no auth)."""
    resp = client.get("/investments/public/quote/VTI")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ticker"] == "VTI"
    assert "price_cents" in body


def test_investments_require_auth(client):
    """Portfolio and order endpoints require auth."""
    assert client.get("/investments/portfolio").status_code in (401, 403)
    assert client.post("/investments/orders/buy", json={}).status_code in (401, 403)
