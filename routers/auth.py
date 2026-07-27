import hashlib
import os
import secrets

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, timedelta, timezone
from jose import jwt, jwk
import bcrypt
from database import get_db
from models import User, PasswordResetToken, SocialIdentity
from schemas import (
    RegisterRequest,
    LoginRequest,
    TokenResponse,
    UserResponse,
    ForgotPasswordRequest,
    ResetPasswordRequest,
    ChangePasswordRequest,
    UpdateMeRequest,
    SocialLoginRequest,
)
from config import settings
from dependencies import get_current_user
from services.crypto_wallet import create_wallet, WalletNotInitialized
from services.username_service import assign_username_to_user, is_valid_username
from rate_limiting import limiter

router = APIRouter(prefix="/auth", tags=["auth"])

RESET_LINK_EXPIRY_MINUTES = 30
RESET_LINK_BASE = "https://goldstarorbital.github.io/fawn-landing/reset-password.html"


def _password_contains_username(password: str, username: str) -> bool:
    password_lower = password.casefold()
    username_lower = username.casefold()
    parts = [part for part in username_lower.split("_") if len(part) >= 3]
    return any(part in password_lower for part in [username_lower, *parts])


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
        if r.status_code not in (200, 201):
            print(f"[auth] password reset email to {email} failed: {r.status_code} {r.text[:300]}")
            return False
        return True
    except Exception as e:
        print(f"[auth] password reset email to {email} raised: {e}")
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


async def _verify_social_token(provider: str, raw_token: str) -> dict:
    """Verify a provider-signed ID token and return only trusted claims."""
    if not raw_token or len(raw_token) > 16_000:
        raise HTTPException(status_code=400, detail="Invalid social sign-in token")
    if provider == "google":
        issuer = "https://accounts.google.com"
        audience = settings.google_oauth_client_id
        jwks_url = "https://www.googleapis.com/oauth2/v3/certs"
    else:
        issuer = "https://appleid.apple.com"
        audience = settings.apple_oauth_client_id
        jwks_url = "https://appleid.apple.com/auth/keys"
    if not audience:
        raise HTTPException(status_code=503, detail=f"{provider.title()} sign-in is not configured yet")
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            jwks = (await client.get(jwks_url)).json().get("keys", [])
        header = jwt.get_unverified_header(raw_token)
        key_data = next((key for key in jwks if key.get("kid") == header.get("kid")), None)
        if not key_data:
            raise ValueError("Unknown signing key")
        claims = jwt.decode(raw_token, jwk.construct(key_data), algorithms=["RS256"], audience=audience, issuer=issuer)
    except Exception:
        raise HTTPException(status_code=401, detail="Social sign-in could not be verified")
    email = (claims.get("email") or "").strip().lower()
    subject = str(claims.get("sub") or "")
    if not email or not subject or claims.get("email_verified") not in (True, "true"):
        raise HTTPException(status_code=401, detail="A verified email is required for social sign-in")
    return {"email": email, "subject": subject, "full_name": claims.get("name") or claims.get("email", "").split("@")[0], "avatar_url": claims.get("picture")}


@router.post("/social", response_model=TokenResponse)
@limiter.limit("10/minute")
async def social_login(request: Request, req: SocialLoginRequest, db: Session = Depends(get_db)):
    """Sign in or create an account from a verified Google/Apple ID token.

    A new social account must still choose a unique FAWN username. Existing
    password accounts with the same verified email may sign in, but the social
    identity is linked only after that email match is established.
    """
    claims = await _verify_social_token(req.provider, req.id_token)
    identity = db.query(SocialIdentity).filter(
        SocialIdentity.provider == req.provider,
        SocialIdentity.subject == claims["subject"],
    ).first()
    if identity:
        return TokenResponse(access_token=_make_token(identity.user_id))

    user = db.query(User).filter(func.lower(User.email) == claims["email"]).first()
    if not user:
        if not req.username:
            raise HTTPException(status_code=409, detail="Choose a username to finish creating your FAWN account")
        if not is_valid_username(req.username) or db.query(User).filter(User.username.ilike(req.username)).first():
            raise HTTPException(status_code=400, detail="That username is unavailable")
        user = User(
            email=claims["email"], hashed_password=_hash(secrets.token_urlsafe(32)),
            full_name=(req.full_name or claims["full_name"])[:200], is_student=req.is_student,
            school=req.school, avatar_url=claims.get("avatar_url"),
        )
        db.add(user)
        db.flush()
        if not assign_username_to_user(db, user, req.username, commit=False):
            db.rollback()
            raise HTTPException(status_code=400, detail="That username is unavailable")
        await create_wallet(user.id, db, wallet_type="fawn_custodial")
    db.add(SocialIdentity(user_id=user.id, provider=req.provider, subject=claims["subject"], email=claims["email"]))
    db.commit()
    return TokenResponse(access_token=_make_token(user.id))


@router.post("/register", response_model=TokenResponse, status_code=201)
@limiter.limit("5/minute")
async def register(request: Request, req: RegisterRequest, db: Session = Depends(get_db)):
    if db.query(User).filter(func.lower(User.email) == req.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")
    if req.username:
        if not is_valid_username(req.username):
            raise HTTPException(status_code=400, detail="That username is reserved or unavailable")
        if db.query(User).filter(User.username.ilike(req.username)).first():
            raise HTTPException(status_code=400, detail="Username already taken")

    user = User(
        email=req.email,
        hashed_password=_hash(req.password),
        full_name=req.full_name,
        phone=req.phone,
        is_student=req.is_student,
        school=req.school,
        location=req.location,
        military_status=req.military_status,
    )
    db.add(user)
    db.flush()

    try:
        if not assign_username_to_user(db, user, req.username, commit=False):
            raise RuntimeError("Unable to assign username")
        if _password_contains_username(req.password, user.username or ""):
            db.rollback()
            raise HTTPException(status_code=400, detail="Password cannot contain your username or a username part")
        # Provision the custodial wallet in the same signup workflow. The
        # wallet service commits the user + wallet atomically from the
        # session's point of view, so a successful registration never leaves
        # a pilot user with a money account but no usable wallet.
        await create_wallet(user.id, db, wallet_type="fawn_custodial")
        db.refresh(user)
        from services.product_metrics import record_metric
        record_metric(db, "onboarding_completed", user_id=user.id, success=True, path="/auth/register")
    except HTTPException:
        raise
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
@limiter.limit("10/minute")
def token(request: Request, form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
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
    if req.location is not None:
        current_user.location = req.location
    if req.military_status is not None:
        current_user.military_status = req.military_status
    if "avatar_url" in req.model_fields_set:
        current_user.avatar_url = req.avatar_url
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

    if _password_contains_username(req.new_password, user.username or ""):
        raise HTTPException(status_code=400, detail="Password cannot contain your username or a username part")

    user.hashed_password = _hash(req.new_password)
    record.used = True
    db.commit()

    return {"message": "Password updated. You can now log in."}


@router.post("/change-password")
@limiter.limit("5/minute")
def change_password(
    request: Request,
    req: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Change the signed-in user's password after verifying the current one."""
    if not _verify(req.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if _password_contains_username(req.new_password, current_user.username or ""):
        raise HTTPException(status_code=400, detail="Password cannot contain your username or a username part")
    if _verify(req.new_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Choose a password you have not used for this account")

    current_user.hashed_password = _hash(req.new_password)
    db.commit()
    return {"message": "Password updated."}


@router.post("/wallets/create")
async def create_user_wallet(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Create a new stablecoin wallet for the current user.

    Returns:
        {
            "wallet_address": "0x...",
            "wallet_type": "fawn_custodial",
            "usdc_balance": 0.0,
            "chain": "ethereum",
            "seed_phrase": null
        }

    FAWN accounts are custodial: the signing key is encrypted at rest and is
    never returned to the browser. This legacy route intentionally has no
    wallet-type input so clients cannot opt into an unsupported key model.
    """
    if current_user.crypto_wallet_address:
        return {
            "wallet_address": current_user.crypto_wallet_address,
            "wallet_type": current_user.wallet_type or "fawn_custodial",
            "usdc_balance": current_user.usdc_balance_cents / 100.0,
            "chain": "polygon",
            "seed_phrase": None,
        }

    try:
        wallet_data = await create_wallet(current_user.id, db, wallet_type="fawn_custodial")
        return wallet_data
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Wallet creation failed: {str(e)}")
