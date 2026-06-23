import hashlib
import os
import secrets

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, timedelta, timezone
from jose import jwt
import bcrypt
from slowapi import Limiter
from slowapi.util import get_remote_address

from database import get_db
from models import User, PasswordResetToken
from schemas import (
    RegisterRequest,
    LoginRequest,
    TokenResponse,
    UserResponse,
    ForgotPasswordRequest,
    ResetPasswordRequest,
    UpdateMeRequest,
)
from config import settings
from dependencies import get_current_user
from services import unit as unit_svc

router = APIRouter(prefix="/auth", tags=["auth"])
limiter = Limiter(key_func=get_remote_address)

RESET_LINK_EXPIRY_MINUTES = 30
RESET_LINK_BASE = "https://goldstarorbital.github.io/fawn-landing/reset-password.html"


def _reset_token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _send_reset_email(email: str, raw_token: str) -> bool:
    api_key = os.environ.get("RESEND_API_KEY", "") or settings.resend_api_key
    if not api_key:
        return False
    link = f"{RESET_LINK_BASE}?token={raw_token}"
    html = f"""
    <div style="font-family:-apple-system,sans-serif;max-width:480px;padding:32px;background:#0a0a0a;color:#f0f0f0;border-radius:16px;">
      <h2 style="color:#00c896;margin:0 0 8px;">Reset your FAWN password</h2>
      <p style="color:#888;margin:0 0 24px;font-size:0.9rem;">This link expires in {RESET_LINK_EXPIRY_MINUTES} minutes.</p>
      <a href="{link}" style="display:inline-block;background:#00c896;color:#000;font-weight:700;text-decoration:none;padding:14px 28px;border-radius:8px;font-size:0.95rem;">
        Reset my password →
      </a>
      <p style="margin-top:24px;font-size:0.75rem;color:#444;">
        If you didn't request this, ignore this email. Link works once.
      </p>
    </div>
    """
    try:
        r = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "from": f"Alex at FAWN <{settings.from_email}>",
                "to": [email],
                "subject": "Reset your FAWN password",
                "html": html,
            },
            timeout=10.0,
        )
        return r.status_code in (200, 201)
    except Exception:
        return False


def _hash(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def _verify(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())

def _make_token(user_id: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.jwt_expire_minutes)
    return jwt.encode(
        {"sub": user_id, "exp": expire},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


@router.post("/register", response_model=TokenResponse, status_code=201)
@limiter.limit("5/minute")
async def register(request: Request, req: RegisterRequest, db: Session = Depends(get_db)):
    if db.query(User).filter(func.lower(User.email) == req.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(
        email=req.email,
        hashed_password=_hash(req.password),
        full_name=req.full_name,
        phone=req.phone,
        is_student=req.is_student,
        school=req.school,
    )
    db.add(user)
    db.flush()

    # Submit KYC application to Unit.
    # SSN is used here and immediately discarded — never persisted.
    unit_token_set = settings.unit_api_token not in ("UNIT_TOKEN_NOT_SET", "")
    if unit_token_set:
        try:
            application = await unit_svc.create_application(
                full_name=req.full_name,
                email=req.email,
                phone=req.phone,
                ssn=req.ssn,          # real SSN, not stored after this line
                date_of_birth=req.date_of_birth,
                address=req.address,
                occupation=req.occupation,
            )
            app_status = application.get("attributes", {}).get("status", "pending")
            app_id = application.get("id")

            if app_status == "approved":
                relationships = application.get("relationships", {})
                customer_data = relationships.get("customer", {}).get("data", {})
                unit_customer_id = customer_data.get("id")
                if unit_customer_id:
                    user.unit_customer_id = unit_customer_id
                    account = await unit_svc.create_deposit_account(unit_customer_id)
                    user.unit_account_id = account["id"]
            else:
                # pending/manual — store application id so we can poll later
                user.unit_application_id = app_id

        except Exception as e:
            print(f"[Unit] KYC application failed: {e}")
            # Don't block registration — account creation will retry via webhook

    try:
        db.commit()
        db.refresh(user)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="Registration failed. Please try again.")

    return TokenResponse(access_token=_make_token(user.id))


@router.post("/login", response_model=TokenResponse)
@limiter.limit("10/minute")
def login(request: Request, req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(func.lower(User.email) == req.email).first()
    if not user or not _verify(req.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return TokenResponse(access_token=_make_token(user.id))


@router.post("/token", response_model=TokenResponse, include_in_schema=False)
def token(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(func.lower(User.email) == form.username.lower()).first()
    if not user or not _verify(form.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return TokenResponse(access_token=_make_token(user.id))


@router.get("/me", response_model=UserResponse)
def me(current_user: User = Depends(get_current_user)):
    return UserResponse.from_orm_user(current_user)


@router.patch("/me", response_model=UserResponse)
def update_me(
    req: UpdateMeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if req.school is not None:
        current_user.school = req.school
    db.commit()
    db.refresh(current_user)
    return UserResponse.from_orm_user(current_user)


@router.post("/forgot-password")
@limiter.limit("5/minute")
def forgot_password(request: Request, req: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """Request a password reset link. Always returns the same response,
    regardless of whether the email exists, to prevent email enumeration."""
    user = db.query(User).filter(func.lower(User.email) == req.email).first()
    if user:
        raw_token = secrets.token_urlsafe(32)
        token_record = PasswordResetToken(
            user_id=user.id,
            token_hash=_reset_token_hash(raw_token),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=RESET_LINK_EXPIRY_MINUTES),
        )
        db.add(token_record)
        db.commit()
        _send_reset_email(user.email, raw_token)

    return {"message": "If that email is registered, a reset link is on its way."}


@router.post("/reset-password")
@limiter.limit("10/minute")
def reset_password(request: Request, req: ResetPasswordRequest, db: Session = Depends(get_db)):
    """Exchange a valid, unused reset token for a new password."""
    token_hash = _reset_token_hash(req.token)
    record = (
        db.query(PasswordResetToken)
        .filter(PasswordResetToken.token_hash == token_hash, PasswordResetToken.used == False)
        .first()
    )
    if not record:
        raise HTTPException(status_code=400, detail="Invalid or already-used reset link.")

    expires = record.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires:
        raise HTTPException(status_code=400, detail="Reset link expired — request a new one.")

    user = db.query(User).filter(User.id == record.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    user.hashed_password = _hash(req.new_password)
    record.used = True
    db.commit()

    return {"message": "Password updated. You can now log in."}
