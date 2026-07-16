from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from dependencies import get_current_user
from models import User
from services.username_service import update_username, is_valid_username
from pydantic import BaseModel

router = APIRouter(prefix="/accounts", tags=["accounts"])


# ── USERNAME MANAGEMENT ──

class UsernameUpdate(BaseModel):
    username: str


@router.get("/username")
async def get_username(current_user: User = Depends(get_current_user)):
    """Get current user's username."""
    return {
        "username": current_user.username,
        "available": bool(current_user.username),
    }


@router.put("/username")
async def update_user_username(
    req: UsernameUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update current user's username."""
    success, message = update_username(db, current_user, req.username)
    if not success:
        raise HTTPException(status_code=400, detail=message)
    return {
        "username": current_user.username,
        "message": message,
    }


@router.get("/check-username/{username}")
async def check_username_available(username: str, db: Session = Depends(get_db)):
    """Check if a username is available."""
    if not is_valid_username(username):
        return {
            "available": False,
            "reason": "Invalid format (3-30 chars, lowercase, letters/numbers/underscore)",
        }

    existing = db.query(User).filter(User.username.ilike(username)).first()
    if existing:
        return {"available": False, "reason": "Username taken"}

    return {"available": True}
