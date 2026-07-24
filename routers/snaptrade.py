"""Optional SnapTrade brokerage connections for FAWN users."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from dependencies import get_current_user
from models import User, SnapTradeUser
from services import snaptrade as snaptrade_svc

router = APIRouter(prefix="/brokerage", tags=["brokerage"])


class PortalRequest(BaseModel):
    redirect_uri: str | None = Field(default=None, max_length=500)


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, snaptrade_svc.SnapTradeNotConfigured):
        return HTTPException(status_code=503, detail="Brokerage connections are not configured yet.")
    if isinstance(exc, snaptrade_svc.SnapTradeError):
        return HTTPException(status_code=503, detail=str(exc))
    return HTTPException(status_code=502, detail="Brokerage provider error.")


def _get_or_create(current_user: User, db: Session) -> SnapTradeUser:
    row = db.query(SnapTradeUser).filter(SnapTradeUser.user_id == current_user.id).first()
    if row:
        return row
    row = SnapTradeUser(user_id=current_user.id, snaptrade_user_id=f"fawn-{current_user.id}", encrypted_user_secret=b"pending", status="provisioning")
    db.add(row)
    db.flush()
    return row


async def _ensure_user(current_user: User, db: Session) -> tuple[SnapTradeUser, str]:
    row = _get_or_create(current_user, db)
    if row.status == "active":
        return row, snaptrade_svc.decrypt_user_secret(row.encrypted_user_secret)
    try:
        created = await snaptrade_svc.register_user(row.snaptrade_user_id)
        secret = created.get("userSecret") or created.get("user_secret")
        if not secret:
            raise snaptrade_svc.SnapTradeError("SnapTrade did not return a user secret.")
        row.encrypted_user_secret = snaptrade_svc.encrypt_user_secret(secret)
        row.status = "active"
        db.commit()
        return row, secret
    except Exception:
        db.rollback()
        raise


def _safe_connection(item: dict) -> dict:
    brokerage = item.get("brokerage") or {}
    return {"id": item.get("id"), "institution_name": brokerage.get("name") or brokerage.get("display_name") or "Brokerage", "status": item.get("status") or ("disabled" if item.get("disabled") else "active"), "created_date": item.get("created_date")}


@router.get("/status")
def status(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.query(SnapTradeUser).filter(SnapTradeUser.user_id == current_user.id).first()
    return {"configured": bool(row and row.status == "active"), "status": row.status if row else "not_connected"}


@router.post("/connect")
async def connect(req: PortalRequest, request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        row, secret = await _ensure_user(current_user, db)
        portal = await snaptrade_svc.create_portal(row.snaptrade_user_id, secret, req.redirect_uri)
        return {"redirect_uri": portal.get("redirectURI") or portal.get("redirect_uri"), "session_id": portal.get("sessionId") or portal.get("session_id")}
    except Exception as exc:
        raise _error(exc)


@router.get("/connections")
async def connections(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        row, secret = await _ensure_user(current_user, db)
        return {"connections": [_safe_connection(item) for item in await snaptrade_svc.list_connections(row.snaptrade_user_id, secret)]}
    except Exception as exc:
        raise _error(exc)


@router.get("/accounts")
async def accounts(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        row, secret = await _ensure_user(current_user, db)
        items = await snaptrade_svc.list_accounts(row.snaptrade_user_id, secret)
        return {"accounts": [{"id": i.get("id"), "name": i.get("name"), "institution_name": i.get("institution_name"), "number": i.get("number"), "type": i.get("type") or (i.get("meta") or {}).get("type")} for i in items]}
    except Exception as exc:
        raise _error(exc)
