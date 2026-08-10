"""Merchant onboarding models: KYB (know-your-business), API keys, and
settlement configuration.

Kept in a separate module from models.py so merchant onboarding can evolve
without touching the core ledger models. Shares the same declarative Base,
so main.py's Base.metadata.create_all() picks these up automatically once
this module is imported (routers/merchant_onboarding.py imports it).

DESIGN NOTES
------------
- KYB is a *record of what the business attested and what FAWN verified*.
  It deliberately stores an EIN only as last-4 plus a salted hash: FAWN has
  no need to ever display a full EIN, and storing one creates breach
  liability with no product benefit.
- Cannabis / other regulated verticals get first-class fields
  (state license number + expiry) because for a marijuana-related business
  (MRB) the license is the single most important diligence artifact, and it
  EXPIRES — an onboarding system that captures it once and never re-checks
  is how processors end up serving unlicensed sellers.
- API keys are stored ONLY as a SHA-256 hash. The plaintext key is returned
  exactly once, at creation. There is no "show key again" path by design.
"""
import hashlib
import secrets

from sqlalchemy import (
    Boolean, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String, Text,
)
from sqlalchemy.sql import func

from database import Base
from models import new_id


# --- Verification lifecycle -------------------------------------------------
KYB_STATUSES = ("draft", "submitted", "under_review", "verified", "rejected", "expired")

# Verticals FAWN treats as high-risk: they can never be auto-approved and
# require a human reviewer plus (where applicable) a valid state license.
HIGH_RISK_VERTICALS = ("cannabis", "cbd_hemp", "firearms", "adult", "crypto_atm", "money_services")


def hash_secret(raw: str) -> str:
    """SHA-256 hex digest. Used for API keys and EIN fingerprints.

    API keys are high-entropy (256-bit) random strings, so a plain SHA-256 is
    appropriate and fast; they are NOT low-entropy user passwords (those use
    bcrypt elsewhere in this codebase).
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class MerchantKyb(Base):
    """Know-Your-Business record for a merchant account.

    One row per merchant. Holds the business's attested identity, its
    regulated-vertical licensing (if any), and FAWN's verification outcome.
    """
    __tablename__ = "merchant_kyb"

    id = Column(String, primary_key=True, default=new_id)
    merchant_id = Column(
        String, ForeignKey("merchant_accounts.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )

    # --- Legal identity ---
    legal_business_name = Column(String, nullable=False)
    entity_type = Column(String, nullable=True)          # llc | corporation | sole_prop | partnership
    state_of_incorporation = Column(String, nullable=True)
    ein_last4 = Column(String, nullable=True)            # display only
    ein_hash = Column(String, nullable=True)             # fingerprint for dedupe; never reversible

    # --- Physical location (drives which state's rules apply) ---
    address_line1 = Column(String, nullable=True)
    address_line2 = Column(String, nullable=True)
    city = Column(String, nullable=True)
    state = Column(String, nullable=True, index=True)
    postal_code = Column(String, nullable=True)

    # --- Vertical / risk ---
    vertical = Column(String, nullable=False, default="general", index=True)
    is_high_risk = Column(Boolean, nullable=False, default=False, index=True)

    # --- Regulated licensing (cannabis and similar) ---
    state_license_number = Column(String, nullable=True, index=True)
    state_license_state = Column(String, nullable=True)
    state_license_expires_on = Column(DateTime(timezone=True), nullable=True)
    license_verified_at = Column(DateTime(timezone=True), nullable=True)
    license_verified_by = Column(String, nullable=True)   # admin identifier / "manual"

    # --- Beneficial owners: JSON list of {name, title, ownership_percent}.
    # Names/titles/percentages only — no SSNs. FAWN does not need, and
    # therefore must not hold, owner SSNs for this flow.
    beneficial_owners_json = Column(Text, nullable=True)

    # --- Attestations (the merchant affirmatively agrees) ---
    attested_accurate = Column(Boolean, nullable=False, default=False)
    attested_compliance = Column(Boolean, nullable=False, default=False)
    attested_at = Column(DateTime(timezone=True), nullable=True)

    # --- Outcome ---
    status = Column(String, nullable=False, default="draft", index=True)
    review_notes = Column(Text, nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','submitted','under_review','verified','rejected','expired')",
            name="ck_merchant_kyb_status",
        ),
        Index("idx_merchant_kyb_review_queue", "status", "is_high_risk"),
    )


class MerchantApiKey(Base):
    """Server-to-server API credential for a merchant's POS or website.

    Only the hash is stored. `key_prefix` is a short non-secret identifier
    shown in dashboards ("fawn_sk_live_a1b2…") so a merchant can tell keys
    apart and revoke the right one.
    """
    __tablename__ = "merchant_api_keys"

    id = Column(String, primary_key=True, default=new_id)
    merchant_id = Column(
        String, ForeignKey("merchant_accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    label = Column(String, nullable=False, default="default")
    key_prefix = Column(String, nullable=False, index=True)
    key_hash = Column(String, nullable=False, unique=True, index=True)
    mode = Column(String, nullable=False, default="test", index=True)  # test | live
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint("mode IN ('test','live')", name="ck_merchant_api_key_mode"),
    )

    @staticmethod
    def generate(mode: str = "test") -> tuple[str, str, str]:
        """Return (plaintext_key, key_prefix, key_hash). Plaintext is shown once."""
        raw = f"fawn_sk_{mode}_{secrets.token_hex(24)}"
        return raw, raw[:20], hash_secret(raw)


class MerchantSettlement(Base):
    """How a merchant gets paid out.

    FAWN settles merchant checkouts into the merchant's own FAWN custodial
    USDC balance instantly (an internal ledger move). This row records what
    the merchant wants to happen *after* that: hold as USDC, or auto-withdraw
    on-chain to an address they control.

    Deliberately NOT modelled: automatic fiat/bank payout. For a
    marijuana-related business that step requires an MRB-friendly banking
    partner and is the single hardest part of the stack — it must not be
    implied as working in code before it exists.
    """
    __tablename__ = "merchant_settlement"

    id = Column(String, primary_key=True, default=new_id)
    merchant_id = Column(
        String, ForeignKey("merchant_accounts.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )
    method = Column(String, nullable=False, default="hold_usdc")  # hold_usdc | auto_withdraw_usdc
    payout_address = Column(String, nullable=True)                 # 0x… for auto_withdraw_usdc
    payout_chain = Column(String, nullable=True, default="polygon")
    # Auto-withdraw only fires once the balance clears this floor, so a
    # merchant never pays on-chain gas to move trivial amounts.
    min_payout_cents = Column(Integer, nullable=False, default=25_000)  # $250
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("method IN ('hold_usdc','auto_withdraw_usdc')", name="ck_merchant_settlement_method"),
        CheckConstraint("min_payout_cents >= 0", name="ck_merchant_settlement_min"),
    )
