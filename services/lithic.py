"""
Lithic card-issuing API calls (virtual cards + real-time auth).

Lithic issues the debit cards that spend against a FAWN banking account.
Auth is a single `Authorization: <api-key>` header (no "Bearer" prefix).
Like Column, payloads are flat JSON, not Unit's JSON:API envelope.

Card state lives at Lithic; the local `Card` row only records ownership
(see models.Card). We never retrieve full PAN/CVV here — only masked
last4/status — same deliberate limit as the Unit card service.

Real-time authorization (approve/decline a swipe as it happens) arrives as
a webhook, verified in routers/lithic_webhook.py; this client only covers
the issuing/management REST surface.

Guarded by _require_configured(): unset LITHIC_API_KEY raises rather than
firing an unauthenticated request.
"""
from __future__ import annotations

import httpx
from config import settings


class LithicNotConfigured(RuntimeError):
    pass


class LithicError(RuntimeError):
    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Lithic API {status_code}: {body[:300]}")


def _require_configured() -> None:
    if not settings.lithic_api_key:
        raise LithicNotConfigured(
            "Lithic is not configured. Set LITHIC_API_KEY to enable card issuing."
        )


def _headers(idempotency_key: str | None = None) -> dict:
    headers = {
        "Authorization": settings.lithic_api_key,  # Lithic wants the raw key, no "Bearer"
        "Content-Type": "application/json",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


async def _request(method: str, path: str, *, json: dict | None = None,
                   params: dict | None = None, idempotency_key: str | None = None) -> dict:
    _require_configured()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.request(
            method,
            f"{settings.lithic_base_url}{path}",
            json=json,
            params=params,
            headers=_headers(idempotency_key),
        )
    if resp.status_code >= 300:
        raise LithicError(resp.status_code, resp.text)
    return resp.json() if resp.content else {}


def _card_summary(card: dict) -> dict:
    """Normalize a Lithic card object to the shape routers/cards.py expects
    (the same masked shape unit._card_summary produced)."""
    exp_month = str(card.get("exp_month", "")).zfill(2)
    exp_year = str(card.get("exp_year", ""))[-2:]
    return {
        "id": card.get("token", ""),
        "last4Digits": card.get("last_four", ""),
        "expirationDate": f"{exp_month}{exp_year}" if exp_month and exp_year else "",
        "status": card.get("state", ""),  # OPEN | PAUSED | CLOSED
        "createdAt": card.get("created", ""),
    }


async def create_virtual_card(financial_account_token: str, idempotency_key: str) -> dict:
    """Issue a virtual debit card bound to a FAWN financial account.

    `financial_account_token` links the card to the funding account so Lithic
    routes real-time auth against the right balance. Returns the raw Lithic
    card object; callers normalize via _card_summary()."""
    data = await _request(
        "POST",
        "/cards",
        json={
            "type": "VIRTUAL",
            "state": "OPEN",
            "financial_account_token": financial_account_token,
        },
        idempotency_key=idempotency_key,
    )
    # Lithic returns the created card either bare or under "data".
    return data.get("data", data)


async def get_card(card_token: str) -> dict:
    data = await _request("GET", f"/cards/{card_token}")
    return _card_summary(data.get("data", data))


async def list_cards(financial_account_token: str) -> list:
    data = await _request("GET", "/cards", params={"financial_account_token": financial_account_token})
    return [_card_summary(c) for c in data.get("data", [])]


async def _set_state(card_token: str, state: str) -> dict:
    data = await _request("PATCH", f"/cards/{card_token}", json={"state": state})
    return _card_summary(data.get("data", data))


async def freeze_card(card_token: str, reason: str = "userRequested") -> dict:
    """PAUSED is Lithic's reversible freeze (vs CLOSED, which is permanent)."""
    return await _set_state(card_token, "PAUSED")


async def unfreeze_card(card_token: str) -> dict:
    return await _set_state(card_token, "OPEN")
