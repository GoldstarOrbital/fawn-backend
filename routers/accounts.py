from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from database import get_db
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


@router.post("/refresh-application-status")
async def refresh_application_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Poll Unit for a pending KYC application and, if now approved,
    finish account setup (create customer/deposit account) the same way
    register() does. Used to unstick users whose manual KYC review later
    approves them, without requiring a new registration.

    Frontend: dashboard.html should poll this endpoint on load when
    application_pending is true, to unstick users whose Unit KYC review
    later approves them.
    """
    if current_user.unit_application_id and not current_user.unit_account_id:
        try:
            application = await unit_svc.get_application(current_user.unit_application_id)
            app_status = application.get("attributes", {}).get("status", "pending")

            if app_status == "approved":
                relationships = application.get("relationships", {})
                customer_data = relationships.get("customer", {}).get("data", {})
                unit_customer_id = customer_data.get("id")
                if unit_customer_id:
                    current_user.unit_customer_id = unit_customer_id
                    account = await unit_svc.create_deposit_account(unit_customer_id)
                    current_user.unit_account_id = account["id"]
                    db.commit()
                    db.refresh(current_user)
            # pending/manual or any other non-approved status — fall through, no error
        except Exception as e:
            print(f"[Unit] refresh-application-status failed: {e}")
            # Don't error the request — just fall through with current state

    application_pending = bool(
        getattr(current_user, "unit_application_id", None)
        and not current_user.unit_account_id
    )

    return {
        "account_active": bool(current_user.unit_account_id),
        "application_pending": application_pending,
        "unit_account_id": current_user.unit_account_id,
    }
