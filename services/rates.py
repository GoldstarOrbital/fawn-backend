"""
Live crypto rates / markets hub for FAWN.

Fetches spot USD prices for the major tokens FAWN cares about (BTC, ETH,
USDC, MATIC, USDT, SOL) from the free CoinGecko simple-price API, with an
in-process ~60s cache so the UI can poll cheaply without hammering the API
(mirrors the cache pattern in services/claude.py's digest cache).

READ-ONLY / TRACKING-ONLY. This module never moves money: it never touches
User.usdc_balance_cents, never creates a CryptoTransfer, and never calls any
send/settlement/on-chain code. It only reads public market data and,
optionally, writes an append-only tracking breadcrumb of conversions a user
has viewed to the existing UserAuditLog table (exactly like
routers/automation.py) so a "recent conversions" list is reconstructable.
"""
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

from models import UserAuditLog

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"

# symbol -> CoinGecko id. Single source of truth: the request URL and the
# response mapping are both derived from this dict, so adding a coin is one
# line and the two can never drift apart.
SYMBOL_TO_ID = {
    "btc": "bitcoin",
    "eth": "ethereum",
    "usdc": "usd-coin",
    "matic": "matic-network",
    "usdt": "tether",
    "sol": "solana",
}
ID_TO_SYMBOL = {cg_id: sym for sym, cg_id in SYMBOL_TO_ID.items()}

# Action used for the tracking-only conversion breadcrumb in UserAuditLog.
CONVERSION_ACTION = "rates_conversion"

# ── in-process price cache (mirrors services/claude.py) ──
# {"data": <snapshot dict>, "expires_at": <epoch seconds>}
_PRICE_CACHE: dict = {}
_CACHE_TTL_SECONDS = 60
_HTTP_TIMEOUT = 8
_UA = "Mozilla/5.0 (compatible; FAWN-rates/1.0; +https://fawn.app)"


def _fetch_prices_from_coingecko() -> dict:
    """Do the actual HTTP call to CoinGecko and return {symbol: usd_price}.

    Isolated into its own function so tests can monkeypatch exactly this and
    guarantee no real network call ever happens. Raises on any HTTP/parse
    failure; callers (get_crypto_prices) decide how to degrade.
    """
    ids = ",".join(SYMBOL_TO_ID.values())
    resp = httpx.get(
        COINGECKO_URL,
        params={"ids": ids, "vs_currencies": "usd"},
        headers={"User-Agent": _UA},
        timeout=_HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()

    prices: dict = {}
    for cg_id, sym in ID_TO_SYMBOL.items():
        entry = data.get(cg_id)
        if isinstance(entry, dict) and entry.get("usd") is not None:
            prices[sym] = float(entry["usd"])

    if not prices:
        raise ValueError("CoinGecko returned no usable prices")
    return prices


def get_crypto_prices(force_refresh: bool = False) -> dict:
    """Return cached (<=~60s) spot USD prices for FAWN's tracked tokens.

    Returns a snapshot dict:
      {
        "available": bool,        # False only when there is no data at all
        "prices": {symbol: usd},  # empty dict when unavailable
        "as_of": iso8601 | None,  # when the prices were actually fetched
        "source": "coingecko",
        "cached": bool,           # served from cache without a network hit
        "stale": bool,            # served old cache after a failed refresh
      }

    Degrades gracefully: if CoinGecko is unreachable we serve the last good
    cache (flagged stale=True); if there is no cache at all we return
    available=False rather than raising, so callers never crash on an outage.
    """
    now = time.time()
    cached = _PRICE_CACHE.get("data")

    if not force_refresh and cached and _PRICE_CACHE.get("expires_at", 0) > now:
        return {**cached, "cached": True, "stale": False}

    try:
        prices = _fetch_prices_from_coingecko()
    except Exception:
        # Network/parse failure. Serve the last good cache if we have one,
        # otherwise report unavailable without raising.
        if cached:
            return {**cached, "cached": True, "stale": True}
        return {
            "available": False,
            "prices": {},
            "as_of": None,
            "source": "coingecko",
            "cached": False,
            "stale": False,
        }

    fresh = {
        "available": True,
        "prices": prices,
        "as_of": datetime.now(tz=timezone.utc).isoformat(),
        "source": "coingecko",
    }
    _PRICE_CACHE["data"] = fresh
    _PRICE_CACHE["expires_at"] = now + _CACHE_TTL_SECONDS
    return {**fresh, "cached": False, "stale": False}


def _price_map_with_usd(prices: dict) -> dict:
    """Return prices plus USD as a first-class symbol (numeraire = 1.0),
    enabling BTC<->USD style conversions in either direction."""
    m = dict(prices)
    m["usd"] = 1.0
    return m


def convert(amount: float, from_sym: str, to_sym: str) -> dict:
    """Convert `amount` of `from_sym` into `to_sym` using cached spot prices.

    'usd' is accepted on either side. Raises ValueError for a negative amount
    or an unknown symbol (the router maps these to HTTP 400). Returns a dict
    with available=False when live prices are unavailable (router -> 503),
    otherwise the converted amount plus the exchange rate and USD value.
    """
    if amount is None or amount < 0:
        raise ValueError("amount must be a non-negative number")

    from_s = (from_sym or "").strip().lower()
    to_s = (to_sym or "").strip().lower()

    snapshot = get_crypto_prices()
    if not snapshot["available"]:
        return {
            "available": False,
            "reason": "live prices unavailable",
            "from": from_s,
            "to": to_s,
            "amount": amount,
        }

    pm = _price_map_with_usd(snapshot["prices"])
    supported = sorted(pm.keys())
    if from_s not in pm:
        raise ValueError(f"unsupported symbol '{from_s}'. Supported: {supported}")
    if to_s not in pm:
        raise ValueError(f"unsupported symbol '{to_s}'. Supported: {supported}")

    from_usd = pm[from_s]
    to_usd = pm[to_s]
    usd_value = amount * from_usd
    converted = usd_value / to_usd if to_usd else 0.0
    rate = from_usd / to_usd if to_usd else 0.0

    return {
        "available": True,
        "from": from_s,
        "to": to_s,
        "amount": amount,
        "converted_amount": converted,
        "rate": rate,
        "usd_value": usd_value,
        "as_of": snapshot["as_of"],
        "cached": snapshot.get("cached", False),
        "stale": snapshot.get("stale", False),
        "source": snapshot.get("source", "coingecko"),
    }


def log_conversion(db: Session, user_id: str, result: dict) -> None:
    """Tracking-only: record a completed conversion view in UserAuditLog.

    NEVER moves money and NEVER modifies balances -- it only appends a JSON
    breadcrumb (same persistence pattern as routers/automation.py) so a
    user's recent conversions can be reconstructed. No-op for an unavailable
    result.
    """
    if not result.get("available"):
        return
    db.add(UserAuditLog(
        user_id=user_id,
        action=CONVERSION_ACTION,
        details=json.dumps({
            "from": result.get("from"),
            "to": result.get("to"),
            "amount": result.get("amount"),
            "converted_amount": result.get("converted_amount"),
            "rate": result.get("rate"),
            "as_of": result.get("as_of"),
        }),
        retention_expires_at=datetime.now(tz=timezone.utc) + timedelta(days=365 * 7),
    ))
    db.commit()


def get_conversion_history(db: Session, user_id: str, limit: int = 20) -> dict:
    """Read back a user's most-recent tracked conversions from UserAuditLog.

    Pure read: filters the append-only audit log by action and returns the
    decoded JSON breadcrumbs, newest first.
    """
    logs = (
        db.query(UserAuditLog)
        .filter(
            UserAuditLog.user_id == user_id,
            UserAuditLog.action == CONVERSION_ACTION,
        )
        .order_by(UserAuditLog.created_at.desc())
        .limit(limit)
        .all()
    )
    items = []
    for log in logs:
        try:
            items.append(json.loads(log.details))
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    return {"conversions": items, "count": len(items)}
