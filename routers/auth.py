from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from jose import jwt
import bcrypt
from slowapi import Limiter
from slowapi.util import get_remote_address

from database import get_db
from models import User
from schemas import RegisterRequest, LoginRequest, TokenResponse, UserResponse
from config import settings
from dependencies import get_current_user
from services import unit as unit_svc

router = APIRouter(prefix="/auth", tags=["auth"])
limiter = Limiter(key_func=get_remote_address)


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
    if db.query(User).filter(User.email == req.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(
        email=req.email,
        hashed_password=_hash(req.password),
        full_name=req.full_name,
        phone=req.phone,
        is_student=req.is_student,
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
    user = db.query(User).filter(User.email == req.email).first()
    if not user or not _verify(req.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return TokenResponse(access_token=_make_token(user.id))


@router.post("/token", response_model=TokenResponse, include_in_schema=False)
def token(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == form.username).first()
    if not user or not _verify(form.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return TokenResponse(access_token=_make_token(user.id))


@router.get("/me", response_model=UserResponse)
def me(current_user: User = Depends(get_current_user)):
    return UserResponse.from_orm_user(current_user)
