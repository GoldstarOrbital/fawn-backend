"""
Column banking API calls (deposit accounts, balances, ACH + book transfers).

Column is the banking backend FAWN cuts over to from Unit. It exposes a
plain REST + HTTP Basic-auth API (the secret key is the Basic username,
empty password), unlike Unit's JSON:API envelope — so payloads here are
flat dicts, not {"data": {"type", "attributes"}}.

Sandbox and production share one base URL (https://api.column.com); a
test-mode key vs a live key selects the environment, so there is no
separate base-url switch like Unit had.

Every function is guarded by _require_configured(): if COLUMN_API_KEY is
unset the call raises a clear ColumnNotConfigured error instead of firing
an unauthenticated request. This keeps the provider dormant-but-safe until
a real Column contract and key are in place.
"""
from __future__ import annotations

import httpx
from config import settings


class ColumnNotConfigured(RuntimeError):
    """Raised when a Column call is attempted without COLUMN_API_KEY set."""


class ColumnError(RuntimeError):
    """A non-2xx response from Column, carrying status + body for the caller."""

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Column API {status_code}: {body[:300]}")


def _require_configured() -> None:
    if not settings.column_api_key:
        raise ColumnNotConfigured(
            "Column is not configured. Set COLUMN_API_KEY to enable banking."
        )


def _auth() -> tuple[str, str]:
    # Column uses HTTP Basic auth: API key as username, empty password.
    return (settings.column_api_key, "")


async def _request(method: str, path: str, *, json: dict | None = None,
                   params: dict | None = None, idempotency_key: str | None = None) -> dict:
    _require_configured()
    headers = {"Content-Type": "application/json"}
    if idempotency_key:
        # Column dedupes writes on this header — a retried create returns the
        # original resource instead of a duplicate (mirrors Unit's Idempotency-Key).
        headers["Idempotency-Key"] = idempotency_key
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.request(
            method,
            f"{settings.column_base_url}{path}",
            json=json,
            params=params,
            headers=headers,
            auth=_auth(),
        )
    if resp.status_code >= 300:
        raise ColumnError(resp.status_code, resp.text)
    return resp.json() if resp.content else {}


# ---- Bank (customer/entity) + deposit accounts ------------------------------

async def create_deposit_account(column_entity_id: str, description: str = "FAWN Checking") -> dict:
    """Open a checking bank account for an existing Column entity.

    `column_entity_id` is Column's person/business record — created once per
    user after KYC (Unit still fronts KYC; the approved identity is mirrored
    into Column as an entity out of band). Returns Column's bank-account dict.
    """
    return await _request(
        "POST",
        "/bank-accounts",
        json={"entity_id": column_entity_id, "description": description, "type": "CHECKING"},
        idempotency_key=f"fawn-acct-{column_entity_id}",
    )


async def get_account_balance(column_account_id: str) -> dict:
    """Normalized balance dict, dollars — mirrors unit.get_account_balance()."""
    data = await _request("GET", f"/bank-accounts/{column_account_id}")
    balances = data.get("balances", {})
    return {
        "account_id": column_account_id,
        "available": balances.get("available_amount", 0) / 100,
        "current": balances.get("pending_amount", 0) / 100 + balances.get("available_amount", 0) / 100,
        "currency": "USD",
    }


async def get_account_details(column_account_id: str) -> dict:
    data = await _request("GET", f"/bank-accounts/{column_account_id}")
    return {
        "account_id": column_account_id,
        "routing_number": data.get("routing_number", ""),
        "account_number": data.get("account_number", ""),
        "account_name": data.get("description", ""),
        "status": data.get("status", ""),
        "deposit_product": "checking",
    }


# ---- Money movement ---------------------------------------------------------

async def create_book_transfer(sender_account_id: str, recipient_account_id: str,
                               amount_cents: int, description: str,
                               idempotency_key: str) -> dict:
    """Instant internal transfer between two Column accounts (Unit "book payment"
    equivalent). Both accounts are on Column's ledger, so this settles instantly
    and never touches an external network."""
    return await _request(
        "POST",
        "/transfers/book",
        json={
            "bank_account_id": sender_account_id,
            "counterparty_bank_account_id": recipient_account_id,
            "amount": amount_cents,
            "currency_code": "USD",
            "description": description[:80],
        },
        idempotency_key=idempotency_key,
    )


async def create_ach_credit(column_account_id: str, routing_number: str,
                            account_number: str, account_type: str,
                            account_holder_name: str, amount_cents: int,
                            idempotency_key: str) -> dict:
    """Pull external funds into a FAWN Column account via ACH ("Add funds").

    Counterparty bank details are sent inline and NEVER persisted on our side
    (only the last 4 for the user's reference), mirroring how Unit ACH funding
    handled raw account numbers. ACH settles in days and can be returned, so
    callers must not treat this as instant/final."""
    return await _request(
        "POST",
        "/transfers/ach",
        json={
            "bank_account_id": column_account_id,
            "type": "CREDIT",
            "amount": amount_cents,
            "currency_code": "USD",
            "description": "FAWN Add Funds",
            "counterparty": {
                "routing_number": routing_number,
                "account_number": account_number,
                "account_type": account_type,
                "name": account_holder_name,
            },
        },
        idempotency_key=idempotency_key,
    )


async def list_transactions(column_account_id: str, limit: int = 20) -> list:
    data = await _request("GET", "/transfers", params={"bank_account_id": column_account_id, "limit": limit})
    out = []
    for item in data.get("transfers", data.get("data", [])):
        amount_cents = item.get("amount", 0)
        direction = 1 if item.get("type", "").upper() in ("CREDIT", "BOOK_CREDIT") else -1
        out.append({
            "id": item.get("id", ""),
            "type": item.get("type", ""),
            "amount": round((amount_cents / 100) * direction, 2),
            "description": item.get("description", ""),
            "date": (item.get("created_at", "") or "")[:10],
            "status": item.get("status", ""),
        })
    return out
