import re
from pydantic import BaseModel, EmailStr, ConfigDict, field_validator, model_validator
from typing import Optional, List

# --- Auth ---

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    # Optional for backwards-compatible API clients; the signup UI requires
    # it and sends the user's chosen handle.
    username: Optional[str] = None
    phone: Optional[str] = None  # Optional
    is_student: bool = True
    school: Optional[str] = None  # school key, e.g. "berkeley" — drives Campus Savings on the dashboard
    location: Optional[str] = None
    military_status: Optional[str] = None

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

    @field_validator("username")
    @classmethod
    def normalize_username(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        normalized = v.strip().lower()
        if not re.fullmatch(r"[a-z0-9_]{3,30}", normalized):
            raise ValueError("Username must be 3-30 characters using lowercase letters, numbers, or underscores")
        return normalized

    @model_validator(mode="after")
    def password_cannot_contain_username(self):
        if self.username:
            password = self.password.casefold()
            parts = [p for p in re.split(r"_+", self.username.casefold()) if len(p) >= 3]
            if any(part in password for part in [self.username.casefold(), *parts]):
                raise ValueError("Password cannot contain your username or a username part")
        return self

    @field_validator("phone", mode="before")
    @classmethod
    def phone_digits(cls, v) -> Optional[str]:
        # Handle None, empty string, or whitespace-only strings
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return None
        # Extract digits only
        if isinstance(v, str):
            digits = re.sub(r"\D", "", v)
            if len(digits) < 10:
                raise ValueError("Phone must have at least 10 digits")
            return digits[-10:]
        return v

class ForgotPasswordRequest(BaseModel):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.lower()

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

    @field_validator("email")
    @classmethod
    def normalize_email(cls, v: str) -> str:
        return v.lower()

class UpdateMeRequest(BaseModel):
    school: Optional[str] = None
    location: Optional[str] = None
    military_status: Optional[str] = None
    avatar_url: Optional[str] = None

    @field_validator("avatar_url")
    @classmethod
    def validate_avatar_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if len(v) > 700_000:
            raise ValueError("Profile picture is too large")
        if v.startswith("data:image/"):
            if not re.match(r"^data:image/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$", v):
                raise ValueError("Profile picture must be a PNG, JPEG, or WebP image")
            return v
        if re.match(r"^https://[^\s]+$", v):
            return v
        raise ValueError("Profile picture must use an https URL")

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"

class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    full_name: str
    username: str
    avatar_url: Optional[str] = None
    is_student: bool
    school: Optional[str] = None
    location: Optional[str] = None
    military_status: Optional[str] = None
    wallet_initialized: Optional[bool] = None
    crypto_wallet_address: Optional[str] = None
    wallet_type: Optional[str] = None

    @classmethod
    def from_orm_user(cls, user):  # type: ignore[override]
        return cls(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            username=user.username or "",
            avatar_url=getattr(user, "avatar_url", None),
            is_student=user.is_student,
            school=getattr(user, "school", None),
            location=getattr(user, "location", None),
            military_status=getattr(user, "military_status", None),
            wallet_initialized=bool(user.wallet_initialized),
            crypto_wallet_address=getattr(user, "crypto_wallet_address", None),
            wallet_type=getattr(user, "wallet_type", None),
        )

# --- Cards ---

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
