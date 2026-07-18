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
    username = Column(String, unique=True, nullable=True, index=True)  # @username for profiles
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

    # Third-party account identifiers. FAWN is self-custodial/crypto-native —
    # investing (Alpaca) and bank-account linking (Plaid, see PlaidItem) are
    # the only remaining third parties.
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


# ---- Third-party integrations (Alpaca investing / Plaid bank linking) ------

class PlaidItem(Base):
    """A linked external bank account for a user (Plaid).

    Stores the long-lived Plaid access_token — a secret, never exposed to the
    client. The raw external routing/account numbers are NOT stored here; they
    are fetched from Plaid at funding time and forwarded to the ACH processor,
    keeping only the last-4 mask for display.
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


class CryptoDeposit(Base):
    """An individual incoming USDC transfer detected on-chain by
    services/blockchain_monitor.py, via Transfer event logs (not just a
    balanceOf() diff -- that only tells you the total changed, not who
    sent what, when, or on which chain).

    One row per real on-chain transfer. This is what powers "money in"
    entries in the transaction list and lets a user see exactly where a
    deposit came from (source address, chain, tx hash) instead of just
    watching a balance number change with no explanation.
    """
    __tablename__ = "crypto_deposits"

    id = Column(String, primary_key=True, default=new_id)
    user_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    chain = Column(String, nullable=False)  # "polygon" | "base" | ...
    contract_address = Column(String, nullable=False)  # which USDC variant (native/bridged)
    from_address = Column(String, nullable=False)  # external sender
    to_address = Column(String, nullable=False, index=True)  # this user's FAWN wallet
    amount_cents = Column(Integer, nullable=False)
    tx_hash = Column(String, nullable=False, index=True)
    block_number = Column(Integer, nullable=False)
    credited_to_ledger = Column(Boolean, default=True, nullable=False)  # False for backfilled history pre-dating this feature
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    __table_args__ = (
        CheckConstraint("amount_cents > 0"),
        # A given on-chain transfer should never be recorded twice.
        Index('idx_crypto_deposit_dedupe', 'chain', 'tx_hash', 'contract_address', 'to_address', unique=True),
        Index('idx_crypto_deposit_user_created', 'user_id', 'created_at'),
    )


class ChainScanCheckpoint(Base):
    """Tracks the last block scanned per (wallet, chain) so the deposit
    monitor only queries new blocks each cycle instead of re-scanning
    history every 60 seconds.

    is_backfilled=False means this wallet+chain has never been scanned
    before; the monitor does one bounded historical look-back to record
    (but not double-credit) any deposits that predate this feature, then
    flips this to True so all future scans are purely incremental.
    """
    __tablename__ = "chain_scan_checkpoints"

    id = Column(String, primary_key=True, default=new_id)
    wallet_address = Column(String, nullable=False, index=True)
    chain = Column(String, nullable=False)
    last_scanned_block = Column(Integer, nullable=False)
    is_backfilled = Column(Boolean, default=False, nullable=False)
    # Balance already attributable to this chain that predates per-transfer
    # CryptoDeposit tracking (e.g. reconciled via a one-off manual credit
    # before this feature existed). The balance-diff fallback compares
    # on-chain balance against this PLUS the sum of credited CryptoDeposit
    # rows for this chain -- without it, that comparison under-counts
    # anything credited before CryptoDeposit rows existed, and re-credits
    # it as if it were new (confirmed in production: caused a real $5
    # double-credit). Defaults to 0, which is correct for every wallet+
    # chain scanned from now on -- this field only matters for the
    # handful of checkpoints that predate it.
    pre_ledger_baseline_cents = Column(Integer, default=0, nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index('idx_chain_checkpoint_wallet_chain', 'wallet_address', 'chain', unique=True),
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

    Recipient bank details (routing/account) are sent directly to the ACH
    processor and NOT persisted on our side (only last 4 for reference).

    Processed via Stripe Payouts (instant, <30 sec).
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
    ach_id = Column(String, nullable=True, unique=True, index=True)  # reserved for a future direct-ACH processor
    stripe_payout_id = Column(String, nullable=True, unique=True, index=True)  # Stripe payout ID (if using Stripe)
    stripe_payout_status = Column(String, nullable=True)  # in_transit | paid | failed (Stripe status tracking)
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


class CryptoTrade(Base):
    """A cryptocurrency token swap trade via Uniswap on Polygon.

    Tracks all user trades: price quotes, executed trades, and settlement status.
    Each trade charges a $0.01 flat platform fee on top of Uniswap gas costs.

    States:
    - quote: User requested a price quote (no money debited)
    - pending: Trade submitted, awaiting user signature or blockchain confirmation
    - completed: Trade confirmed and settled
    - failed: Transaction failed or was rejected by user
    - cancelled: User cancelled before signing

    Rate limited to 100 trades per day per user.
    """
    __tablename__ = "crypto_trades"

    id = Column(String, primary_key=True, default=new_id)
    user_id = Column(String, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)

    # Trade pair
    from_token = Column(String, nullable=False)  # symbol: USDC, ETH, MATIC, etc
    to_token = Column(String, nullable=False)

    # Amounts (in cents for USD-like tokens, wei-compatible for others)
    from_amount_cents = Column(Integer, nullable=False)  # what user sent
    to_amount_cents = Column(Integer, nullable=True)  # what user received (null until confirmed)
    expected_to_amount_cents = Column(Integer, nullable=False)  # quoted amount (slippage-adjusted)

    # Pricing & fees
    price_per_unit = Column(String, nullable=False)  # human-readable price (e.g., "$2511.55")
    slippage_tolerance_percent = Column(String, default="0.50", nullable=False)  # user's allowed slippage
    slippage_applied_percent = Column(String, nullable=True)  # actual slippage after execution
    platform_fee_cents = Column(Integer, default=100, nullable=False)  # $0.01 = 100 cents
    gas_estimate_cents = Column(Integer, nullable=True)  # Uniswap gas cost estimate
    actual_gas_used_cents = Column(Integer, nullable=True)  # actual after tx confirmed
    total_cost_cents = Column(Integer, nullable=True)  # fee + gas combined (fee + gas estimate at quote time)

    # Trade execution
    status = Column(String, default="quote", nullable=False, index=True)
    # quote | pending | completed | failed | cancelled
    tx_hash = Column(String, nullable=True, unique=True, index=True)  # blockchain transaction hash
    idempotency_key = Column(String, nullable=False, unique=True, index=True)  # prevent duplicate submits

    # P&L tracking (for history view)
    value_now_cents = Column(Integer, nullable=True)  # current market value of received tokens
    gain_loss_cents = Column(Integer, nullable=True)  # market value - investment (USD cents)
    gain_loss_percent = Column(String, nullable=True)  # percentage change

    # Audit & error handling
    error_message = Column(String, nullable=True)  # if status=failed, why?
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)  # when user signed
    completed_at = Column(DateTime(timezone=True), nullable=True)  # when tx confirmed

    # Constraints and indexes
    __table_args__ = (
        CheckConstraint("from_amount_cents > 0"),
        CheckConstraint("expected_to_amount_cents > 0"),
        CheckConstraint("status IN ('quote', 'pending', 'completed', 'failed', 'cancelled')"),
        Index('idx_crypto_trade_user_created', 'user_id', 'created_at'),
        Index('idx_crypto_trade_user_status', 'user_id', 'status'),
        Index('idx_crypto_trade_pending', 'status', 'created_at'),
    )
