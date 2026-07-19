"""
OFAC sanctions screening for send recipients.

Compliance groundwork: US persons/entities are legally prohibited from
transacting with anyone on OFAC's Specially Designated Nationals (SDN)
list, including their published cryptocurrency addresses. This is a real
legal requirement, not an engineering nice-to-have -- FAWN moves real
money and needs to block sends to sanctioned addresses.

Source: OFAC's public SDN CSV export (no API key, no paid vendor --
https://sanctionslistservice.ofac.treas.gov/api/publicationpreview/exports/sdn.csv).
Digital currency addresses appear inline in each entity's free-text
"remarks" field as "Digital Currency Address - <SYMBOL> <address>", not
as a dedicated column, so they have to be extracted with a regex. EVM-
format (0x + 40 hex) addresses are extracted regardless of which symbol
OFAC tagged them with (ETH, ETC, USDC, USDT, etc.) -- the address format
itself is what determines whether it's checkable against a Polygon/Base
recipient, and a sanctioned entity's Ethereum-format address is the same
address regardless of which EVM chain it's used on.

The parsed list is persisted (SanctionedAddress) so screening survives a
restart and doesn't depend on OFAC's endpoint being reachable at the
exact moment of a send -- only a periodic background refresh needs it
to be reachable. If the list has never been successfully fetched yet
(e.g. moments after a fresh deploy, before the first refresh completes),
screening fails OPEN (does not block sends) rather than blocking all
legitimate traffic on a missing dataset -- this is a real tradeoff, not
an oversight, and GET /admin/sanctions-status makes the list's freshness
visible so a stale-or-empty list doesn't go unnoticed silently.
"""
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from database import SessionLocal
from models import SanctionedAddress, SanctionsListRefresh, UserAuditLog
from logging_config import get_logger

log = get_logger(__name__)

OFAC_SDN_CSV_URL = "https://sanctionslistservice.ofac.treas.gov/api/publicationpreview/exports/sdn.csv"

# Matches e.g. "Digital Currency Address - ETH 0xabc...123" -- the
# negative lookahead prevents silently truncating a longer hex run to
# the first 40 chars (verified against the real OFAC export: several
# addresses would otherwise be captured one character short).
_DIGITAL_CURRENCY_ADDRESS_RE = re.compile(
    r"Digital Currency Address - ([A-Za-z0-9]+) (0x[a-fA-F0-9]{40})(?![a-fA-F0-9])"
)

REFRESH_INTERVAL_SECONDS = 24 * 60 * 60  # daily -- OFAC updates several times/week; not real-time-critical for this app's scale


class RecipientSanctioned(Exception):
    """recipient_address matches OFAC's SDN digital-currency address list."""
    pass


def _extract_evm_addresses(csv_text: str) -> list[tuple[str, str]]:
    """Returns [(address_lowercase, currency_label), ...], deduplicated."""
    seen = {}
    for label, addr in _DIGITAL_CURRENCY_ADDRESS_RE.findall(csv_text):
        seen[addr.lower()] = label
    return sorted(seen.items())


async def refresh_sanctions_list(db: Session) -> dict:
    """Fetch the current OFAC SDN list, extract EVM addresses, and upsert
    them into SanctionedAddress. Records a SanctionsListRefresh row either
    way (success or failure) so refresh health is queryable."""
    try:
        # OFAC's endpoint 302-redirects to a signed S3 URL -- httpx does
        # not follow redirects by default, and treats an unfollowed
        # redirect as a raise_for_status() error. Confirmed live: the
        # first production refresh failed with exactly this (0 addresses
        # loaded, screening correctly failed open rather than blocking
        # sends -- see module docstring on fail-open behavior).
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(OFAC_SDN_CSV_URL)
            resp.raise_for_status()
            csv_text = resp.text
    except Exception as e:
        db.add(SanctionsListRefresh(status="failed", error=str(e)[:500]))
        db.commit()
        return {"status": "failed", "error": str(e)}

    addresses = _extract_evm_addresses(csv_text)

    for address, label in addresses:
        existing = db.query(SanctionedAddress).filter(SanctionedAddress.address == address).first()
        if existing:
            existing.last_confirmed_at = datetime.now(tz=timezone.utc)
            existing.currency_label = label
        else:
            db.add(SanctionedAddress(address=address, currency_label=label, source="OFAC_SDN"))

    db.add(SanctionsListRefresh(status="success", addresses_found=len(addresses)))
    db.commit()

    return {"status": "success", "addresses_found": len(addresses)}


def is_sanctioned(address: str, db: Session) -> bool:
    if not address:
        return False
    return db.query(SanctionedAddress).filter(
        SanctionedAddress.address == address.lower()
    ).first() is not None


async def check_recipient_not_sanctioned(sender_id: str, recipient_address: str, db: Session) -> None:
    """Raises RecipientSanctioned and writes a compliance audit log entry
    if recipient_address is flagged by either the self-built OFAC
    screener (is_sanctioned, always active) or an optional supplementary
    Watchman instance (services/watchman_screening.py -- a clean no-op
    until WATCHMAN_URL is configured). No-op (fails open) if neither
    check finds anything -- see this module's and watchman_screening's
    docstrings for why."""
    from services.watchman_screening import check_address_against_watchman

    flagged_by_ofac = is_sanctioned(recipient_address, db)
    flagged_by_watchman = await check_address_against_watchman(recipient_address)

    if not flagged_by_ofac and not flagged_by_watchman:
        return

    source = "ofac" if flagged_by_ofac else "watchman"
    retention_expires = datetime.now(tz=timezone.utc) + timedelta(days=365 * 7)
    db.add(UserAuditLog(
        user_id=sender_id,
        action="send_blocked_sanctioned_recipient",
        details=json.dumps({
            "recipient_address": recipient_address,
            "source": source,
            "timestamp": datetime.utcnow().isoformat(),
        }),
        retention_expires_at=retention_expires,
    ))
    db.commit()

    log.warning("sanctions.send_blocked", sender_id=sender_id, recipient_address=recipient_address, source=source)
    raise RecipientSanctioned(
        "This recipient address cannot be sent to -- it matches a sanctions list."
    )


async def _refresh_loop():
    log.info("sanctions.refresh_loop_started")
    while True:
        try:
            db = SessionLocal()
            try:
                result = await refresh_sanctions_list(db)
                log.info("sanctions.refresh_completed", **result)
            finally:
                db.close()
        except Exception as e:
            log.error("sanctions.refresh_loop_error", error=str(e))

        import asyncio
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)


def start_sanctions_screening():
    import asyncio
    loop = asyncio.get_event_loop()
    return loop.create_task(_refresh_loop())
