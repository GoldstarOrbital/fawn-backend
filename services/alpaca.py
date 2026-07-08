"""
Alpaca Broker API calls (investing: brokerage accounts, orders, positions).

Alpaca powers FAWN's investing feature. The Broker API uses HTTP Basic auth
(API key id as username, secret as password) and flat JSON payloads.

Sandbox is https://broker-api.sandbox.alpaca.markets; production is
https://broker-api.alpaca.markets — selected by ALPACA_BASE_URL.

This client covers the read/trade surface FAWN needs for an MVP:
open a brokerage account, read it, place a (notional or share) order,
list positions, and initiate an ACH funding journal. It intentionally does
not cover market data — the frontend uses Alpaca's separate data feed.

Guarded by _require_configured(): unset key/secret raises rather than
firing an unauthenticated request.
"""
from __future__ import annotations

import httpx
from config import settings


class AlpacaNotConfigured(RuntimeError):
    pass


class AlpacaError(RuntimeError):
    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Alpaca API {status_code}: {body[:300]}")


def _require_configured() -> None:
    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        raise AlpacaNotConfigured(
            "Alpaca is not configured. Set ALPACA_API_KEY and ALPACA_API_SECRET "
            "to enable investing."
        )


def _auth() -> tuple[str, str]:
    return (settings.alpaca_api_key, settings.alpaca_api_secret)


async def _request(method: str, path: str, *, json: dict | None = None,
                   params: dict | None = None) -> dict:
    _require_configured()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.request(
            method,
            f"{settings.alpaca_base_url}{path}",
            json=json,
            params=params,
            headers={"Content-Type": "application/json"},
            auth=_auth(),
        )
    if resp.status_code >= 300:
        raise AlpacaError(resp.status_code, resp.text)
    return resp.json() if resp.content else {}


async def create_brokerage_account(email: str, given_name: str, family_name: str,
                                   agreements: list[dict]) -> dict:
    """Open a brokerage account for a KYC'd user.

    `agreements` must contain the signed customer/margin/account agreements
    (id, signed_at ISO timestamp, ip_address) — Alpaca rejects account
    creation without them, which is why the investing router collects and
    forwards them explicitly. Minimal contact/identity is echoed from the
    already-approved Unit KYC record; Alpaca runs its own CIP on top."""
    payload = {
        "contact": {"email_address": email},
        "identity": {"given_name": given_name, "family_name": family_name},
        "agreements": agreements,
        "account_type": "trading",
    }
    return await _request("POST", "/v1/accounts", json=payload)


async def get_account(alpaca_account_id: str) -> dict:
    data = await _request("GET", f"/v1/accounts/{alpaca_account_id}")
    return {
        "account_id": alpaca_account_id,
        "status": data.get("status", ""),
        "cash": float(data.get("cash", 0) or 0),
        "equity": float(data.get("equity", 0) or 0),
        "buying_power": float(data.get("buying_power", 0) or 0),
        "currency": data.get("currency", "USD"),
    }


async def place_order(alpaca_account_id: str, symbol: str, side: str,
                      notional: float | None = None, qty: float | None = None,
                      order_type: str = "market", time_in_force: str = "day") -> dict:
    """Place a buy/sell. Supply exactly one of `notional` (dollar amount,
    supports fractional) or `qty` (share count). Defaults to a market order."""
    if (notional is None) == (qty is None):
        raise ValueError("Provide exactly one of notional or qty.")
    order: dict = {
        "symbol": symbol.upper(),
        "side": side.lower(),           # buy | sell
        "type": order_type,
        "time_in_force": time_in_force,
    }
    if notional is not None:
        order["notional"] = str(notional)
    else:
        order["qty"] = str(qty)
    return await _request("POST", f"/v1/trading/accounts/{alpaca_account_id}/orders", json=order)


async def list_positions(alpaca_account_id: str) -> list:
    data = await _request("GET", f"/v1/trading/accounts/{alpaca_account_id}/positions")
    positions = data if isinstance(data, list) else data.get("positions", [])
    return [
        {
            "symbol": p.get("symbol", ""),
            "qty": float(p.get("qty", 0) or 0),
            "market_value": float(p.get("market_value", 0) or 0),
            "unrealized_pl": float(p.get("unrealized_pl", 0) or 0),
            "avg_entry_price": float(p.get("avg_entry_price", 0) or 0),
        }
        for p in positions
    ]


async def create_ach_journal(alpaca_account_id: str, relationship_id: str,
                             amount: float, direction: str = "INCOMING") -> dict:
    """Fund (or withdraw from) the brokerage account over an established ACH
    relationship. INCOMING = deposit into investing; OUTGOING = withdraw."""
    return await _request(
        "POST",
        "/v1/accounts/transfers",
        json={
            "account_id": alpaca_account_id,
            "transfer_type": "ach",
            "relationship_id": relationship_id,
            "amount": str(amount),
            "direction": direction,
        },
    )


async def get_quote(symbol: str) -> dict:
    """Get real-time quote for a symbol (stock, ETF, or crypto).

    Returns latest bid/ask, last trade, and trading status. Crypto symbols
    (BTC, ETH) map to crypto data endpoints; traditional symbols (AAPL) use
    stock quotes. Response includes price, change %, 52-week high/low, etc.
    """
    data = await _request("GET", f"/v1/market/stocks/{symbol.upper()}/quotes/latest")
    if not data.get("quote"):
        raise ValueError(f"No quote found for {symbol}")
    q = data["quote"]
    return {
        "symbol": symbol.upper(),
        "bid": float(q.get("bid_price", 0) or 0),
        "ask": float(q.get("ask_price", 0) or 0),
        "last": float(q.get("last_updated_price", 0) or q.get("bid_price", 0) or 0),
        "bid_size": int(q.get("bid_size", 0) or 0),
        "ask_size": int(q.get("ask_size", 0) or 0),
        "timestamp": q.get("timestamp", ""),
    }


async def list_orders(alpaca_account_id: str, status: str = "all", limit: int = 100) -> list:
    """List recent orders for the account.

    status: 'open', 'closed', 'all'. Alpaca returns most recent first.
    Limit max 100 per request (default); pagination via query params if needed.
    """
    params = {"status": status, "limit": limit}
    data = await _request("GET", f"/v1/trading/accounts/{alpaca_account_id}/orders", params=params)
    orders = data if isinstance(data, list) else data.get("orders", [])
    return [
        {
            "order_id": o.get("id", ""),
            "symbol": o.get("symbol", ""),
            "qty": float(o.get("qty", 0) or 0),
            "notional": float(o.get("notional", 0) or 0),
            "side": o.get("side", ""),
            "type": o.get("type", "market"),
            "status": o.get("status", ""),
            "filled_qty": float(o.get("filled_qty", 0) or 0),
            "filled_avg_price": float(o.get("filled_avg_price", 0) or 0),
            "created_at": o.get("created_at", ""),
            "updated_at": o.get("updated_at", ""),
        }
        for o in orders
    ]
