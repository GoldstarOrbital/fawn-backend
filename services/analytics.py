"""
services/analytics.py

Thin wrapper around PostHog for server-side event capture.

Design goals:
- Single typed event taxonomy shared with frontend.
- Async, non-blocking, fails silent — never breaks a request.
- No-op when POSTHOG_API_KEY isn't configured (local dev / preview deploys).

Event names are documented in docs/events.md.
"""

import os
import threading
import urllib.request
import json
from typing import Optional

POSTHOG_API_KEY = os.environ.get("POSTHOG_API_KEY", "")
POSTHOG_HOST = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com")

# Canonical server-emitted event names. Frontend has its own set in analytics.js.
EVENTS = {
    "WAITLIST_JOINED": "waitlist_joined",
    "FOUNDING_CHECKOUT_STARTED": "founding_checkout_started",
    "FOUNDING_PAYMENT_SUCCEEDED": "founding_payment_succeeded",
    "FOUNDING_PAYMENT_REFUNDED": "founding_payment_refunded",
    "REFERRAL_LINK_SHARED": "referral_link_shared",
    "MAGIC_LINK_REQUESTED": "magic_link_requested",
    "MAGIC_LINK_CONSUMED": "magic_link_consumed",
    "DEAL_SUGGESTION_SUBMITTED": "deal_suggestion_submitted",
    # Crypto wallet events
    "WALLET_CREATED": "wallet_created",
    "TRANSFER_SENT": "transfer_sent",
    "FEES_COLLECTED": "fees_collected",
}


def _send(payload: dict) -> None:
    try:
        req = urllib.request.Request(
            f"{POSTHOG_HOST}/capture/",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass  # fail silent — analytics must never break the app


def capture(event: str, distinct_id: str, properties: Optional[dict] = None) -> None:
    """Capture a server-side event. Non-blocking — runs in a daemon thread."""
    if not POSTHOG_API_KEY:
        return
    payload = {
        "api_key": POSTHOG_API_KEY,
        "event": event,
        "distinct_id": distinct_id,
        "properties": {
            "$lib": "fawn-backend",
            "source": "server",
            **(properties or {}),
        },
    }
    threading.Thread(target=_send, args=(payload,), daemon=True).start()
