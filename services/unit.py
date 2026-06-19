"""
All Unit BaaS API calls.
Sandbox base URL: https://api.s.unit.sh
Docs: https://docs.unit.co
"""
import httpx
from config import settings

def _headers():
    return {
        "Authorization": f"Bearer {settings.unit_api_token}",
        "Content-Type": "application/vnd.api+json",
    }


async def create_application(full_name: str, email: str, phone: str) -> dict:
    """
    Submit an individual application to Unit.
    In sandbox, SSN 721074426 always returns approved instantly.
    Returns the application data dict.
    """
    first, *rest = full_name.strip().split()
    last = " ".join(rest) if rest else "Unknown"

    payload = {
        "data": {
            "type": "individualApplication",
            "attributes": {
                "ssn": "721074426",
                "fullName": {"first": first, "last": last},
                "dateOfBirth": "2000-01-01",
                "address": {
                    "street": "123 Main St",
                    "city": "San Francisco",
                    "state": "CA",
                    "postalCode": "94105",
                    "country": "US",
                },
                "email": email,
                "occupation": "Student",
                "phone": {
                    "countryCode": "1",
                    "number": (phone or "5555550100").replace("-", "").replace(" ", "")[:10],
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


async def create_deposit_account(unit_customer_id: str) -> dict:
    """Open a checking deposit account for an existing Unit customer."""
    payload = {
        "data": {
            "type": "depositAccount",
            "attributes": {"depositProduct": "checking"},
            "relationships": {
                "customer": {"data": {"type": "individualCustomer", "id": unit_customer_id}}
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
    """List all accounts for a Unit customer."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{settings.unit_base_url}/accounts",
            params={"filter[customerId]": unit_customer_id},
            headers=_headers(),
        )
        resp.raise_for_status()
        return resp.json().get("data", [])


async def get_account_details(unit_account_id: str) -> dict:
    """Return routing number, account number, and account name."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{settings.unit_base_url}/accounts/{unit_account_id}",
            headers=_headers(),
        )
        resp.raise_for_status()
        attrs = resp.json()["data"]["attributes"]
        return {
            "account_id": unit_account_id,
            "routing_number": attrs.get("routingNumber", ""),
            "account_number": attrs.get("accountNumber", ""),
            "account_name": attrs.get("name", ""),
            "status": attrs.get("status", ""),
            "deposit_product": attrs.get("depositProduct", "checking"),
        }


async def get_account_balance(unit_account_id: str) -> dict:
    """Fetch live balance for a Unit deposit account."""
    async with httpx.AsyncClient(timeout=30) as client:
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
    """Fetch recent transactions for a Unit account."""
    async with httpx.AsyncClient(timeout=30) as client:
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
            "amount": round((amount_cents / 100) * direction, 2),
            "description": a.get("description", ""),
            "date": a.get("createdAt", "")[:10],
            "status": a.get("status", ""),
        })
    return transactions
