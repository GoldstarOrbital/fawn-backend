from pydantic import BaseModel, EmailStr, ConfigDict, field_validator
from typing import Optional, List
from datetime import datetime

# --- Auth ---

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    phone: Optional[str] = None
    is_student: bool = False

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"

class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    full_name: str
    is_student: bool
    # unit_account_id intentionally excluded — internal BaaS identifier
    # expose only whether an account exists
    account_active: Optional[bool] = None

    @classmethod
    def from_orm_user(cls, user):  # type: ignore[override]
        return cls(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            is_student=user.is_student,
            account_active=bool(user.unit_account_id),
        )

# --- Accounts ---

class AccountBalance(BaseModel):
    account_id: str
    available: float
    current: float
    currency: str = "USD"

# --- Transactions ---

class TransactionItem(BaseModel):
    id: str
    amount: float          # positive = credit, negative = debit
    description: str
    date: str
    status: str
    category: Optional[str] = "Other"
    emoji: Optional[str] = "📎"

class TransactionList(BaseModel):
    transactions: List[TransactionItem]

# --- News / AI ---

class NewsRequest(BaseModel):
    topics: Optional[List[str]] = ["economy", "interest rates", "inflation"]

class NewsResponse(BaseModel):
    articles: List[dict]
    ai_summary: Optional[str] = None
    disclaimer: str = (
        "This is general financial news for informational purposes only. "
        "It is not investment advice. FAWN does not manage or invest your deposits."
    )
