from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from dependencies import get_current_user
from models import User
from schemas import AccountBalance
from services import unit as unit_svc

router = APIRouter(prefix="/accounts", tags=["accounts"])


def _is_approved_unit_status(status: str | None) -> bool:
    return (status or "").lower() == "approved"


async def _finish_approved_application(current_user: User, application: dict, db: Session):
    app_status = application.get("attributes", {}).get("status", "pending")
    if not _is_approved_unit_status(app_status):
        return

    relationships = application.get("relationships", {})
    customer_data = relationships.get("customer", {}).get("data", {})
    unit_customer_id = customer_data.get("id")
    if not unit_customer_id:
        return

    current_user.unit_customer_id = unit_customer_id
    if not current_user.unit_application_id:
        current_user.unit_application_id = application.get("id")
    account = await unit_svc.create_deposit_account(unit_customer_id)
    current_user.unit_account_id = account["id"]
    db.commit()
    db.refresh(current_user)


def _application_from_form_response(form_response: dict) -> dict | None:
    data = form_response.get("data", {})
    relationship_id = (
        data.get("relationships", {})
        .get("application", {})
        .get("data", {})
        .get("id")
    )
    for item in form_response.get("included", []) or []:
        if item.get("id") == relationship_id:
            return item
    if relationship_id:
        return {"id": relationship_id}
    return None


async def _gather(*coros):
    import asyncio
    return await asyncio.gather(*coros)


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


@router.post("/refresh-application-status")
async def refresh_application_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Poll Unit for direct or hosted-form KYC approval and finish setup."""
    if current_user.unit_application_id and not current_user.unit_account_id:
        try:
            application = await unit_svc.get_application(current_user.unit_application_id)
            await _finish_approved_application(current_user, application, db)
        except Exception as e:
            print(f"[Unit] refresh-application-status failed: {e}")

    if (
        not current_user.unit_account_id
        and getattr(current_user, "unit_application_form_id", None)
    ):
        try:
            form_response = await unit_svc.get_application_form(current_user.unit_application_form_id)
            form_application = _application_from_form_response(form_response)
            if form_application:
                application_id = form_application.get("id")
                current_user.unit_application_id = application_id
                application = (
                    form_application
                    if form_application.get("relationships", {}).get("customer")
                    else await unit_svc.get_application(application_id)
                )
                await _finish_approved_application(current_user, application, db)
        except Exception as e:
            print(f"[Unit] refresh hosted application form failed: {e}")

    application_pending = bool(
        getattr(current_user, "unit_application_id", None)
        and not current_user.unit_account_id
    )

    return {
        "account_active": bool(current_user.unit_account_id),
        "application_pending": application_pending,
        "unit_account_id": current_user.unit_account_id,
    }


@router.post("/activate-sandbox")
async def activate_sandbox(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if "s.unit.sh" not in settings.unit_base_url:
        raise HTTPException(
            status_code=403,
            detail="Sandbox-only endpoint - refusing to run against a non-sandbox Unit environment.",
        )

    if current_user.unit_account_id:
        return {"account_active": True, "application_pending": False, "unit_account_id": current_user.unit_account_id}

    if not current_user.unit_application_id:
        raise HTTPException(status_code=400, detail="No application on file - register first.")

    try:
        await unit_svc.approve_application_sandbox(current_user.unit_application_id)
        application = await unit_svc.get_application(current_user.unit_application_id)
        relationships = application.get("relationships", {})
        customer_data = relationships.get("customer", {}).get("data", {})
        unit_customer_id = customer_data.get("id")
        if not unit_customer_id:
            raise HTTPException(status_code=502, detail="Unit approved the application but returned no customer id.")

        current_user.unit_customer_id = unit_customer_id
        account = await unit_svc.create_deposit_account(unit_customer_id)
        current_user.unit_account_id = account["id"]
        db.commit()
        db.refresh(current_user)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Sandbox activation failed: {e}")

    return {"account_active": True, "application_pending": False, "unit_account_id": current_user.unit_account_id}
