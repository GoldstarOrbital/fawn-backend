"""
All Unit BaaS API calls.
Sandbox:    https://api.s.unit.sh
Production: https://api.unit.co
Switch via UNIT_BASE_URL env var on Railway.
"""
from __future__ import annotations

import re
import httpx
from typing import TYPE_CHECKING
from config import settings

if TYPE_CHECKING:
    import schemas


def _headers(idempotency_key: str | None = None):
    headers = {
        "Authorization": f"Bearer {settings.unit_api_token}",
        "Content-Type": "application/vnd.api+json",
    }
    if idempotency_key:
        # Unit dedupes on this header — a retried request with the same key
        # returns the original payment instead of creating a second one.
        headers["Idempotency-Key"] = idempotency_key
    return headers


async def create_application(
    full_name: str,
    email: str,
    phone: str,
    ssn: str,
    date_of_birth: str,
    address: "schemas.Address",
    occupation: str = "Student",
) -> dict:
    """
    Submit an individual KYC application to Unit.
    SSN is passed directly — never stored on our side.
    Returns the application data dict (type, id, attributes, relationships).
    Status will be 'approved' (instant) or 'pending' (manual review).
    """
    first, *rest = full_name.strip().split()
    last = " ".join(rest) if rest else "Unknown"
    phone_digits = re.sub(r"\D", "", phone)[-10:]

    payload = {
        "data": {
            "type": "individualApplication",
            "attributes": {
                "ssn": ssn,
                "fullName": {"first": first, "last": last},
                "dateOfBirth": date_of_birth,
                "address": {
                    "street": address.street,
                    "city": address.city,
                    "state": address.state,
                    "postalCode": address.postal_code,
                    "country": address.country,
                },
                "email": email,
                "occupation": occupation,
                "phone": {
                    "countryCode": "1",
                    "number": phone_digits,
                },
            },
        }
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.unit_base_url}/applications",
            json=payload,
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()["data"]


async def approve_application_sandbox(unit_application_id: str) -> dict:
    """Sandbox-only simulation: force-approve an application stuck in
    PendingReview/AwaitingDocuments. Unit's real production review process
    has no equivalent — this only exists on api.s.unit.sh for developer
    testing. Caller is responsible for checking settings.unit_base_url
    before calling this.
    """
    payload = {
        "data": {
            "type": "applicationApprove",
            "attributes": {"reason": "sandbox"},
        }
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.unit_base_url}/sandbox/applications/{unit_application_id}/approve",
            json=payload,
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()["data"]


async def get_application(unit_application_id: str) -> dict:
    """Poll the status of a pending application."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{settings.unit_base_url}/applications/{unit_application_id}",
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()["data"]


async def create_deposit_account(unit_customer_id: str) -> dict:
    """Open a checking deposit account for an existing Unit customer."""
    payload = {
        "data": {
            "type": "depositAccount",
            "attributes": {"depositProduct": "checking"},
            "relationships": {
                "customer": {
                    "data": {"type": "individualCustomer", "id": unit_customer_id}
                }
            },
        }
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.unit_base_url}/accounts",
            json=payload,
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json()["data"]


async def create_application_form(
    user_id: str,
    full_name: str,
    email: str,
    phone: str,
    is_student: bool,
    school: str | None = None,
    military_status: str | None = None,
) -> dict:
    """Create a Unit-hosted application form for KYC onboarding.

    The user enters SSN/address and other sensitive KYC data inside Unit's
    hosted flow. FAWN sends only non-sensitive prefill fields and tags the
    application with our user id so webhooks can link the approved customer
    back to the local account.
    """
    first, *rest = full_name.strip().split()
    last = " ".join(rest) if rest else "Unknown"
    phone_digits = re.sub(r"\D", "", phone or "")[-10:]

    payload = {
        "data": {
            "type": "applicationForm",
            "attributes": {
                "applicantDetails": {
                    "fullName": {"first": first, "last": last},
                    "email": email,
                    "phone": {
                        "countryCode": "1",
                        "number": phone_digits,
                    },
                    "occupation": "Student" if is_student else "",
                },
                "settingsOverride": {
                    "idempotencyKey": f"fawn-user-{user_id}",
                    "tags": {
                        "fawnUserId": user_id,
                        "school": school or "",
                        "militaryStatus": military_status or "",
                    },
                },
            },
        }
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.unit_base_url}/application-forms",
            json=payload,
            headers=_headers(idempotency_key=f"fawn-application-form-{user_id}"),
        )
        resp.raise_for_status()
        return resp.json()["data"]


async def get_customer_accounts(unit_customer_id: str) -> list:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{settings.unit_base_url}/accounts",
            params={"filter[customerId]": unit_customer_id},
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json().get("data", [])


async def get_account_details(unit_account_id: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{settings.unit_base_url}/accounts/{unit_account_id}",
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        attrs = data["attributes"]
        return {
            "account_id": unit_account_id,
            "routing_number": attrs.get("routingNumber", ""),
            "account_number": attrs.get("accountNumber", ""),
            "account_name": attrs.get("name", ""),
            "status": attrs.get("status", ""),
            "deposit_product": attrs.get("depositProduct", "checking"),
        }


async def get_account_balance(unit_account_id: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{settings.unit_base_url}/accounts/{unit_account_id}",
            headers=_headers(),
        )
        resp.raise_for_status()
        attrs = resp.json()["data"]["attributes"]
        return {
            "account_id": unit_account_id,
            "available": attrs.get("available", 0) / 100,
            "current": attrs.get("balance", 0) / 100,
            "currency": attrs.get("currency", "USD"),
        }


async def list_transactions(unit_account_id: str, limit: int = 20) -> list:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{settings.unit_base_url}/transactions",
            params={"filter[accountId]": unit_account_id, "page[limit]": limit},
            headers=_headers(),
        )
        resp.raise_for_status()
        items = resp.json().get("data", [])

    transactions = []
    for item in items:
        a = item["attributes"]
        amount_cents = a.get("amount", 0)
        direction = 1 if a.get("direction", "").lower() == "credit" else -1
        transactions.append({
            "id": item["id"],
            "type": item["type"],
            "amount": round((amount_cents / 100) * direction, 2),
            "description": a.get("description", ""),
            "date": a.get("createdAt", "")[:10],
            "status": a.get("status", ""),
        })
    return transactions


async def create_book_payment(
    sender_account_id: str,
    recipient_account_id: str,
    amount_cents: int,
    description: str,
    idempotency_key: str,
) -> dict:
    """Move money between two deposit accounts at the same sponsor bank.

    Unit Book Payments settle instantly (sub-second) since no external
    network is involved — both accounts live under the same Unit org.
    This is the only Unit call Tier 1 P2P sends make. The Idempotency-Key
    header means a network-retry of this exact call can never double-send.
    """
    payload = {
        "data": {
            "type": "bookPayment",
            "attributes": {
                "amount": amount_cents,
                "description": description[:80],  # Unit truncates descriptions; keep it predictable
            },
            "relationships": {
                "account": {"data": {"type": "depositAccount", "id": sender_account_id}},
                "counterpartyAccount": {"data": {"type": "depositAccount", "id": recipient_account_id}},
            },
        }
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.unit_base_url}/payments",
            json=payload,
            headers=_headers(idempotency_key=idempotency_key),
        )
        resp.raise_for_status()
        return resp.json()["data"]


async def create_virtual_card(unit_account_id: str, idempotency_key: str) -> dict:
    """Issue an individual virtual debit card on a deposit account.

    Deliberately the only card-creation path we expose — no full PAN/CVV
    retrieval anywhere in this service. Unit's "sensitive" card-details
    endpoint (full number, CVV) requires PCI-scoped handling we haven't
    built and don't need for an MVP; cardholders only ever see last4/status.
    """
    payload = {
        "data": {
            "type": "individualVirtualDebitCard",
            "attributes": {"idempotencyKey": idempotency_key},
            "relationships": {
                "account": {"data": {"type": "depositAccount", "id": unit_account_id}},
            },
        }
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.unit_base_url}/cards",
            json=payload,
            headers=_headers(idempotency_key=idempotency_key),
        )
        resp.raise_for_status()
        return resp.json()["data"]


def _card_summary(card: dict) -> dict:
    attrs = card.get("attributes", {})
    return {
        "id": card["id"],
        "last4Digits": attrs.get("last4Digits", ""),
        "expirationDate": attrs.get("expirationDate", ""),
        "status": attrs.get("status", ""),
        "createdAt": attrs.get("createdAt", ""),
    }


async def list_cards(unit_account_id: str) -> list:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{settings.unit_base_url}/cards",
            params={"filter[accountId]": unit_account_id},
            headers=_headers(),
        )
        resp.raise_for_status()
        return [_card_summary(c) for c in resp.json().get("data", [])]


async def get_card(unit_card_id: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{settings.unit_base_url}/cards/{unit_card_id}",
            headers=_headers(),
        )
        resp.raise_for_status()
        return _card_summary(resp.json()["data"])


async def freeze_card(unit_card_id: str, reason: str = "userRequested") -> dict:
    payload = {"data": {"type": "freezeCard", "attributes": {"reason": reason}}}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{settings.unit_base_url}/cards/{unit_card_id}/freeze",
            json=payload,
            headers=_headers(),
        )
        resp.raise_for_status()
        return _card_summary(resp.json()["data"])


async def unfreeze_card(unit_card_id: str) -> dict:
    payload = {"data": {"type": "unfreezeCard"}}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{settings.unit_base_url}/cards/{unit_card_id}/unfreeze",
            json=payload,
            headers=_headers(),
        )
        resp.raise_for_status()
        return _card_summary(resp.json()["data"])


async def create_ach_funding_payment(
    unit_account_id: str,
    routing_number: str,
    account_number: str,
    account_type: str,
    account_holder_name: str,
    amount_cents: int,
    idempotency_key: str,
) -> dict:
    """Pull money from an external bank account into a FAWN deposit
    account via ACH — "Add funds."

    Uses Unit's inline-counterparty ACH payment (no Plaid Link integration
    yet — that's the natural next step once there's a reason to add a
    second vendor dependency). direction="Credit" means the FAWN deposit
    account is credited; the external account is debited. ACH settles in
    days, not instantly, and can be returned by the sending bank — unlike
    a Book Payment this is never treated as instant or final.

    routing_number/account_number are sent directly to Unit and never
    persisted on our side — callers must not store them, only the last 4
    digits for the user's own reference (mirrors how SSN is handled).
    """
    payload = {
        "data": {
            "type": "achPayment",
            "attributes": {
                "amount": amount_cents,
                "direction": "Credit",
                "description": "FAWN Add Funds",
                "counterparty": {
                    "routingNumber": routing_number,
                    "accountNumber": account_number,
                    "accountType": account_type,
                    "name": account_holder_name,
                },
            },
            "relationships": {
                "account": {"data": {"type": "depositAccount", "id": unit_account_id}},
            },
        }
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.unit_base_url}/payments",
            json=payload,
            headers=_headers(idempotency_key=idempotency_key),
        )
        resp.raise_for_status()
        return resp.json()["data"]
