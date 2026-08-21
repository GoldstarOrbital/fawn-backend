"""Apply the one-cent fee schema for cash-outs and investing orders.

Revision ID: d1e0f001cents
Revises: c8a93dfde825
"""
from alembic import op
import sqlalchemy as sa


revision = "d1e0f001cents"
down_revision = "c8a93dfde825"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stablecoin_redemptions",
        sa.Column("fee_cents", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "investing_orders",
        sa.Column("fee_cents", sa.Integer(), nullable=False, server_default="0"),
    )
    op.drop_constraint("ck_redemption_one_to_one", "stablecoin_redemptions", type_="check")
    op.create_check_constraint(
        "ck_redemption_payout_and_fee",
        "stablecoin_redemptions",
        "payout_cents + fee_cents = usdc_cents",
    )
    op.create_check_constraint(
        "ck_redemption_fee_nonneg", "stablecoin_redemptions", "fee_cents >= 0"
    )
    op.alter_column("stablecoin_redemptions", "fee_cents", server_default=None)
    op.alter_column("investing_orders", "fee_cents", server_default=None)


def downgrade() -> None:
    op.drop_constraint("ck_redemption_fee_nonneg", "stablecoin_redemptions", type_="check")
    op.drop_constraint("ck_redemption_payout_and_fee", "stablecoin_redemptions", type_="check")
    op.create_check_constraint(
        "ck_redemption_one_to_one", "stablecoin_redemptions", "payout_cents = usdc_cents"
    )
    op.drop_column("investing_orders", "fee_cents")
    op.drop_column("stablecoin_redemptions", "fee_cents")
