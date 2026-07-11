"""
Card models for FAWN debit card authorization system.
- Cards linked 1:1 to User USDC wallets
- Transaction history with audit logging
- Compliance: 7-year audit trail, transaction idempotency
"""

from sqlalchemy import Column, String, Integer, DateTime, Boolean, Enum, Float, Text, UniqueConstraint, Index
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime
import enum
import uuid

Base = declarative_base()


class CardStatus(str, enum.Enum):
    """Card lifecycle states"""
    PENDING = "pending"
    ACTIVE = "active"
    FROZEN = "frozen"
    SUSPENDED = "suspended"  # Admin action (fraud, breach)
    REVOKED = "revoked"  # User requested closure


class TransactionStatus(str, enum.Enum):
    """Transaction lifecycle (idempotency via idempotency_key)"""
    PENDING = "pending"
    AUTHORIZED = "authorized"
    DECLINED = "declined"
    APPROVED = "approved"
    REVERSED = "reversed"
    SETTLED = "settled"
    FAILED = "failed"


class AuditEventType(str, enum.Enum):
    """Audit log event types (7-year retention)"""
    CARD_ISSUED = "card_issued"
    CARD_ACTIVATED = "card_activated"
    CARD_FROZEN = "card_frozen"
    CARD_UNFROZEN = "card_unfrozen"
    CARD_SUSPENDED = "card_suspended"
    CARD_REVOKED = "card_revoked"
    AUTH_ATTEMPTED = "auth_attempted"
    AUTH_APPROVED = "auth_approved"
    AUTH_DECLINED = "auth_declined"
    AUTH_REVERSED = "auth_reversed"
    BALANCE_CHECK = "balance_check"
    PROCESSOR_ERROR = "processor_error"
    FALLBACK_MANUAL_REVIEW = "fallback_manual_review"


class Card(Base):
    """
    Virtual debit card linked to USDC wallet.
    - 1:1 with User.crypto_wallet_address
    - Issued via Lithic processor
    """
    __tablename__ = "cards"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), nullable=False)

    # Card issuance details
    lithic_card_token = Column(String(255), unique=True, nullable=False, index=True)  # Processor card ID
    card_last_four = Column(String(4), nullable=False)  # Last 4 digits displayed to user
    card_brand = Column(String(50), default="VISA")

    # Wallet link (denormalized for speed)
    wallet_address = Column(String(255), nullable=False, index=True)

    # Card state
    status = Column(
        Enum(CardStatus),
        default=CardStatus.PENDING,
        nullable=False,
        index=True
    )
    is_virtual = Column(Boolean, default=True)  # Virtual=immediate, Physical=shipped

    # Rate limiting & velocity
    daily_transaction_count = Column(Integer, default=0)
    daily_transaction_total_cents = Column(Integer, default=0)
    monthly_transaction_count = Column(Integer, default=0)
    monthly_transaction_total_cents = Column(Integer, default=0)

    # Daily limits (configurable per card)
    daily_limit_cents = Column(Integer, default=1000000)  # $10,000/day default
    monthly_limit_cents = Column(Integer, default=30000000)  # $300,000/month
    transaction_limit_cents = Column(Integer, default=100000)  # $1,000 per txn

    # Metadata
    issued_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    activated_at = Column(DateTime, nullable=True)
    frozen_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)

    # Processor details
    processor_status = Column(String(50), default="active")  # Lithic status
    processor_error = Column(Text, nullable=True)

    # For manual review fallback
    requires_manual_review = Column(Boolean, default=False)
    manual_review_reason = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint('user_id', 'lithic_card_token', name='uq_user_card'),
        Index('ix_cards_user_status', 'user_id', 'status'),
        Index('ix_cards_wallet', 'wallet_address'),
    )


class CardTransaction(Base):
    """
    Transaction record with idempotency.
    - Processor calls via webhook (processor_transaction_id is unique)
    - Same processor_transaction_id = idempotent (returned 200, no charge)
    """
    __tablename__ = "card_transactions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # Processor tracking (idempotency key)
    processor_transaction_id = Column(String(255), unique=True, nullable=False, index=True)
    idempotency_key = Column(String(255), unique=True, nullable=False, index=True)  # Upstream unique id

    # Card & wallet
    card_id = Column(String(36), nullable=False, index=True)
    user_id = Column(String(36), nullable=False, index=True)
    wallet_address = Column(String(255), nullable=False, index=True)

    # Transaction details
    merchant_name = Column(String(255), nullable=False)
    merchant_mcc = Column(String(4), nullable=True)  # Merchant Category Code
    transaction_amount_cents = Column(Integer, nullable=False)  # Amount user sees (USD)
    usdc_amount_cents = Column(Integer, nullable=False)  # Actual USDC charged (may differ if rates)
    currency = Column(String(3), default="USD")

    # Status tracking
    status = Column(
        Enum(TransactionStatus),
        default=TransactionStatus.PENDING,
        nullable=False,
        index=True
    )

    # Balance check (auth decision)
    wallet_balance_at_auth_cents = Column(Integer, nullable=True)
    auth_approved = Column(Boolean, default=False)
    auth_decline_reason = Column(String(255), nullable=True)

    # Timing
    requested_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    authorized_at = Column(DateTime, nullable=True)
    settled_at = Column(DateTime, nullable=True)

    # Processor details
    processor_response_code = Column(String(10), nullable=True)
    processor_response_message = Column(String(255), nullable=True)

    # Fallback & manual review
    fallback_status = Column(String(50), nullable=True)  # 'processor_timeout', 'processor_error', 'manual_review'
    requires_manual_review = Column(Boolean, default=False)
    manual_review_notes = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint('processor_transaction_id', name='uq_processor_txn'),
        UniqueConstraint('idempotency_key', name='uq_idempotency'),
        Index('ix_card_txn_user_status', 'user_id', 'status'),
        Index('ix_card_txn_card_status', 'card_id', 'status'),
        Index('ix_card_txn_wallet', 'wallet_address'),
        Index('ix_card_txn_date', 'requested_at'),
    )


class CardAuditLog(Base):
    """
    7-year audit trail (compliance).
    - Immutable: INSERT-only, no UPDATE/DELETE
    - Tracks all card and transaction events
    """
    __tablename__ = "card_audit_logs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # Event details
    event_type = Column(Enum(AuditEventType), nullable=False, index=True)
    entity_type = Column(String(50), nullable=False)  # 'card', 'transaction'
    entity_id = Column(String(36), nullable=False, index=True)

    # User & context
    user_id = Column(String(36), nullable=False, index=True)
    actor_type = Column(String(50), default="user")  # 'user', 'processor', 'admin', 'system'
    actor_id = Column(String(36), nullable=True)

    # Data (immutable snapshot)
    details = Column(Text, nullable=True)  # JSON blob of event data

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    retention_until = Column(DateTime, nullable=False)  # 7 years for compliance

    # Processor info
    processor_id = Column(String(255), nullable=True)
    processor_event_id = Column(String(255), nullable=True, unique=True)

    __table_args__ = (
        Index('ix_audit_user_type', 'user_id', 'event_type'),
        Index('ix_audit_entity', 'entity_type', 'entity_id'),
        Index('ix_audit_retention', 'retention_until'),  # For cleanup queries
    )


class CardRateLimit(Base):
    """
    Rate limiting state (sliding window).
    - Prevent spam/fraud (e.g., rapid card creation)
    """
    __tablename__ = "card_rate_limits"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), unique=True, nullable=False, index=True)

    # Issue rate (cards per day)
    cards_issued_today = Column(Integer, default=0)
    cards_issued_today_reset_at = Column(DateTime, nullable=False)

    # Transaction rate (per card)
    last_reset_at = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index('ix_rate_limit_user', 'user_id'),
    )


class ProcessorWebhookLog(Base):
    """
    Webhook delivery & retry tracking.
    - Processor may retry on our 5xx/timeout
    - Prevents duplicate processing
    """
    __tablename__ = "processor_webhook_logs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # Webhook details
    webhook_id = Column(String(255), unique=True, nullable=False, index=True)  # Processor-provided
    event_type = Column(String(50), nullable=False)  # 'card.issued', 'transaction.auth', etc.

    # Payload
    payload_hash = Column(String(64), nullable=False)  # SHA256 for dedup
    processed = Column(Boolean, default=False)

    # Retry info
    attempt_count = Column(Integer, default=0)
    last_attempt_at = Column(DateTime, nullable=True)
    last_attempt_status = Column(Integer, nullable=True)

    received_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    processed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index('ix_webhook_event_type', 'event_type'),
    )
