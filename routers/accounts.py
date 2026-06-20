from fastapi import APIRouter, Depends, HTTPException
from models import User
from schemas import AccountBalance
from dependencies import get_current_user
from services import unit as unit_svc

router = APIRouter(prefix="/accounts", tags=["accounts"])


@router.get("/balance", response_model=AccountBalance)
async def get_balance(current_user: User = Depends(get_current_user)):
    if not current_user.unit_account_id:
        raise HTTPException(status_code=404, detail="No bank account linked yet.")
    return await unit_svc.get_account_balance(current_user.unit_account_id)


@router.get("/details")
async def get_account_details(current_user: User = Depends(get_current_user)):
    if not current_user.unit_account_id:
        raise HTTPException(status_code=404, detail="No bank account linked yet.")
    return await unit_svc.get_account_details(current_user.unit_account_id)


@router.get("/dashboard")
async def get_dashboard(current_user: User = Depends(get_current_user)):
    """Single call: balance + account details + last 10 transactions.

    Returns application_pending=True when KYC is under manual review
    so the frontend can show a "your account is being reviewed" state.
    """
    application_pending = bool(
        getattr(current_user, "unit_application_id", None)
        and not current_user.unit_account_id
    )

    if not current_user.unit_account_id:
        return {
            "account_active": False,
            "application_pending": application_pending,
            "balance": None,
            "account_details": None,
            "transactions": [],
        }

    account_id = current_user.unit_account_id
    balance, details, transactions = await _gather(
        unit_svc.get_account_balance(account_id),
        unit_svc.get_account_details(account_id),
        unit_svc.list_transactions(account_id, limit=10),
    )

    return {
        "account_active": True,
        "application_pending": False,
        "balance": balance,
        "account_details": details,
        "transactions": transactions,
    }


async def _gather(*coros):
    import asyncio
    return await asyncio.gather(*coros)
