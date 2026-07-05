"""Stripe Connect + Treasury onboarding: hosted KYC application flow.

See services/stripe_baas.py for the underlying Connect Account + Account
Link calls this wraps. The user enters SSN/DOB/address inside Stripe's own
hosted flow instead of FAWN collecting it; FAWN only ever receives the
Connect account id back, plus (once webhooks confirm activation) a
Treasury Financial Account id.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from dependencies import get_current_user
from models import User
from schemas import StripeOnboardingResponse
from services import stripe_baas as stripe_svc

router = APIRouter(prefix="/stripe", tags=["stripe-onboarding"])


@router.post("/onboarding", response_model=StripeOnboardingResponse)
async def create_onboarding(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create (or resume) a Stripe-hosted Connect onboarding flow for the current user."""
    if current_user.stripe_financial_account_id:
        raise HTTPException(status_code=409, detail="Bank account is already active.")
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=503, detail="Stripe API key is not configured.")

    account_id = current_user.stripe_account_id
    if not account_id:
        try:
            account = await stripe_svc.create_connect_account_stub(
                full_name=current_user.full_name,
                email=current_user.email,
                phone=current_user.phone or "",
                occupation="Student" if current_user.is_student else "",
            )
        except Exception as e:
            print(f"[Stripe] connect account creation failed: {e}")
            raise HTTPException(status_code=502, detail="Could not start Stripe onboarding.")
        account_id = account.get("id")
        current_user.stripe_account_id = account_id
        db.commit()
        db.refresh(current_user)

    try:
        link = await stripe_svc.create_account_onboarding_link(
            account_id=account_id,
            refresh_url=settings.stripe_onboarding_refresh_url,
            return_url=settings.stripe_onboarding_return_url,
        )
    except Exception as e:
        print(f"[Stripe] account link creation failed: {e}")
        raise HTTPException(status_code=502, detail="Could not create the Stripe onboarding link.")

    onboarding_url = link.get("url", "")
    if not onboarding_url:
        raise HTTPException(status_code=502, detail="Stripe did not return an onboarding URL.")

    return StripeOnboardingResponse(onboarding_url=onboarding_url, stripe_account_id=account_id)
