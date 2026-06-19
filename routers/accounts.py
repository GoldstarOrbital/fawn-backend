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
    """Return routing + account number for the user's deposit account."""
    if not current_user.unit_account_id:
        raise HTTPException(status_code=404, detail="No bank account linked yet.")
    return await unit_svc.get_account_details(current_user.unit_account_id)
