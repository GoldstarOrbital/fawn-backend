"""add off-chain Fawn Bucks ledger"""
from alembic import op
import sqlalchemy as sa

revision = "20260814_fawn_bucks"
down_revision = "c8a93dfde825"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("buck_funding_payments", sa.Column("id", sa.String(), primary_key=True), sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False), sa.Column("stripe_checkout_session_id", sa.String(), unique=True), sa.Column("stripe_payment_intent_id", sa.String(), unique=True), sa.Column("amount_cents", sa.Integer(), nullable=False), sa.Column("fee_cents", sa.Integer(), nullable=False), sa.Column("total_cents", sa.Integer(), nullable=False), sa.Column("buck_count", sa.Integer(), nullable=False), sa.Column("status", sa.String(), nullable=False), sa.Column("refunded_cents", sa.Integer(), nullable=False, server_default="0"), sa.Column("disputed_at", sa.DateTime(timezone=True)), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()), sa.Column("completed_at", sa.DateTime(timezone=True)))
    op.create_index("ix_buck_funding_payments_user_id", "buck_funding_payments", ["user_id"])
    op.create_table("buck_credits", sa.Column("id", sa.String(), primary_key=True), sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False), sa.Column("payment_id", sa.String(), sa.ForeignKey("buck_funding_payments.id"), nullable=False), sa.Column("serial_number", sa.String(), nullable=False, unique=True), sa.Column("status", sa.String(), nullable=False), sa.Column("issued_at", sa.DateTime(timezone=True), server_default=sa.func.now()), sa.Column("reversed_at", sa.DateTime(timezone=True)))
    op.create_index("ix_buck_credits_user_id", "buck_credits", ["user_id"])
    op.create_index("ix_buck_credits_payment_id", "buck_credits", ["payment_id"])
    op.create_table("buck_ledger_entries", sa.Column("id", sa.String(), primary_key=True), sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False), sa.Column("payment_id", sa.String(), sa.ForeignKey("buck_funding_payments.id"), nullable=False), sa.Column("entry_type", sa.String(), nullable=False), sa.Column("bucks_delta", sa.Integer(), nullable=False), sa.Column("serial_numbers", sa.Text(), nullable=False), sa.Column("reason", sa.String()), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()))
    op.create_index("ix_buck_ledger_entries_user_id", "buck_ledger_entries", ["user_id"])
    op.create_index("ix_buck_ledger_entries_payment_id", "buck_ledger_entries", ["payment_id"])

def downgrade():
    op.drop_table("buck_ledger_entries")
    op.drop_table("buck_credits")
    op.drop_table("buck_funding_payments")
