from sqlalchemy import Column, String, DateTime, Boolean, Numeric, Integer
from sqlalchemy.sql import func
from database import Base
import uuid

def new_id():
    return str(uuid.uuid4())

class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=new_id)
    email = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    full_name = Column(String, nullable=False)
    phone = Column(String, nullable=True)
    is_student = Column(Boolean, default=False)
    school = Column(String, nullable=True)  # school key, e.g. "berkeley" — drives Campus Savings on the dashboard
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Unit identifiers — set after BaaS account creation
    unit_customer_id = Column(String, nullable=True)
    unit_account_id = Column(String, nullable=True)
    unit_application_id = Column(String, nullable=True)  # set when KYC is pending review

    # Referral
    referral_code = Column(String, unique=True, nullable=True, index=True)
    referred_by = Column(String, nullable=True)   # referral_code of inviter
    referral_count = Column(Integer, default=0, nullable=False)


class WaitlistEntry(Base):
    __tablename__ = "waitlist"

    id = Column(String, primary_key=True, default=new_id)
    email = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, nullable=True)
    source = Column(String, nullable=True, default="landing")
    referral_code = Column(String, nullable=True)  # referral code used when joining
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class FoundingMember(Base):
    """A real customer who paid for a founding-tier offer via Stripe.

    Created by the Stripe webhook on `checkout.session.completed`.
    Sole source of truth for `founding_sold` counts and member-dashboard data.
    """
    __tablename__ = "founding_members"

    id = Column(String, primary_key=True, default=new_id)
    email = Column(String, nullable=False, index=True)
    member_number = Column(Integer, nullable=False, index=True)
    tier = Column(String, nullable=False)  # "founding" | "inner_circle" | "dev_sprint"
    amount_cents = Column(Integer, nullable=False)
    stripe_customer_id = Column(String, nullable=True)
    stripe_session_id = Column(String, nullable=True, unique=True)
    referral_code_used = Column(String, nullable=True)
    refunded = Column(Boolean, default=False, nullable=False)
    joined_at = Column(DateTime(timezone=True), server_default=func.now())


class StripeEvent(Base):
    """Idempotency record — every processed Stripe event id is stored here."""
    __tablename__ = "stripe_events"

    id = Column(String, primary_key=True)  # stripe event.id
    type = Column(String, nullable=False)
    received_at = Column(DateTime(timezone=True), server_default=func.now())


class MagicLinkToken(Base):
    """Short-lived passwordless login tokens for founding-member dashboard."""
    __tablename__ = "magic_link_tokens"

    id = Column(String, primary_key=True, default=new_id)
    email = Column(String, nullable=False, index=True)
    token_hash = Column(String, nullable=False, unique=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class PasswordResetToken(Base):
    """Short-lived, single-use tokens for the /auth/forgot-password flow.

    Only the SHA-256 hash of the raw token is stored — mirrors MagicLinkToken's
    pattern so a leaked DB row can't be replayed as a working reset link.
    """
    __tablename__ = "password_reset_tokens"

    id = Column(String, primary_key=True, default=new_id)
    user_id = Column(String, nullable=False, index=True)
    token_hash = Column(String, nullable=False, unique=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class DealSuggestion(Base):
    """Community-submitted campus deal suggestions for the Campus Savings hub.

    Submitted via the frontend form on index.html. Reviewed by Alex via
    GET /deals/suggestions (admin-key protected) and manually folded into
    the SCHOOLS data array in index.html once verified.
    """
    __tablename__ = "deal_suggestions"

    id = Column(String, primary_key=True, default=new_id)
    school = Column(String, nullable=False, index=True)
    category = Column(String, nullable=False)  # gas | food | coffee | housing | bars | bulk | coupons
    suggestion = Column(String, nullable=False)
    submitter_email = Column(String, nullable=True)
    status = Column(String, default="pending", nullable=False, index=True)  # pending | approved | rejected
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class EmailLog(Base):
    """Tracks which nurture emails have been sent to each waitlist address.

    Used by the email_automation router to ensure idempotency — each
    (email, email_number) pair is only ever sent once.
    """
    __tablename__ = "email_log"

    id = Column(String, primary_key=True, default=new_id)
    email = Column(String, nullable=False, index=True)
    email_number = Column(Integer, nullable=False)   # 2, 3, 4, or 5
    sent_at = Column(DateTime(timezone=True), server_default=func.now())
