"""
Optional supplementary sanctions screening via a self-hosted Moov
Watchman instance (https://github.com/moov-io/watchman, Apache-2.0).

This is ADDITIVE to services/sanctions_screening.py's self-built OFAC
CSV scraper, not a replacement -- that scraper is already deployed and
proven (76 real addresses loaded, confirmed live 2026-07-19). Watchman
aggregates a broader set of free government lists (OFAC SDN + non-SDN,
US Consolidated Screening List, UN, EU, UK OFSI, BIS Denied Persons)
with fuzzy/phonetic matching, which is a real improvement -- but it
needs its own deployed HTTP service (a Go binary, published as the
`moov/watchman` Docker image), which is infrastructure FAWN's backend
doesn't control or provision by itself.

Deploying it:
  1. In Railway, create a new service -> "Deploy from Docker Image" ->
     `moov/watchman` (no build step, no Dockerfile needed in this repo).
  2. No required config for basic operation -- it self-downloads and
     refreshes the included lists on its own schedule. Exposes port 8084.
  3. Once it has a public Railway URL, set WATCHMAN_URL in this backend's
     env vars to that URL (e.g. https://watchman-production.up.railway.app).

Until WATCHMAN_URL is set, every function here is a clean no-op (returns
None immediately, no network call) -- merging this code changes nothing
about current behavior. Once configured, checks run ALONGSIDE the
existing OFAC screener in services/sanctions_screening.py, not instead
of it -- either one flagging an address is enough to hold/block a send.
"""
from typing import Optional

import httpx

from config import settings

# EVM-compatible chains (Polygon, Base) share the same 0x address format
# as Ethereum -- Watchman's crypto-address matching is keyed on this
# format regardless of which specific EVM chain the address is used on.
WATCHMAN_CURRENCY_SYMBOL = "ETH"

# A real Watchman hit is scored 0-1 (Jaro-Winkler-style similarity); this
# threshold favors precision (few false positives blocking sends) over
# recall for a supplementary check layered on top of an existing OFAC
# screener -- 0.95 catches near-exact address matches, not fuzzy misses.
MIN_MATCH_SCORE = 0.95


async def check_address_against_watchman(address: str) -> Optional[bool]:
    """Returns True if Watchman finds a high-confidence sanctions match
    for this crypto address, False if it found none, or None if the
    check couldn't run at all (WATCHMAN_URL not configured, or the
    lookup failed) -- callers must treat None as "unknown, not clean,"
    same reasoning as services/sanctions_screening.py and
    services/address_risk.py fail-open elsewhere in this codebase."""
    base_url = settings.watchman_url
    if not base_url:
        return None

    query = f"{WATCHMAN_CURRENCY_SYMBOL}:{address}"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{base_url.rstrip('/')}/v2/search",
                params={"cryptoAddress": query, "limit": 1},
            )
            resp.raise_for_status()
            body = resp.json()
    except Exception:
        return None

    entities = body.get("entities")
    if not isinstance(entities, list) or not entities:
        return False

    top_match = entities[0].get("match")
    return isinstance(top_match, (int, float)) and top_match >= MIN_MATCH_SCORE
