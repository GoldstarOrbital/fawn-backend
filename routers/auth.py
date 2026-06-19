from fastapi import APIRouter, Depends, HTTPException, Form, Request
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
    db.flush()  # assigns user.id before Unit calls

    # Create Unit customer + deposit account
    # Gracefully skips if UNIT_API_TOKEN isn't set yet
    try:
        application = await unit_svc.create_application(
            req.full_name, req.email, req.phone or ""
        )
        # Application approval creates a customer automatically
        relationships = application.get("relationships", {})
        customer_data = relationships.get("customer", {}).get("data", {})
        unit_customer_id = customer_data.get("id")
        if not unit_customer_id:
            raise ValueError("Application not approved; status may be pending")
        user.unit_customer_id = unit_customer_id
        # Unit doesn't auto-create an account — open one explicitly
        account = await unit_svc.create_deposit_account(unit_customer_id)
        user.unit_account_id = account["id"]
    except Exception as e:
        print(f"[Unit] Skipped BaaS account creation: {e}")

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


# OAuth2 form-compatible login — used by the Swagger UI Authorize button
@router.post("/token", response_model=TokenResponse, include_in_schema=False)
def token(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == form.username).first()
    if not user or not _verify(form.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return TokenResponse(access_token=_make_token(user.id))


@router.get("/me", response_model=UserResponse)
def me(current_user: User = Depends(get_current_user)):
    return UserResponse.from_orm_user(current_user)
