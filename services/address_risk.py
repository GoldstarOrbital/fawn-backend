"""
Malicious-address risk scoring for send recipients, via GoPlus Security's
free Address Security API (https://api.gopluslabs.io) -- the same
threat-intel data used by MetaMask, Trust Wallet, Uniswap, and SafePal.
No API key required.

Distinct from services/sanctions_screening.py: OFAC screening is a legal
requirement and blocks a send outright. This is a fraud SIGNAL, not a
legal mandate -- a false positive here shouldn't hard-block a legitimate
user, so a flagged recipient routes into the existing review-hold queue
(services/crypto_wallet.py::send_usdc) rather than being rejected. A
false negative (API down, address not yet flagged) should never block a
legitimate send either -- this fails open, same as sanctions screening,
and for the same reason: a third party's uptime should never be able to
freeze FAWN's own send path.
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from models import UserAuditLog

GOPLUS_API_URL = "https://api.gopluslabs.io/api/v1/address_security/{address}"

# GoPlus returns ~20 boolean-ish ("0"/"1") risk categories. These are the
# ones that indicate the address itself is a known bad actor (scam,
# theft, laundering, etc.) as opposed to categories that don't apply to
# a simple recipient-address check (e.g. contract-specific fields like
# fake_standard_interface, honeypot_related_address still included since
# a "honeypot-related" EOA is still worth a second look).
RISK_FLAGS = (
    "cybercrime",
    "money_laundering",
    "financial_crime",
    "darkweb_transactions",
    "phishing_activities",
    "fake_kyc",
    "blacklist_doubt",
    "stealing_attack",
    "blackmail_activities",
    "sanctioned",
    "malicious_mining_activities",
    "mixer",
    "fake_token",
    "honeypot_related_address",
)


async def check_address_risk(address: str) -> Optional[dict]:
    """Returns {"flagged": bool, "reasons": [...], "data_source": str} or
    None if the lookup itself failed (network error, timeout, malformed
    response) -- callers must treat None as "unknown," not "clean."""
    url = GOPLUS_API_URL.format(address=address.lower())
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, params={"chain_id": 137})  # Polygon; flags are address-behavior, not chain-specific
            resp.raise_for_status()
            body = resp.json()
    except Exception:
        return None

    result = body.get("result")
    if not isinstance(result, dict):
        return None

    reasons = [flag for flag in RISK_FLAGS if result.get(flag) == "1"]
    return {
        "flagged": len(reasons) > 0,
        "reasons": reasons,
        "data_source": result.get("data_source") or None,
    }


async def flag_if_risky_for_review(sender_id: str, recipient_address: str, db: Session) -> bool:
    """Returns True if recipient_address is flagged by GoPlus and should
    be routed to the review-hold queue. Fails open (returns False) on any
    lookup failure -- see module docstring. Logs a security-relevant
    audit event whenever a real flag is found, independent of whether
    the caller ends up holding or blocking the send."""
    risk = await check_address_risk(recipient_address)
    if risk is None or not risk["flagged"]:
        return False

    retention_expires = datetime.now(tz=timezone.utc) + timedelta(days=365 * 7)
    db.add(UserAuditLog(
        user_id=sender_id,
        action="recipient_flagged_by_address_risk_check",
        details=json.dumps({
            "recipient_address": recipient_address,
            "reasons": risk["reasons"],
            "data_source": risk["data_source"],
            "timestamp": datetime.utcnow().isoformat(),
        }),
        retention_expires_at=retention_expires,
    ))
    db.commit()
    return True
