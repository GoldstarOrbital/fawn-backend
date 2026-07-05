from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from dependencies import get_current_user
from models import User
from schemas import AccountBalance
from services import stripe_baas as stripe_svc

router = APIRouter(prefix="/accounts", tags=["accounts"])


async def _finish_active_account(current_user: User, account: dict, db: Session):
    if not stripe_svc.account_is_active(account):
        return

    if not current_user.stripe_account_id:
        current_user.stripe_account_id = account.get("id")
    financial_account = await stripe_svc.create_financial_account(current_user.stripe_account_id)
    current_user.stripe_financial_account_id = financial_account["id"]
    db.commit()
    db.refresh(current_user)


async def _gather(*coros):
    import asyncio
    return await asyncio.gather(*coros)


@router.get("/balance", response_model=AccountBalance)
async def get_balance(current_user: User = Depends(get_current_user)):
    if not current_user.stripe_financial_account_id:
        raise HTTPException(status_code=404, detail="No bank account linked yet.")
    return await stripe_svc.get_account_balance(current_user.stripe_account_id, current_user.stripe_financial_account_id)


@router.get("/details")
async def get_account_details(current_user: User = Depends(get_current_user)):
    if not current_user.stripe_financial_account_id:
        raise HTTPException(status_code=404, detail="No bank account linked yet.")
    return await stripe_svc.get_financial_account_details(current_user.stripe_account_id, current_user.stripe_financial_account_id)


@router.get("/dashboard")
async def get_dashboard(current_user: User = Depends(get_current_user)):
    application_pending = bool(
        getattr(current_user, "stripe_account_id", None)
        and not current_user.stripe_financial_account_id
    )

    if not current_user.stripe_financial_account_id:
        return {
            "account_active": False,
            "application_pending": application_pending,
            "balance": None,
            "account_details": None,
            "transactions": [],
        }

    account_id = current_user.stripe_account_id
    financial_account_id = current_user.stripe_financial_account_id
    balance, details, transactions = await _gather(
        stripe_svc.get_account_balance(account_id, financial_account_id),
        stripe_svc.get_financial_account_details(account_id, financial_account_id),
        stripe_svc.list_transactions(account_id, financial_account_id, limit=10),
    )

    return {
        "account_active": True,
        "application_pending": False,
        "balance": balance,
        "account_details": details,
        "transactions": transactions,
    }


@router.post("/refresh-application-status")
async def refresh_application_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Poll Stripe for Connect account KYC/capability approval and finish setup."""
    if current_user.stripe_account_id and not current_user.stripe_financial_account_id:
        try:
            account = await stripe_svc.get_account(current_user.stripe_account_id)
            await _finish_active_account(current_user, account, db)
        except Exception as e:
            print(f"[Stripe] refresh-application-status failed: {e}")

    application_pending = bool(
        getattr(current_user, "stripe_account_id", None)
        and not current_user.stripe_financial_account_id
    )

    return {
        "account_active": bool(current_user.stripe_financial_account_id),
        "application_pending": application_pending,
        "stripe_financial_account_id": current_user.stripe_financial_account_id,
    }


@router.post("/activate-sandbox")
async def activate_sandbox(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not (settings.stripe_secret_key or "").startswith("sk_test_"):
        raise HTTPException(
            status_code=403,
            detail="Sandbox-only endpoint - refusing to run against a non-test-mode Stripe key.",
        )

    if current_user.stripe_financial_account_id:
        return {
            "account_active": True,
            "application_pending": False,
            "stripe_financial_account_id": current_user.stripe_financial_account_id,
        }

    if not current_user.stripe_account_id:
        raise HTTPException(status_code=400, detail="No application on file - register first.")

    try:
        # Stripe test mode has no manual "force-approve" simulator endpoint —
        # test-mode Connect accounts activate automatically once required
        # fields are present. Just poll current status here.
        account = await stripe_svc.get_account(current_user.stripe_account_id)
        await _finish_active_account(current_user, account, db)
        if not current_user.stripe_financial_account_id:
            raise HTTPException(
                status_code=409,
                detail="Stripe account isn't active yet - complete the hosted onboarding form first.",
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Sandbox activation failed: {e}")

    return {
        "account_active": True,
        "application_pending": False,
        "stripe_financial_account_id": current_user.stripe_financial_account_id,
    }
