from sqlalchemy import Column, String, DateTime, Boolean, Numeric, Integer, LargeBinary, ForeignKey, CheckConstraint, Index
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

    # ── CRYPTO-NATIVE WALLET (NEW ARCHITECTURE) ──
    # Stablecoin wallet address (Ethereum/Polygon). Set when user creates wallet.
    crypto_wallet_address = Column(String, nullable=True, unique=True, index=True)
    # Wallet type: "non_custodial" (user manages keys) or "fawn_custodial" (FAWN holds keys)
    wallet_type = Column(String, nullable=True)  # default null until wallet created
    # USDC balance in platform ledger (Decimal, in whole USD cents for precision)
    # This tracks the balance per our internal ledger, not necessarily on-chain (may lag)
    usdc_balance_cents = Column(Integer, default=0, nullable=False)  # balance in cents, e.g., 1000 = $10.00
    # Flag: has user set up their wallet yet?
    wallet_initialized = Column(Boolean, default=False, nullable=False)
    # Total platform fees paid (in cents), for analytics/reporting
    total_fees_paid_cents = Column(Integer, default=0, nullable=False)

    # ── LEGACY BANKING FIELDS (kept for migration reference, not used) ──
    unit_customer_id = Column(String, nullable=True)
    unit_account_id = Column(String, nullable=True)
    unit_application_id = Column(String, nullable=True)
    unit_application_form_id = Column(String, nullable=True)

    # Multi-BaaS provider identifiers (Column / Lithic / Alpaca). All nullable —
    # populated only once a user is opted into each provider.
    column_entity_id = Column(String, nullable=True)      # Column person/entity record
    column_account_id = Column(String, nullable=True)     # Column bank account
    lithic_account_token = Column(String, nullable=True)  # Lithic financial account backing cards
    alpaca_account_id = Column(String, nullable=True)     # Alpaca brokerage account

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
    # Opaque provider card id. Historically a Unit card id; for Lithic-issued
    # cards this holds the Lithic card token. `provider` disambiguates which
    # card service owns it. Column name kept as unit_card_id for schema
    # backward-compatibility (renaming would need a data migration).
    unit_card_id = Column(String, nullable=False, unique=True, index=True)
    provider = Column(String, nullable=False, default="unit")  # "unit" | "lithic"
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


class PodcastEpisode(Base):
    """One AI-generated daily news brief ("FAWN Daily Brief").

    Generated on a schedule (3:30 AM Pacific) or via the admin endpoint.
    episode_date is unique — generation is idempotent per day, so scheduler
    restarts or a manual re-trigger can never produce duplicate episodes.
    Audio is stored inline (one ~4-5MB MP3 per day, pruned after 14 days)
    because Railway's filesystem is ephemeral and this avoids adding an
    object-storage dependency for a single small daily file.
    """
    __tablename__ = "podcast_episodes"

    id = Column(String, primary_key=True, default=new_id)
    episode_date = Column(String, nullable=False, unique=True, index=True)  # YYYY-MM-DD (America/Los_Angeles)
    title = Column(String, nullable=False)
    script = Column(String, nullable=False)              # full spoken text, shown as transcript
    audio_mp3 = Column(LargeBinary, nullable=True)       # null if TTS failed — transcript still served
    word_count = Column(Integer, nullable=False, default=0)
    est_duration_seconds = Column(Integer, nullable=False, default=0)
    source_headline_count = Column(Integer, nullable=False, default=0)
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


# ---- Multi-BaaS provider records (Column / Lithic / Alpaca / Plaid) ---------

class PlaidItem(Base):
    """A linked external bank account for a user (Plaid).

    Stores the long-lived Plaid access_token — a secret, never exposed to the
    client. The raw external routing/account numbers are NOT stored here; they
    are fetched from Plaid at funding time and forwarded to the banking
    provider, keeping only the last-4 mask for display (same rule as Unit
    ACH funding).
    """
    __tablename__ = "plaid_items"

    id = Column(String, primary_key=True, default=new_id)
    user_id = Column(String, nullable=False, index=True)
    item_id = Column(String, nullable=False, unique=True, index=True)  # Plaid item id
    access_token = Column(String, nullable=False)  # Plaid access_token (secret)
    institution_name = Column(String, nullable=True)
    account_mask = Column(String, nullable=True)   # last 4 of linked account
    status = Column(String, nullable=False, default="active", index=True)  # active | removed
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class InvestingOrder(Base):
    """A buy/sell order placed against a user's Alpaca brokerage account.

    Alpaca is the source of truth for fill state; this row is a local audit
    record linking the order back to a FAWN user and letting the UI show
    order history without an Alpaca round-trip per view.
    """
    __tablename__ = "investing_orders"

    id = Column(String, primary_key=True, default=new_id)
    user_id = Column(String, nullable=False, index=True)
    alpaca_order_id = Column(String, nullable=True, unique=True, index=True)
    symbol = Column(String, nullable=False)
    side = Column(String, nullable=False)   # buy | sell
    notional_cents = Column(Integer, nullable=True)  # dollar order (fractional), if used
    qty = Column(Numeric, nullable=True)             # share order, if used
    status = Column(String, nullable=False, default="pending", index=True)
    idempotency_key = Column(String, nullable=False, unique=True, index=True)
    error_message = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ColumnEvent(Base):
    """Idempotency record — every processed Column webhook event id is stored
    here, mirroring UnitEvent/StripeEvent."""
    __tablename__ = "column_events"

    id = Column(String, primary_key=True)  # Column event id
    type = Column(String, nullable=False)
    received_at = Column(DateTime(timezone=True), server_default=func.now())


class LithicEvent(Base):
    """Idempotency record — every processed Lithic webhook/auth-stream event id."""
    __tablename__ = "lithic_events"

    id = Column(String, primary_key=True)  # Lithic event token
    type = Column(String, nullable=False)
    received_at = Column(DateTime(timezone=True), server_default=func.now())


class CryptoWallet(Base):
    """Stablecoin wallet per user — tracks address, balance, and metadata.

    Users can have at most one wallet per account, but this table allows
    wallet migration / multi-chain support in the future without schema rewrites.
    balance_cents is the canonical source of truth for ledger balance.
    Custodial private keys are encrypted with Fernet (AES-256-GCM).
    """
    __tablename__ = "crypto_wallets"

    id = Column(String, primary_key=True, default=new_id)
    user_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, unique=True, index=True)  # 1:1 per user
    wallet_address = Column(String, nullable=False, unique=True, index=True)
    wallet_type = Column(String, nullable=False)  # "non_custodial" | "fawn_custodial"
    chain = Column(String, nullable=False, default="polygon")  # "polygon" | "ethereum"
    usdc_balance_cents = Column(Integer, default=0, nullable=False)
    encrypted_private_key = Column(LargeBinary, nullable=True)  # Fernet-encrypted key for custodial wallets only
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Constraints and indexes
    __table_args__ = (
        CheckConstraint("wallet_type IN ('non_custodial', 'fawn_custodial')"),
        CheckConstraint("chain IN ('polygon', 'ethereum')"),
        CheckConstraint("usdc_balance_cents >= 0"),
        Index('idx_crypto_wallet_user_chain', 'user_id', 'chain'),
    )


class InvestingWatchlist(Base):
    """Watchlist of symbols (stocks, ETFs, crypto) a user is tracking.

    Lets users save symbols for later review without placing an order.
    One row per unique (user_id, symbol) pair.
    """
    __tablename__ = "investing_watchlist"

    id = Column(String, primary_key=True, default=new_id)
    user_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    symbol = Column(String, nullable=False)  # uppercase: AAPL, BTC, etc
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Unique constraint: one row per user per symbol
    __table_args__ = (
        Index('idx_watchlist_user_symbol', 'user_id', 'symbol', unique=True),
    )


class CryptoTransfer(Base):
    """Internal ledger entry for P2P USDC transfers.

    Each transfer costs the sender $0.01 (1000 cents) in platform fees.
    Transfers are instant (no blockchain needed) — money moves in our ledger.
    No gas fees — the $0.01 is pure platform revenue.
    """
    __tablename__ = "crypto_transfers"

    id = Column(String, primary_key=True, default=new_id)
    sender_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    recipient_address = Column(String, nullable=False, index=True)
    amount_cents = Column(Integer, nullable=False)
    fee_cents = Column(Integer, default=100, nullable=False)
    status = Column(String, default="completed", nullable=False, index=True)
    tx_hash = Column(String, nullable=True)
    memo = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # Constraints and indexes
    __table_args__ = (
        CheckConstraint("amount_cents > 0"),
        CheckConstraint("status IN ('pending', 'completed', 'failed')"),
        Index('idx_crypto_transfer_sender_created', 'sender_id', 'created_at'),
        Index('idx_crypto_transfer_recipient_date', 'recipient_address', 'created_at'),
        Index('idx_crypto_transfer_pending', 'sender_id', 'status'),
    )


class FeeCollection(Base):
    """Daily/periodic aggregation of platform fees collected.

    Used for accounting and treasury management — tracks when fees are swept
    to the FAWN treasury wallet and how much was collected each period.
    """
    __tablename__ = "fee_collections"

    id = Column(String, primary_key=True, default=new_id)
    collection_date = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    total_fees_cents = Column(Integer, nullable=False)  # sum of all fees from transfers this period
    transfer_count = Column(Integer, default=0, nullable=False)  # how many transfers generated fees
    treasury_wallet = Column(String, nullable=False)  # FAWN's wallet that received the fees
    tx_hash = Column(String, nullable=True)  # on-chain sweep tx (if applicable)
    collected_at = Column(DateTime(timezone=True), nullable=True)


class BankTransfer(Base):
    """ACH transfer from FAWN USDC wallet to any traditional bank account.

    User sends USDC from their FAWN wallet, which is converted 1:1 to USD
    and sent via ACH to a recipient bank account. Settlement time: standard
    ACH 1-3 business days. Costs $0.01 flat fee (same as P2P).

    Recipient bank details (routing/account) are sent directly to Column and
    NOT persisted on our side (only last 4 for reference), mirroring ACH
    funding's handling of raw account numbers for security.
    """
    __tablename__ = "bank_transfers"

    id = Column(String, primary_key=True, default=new_id)
    sender_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    recipient_name = Column(String, nullable=False)
    recipient_routing_number = Column(String, nullable=False)  # last 4 stored for reference
    recipient_account_last4 = Column(String, nullable=False)  # mask for user reference
    amount_cents = Column(Integer, nullable=False)  # USDC amount (1:1 USD conversion)
    fee_cents = Column(Integer, default=100, nullable=False)  # $0.01 = 100 cents
    status = Column(String, default="pending", nullable=False, index=True)  # pending | completed | failed
    memo = Column(String, nullable=True)
    ach_id = Column(String, nullable=True, unique=True, index=True)  # Column ACH transfer ID (populated on success)
    idempotency_key = Column(String, nullable=False, unique=True, index=True)  # prevent retries creating dupes
    error_message = Column(String, nullable=True)  # error detail if status=failed
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # Constraints and indexes
    __table_args__ = (
        CheckConstraint("amount_cents > 0"),
        CheckConstraint("status IN ('pending', 'completed', 'failed')"),
        Index('idx_bank_transfer_sender_created', 'sender_id', 'created_at'),
        Index('idx_bank_transfer_pending', 'sender_id', 'status'),
    )


class UserAuditLog(Base):
    """Append-only audit trail for every significant user action.

    Required for compliance and security. Logs wallet creation, transfers,
    failed auth attempts, data exports. Immutable (never delete) for 7 years.
    """
    __tablename__ = "user_audit_log"

    id = Column(String, primary_key=True, default=new_id)
    user_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    action = Column(String, nullable=False, index=True)  # created_wallet, sent_transfer, export_data, failed_auth
    details = Column(String, nullable=True)  # JSON-encoded details (wallet_type, amount, recipient truncated, etc)
    ip_address = Column(String, nullable=True)  # source IP for geo/fraud detection (hashed in production)
    user_agent = Column(String, nullable=True)  # browser/client info
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    retention_expires_at = Column(DateTime(timezone=True), nullable=False, index=True)  # 7-year compliance retention

    # Composite index for efficient audit trail queries (user + date range)
    __table_args__ = (
        Index('idx_audit_log_user_date', 'user_id', 'created_at'),
    )
