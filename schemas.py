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
    date_of_birth: Optional[str] = None  # YYYY-MM-DD, passed to Stripe when direct KYC is used
    ssn: Optional[str] = None            # 9 digits, passed to Stripe when direct KYC is used, NEVER stored
    address: Optional[Address] = None
    is_us_citizen: bool = False          # KYC eligibility attestation — FAWN banking is U.S.-citizens-only; required to submit an SSN/KYC payload
    is_student: bool = True
    occupation: str = "Student"
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

    @field_validator("ssn")
    @classmethod
    def ssn_format(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        digits = re.sub(r"\D", "", v)
        if len(digits) != 9:
            raise ValueError("SSN must be 9 digits")
        return digits

    @field_validator("date_of_birth")
    @classmethod
    def dob_format(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
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
    location: Optional[str] = None
    military_status: Optional[str] = None
    account_active: Optional[bool] = None
    application_pending: Optional[bool] = None
    stripe_onboarding_ready: Optional[bool] = None

    @classmethod
    def from_orm_user(cls, user):  # type: ignore[override]
        return cls(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            is_student=user.is_student,
            school=getattr(user, "school", None),
            location=getattr(user, "location", None),
            military_status=getattr(user, "military_status", None),
            account_active=bool(user.stripe_financial_account_id),
            application_pending=bool(
                getattr(user, "stripe_account_id", None) and not user.stripe_financial_account_id
            ),
            stripe_onboarding_ready=not bool(user.stripe_financial_account_id),
        )


class StripeOnboardingResponse(BaseModel):
    onboarding_url: str
    stripe_account_id: str

# --- Accounts ---

class AccountBalance(BaseModel):
    account_id: str
    available: float
    current: float
    currency: str = "USD"

# --- Cards ---

class CardOut(BaseModel):
    id: str
    last4_digits: str
    expiration_date: str
    status: str
    created_at: str

class CardList(BaseModel):
    cards: List[CardOut]

class CardFreezeRequest(BaseModel):
    reason: Optional[str] = "userRequested"

# --- Funding (Add Funds via ACH) ---

class AddFundsRequest(BaseModel):
    amount_cents: int
    routing_number: str
    account_number: str
    account_type: str  # "Checking" | "Savings"
    account_holder_name: str
    idempotency_key: str

    @field_validator("amount_cents")
    @classmethod
    def amount_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("amount_cents must be positive")
        return v

    @field_validator("routing_number")
    @classmethod
    def routing_number_format(cls, v: str) -> str:
        digits = re.sub(r"\D", "", v)
        if len(digits) != 9:
            raise ValueError("Routing number must be 9 digits")
        return digits

    @field_validator("account_number")
    @classmethod
    def account_number_format(cls, v: str) -> str:
        digits = re.sub(r"\D", "", v)
        if not (4 <= len(digits) <= 17):
            raise ValueError("Account number must be 4-17 digits")
        return digits

    @field_validator("account_type")
    @classmethod
    def account_type_valid(cls, v: str) -> str:
        if v not in ("Checking", "Savings"):
            raise ValueError("account_type must be 'Checking' or 'Savings'")
        return v

class FundingRequestOut(BaseModel):
    id: str
    amount_cents: int
    status: str
    external_account_last4: str
    created_at: str
    completed_at: Optional[str] = None
    error_message: Optional[str] = None

class FundingRequestList(BaseModel):
    requests: List[FundingRequestOut]

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

# --- P2P payments ---

_HANDLE_RE = re.compile(r"^[a-z0-9_]{3,20}$")

class HandleClaimRequest(BaseModel):
    handle: str

    @field_validator("handle")
    @classmethod
    def normalize_handle(cls, v: str) -> str:
        v = v.strip().lstrip("@").lower()
        if not _HANDLE_RE.match(v):
            raise ValueError("Handle must be 3-20 characters: lowercase letters, numbers, underscore only.")
        return v

class HandleOut(BaseModel):
    handle: str

class HandleLookupOut(BaseModel):
    handle: str
    claimable: bool
    display_name: Optional[str] = None  # first name + last initial only, never full identity

class P2PSendRequest(BaseModel):
    to_handle: str
    amount_cents: int
    note: Optional[str] = None
    idempotency_key: str

    @field_validator("to_handle")
    @classmethod
    def normalize_to_handle(cls, v: str) -> str:
        return v.strip().lstrip("@").lower()

    @field_validator("amount_cents")
    @classmethod
    def amount_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("amount_cents must be positive")
        if v > 100_000_00:  # $100,000 hard ceiling regardless of tier — sanity bound, not the real limit
            raise ValueError("amount_cents exceeds the maximum allowed transfer size")
        return v

class P2PRequestRequest(BaseModel):
    from_handle: str  # the person being asked to pay
    amount_cents: int
    note: Optional[str] = None
    idempotency_key: str

    @field_validator("from_handle")
    @classmethod
    def normalize_from_handle(cls, v: str) -> str:
        return v.strip().lstrip("@").lower()

    @field_validator("amount_cents")
    @classmethod
    def amount_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("amount_cents must be positive")
        return v

class P2PSplitRequest(BaseModel):
    total_amount_cents: int
    recipient_handles: List[str]
    note: Optional[str] = None
    idempotency_key: str  # one key for the whole split; per-row keys are derived from it

    @field_validator("recipient_handles")
    @classmethod
    def normalize_handles(cls, v: List[str]) -> List[str]:
        cleaned = [h.strip().lstrip("@").lower() for h in v]
        if len(cleaned) < 1:
            raise ValueError("At least one recipient handle is required")
        if len(cleaned) > 20:
            raise ValueError("Splits are capped at 20 people")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("Duplicate recipient handles in split")
        return cleaned

    @field_validator("total_amount_cents")
    @classmethod
    def amount_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("total_amount_cents must be positive")
        return v

class P2PConfirmRequest(BaseModel):
    step_up_acknowledged: bool = False

class P2PTransferOut(BaseModel):
    id: str
    type: str
    status: str
    direction: str  # "sent" | "received" | "request_outgoing" | "request_incoming" (relative to current user)
    counterparty_handle: str
    amount_cents: int
    note: Optional[str] = None
    warning: Optional[str] = None
    group_id: Optional[str] = None
    step_up_required: bool = False
    created_at: str
    completed_at: Optional[str] = None
    error_message: Optional[str] = None

class P2PTransferList(BaseModel):
    transfers: List[P2PTransferOut]

class P2PDisputeRequest(BaseModel):
    reason: str

    @field_validator("reason")
    @classmethod
    def reason_not_empty(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 10:
            raise ValueError("Please describe the issue in at least 10 characters.")
        return v[:1000]

class P2PDisputeOut(BaseModel):
    id: str
    transfer_id: str
    status: str
    reason: str
    created_at: str

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
