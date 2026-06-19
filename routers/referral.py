import random, string
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from database import get_db
from models import User
from dependencies import get_current_user

router = APIRouter(prefix="/referral", tags=["referral"])


def _gen_code(name: str) -> str:
    """Generate a readable referral code like ALEX-X7K2."""
    prefix = (name.split()[0][:4]).upper()
    suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"{prefix}-{suffix}"


@router.get("/code")
def get_or_create_code(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return (and lazily create) the user's referral code."""
    # Re-fetch user via this route's db session to avoid dual-session write issues
    user = db.query(User).filter(User.id == current_user.id).first()
    if not user.referral_code:
        code = _gen_code(user.full_name)
        # ensure uniqueness
        while db.query(User).filter(User.referral_code == code).first():
            code = _gen_code(user.full_name)
        user.referral_code = code
        db.commit()
        db.refresh(user)

    referred = db.query(User).filter(User.referred_by == user.referral_code).count()
    return {
        "code": user.referral_code,
        "invite_url": f"https://fawn.app/join?ref={user.referral_code}",
        "referrals": referred,
    }


class UseReferralRequest(BaseModel):
    code: str


@router.post("/use")
def use_referral(
    req: UseReferralRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Apply a referral code to the current user's account."""
    # Re-fetch user via this route's db session to avoid dual-session write issues
    user = db.query(User).filter(User.id == current_user.id).first()
    if user.referred_by:
        raise HTTPException(status_code=400, detail="Referral already applied.")
    inviter = db.query(User).filter(User.referral_code == req.code.upper()).first()
    if not inviter:
        raise HTTPException(status_code=404, detail="Referral code not found.")
    if inviter.id == user.id:
        raise HTTPException(status_code=400, detail="Can't refer yourself.")

    user.referred_by = req.code.upper()
    db.commit()
    return {"message": f"Referral applied! Thanks for joining via {inviter.full_name.split()[0]}'s invite."}
