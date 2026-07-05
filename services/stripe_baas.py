"""
All Stripe BaaS API calls (Connect + Treasury + Issuing).

Stripe has one API host for both test and live mode — which mode you're in
is determined entirely by which secret key you use (sk_test_... vs
sk_live_...). Switch via STRIPE_SECRET_KEY env var on Railway.

Architecture:
  Stripe Connect (Custom accounts + Account Links) -> hosted KYC/onboarding
  Stripe Treasury                                  -> deposit ("Financial")
                                                       accounts, balances,
                                                       transactions, transfers
  Stripe Issuing                                   -> virtual debit cards

Every Treasury/Issuing/Cardholder call below is made "on behalf of" the
end user's own Connect account via the `Stripe-Account` header — the
platform's secret key authenticates the call, and Stripe-Account scopes it
to that connected account's ledger.

NOTE: Stripe Treasury and Issuing both require Stripe to enable those
products on the platform account first (they're invite/approval-gated,
similar to how a sponsor-bank agreement gates production access for other
BaaS providers). This module is written against the documented API shape
so it's ready to go the moment those capabilities are approved.
"""
from __future__ import annotations

import re
import httpx
from typing import TYPE_CHECKING
from config import settings

if TYPE_CHECKING:
    import schemas

STRIPE_API_BASE = "https://api.stripe.com/v1"


def _auth_headers(account_id: str | None = None, idempotency_key: str | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {settings.stripe_secret_key}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if account_id:
        # Scopes this call to the end user's Connect account (Treasury
        # Financial Account, Issuing cards/cardholders, etc. all live here).
        headers["Stripe-Account"] = account_id
    if idempotency_key:
        # Stripe dedupes on this header — a retried request with the same
        # key returns the original object instead of
        # creating a second one.
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _flatten(prefix: str, value, out: dict):
    """Flatten a nested dict/list into Stripe's bracket form-encoding, e.g.
    {"individual": {"dob": {"day": 1}}} -> "individual[dob][day]"."""
    if isinstance(value, dict):
        for k, v in value.items():
            if v is None:
                continue
            _flatten(f"{prefix}[{k}]", v, out)
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _flatten(f"{prefix}[{i}]", v, out)
    else:
        out[prefix] = value


def _encode(data: dict) -> dict:
    out: dict = {}
    for k, v in data.items():
        if v is None:
            continue
        _flatten(k, v, out)
    return out


async def _post(path: str, data: dict, account_id: str | None = None, idempotency_key: str | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{STRIPE_API_BASE}{path}",
            data=_encode(data),
            headers=_auth_headers(account_id=account_id, idempotency_key=idempotency_key),
        )
        resp.raise_for_status()
        return resp.json()


async def _get(path: str, params: dict | None = None, account_id: str | None = None) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{STRIPE_API_BASE}{path}",
            params=params or {},
            headers=_auth_headers(account_id=account_id),
        )
        resp.raise_for_status()
        return resp.json()


# --- Connect (KYC / onboarding) ---

async def create_connect_account(
    full_name: str,
    email: str,
    phone: str,
    ssn: str,
    date_of_birth: str,
    address: "schemas.Address",
    occupation: str = "Student",
) -> dict:
    """Create an individual Connect Custom account and submit KYC directly.

    SSN is passed straight through as `individual[id_number]` — never stored
    on our side. Stripe evaluates the account's `requirements` and
    `capabilities` (treasury/card_issuing) asynchronously; a freshly created
    account is rarely "active" immediately — poll get_account() or wait for
    the `account.updated` webhook the same way accounts.py already polls
    pending applications.
    """
    first, *rest = full_name.strip().split()
    last = " ".join(rest) if rest else "Unknown"
    year, month, day = (date_of_birth.split("-") + ["", "", ""])[:3]
    phone_digits = re.sub(r"\D", "", phone)[-10:]

    payload = {
        "type": "custom",
        "country": "US",
        "email": email,
        "business_type": "individual",
        "capabilities": {
            "treasury": {"requested": "true"},
            "card_issuing": {"requested": "true"},
            "transfers": {"requested": "true"},
            "us_bank_account_ach_payments": {"requested": "true"},
        },
        "individual": {
            "first_name": first,
            "last_name": last,
            "email": email,
            "phone": f"+1{phone_digits}",
            "id_number": ssn,
            "dob": {"day": day, "month": month, "year": year},
            "address": {
                "line1": address.street,
                "city": address.city,
                "state": address.state,
                "postal_code": address.postal_code,
                "country": address.country,
            },
        },
        "business_profile": {"mcc": "6012", "product_description": f"FAWN student banking ({occupation})"},
        "tos_acceptance": {"service_agreement": "recipient"},
    }
    return await _post("/accounts", payload)


async def create_connect_account_stub(full_name: str, email: str, phone: str, occupation: str = "Student") -> dict:
    """Create a bare Connect Custom account with no SSN/DOB/address collected
    by FAWN — used ahead of Stripe's hosted onboarding flow (Account Link),
    where the user enters that sensitive KYC data themselves inside Stripe's
    hosted UI.
    """
    first, *rest = full_name.strip().split()
    last = " ".join(rest) if rest else "Unknown"
    phone_digits = re.sub(r"\D", "", phone or "")[-10:]

    payload = {
        "type": "custom",
        "country": "US",
        "email": email,
        "business_type": "individual",
        "capabilities": {
            "treasury": {"requested": "true"},
            "card_issuing": {"requested": "true"},
            "transfers": {"requested": "true"},
            "us_bank_account_ach_payments": {"requested": "true"},
        },
        "individual": {
            "first_name": first,
            "last_name": last,
            "email": email,
            "phone": f"+1{phone_digits}" if phone_digits else None,
        },
        "business_profile": {"mcc": "6012", "product_description": f"FAWN student banking ({occupation})"},
        "tos_acceptance": {"service_agreement": "recipient"},
    }
    return await _post("/accounts", payload, idempotency_key=f"fawn-account-{email}")


async def create_account_onboarding_link(account_id: str, refresh_url: str, return_url: str) -> dict:
    """Create a Stripe-hosted onboarding link for the given Connect account —
    the user enters SSN/DOB/address themselves inside Stripe's hosted flow
    instead of FAWN collecting it, and FAWN only ever receives the account
    id back.
    """
    payload = {
        "account": account_id,
        "refresh_url": refresh_url,
        "return_url": return_url,
        "type": "account_onboarding",
    }
    return await _post("/account_links", payload)


async def get_account(account_id: str) -> dict:
    """Poll the status of a Connect account (KYC + capability activation)."""
    return await _get(f"/accounts/{account_id}")


def account_is_active(account: dict) -> bool:
    caps = account.get("capabilities", {}) or {}
    return caps.get("treasury") == "active"


# --- Treasury (deposit accounts / balances / transactions) ---

async def create_financial_account(account_id: str) -> dict:
    """Open a Treasury Financial Account for an existing, KYC'd Connect account."""
    payload = {
        "supported_currencies": ["usd"],
        "features": {
            "card_issuing": {"requested": "true"},
            "deposit_insurance": {"requested": "true"},
            "financial_addresses": {"aba": {"requested": "true"}},
            "inbound_transfers": {"ach": {"requested": "true"}},
            "intra_stripe_flows": {"requested": "true"},
            "outbound_payments": {"ach": {"requested": "true"}, "us_domestic_wire": {"requested": "true"}},
            "outbound_transfers": {"ach": {"requested": "true"}, "us_domestic_wire": {"requested": "true"}},
        },
    }
    return await _post("/treasury/financial_accounts", payload, account_id=account_id)


async def get_financial_account_details(account_id: str, financial_account_id: str) -> dict:
    data = await _get(
        f"/treasury/financial_accounts/{financial_account_id}",
        params={"expand[]": "financial_addresses"},
        account_id=account_id,
    )
    addresses = data.get("financial_addresses", []) or []
    aba = next((a.get("aba", {}) for a in addresses if a.get("type") == "aba"), {})
    return {
        "account_id": financial_account_id,
        "routing_number": aba.get("routing_number", ""),
        "account_number": aba.get("account_number", ""),
        "account_name": "FAWN Checking",
        "status": data.get("status", ""),
        "deposit_product": "checking",
    }


async def get_account_balance(account_id: str, financial_account_id: str) -> dict:
    data = await _get(f"/treasury/financial_accounts/{financial_account_id}", account_id=account_id)
    balance = data.get("balance", {}) or {}
    cash = (balance.get("cash") or {}).get("usd", 0)
    outbound_pending = (balance.get("outbound_pending") or {}).get("usd", 0)
    return {
        "account_id": financial_account_id,
        "available": (cash - outbound_pending) / 100,
        "current": cash / 100,
        "currency": "USD",
    }


async def list_transactions(account_id: str, financial_account_id: str, limit: int = 20) -> list:
    data = await _get(
        "/treasury/transactions",
        params={"financial_account": financial_account_id, "limit": limit},
        account_id=account_id,
    )
    items = data.get("data", [])

    transactions = []
    for item in items:
        amount_cents = item.get("amount", 0)  # Stripe signs this: positive = credit, negative = debit
        transactions.append({
            "id": item["id"],
            "type": item.get("flow_type", "treasury_transaction"),
            "amount": round(amount_cents / 100, 2),
            "description": item.get("description", ""),
            "date": (item.get("created_datetime") or "")[:10],
            "status": item.get("status", ""),
        })
    return transactions


async def create_treasury_transfer(
    sender_account_id: str,
    sender_financial_account_id: str,
    recipient_financial_account_id: str,
    amount_cents: int,
    description: str,
    idempotency_key: str,
) -> dict:
    """Move money between two Financial Accounts under the same platform.

    Uses a Treasury Outbound Payment with a `financial_account`-type
    destination — Stripe's supported pattern for platform-internal,
    same-day/instant transfers between Financial Accounts you manage on
    behalf of two different connected accounts. This is the only Stripe
    call Tier 1 P2P sends make. The
    Idempotency-Key header means a network-retry of this exact call can
    never double-send.
    """
    payload = {
        "financial_account": sender_financial_account_id,
        "amount": amount_cents,
        "currency": "usd",
        "description": description[:80],
        "destination_payment_method_data": {
            "type": "financial_account",
            "financial_account": recipient_financial_account_id,
        },
    }
    return await _post(
        "/treasury/outbound_payments",
        payload,
        account_id=sender_account_id,
        idempotency_key=idempotency_key,
    )


# --- Issuing (virtual cards) ---

async def create_issuing_cardholder(account_id: str, full_name: str, email: str, phone: str, address: "schemas.Address | None" = None) -> dict:
    first, *rest = full_name.strip().split()
    last = " ".join(rest) if rest else "Unknown"
    phone_digits = re.sub(r"\D", "", phone or "")[-10:]
    payload = {
        "type": "individual",
        "name": f"{first} {last}",
        "email": email,
        "phone_number": f"+1{phone_digits}" if phone_digits else None,
        "billing": {
            "address": {
                "line1": address.street if address else "1 Unknown St",
                "city": address.city if address else "Unknown",
                "state": address.state if address else "CA",
                "postal_code": address.postal_code if address else "00000",
                "country": address.country if address else "US",
            }
        },
    }
    return await _post("/issuing/cardholders", payload, account_id=account_id)


async def create_virtual_card(account_id: str, cardholder_id: str, financial_account_id: str, idempotency_key: str) -> dict:
    """Issue an individual virtual debit card tied to a Treasury Financial Account.

    Deliberately the only card-creation path we expose — no full PAN/CVV
    retrieval anywhere in this service. Stripe's card "number"/"cvc" fields
    require PCI-scoped handling (via Stripe.js element or the sensitive
    details API) we haven't built and don't need for an MVP; cardholders
    only ever see last4/status.
    """
    payload = {
        "cardholder": cardholder_id,
        "currency": "usd",
        "type": "virtual",
        "financial_account": financial_account_id,
    }
    return await _post("/issuing/cards", payload, account_id=account_id, idempotency_key=idempotency_key)


def _card_summary(card: dict) -> dict:
    return {
        "id": card["id"],
        "last4Digits": card.get("last4", ""),
        "expirationDate": f"{card.get('exp_month', ''):0>2}/{card.get('exp_year', '')}" if card.get("exp_month") else "",
        "status": card.get("status", ""),
        "createdAt": card.get("created", ""),
    }


async def list_cards(account_id: str, financial_account_id: str) -> list:
    data = await _get("/issuing/cards", params={"financial_account": financial_account_id}, account_id=account_id)
    return [_card_summary(c) for c in data.get("data", [])]


async def get_card(account_id: str, card_id: str) -> dict:
    data = await _get(f"/issuing/cards/{card_id}", account_id=account_id)
    return _card_summary(data)


async def freeze_card(account_id: str, card_id: str, reason: str = "userRequested") -> dict:
    payload = {"status": "inactive"}
    data = await _post(f"/issuing/cards/{card_id}", payload, account_id=account_id)
    return _card_summary(data)


async def unfreeze_card(account_id: str, card_id: str) -> dict:
    payload = {"status": "active"}
    data = await _post(f"/issuing/cards/{card_id}", payload, account_id=account_id)
    return _card_summary(data)


# --- Inbound ACH funding ---

async def create_inbound_transfer(
    account_id: str,
    financial_account_id: str,
    routing_number: str,
    account_number: str,
    account_type: str,
    account_holder_name: str,
    amount_cents: int,
    idempotency_key: str,
) -> dict:
    """Pull money from an external bank account into a FAWN Financial
    Account via ACH — "Add funds."

    Creates a one-off `us_bank_account` PaymentMethod from the raw
    routing/account number (no Financial Connections verification yet —
    that's the natural next step once there's a reason to add a second
    verification dependency), then an Inbound Transfer pulling from it. ACH settles in
    days, not instantly, and can be returned by the sending bank — unlike
    a Treasury transfer this is never treated as instant or final.

    routing_number/account_number are sent directly to Stripe and never
    persisted on our side — callers must not store them, only the last 4
    digits for the user's own reference (mirrors how SSN is handled).
    """
    pm_payload = {
        "type": "us_bank_account",
        "us_bank_account": {
            "account_number": account_number,
            "routing_number": routing_number,
            "account_holder_type": "individual",
            "account_type": account_type.lower(),
        },
        "billing_details": {"name": account_holder_name},
    }
    payment_method = await _post(
        "/payment_methods", pm_payload, account_id=account_id, idempotency_key=f"{idempotency_key}:pm",
    )

    transfer_payload = {
        "financial_account": financial_account_id,
        "amount": amount_cents,
        "currency": "usd",
        "origin_payment_method": payment_method["id"],
        "description": "FAWN Add Funds",
    }
    return await _post(
        "/treasury/inbound_transfers", transfer_payload, account_id=account_id, idempotency_key=idempotency_key,
    )
