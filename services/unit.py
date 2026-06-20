"""
All Unit BaaS API calls.
Sandbox:    https://api.s.unit.sh
Production: https://api.unit.co
Switch via UNIT_BASE_URL env var on Railway.
"""
import re
import httpx
from config import settings


def _headers():
    return {
        "Authorization": f"Bearer {settings.unit_api_token}",
        "Content-Type": "application/vnd.api+json",
    }


async def create_application(
    full_name: str,
    email: str,
    phone: str,
    ssn: str,
    date_of_birth: str,
    address: "schemas.Address",  # type: ignore[name-defined]
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
