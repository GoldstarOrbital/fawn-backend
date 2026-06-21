import re
from pydantic import BaseModel, EmailStr, ConfigDict, field_validator
from typing import Optional, List
from datetime import date as _date

# --- Auth ---

class Address(BaseModel):
    street: str
    city: str
    state: str       # 2-letter US state code
    postal_code: str
    country: str = "US"

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    phone: str
    date_of_birth: str   # YYYY-MM-DD, passed to Unit, not stored
    ssn: str             # 9 digits, passed to Unit, NEVER stored
    address: Address
    is_student: bool = True
    occupation: str = "Student"
    school: Optional[str] = None  # school key, e.g. "berkeley" — drives Campus Savings on the dashboard

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        # Login does an exact match against the stored value — without this,
        # "Jane@X.com" at signup and "jane@x.com" at login silently fail to match.
        return v.lower()

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v

    @field_validator("ssn")
    @classmethod
    def ssn_format(cls, v: str) -> str:
        digits = re.sub(r"\D", "", v)
        if len(digits) != 9:
            raise ValueError("SSN must be 9 digits")
        return digits

    @field_validator("date_of_birth")
    @classmethod
    def dob_format(cls, v: str) -> str:
        try:
            _date.fromisoformat(v)
        except ValueError:
            raise ValueError("date_of_birth must be YYYY-MM-DD")
        return v

    @field_validator("phone")
    @classmethod
    def phone_digits(cls, v: str) -> str:
        digits = re.sub(r"\D", "", v)
        if len(digits) < 10:
            raise ValueError("Phone must have at least 10 digits")
        return digits[-10:]

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.lower()

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"

class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    full_name: str
    is_student: bool
    school: Optional[str] = None
    account_active: Optional[bool] = None
    application_pending: Optional[bool] = None

    @classmethod
    def from_orm_user(cls, user):  # type: ignore[override]
        return cls(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            is_student=user.is_student,
            school=getattr(user, "school", None),
            account_active=bool(user.unit_account_id),
            application_pending=bool(
                getattr(user, "unit_application_id", None) and not user.unit_account_id
            ),
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
    amount: float
    description: str
    date: str
    status: str
    category: Optional[str] = "Other"

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
