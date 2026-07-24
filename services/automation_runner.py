"""Periodic evaluation for non-custodial-action automation rules.

Only price-alert rules run unattended. Transfer and DCA rules stay review-only
until a dedicated, idempotent execution service and provider are configured.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

from models import UserAuditLog
from services.webhook_delivery import dispatch_user_event

COINGECKO_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "USDC": "usd-coin",
    "USDT": "tether", "MATIC": "matic-network", "LINK": "chainlink",
}


async def _prices(tokens: set[str]) -> dict[str, float]:
    ids = {token: COINGECKO_IDS.get(token) for token in tokens}
    requested = sorted({value for value in ids.values() if value})
    if not requested:
        return {}
    async with httpx.AsyncClient(timeout=8.0) as client:
        response = await client.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": ",".join(requested), "vs_currencies": "usd"},
        )
        response.raise_for_status()
        payload = response.json()
    return {token: float(payload[coin_id]["usd"]) for token, coin_id in ids.items() if coin_id and payload.get(coin_id, {}).get("usd") is not None}


async def run_price_alert_checks(db: Session) -> dict:
    """Check alerts once, trigger only on a false-to-true threshold crossing."""
    rows = db.query(UserAuditLog).filter(UserAuditLog.action == "price_alert_created").all()
    parsed: list[tuple[UserAuditLog, dict]] = []
    for row in rows:
        try:
            config = json.loads(row.details or "{}")
            if config.get("active"):
                parsed.append((row, config))
        except (TypeError, ValueError):
            continue
    try:
        prices = await _prices({str(config.get("token", "")).upper() for _, config in parsed})
    except Exception:
        return {"checked": 0, "triggered": 0, "price_source": "unavailable"}

    now = datetime.now(timezone.utc)
    triggered = 0
    for row, config in parsed:
        token = str(config.get("token", "")).upper()
        price = prices.get(token)
        if price is None:
            continue
        threshold = float(config.get("threshold_usd", 0))
        is_met = price >= threshold if config.get("direction") == "above" else price <= threshold
        was_met = bool(config.get("condition_met", False))
        config.update({"last_price_usd": price, "last_checked_at": now.isoformat(), "condition_met": is_met})
        if is_met and not was_met:
            event = {
                "token": token,
                "direction": config.get("direction"),
                "threshold_usd": threshold,
                "price_usd": price,
                "alert_created_at": config.get("created_at"),
            }
            config["last_triggered_at"] = now.isoformat()
            db.add(UserAuditLog(
                user_id=row.user_id,
                action="price_alert_triggered",
                details=json.dumps(event),
                retention_expires_at=now + timedelta(days=365 * 7),
            ))
            await dispatch_user_event(db, row.user_id, "price_alert.triggered", event)
            triggered += 1
        row.details = json.dumps(config)
    db.commit()
    return {"checked": len(parsed), "triggered": triggered, "price_source": "coingecko"}
