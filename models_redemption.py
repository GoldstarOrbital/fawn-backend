"""Stablecoin redemption: user sells USDC back to FAWN at exactly 1:1 for USD.

This is FAWN's own cash-out rail, independent of Stripe. FAWN is the
counterparty: the user surrenders USDC ledger balance and FAWN owes them USD.

WHY THE BALANCE IS DEBITED AT REQUEST TIME
------------------------------------------
The USDC is moved out of the user's spendable balance the moment a redemption
is requested, and held on the redemption row itself (an escrow hold). If it
stayed spendable while the request sat in the review queue, a user could
request a $500 redemption and then spend the same $500 before it was paid —
FAWN would pay out dollars it never received USDC for. Rejection and
cancellation refund the exact held amount.

EXACTLY 1:1, NO SPREAD
----------------------
`payout_cents == usdc_cents`, enforced by a DB CHECK constraint. FAWN makes
its money on the $0.01 transfer fee, not on a redemption spread. Encoding this
as a constraint rather than a convention means a future code change cannot
quietly introduce a spread.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not move fiat. No ACH, no wire, no card. An operator executes the
actual USD payment through whatever banking rail exists and then records the
reference here. Anything else would imply a payment capability FAWN does not
have.
"""
from sqlalchemy import (
    CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, String, Text,
)
from sqlalchemy.sql import func

from database import Base
from models import new_id

# requested → approved → paid   (terminal: paid, rejected, cancelled, failed)
REDEMPTION_STATUSES = ("requested", "approved", "paid", "rejected", "cancelled", "failed")

# How the operator actually sent the dollars. Recorded for reconciliation and
# audit; FAWN does not execute any of these programmatically.
PAYOUT_METHODS = ("ach", "wire", "check", "internal_credit", "other")

# Terminal states still holding no escrow (funds already released one way or
# the other). Used by the float calculation.
OPEN_STATUSES = ("requested", "approved")


class StablecoinRedemption(Base):
    """One user's request to sell USDC back to FAWN for USD at 1:1."""
    __tablename__ = "stablecoin_redemptions"

    id = Column(String, primary_key=True, default=new_id)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    # Amounts. Equal by construction — see the CHECK constraint below.
    usdc_cents = Column(Integer, nullable=False)
    payout_cents = Column(Integer, nullable=False)

    status = Column(String, nullable=False, default="requested", index=True)

    # Escrow: how much USDC is currently held against this row. Set to
    # usdc_cents on request; zeroed when paid (consumed) or refunded.
    held_cents = Column(Integer, nullable=False, default=0)

    # Payout details, filled by the operator when the money actually moves.
    payout_method = Column(String, nullable=True)
    payout_reference = Column(String, nullable=True)   # ACH trace / wire ref / check no.
    payout_note = Column(Text, nullable=True)

    # Optional destination the user supplied (e.g. "Chase ****1234").
    # Full bank credentials are deliberately NOT stored here.
    destination_label = Column(String, nullable=True)

    # Guards against a double-submitted request creating two redemptions.
    idempotency_key = Column(String, nullable=True, unique=True, index=True)

    reviewed_by = Column(String, nullable=True)
    review_notes = Column(Text, nullable=True)

    requested_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('requested','approved','paid','rejected','cancelled','failed')",
            name="ck_redemption_status",
        ),
        # 1:1 is a database invariant, not a convention.
        CheckConstraint("payout_cents = usdc_cents", name="ck_redemption_one_to_one"),
        CheckConstraint("usdc_cents > 0", name="ck_redemption_positive"),
        CheckConstraint("held_cents >= 0", name="ck_redemption_held_nonneg"),
        Index("idx_redemption_queue", "status", "requested_at"),
        Index("idx_redemption_user_status", "user_id", "status"),
    )
