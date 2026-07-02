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
    location = Column(String, nullable=True)  # user-entered city/campus location for local personalization
    military_status = Column(String, nullable=True)  # "none", "military_veteran_or_rotc", etc.
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Unit identifiers — set after BaaS account creation
    unit_customer_id = Column(String, nullable=True)
    unit_account_id = Column(String, nullable=True)
    unit_application_id = Column(String, nullable=True)  # set when KYC is pending review
    unit_application_form_id = Column(String, nullable=True)  # set for hosted Unit application forms

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


class Card(Base):
    """Ownership record for a Unit virtual debit card.

    Unit is the source of truth for card state (status, last4, etc.) —
    this table only exists so we can cheaply verify "does this card
    belong to this user" without an extra Unit API call on every request.
    """
    __tablename__ = "cards"

    id = Column(String, primary_key=True, default=new_id)
    user_id = Column(String, nullable=False, index=True)
    unit_card_id = Column(String, nullable=False, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class FundingRequest(Base):
    """A request to pull money from an external bank account into a FAWN
    deposit account via ACH (Unit's inline-counterparty ACH debit/Credit
    direction — see services/unit.py).

    The full external account/routing number is NEVER stored here — only
    the last 4 digits, for the user's own reference. The full numbers are
    sent directly to Unit and discarded immediately after the API call,
    mirroring how SSN is handled in the registration flow.
    """
    __tablename__ = "funding_requests"

    id = Column(String, primary_key=True, default=new_id)
    user_id = Column(String, nullable=False, index=True)
    amount_cents = Column(Integer, nullable=False)
    status = Column(String, nullable=False, default="pending", index=True)  # pending | completed | failed
    external_account_last4 = Column(String, nullable=False)
    external_bank_name = Column(String, nullable=True)
    unit_payment_id = Column(String, nullable=True)
    idempotency_key = Column(String, nullable=False, unique=True, index=True)
    error_message = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)


class UnitEvent(Base):
    """Idempotency record — every processed Unit webhook event id is stored
    here, mirroring StripeEvent's pattern."""
    __tablename__ = "unit_events"

    id = Column(String, primary_key=True)  # Unit event id
    type = Column(String, nullable=False)
    received_at = Column(DateTime(timezone=True), server_default=func.now())


class Handle(Base):
    """A unique @handle a user claims to send/receive P2P payments.

    Lets senders type "@maria" instead of an account number. One handle
    per user; editable, but always unique across the table.
    """
    __tablename__ = "handles"

    id = Column(String, primary_key=True, default=new_id)
    user_id = Column(String, nullable=False, unique=True, index=True)
    handle = Column(String, nullable=False, unique=True, index=True)  # lowercase, no "@"
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class P2PTransfer(Base):
    """A single P2P money movement: a direct send, or a request-to-pay.

    Direction is always expressed as from_user_id (debited) -> to_user_id
    (credited), regardless of whether the row originated as a "send"
    (creator is from_user_id) or a "request" (creator is to_user_id,
    asking from_user_id to pay). Splits are N of these rows sharing a
    group_id.

    Every send is created in status="pending" and does NOT touch Unit
    until POST /p2p/transfers/{id}/confirm is called — this is what
    forces the irreversible-action confirmation screen in the UI for
    every transfer, not just risky ones. Requests start in
    status="requested" and only become a real money movement once the
    payer creates+confirms a linked transfer referencing
    source_request_id.
    """
    __tablename__ = "p2p_transfers"

    id = Column(String, primary_key=True, default=new_id)
    type = Column(String, nullable=False, index=True)  # "send" | "request"
    status = Column(String, nullable=False, default="pending", index=True)
    # pending | requires_step_up | completed | failed | requested | declined | disputed | expired

    from_user_id = Column(String, nullable=False, index=True)   # debited / payer
    to_user_id = Column(String, nullable=False, index=True)     # credited / payee
    from_handle = Column(String, nullable=False)
    to_handle = Column(String, nullable=False)

    amount_cents = Column(Integer, nullable=False)
    note = Column(String, nullable=True)
    warning = Column(String, nullable=True)  # scam-warning text shown to the user, if any

    group_id = Column(String, nullable=True, index=True)          # links split-the-bill rows
    source_request_id = Column(String, nullable=True, index=True)  # set when a "send" fulfills a "request"
    related_transfer_id = Column(String, nullable=True, index=True)  # links a refund back to the original

    idempotency_key = Column(String, nullable=False, unique=True, index=True)
    unit_book_payment_id = Column(String, nullable=True)
    error_message = Column(String, nullable=True)

    step_up_required = Column(Boolean, default=False, nullable=False)
    step_up_acknowledged = Column(Boolean, default=False, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)


class P2PDispute(Base):
    """A Reg E–style dispute filed against a completed transfer.

    Money has already moved at Unit by the time a dispute is filed —
    this is a claims/review layer on top, not an automatic reversal.
    Admin resolves manually via /admin/p2p/disputes/{id}/resolve, which
    if approved triggers a separate refund P2PTransfer.
    """
    __tablename__ = "p2p_disputes"

    id = Column(String, primary_key=True, default=new_id)
    transfer_id = Column(String, nullable=False, index=True)
    # Canonical id of the underlying money movement this dispute covers.
    # A "request" row and the linked "send" row that fulfills it represent
    # the SAME real Unit Book Payment (see pay_request/confirm_transfer in
    # routers/p2p.py) — payment_id always resolves to the "send" row's id
    # so that disputing either row is recognized as disputing one payment.
    # Nullable for backward compatibility with rows created before this
    # column existed.
    payment_id = Column(String, nullable=True, index=True)
    filer_user_id = Column(String, nullable=False, index=True)
    reason = Column(String, nullable=False)
    status = Column(String, default="open", nullable=False, index=True)  # open | refunded | denied
    resolution_note = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    resolved_at = Column(DateTime(timezone=True), nullable=True)


class P2PAuditLog(Base):
    """Append-only log of every state change on a P2P transfer.

    Required compliance trail — every create/step-up/confirm/fail/dispute
    event gets a row here, independent of the mutable P2PTransfer state.
    """
    __tablename__ = "p2p_audit_log"

    id = Column(String, primary_key=True, default=new_id)
    transfer_id = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    event_type = Column(String, nullable=False)  # created | step_up_required | confirmed | failed | disputed | dispute_resolved
    metadata_json = Column(String, nullable=True)  # json.dumps'd dict — kept as text for portability
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class NewsAlert(Base):
    """A saved news-watch query for a user ("AI alerts").

    The user saves a topic ("student loans", "fed rates", "tuition") and
    the app surfaces fresh matching headlines on each check.
    last_checked_at lets the UI say "3 new since yesterday" — matching is
    recomputed live against current feeds on every check.
    """
    __tablename__ = "news_alerts"

    id = Column(String, primary_key=True, default=new_id)
    user_id = Column(String, nullable=False, index=True)
    query = Column(String, nullable=False)     # the search phrase, <= 60 chars
    category = Column(String, nullable=True)   # markets | world | crypto | None = default mix
    last_checked_at = Column(DateTime(timezone=True), nullable=True)
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
