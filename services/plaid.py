"""
Plaid API calls (bank-account linking for funding-source switching).

Plaid lets a user securely link an external bank account (via Plaid Link in
the frontend) so FAWN can pull funds without the user hand-typing routing +
account numbers. The flow:

  1. backend  create_link_token()      -> frontend opens Plaid Link with it
  2. frontend user logs into their bank -> Link returns a public_token
  3. backend  exchange_public_token()   -> permanent access_token (stored)
  4. backend  get_auth()                -> routing/account numbers for ACH

Plaid auth is body-based: client_id + secret go in every request JSON, not
in a header. Sandbox is https://sandbox.plaid.com.

The access_token is a long-lived secret — callers persist it on the
PlaidItem row and must never expose it to the client. get_auth() returns
raw routing/account numbers that follow the same handling rule as Unit/
Column funding: forward to the banking provider, store only the last 4.

Guarded by _require_configured(): unset client_id/secret raises.
"""
from __future__ import annotations

import httpx
from config import settings


class PlaidNotConfigured(RuntimeError):
    pass


class PlaidError(RuntimeError):
    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Plaid API {status_code}: {body[:300]}")


def _require_configured() -> None:
    if not settings.plaid_client_id or not settings.plaid_secret:
        raise PlaidNotConfigured(
            "Plaid is not configured. Set PLAID_CLIENT_ID and PLAID_SECRET "
            "to enable bank linking."
        )


async def _request(path: str, body: dict) -> dict:
    _require_configured()
    payload = {
        "client_id": settings.plaid_client_id,
        "secret": settings.plaid_secret,
        **body,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.plaid_base_url}{path}",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
    if resp.status_code >= 300:
        raise PlaidError(resp.status_code, resp.text)
    return resp.json()


async def create_link_token(user_id: str) -> dict:
    """Create a short-lived link_token the frontend feeds to Plaid Link."""
    data = await _request(
        "/link/token/create",
        {
            "user": {"client_user_id": user_id},
            "client_name": "FAWN",
            "products": ["auth"],
            "country_codes": ["US"],
            "language": "en",
        },
    )
    return {"link_token": data.get("link_token", ""), "expiration": data.get("expiration", "")}


async def exchange_public_token(public_token: str) -> dict:
    """Swap the one-time public_token from Link for a permanent access_token."""
    data = await _request("/item/public_token/exchange", {"public_token": public_token})
    return {"access_token": data.get("access_token", ""), "item_id": data.get("item_id", "")}


async def get_auth(access_token: str) -> dict:
    """Fetch routing/account numbers for the linked account so the banking
    provider (Column/Unit) can set up ACH. Returns the first account's numbers
    plus an institution-name best-effort for display."""
    data = await _request("/auth/get", {"access_token": access_token})
    numbers = data.get("numbers", {}).get("ach", [])
    accounts = data.get("accounts", [])
    first = numbers[0] if numbers else {}
    account_meta = next(
        (a for a in accounts if a.get("account_id") == first.get("account_id")), {}
    )
    return {
        "routing_number": first.get("routing", ""),
        "account_number": first.get("account", ""),
        "account_id": first.get("account_id", ""),
        "account_type": (account_meta.get("subtype") or "checking"),
        "account_name": account_meta.get("name", ""),
        "mask": account_meta.get("mask", ""),
    }
