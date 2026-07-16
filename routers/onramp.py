"""Buy Crypto — multi-provider fiat-to-USDC on-ramp.

Aggregates Ramp, Coinbase Onramp, MoonPay, and Transak behind one Add
Funds modal on the frontend. Three of the four (Ramp/MoonPay/Transak)
embed directly with a public client-side key; /config exposes exactly
which ones are configured so the frontend only renders live tabs.

Coinbase Onramp is the exception — it needs a server-signed session
token per purchase (see services/coinbase_onramp.py), which is what
/coinbase/session-token exists for.

None of these providers ever touch a FAWN-held bank account or hold a
KYC relationship with FAWN itself — each purchase is between the user
and the on-ramp provider directly; FAWN only receives the resulting
USDC on-chain, same as any other incoming transfer.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from models import User
from dependencies import get_current_user
from services import coinbase_onramp as coinbase_svc
from config import settings

router = APIRouter(prefix="/onramp", tags=["onramp"])


@router.get("/config")
async def get_onramp_config():
    """Public client-side config: which providers are live, and their
    publishable/host keys. Safe to expose — none of these are secrets."""
    return {
        "ramp": {
            "enabled": bool(settings.ramp_host_app_id),
            "host_app_id": settings.ramp_host_app_id or None,
        },
        "coinbase": {
            "enabled": bool(
                settings.coinbase_onramp_project_id
                and settings.coinbase_cdp_api_key_name
                and settings.coinbase_cdp_api_key_secret
            ),
            "project_id": settings.coinbase_onramp_project_id or None,
        },
        "moonpay": {
            "enabled": bool(settings.moonpay_api_key),
            "api_key": settings.moonpay_api_key or None,
        },
        "transak": {
            "enabled": bool(settings.transak_api_key),
            "api_key": settings.transak_api_key or None,
            "environment": settings.transak_env,
        },
    }


class CoinbaseSessionRequest(BaseModel):
    pass


@router.post("/coinbase/session-token")
async def create_coinbase_session_token(
    req: CoinbaseSessionRequest,
    current_user: User = Depends(get_current_user),
):
    """Mint a Coinbase Onramp session token scoped to the caller's own
    FAWN wallet address."""
    if not current_user.crypto_wallet_address:
        raise HTTPException(
            status_code=404,
            detail="No stablecoin wallet. Create a wallet before buying crypto.",
        )
    try:
        return await coinbase_svc.create_session_token(current_user.crypto_wallet_address)
    except coinbase_svc.CoinbaseOnrampNotConfigured:
        raise HTTPException(status_code=503, detail="Buying crypto via Coinbase isn't available yet.")
    except coinbase_svc.CoinbaseOnrampError as e:
        raise HTTPException(status_code=502, detail=f"Coinbase Onramp error: {e}")
