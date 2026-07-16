"""Coinbase Onramp — server-signed session tokens.

Unlike Ramp/MoonPay/Transak (which embed with a public client-side key),
Coinbase Onramp requires a short-lived session token minted server-side
with a CDP API key pair (EC private key), then handed to the frontend to
open Coinbase's hosted onramp URL. The CDP secret never reaches the client.

Auth: a JWT signed ES256, `kid` = key name, 2-minute expiry, sent as a
Bearer token to POST /onramp/v1/token. This mirrors Coinbase's documented
CDP JWT auth scheme used across their Advanced Trade / Onramp APIs.

Guarded by _require_configured(): unset project id or key pair raises.
"""
from __future__ import annotations

import time
import uuid

import httpx
from jose import jwt as jose_jwt

from config import settings

ONRAMP_TOKEN_URL = "https://api.developer.coinbase.com/onramp/v1/token"
ONRAMP_HOST_URL = "https://pay.coinbase.com/buy/select-asset"


class CoinbaseOnrampNotConfigured(RuntimeError):
    pass


class CoinbaseOnrampError(RuntimeError):
    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Coinbase Onramp API {status_code}: {body[:300]}")


def _require_configured() -> None:
    if not (
        settings.coinbase_onramp_project_id
        and settings.coinbase_cdp_api_key_name
        and settings.coinbase_cdp_api_key_secret
    ):
        raise CoinbaseOnrampNotConfigured(
            "Coinbase Onramp is not configured. Set COINBASE_ONRAMP_PROJECT_ID, "
            "COINBASE_CDP_API_KEY_NAME, and COINBASE_CDP_API_KEY_SECRET to enable it."
        )


def _build_cdp_jwt() -> str:
    """Sign a short-lived ES256 JWT with the CDP API key pair.

    `sub`/`iss` = key name, `kid` header = key name, `nonce` prevents
    replay. Coinbase's CDP auth requires the private key in PEM form.
    """
    now = int(time.time())
    payload = {
        "sub": settings.coinbase_cdp_api_key_name,
        "iss": "cdp",
        "nbf": now,
        "exp": now + 120,
        "uri": f"POST api.developer.coinbase.com/onramp/v1/token",
    }
    headers = {"kid": settings.coinbase_cdp_api_key_name, "nonce": uuid.uuid4().hex}
    return jose_jwt.encode(
        payload, settings.coinbase_cdp_api_key_secret, algorithm="ES256", headers=headers
    )


async def create_session_token(destination_address: str, assets: list[str] | None = None) -> dict:
    """Mint a one-time Onramp session token for a specific destination wallet.

    The returned token is embedded in the hosted onramp URL the frontend
    opens — it authorizes exactly one purchase flow, scoped to this wallet.
    """
    _require_configured()
    body = {
        "addresses": [{"address": destination_address, "blockchains": ["polygon"]}],
        "assets": assets or ["USDC"],
    }
    headers = {"Authorization": f"Bearer {_build_cdp_jwt()}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(ONRAMP_TOKEN_URL, json=body, headers=headers)

    if r.status_code >= 400:
        raise CoinbaseOnrampError(r.status_code, r.text)

    data = r.json()
    token = data.get("token") or data.get("channel_id")
    if not token:
        raise CoinbaseOnrampError(502, "Coinbase Onramp returned no session token")

    return {
        "session_token": token,
        "onramp_url": f"{ONRAMP_HOST_URL}?sessionToken={token}&defaultAsset=USDC&defaultNetwork=polygon",
    }
