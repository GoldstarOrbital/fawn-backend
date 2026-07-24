"""Signed, best-effort delivery for user-configured FAWN webhooks.

Webhook failures are recorded but never roll back the financial or alert event
that caused them. Subscriptions live in the immutable audit trail; the signing
secret is returned once at creation and omitted from all read responses.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import socket
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from models import UserAuditLog


def public_subscription(config: dict) -> dict:
    """Return a subscription without its one-time signing secret."""
    return {key: value for key, value in config.items() if key != "signing_secret"}


def _is_public_target(url: str) -> bool:
    """Resolve the target at delivery time to mitigate DNS-rebinding SSRF."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)}
        return bool(addresses) and all(not (ipaddress.ip_address(address).is_private or ipaddress.ip_address(address).is_loopback or ipaddress.ip_address(address).is_link_local or ipaddress.ip_address(address).is_reserved) for address in addresses)
    except (OSError, ValueError):
        return False


async def dispatch_user_event(db: Session, user_id: str, event: str, payload: dict) -> int:
    """Deliver an event to matching active subscriptions and return successes."""
    rows = db.query(UserAuditLog).filter(
        UserAuditLog.user_id == user_id,
        UserAuditLog.action == "webhook_subscribed",
    ).all()
    timestamp = datetime.now(timezone.utc).isoformat()
    delivered = 0
    for row in rows:
        try:
            config = json.loads(row.details or "{}")
            if not config.get("active") or event not in config.get("events", []):
                continue
            if not _is_public_target(config.get("url", "")):
                config["delivery_failure_count"] = int(config.get("delivery_failure_count", 0)) + 1
                config["last_error"] = "Webhook target is not a public HTTPS address"
                row.details = json.dumps(config)
                continue
            body = json.dumps({"event": event, "created_at": timestamp, "data": payload}, separators=(",", ":"))
            secret = str(config.get("signing_secret", ""))
            signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
            try:
                async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
                    response = await client.post(
                        config["url"], content=body,
                        headers={
                            "Content-Type": "application/json",
                            "User-Agent": "FAWN-Webhooks/1.0",
                            "X-FAWN-Event": event,
                            "X-FAWN-Signature": f"sha256={signature}",
                        },
                    )
                if 200 <= response.status_code < 300:
                    config["delivery_success_count"] = int(config.get("delivery_success_count", 0)) + 1
                    config["last_triggered"] = timestamp
                    delivered += 1
                else:
                    config["delivery_failure_count"] = int(config.get("delivery_failure_count", 0)) + 1
                    config["last_error"] = f"HTTP {response.status_code}"
            except Exception as exc:
                config["delivery_failure_count"] = int(config.get("delivery_failure_count", 0)) + 1
                config["last_error"] = str(exc)[:160]
            row.details = json.dumps(config)
        except Exception:
            continue
    db.commit()
    return delivered
