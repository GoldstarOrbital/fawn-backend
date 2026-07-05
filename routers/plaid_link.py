"""Plaid bank-linking endpoints (funding-source switching).

Flow: client asks for a link_token, opens Plaid Link, and posts back the
public_token; we exchange it for a permanent access_token stored on a
PlaidItem row. Later, funding pulls the routing/account numbers from Plaid
just-in-time and forwards them to the banking provider — only the last-4
mask is ever persisted here.

Guarded in services/plaid.py: unconfigured Plaid returns 503.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import User, PlaidItem
from dependencies import get_current_user
from services import plaid as plaid_svc

router = APIRouter(prefix="/plaid", tags=["plaid"])


class ExchangeRequest(BaseModel):
    public_token: str


def _svc_error(e: Exception) -> HTTPException:
    if isinstance(e, plaid_svc.PlaidNotConfigured):
        return HTTPException(status_code=503, detail="Bank linking isn't available yet.")
    return HTTPException(status_code=502, detail=f"Plaid error: {e}")


@router.post("/link-token")
async def create_link_token(current_user: User = Depends(get_current_user)):
    try:
        return await plaid_svc.create_link_token(current_user.id)
    except Exception as e:
        raise _svc_error(e)


@router.post("/exchange", status_code=201)
async def exchange(req: ExchangeRequest, current_user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    try:
        exchanged = await plaid_svc.exchange_public_token(req.public_token)
    except Exception as e:
        raise _svc_error(e)

    item_id = exchanged["item_id"]
    existing = db.query(PlaidItem).filter(PlaidItem.item_id == item_id).first()
    if existing:
        # Re-linking the same institution — refresh the token, keep one row.
        existing.access_token = exchanged["access_token"]
        existing.status = "active"
        db.commit()
        item = existing
    else:
        item = PlaidItem(user_id=current_user.id, item_id=item_id,
                         access_token=exchanged["access_token"])
        db.add(item)
        db.commit()

    # Best-effort enrich display metadata; never fail the link on this.
    try:
        auth = await plaid_svc.get_auth(item.access_token)
        item.account_mask = auth.get("mask") or auth.get("account_number", "")[-4:]
        item.institution_name = auth.get("account_name", "")
        db.commit()
    except Exception:
        pass

    return {"item_id": item.item_id, "institution_name": item.institution_name,
            "account_mask": item.account_mask, "status": item.status}


@router.get("/items")
def list_items(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    items = db.query(PlaidItem).filter(
        PlaidItem.user_id == current_user.id, PlaidItem.status == "active"
    ).all()
    return {"items": [
        {"item_id": i.item_id, "institution_name": i.institution_name,
         "account_mask": i.account_mask} for i in items
    ]}
