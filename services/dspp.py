"""DSPP (Direct Stock Purchase Plan) integration layer.

Abstraction over company and provider DSPPs so FAWN can route buys/sells
to the right provider (Computershare, company-direct, etc.) without
duplicating logic. Each provider implements the same interface.

Regulatory note: This service handles securities transactions and should
be reviewed by compliance counsel before production launch. All orders
are logged and carry explicit disclaimers.
"""
import asyncio
import time
from abc import ABC, abstractmethod
from typing import Optional

import httpx

from config import settings

# In-memory price cache: {ticker: (price_cents, fetched_at_epoch_seconds)}.
# Alpha Vantage's free tier caps at 25 requests/day total -- with 99
# securities in the catalog, fetching live on every call would exhaust the
# entire daily quota after ~25 page views. Caching the same ticker for a
# window means repeat requests (a user reloading their portfolio, multiple
# users quoting the same popular ticker) cost zero extra API calls.
# Process-local only (fine for Railway's single web instance); resets on
# deploy/restart.
_PRICE_CACHE: dict[str, tuple[int, float]] = {}
_PRICE_CACHE_TTL_SECONDS = 900  # 15 minutes


class DSPPProvider(ABC):
    """Base class for DSPP providers."""

    @abstractmethod
    async def authenticate(self) -> bool:
        """Verify credentials are valid."""
        pass

    @abstractmethod
    async def get_price(self, ticker: str) -> int:
        """Return current price in cents."""
        pass

    @abstractmethod
    async def buy(self, user_id: str, ticker: str, amount_cents: int) -> dict:
        """Place a buy order. Return {order_id, status, error}."""
        pass

    @abstractmethod
    async def sell(self, user_id: str, ticker: str, quantity: float) -> dict:
        """Place a sell order. Return {order_id, status, error}."""
        pass

    @abstractmethod
    async def get_holdings(self, user_id: str) -> list[dict]:
        """Return [{ticker, quantity, avg_cost_cents}]."""
        pass


class ComputershareProvider(DSPPProvider):
    """Computershare DSPP wrapper (handles ~70% of US company DSPPs).

    In production, this would authenticate via Computershare's investor
    portal (via OAuth or API key) to place orders on behalf of users.
    For MVP, orders are logged and marked "pending_manual_execution" so
    compliance can manually submit them through Computershare's portal.

    Real integration would require Computershare's B2B API partnership.
    """

    async def authenticate(self) -> bool:
        """Stub: always true in MVP."""
        return True

    async def get_price(self, ticker: str) -> int:
        """Fetch live price from a free stock API."""
        try:
            # Using Alpha Vantage (free tier, 5 calls/min)
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://www.alphavantage.co/query",
                    params={
                        "function": "GLOBAL_QUOTE",
                        "symbol": ticker,
                        "apikey": settings.alpha_vantage_key or "demo",
                    },
                )
                data = resp.json()
                price_str = data.get("Global Quote", {}).get("05. price")
                if price_str:
                    return int(float(price_str) * 100)
        except Exception as e:
            print(f"[dspp] price fetch failed for {ticker}: {e}")
        return 0

    async def buy(self, user_id: str, ticker: str, amount_cents: int) -> dict:
        """Log buy order for manual Computershare submission."""
        print(f"[dspp-computershare] BUY order: user={user_id} ticker={ticker} amount=${amount_cents/100:.2f}")
        return {
            "order_id": f"cs_{user_id}_{ticker}_{amount_cents}",
            "status": "pending_manual_execution",
            "error": None,
            "provider": "computershare",
            "note": "Order queued for manual submission via Computershare portal.",
        }

    async def sell(self, user_id: str, ticker: str, quantity: float) -> dict:
        """Log sell order for manual Computershare submission."""
        print(f"[dspp-computershare] SELL order: user={user_id} ticker={ticker} qty={quantity}")
        return {
            "order_id": f"cs_{user_id}_{ticker}_sell",
            "status": "pending_manual_execution",
            "error": None,
            "provider": "computershare",
            "note": "Order queued for manual submission via Computershare portal.",
        }

    async def get_holdings(self, user_id: str) -> list[dict]:
        """Stub: no holdings yet (MVP does not sync Computershare accounts)."""
        return []


class ETFDirectProvider(DSPPProvider):
    """Direct ETF purchase via provider APIs (Vanguard, iShares, Fidelity ETFs).

    For MVP: this is also a stub that logs orders. Real implementation would
    integrate with the ETF issuer's API or a broker partner (e.g., Alpaca)
    that handles ETF purchases.
    """

    async def authenticate(self) -> bool:
        return True

    async def get_price(self, ticker: str) -> int:
        """Fetch live ETF price."""
        # Same as Computershare
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://www.alphavantage.co/query",
                    params={
                        "function": "GLOBAL_QUOTE",
                        "symbol": ticker,
                        "apikey": settings.alpha_vantage_key or "demo",
                    },
                )
                data = resp.json()
                price_str = data.get("Global Quote", {}).get("05. price")
                if price_str:
                    return int(float(price_str) * 100)
        except Exception as e:
            print(f"[dspp-etf] price fetch failed for {ticker}: {e}")
        return 0

    async def buy(self, user_id: str, ticker: str, amount_cents: int) -> dict:
        """Log ETF buy order."""
        print(f"[dspp-etf] BUY order: user={user_id} ticker={ticker} amount=${amount_cents/100:.2f}")
        return {
            "order_id": f"etf_{user_id}_{ticker}_{amount_cents}",
            "status": "pending_manual_execution",
            "error": None,
            "provider": "etf_direct",
            "note": "Order queued for manual submission via ETF provider.",
        }

    async def sell(self, user_id: str, ticker: str, quantity: float) -> dict:
        """Log ETF sell order."""
        print(f"[dspp-etf] SELL order: user={user_id} ticker={ticker} qty={quantity}")
        return {
            "order_id": f"etf_{user_id}_{ticker}_sell",
            "status": "pending_manual_execution",
            "error": None,
            "provider": "etf_direct",
            "note": "Order queued for manual submission via ETF provider.",
        }

    async def get_holdings(self, user_id: str) -> list[dict]:
        return []


# Provider registry
PROVIDERS = {
    "computershare": ComputershareProvider(),
    "etf_direct": ETFDirectProvider(),
}


async def get_provider(provider_name: str) -> Optional[DSPPProvider]:
    """Return the provider instance."""
    return PROVIDERS.get(provider_name)


async def get_price_cached(ticker: str) -> int:
    """Fetch price from the default provider (Computershare handles both stocks & ETFs).

    Cached for _PRICE_CACHE_TTL_SECONDS to conserve Alpha Vantage's tiny
    free-tier daily quota. If a live fetch fails (rate-limited, network
    error) but a stale cached price exists, that stale price is returned
    rather than 0 -- a slightly-old price is far more useful to a buyer
    than an outright failure.
    """
    now = time.time()
    cached = _PRICE_CACHE.get(ticker)
    if cached and (now - cached[1]) < _PRICE_CACHE_TTL_SECONDS:
        return cached[0]

    provider = PROVIDERS["computershare"]
    price_cents = await provider.get_price(ticker)

    if price_cents > 0:
        _PRICE_CACHE[ticker] = (price_cents, now)
        return price_cents

    # Live fetch failed/rate-limited -- serve stale cache if we have one.
    if cached:
        print(f"[dspp] live price fetch failed for {ticker}; serving stale cached price from {int(now - cached[1])}s ago")
        return cached[0]

    return 0


# === Public API ===

async def place_buy_order(user_id: str, ticker: str, amount_cents: int, provider: str = "computershare") -> dict:
    """Place a buy order through the specified provider."""
    p = PROVIDERS.get(provider)
    if not p:
        return {"error": f"Unknown provider: {provider}", "status": "failed"}
    return await p.buy(user_id, ticker, amount_cents)


async def place_sell_order(user_id: str, ticker: str, quantity: float, provider: str = "computershare") -> dict:
    """Place a sell order through the specified provider."""
    p = PROVIDERS.get(provider)
    if not p:
        return {"error": f"Unknown provider: {provider}", "status": "failed"}
    return await p.sell(user_id, ticker, quantity)
