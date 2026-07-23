import json
import logging
import random
import string
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from config import settings
from database import get_db
from models import User, UserAuditLog
from dependencies import get_current_user

router = APIRouter(prefix="/referral", tags=["referral"])
logger = logging.getLogger(__name__)

REFERRAL_BONUS_ACTION = "referral_bonus_paid"


def _gen_code(name: str) -> str:
    """Generate a readable referral code like ALEX-X7K2."""
    prefix = (name.split()[0][:4]).upper()
    suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    return f"{prefix}-{suffix}"


def _pay_bonus(db: Session, payee: User, role: str, counterparty_id: str, code: str) -> int:
    """Credit the referral bonus to `payee`'s USDC ledger balance.

    Returns the amount credited in cents (0 if rewards are disabled).
    The credit is audit-logged so total earnings are reconstructable and the
    payout itself is idempotent per (payee, counterparty) pair.
    """
    bonus = settings.referral_bonus_cents
    if not settings.referral_rewards_enabled or bonus <= 0:
        return 0

    payee.usdc_balance_cents = (payee.usdc_balance_cents or 0) + bonus
    db.add(UserAuditLog(
        user_id=payee.id,
        action=REFERRAL_BONUS_ACTION,
        details=json.dumps({
            "amount_cents": bonus,
            "role": role,  # "inviter" | "invitee"
            "counterparty_user_id": counterparty_id,
            "code": code,
        }),
        retention_expires_at=datetime.now(tz=timezone.utc) + timedelta(days=365 * 7),
    ))
    return bonus


def _total_earned_cents(db: Session, user_id: str) -> int:
    """Sum of all referral bonuses ever credited to this user."""
    logs = db.query(UserAuditLog).filter(
        UserAuditLog.user_id == user_id,
        UserAuditLog.action == REFERRAL_BONUS_ACTION,
    ).all()
    total = 0
    for log in logs:
        try:
            total += int(json.loads(log.details).get("amount_cents", 0))
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    return total


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
        "rewards_enabled": settings.referral_rewards_enabled,
        "bonus_cents_per_referral": settings.referral_bonus_cents if settings.referral_rewards_enabled else 0,
        "total_earned_cents": _total_earned_cents(db, user.id),
    }


class UseReferralRequest(BaseModel):
    code: str


@router.post("/use")
def use_referral(
    req: UseReferralRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Apply a referral code to the current user's account.

    When rewards are enabled, BOTH sides are credited immediately: the
    inviter and the new user each receive `settings.referral_bonus_cents`
    on their USDC ledger balance. `referred_by` is set in the same
    transaction, which makes the payout once-per-account by construction.
    """
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
    inviter.referral_count = (inviter.referral_count or 0) + 1

    inviter_bonus = _pay_bonus(db, inviter, "inviter", user.id, req.code.upper())
    invitee_bonus = _pay_bonus(db, user, "invitee", inviter.id, req.code.upper())

    db.commit()

    if invitee_bonus:
        logger.info(
            "[referral] code %s applied: inviter %s +%s cents, invitee %s +%s cents",
            req.code.upper(), inviter.id, inviter_bonus, user.id, invitee_bonus,
        )
        first_name = inviter.full_name.split()[0]
        return {
            "message": (
                f"Referral applied! You and {first_name} each earned "
                f"${invitee_bonus / 100:.2f} in USDC."
            ),
            "bonus_cents": invitee_bonus,
        }
    return {
        "message": f"Referral applied! Thanks for joining via {inviter.full_name.split()[0]}'s invite.",
        "bonus_cents": 0,
    }
