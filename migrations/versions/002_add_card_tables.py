"""Add card, transaction, and audit tables for debit card authorization.

Revision ID: 002
Revises: 001
Create Date: 2026-07-08 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '002'
down_revision = '001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create cards table
    op.create_table(
        'cards',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('user_id', sa.String(36), nullable=False),
        sa.Column('lithic_card_token', sa.String(255), nullable=False),
        sa.Column('card_last_four', sa.String(4), nullable=False),
        sa.Column('card_brand', sa.String(50), nullable=False, server_default='VISA'),
        sa.Column('wallet_address', sa.String(255), nullable=False),
        sa.Column('status', sa.Enum('PENDING', 'ACTIVE', 'FROZEN', 'SUSPENDED', 'REVOKED', name='cardstatus'), nullable=False, server_default='PENDING'),
        sa.Column('is_virtual', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('daily_transaction_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('daily_transaction_total_cents', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('monthly_transaction_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('monthly_transaction_total_cents', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('daily_limit_cents', sa.Integer(), nullable=False, server_default='1000000'),
        sa.Column('monthly_limit_cents', sa.Integer(), nullable=False, server_default='30000000'),
        sa.Column('transaction_limit_cents', sa.Integer(), nullable=False, server_default='100000'),
        sa.Column('issued_at', sa.DateTime(), nullable=False),
        sa.Column('activated_at', sa.DateTime(), nullable=True),
        sa.Column('frozen_at', sa.DateTime(), nullable=True),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.Column('processor_status', sa.String(50), nullable=False, server_default='active'),
        sa.Column('processor_error', sa.Text(), nullable=True),
        sa.Column('requires_manual_review', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('manual_review_reason', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('lithic_card_token', name='uq_lithic_token'),
        sa.UniqueConstraint('user_id', 'lithic_card_token', name='uq_user_card'),
    )
    op.create_index('ix_cards_user_status', 'cards', ['user_id', 'status'])
    op.create_index('ix_cards_wallet', 'cards', ['wallet_address'])

    # Create card_transactions table
    op.create_table(
        'card_transactions',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('processor_transaction_id', sa.String(255), nullable=False),
        sa.Column('idempotency_key', sa.String(255), nullable=False),
        sa.Column('card_id', sa.String(36), nullable=False),
        sa.Column('user_id', sa.String(36), nullable=False),
        sa.Column('wallet_address', sa.String(255), nullable=False),
        sa.Column('merchant_name', sa.String(255), nullable=False),
        sa.Column('merchant_mcc', sa.String(4), nullable=True),
        sa.Column('transaction_amount_cents', sa.Integer(), nullable=False),
        sa.Column('usdc_amount_cents', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(3), nullable=False, server_default='USD'),
        sa.Column('status', sa.Enum('PENDING', 'AUTHORIZED', 'DECLINED', 'APPROVED', 'REVERSED', 'SETTLED', 'FAILED', name='transactionstatus'), nullable=False, server_default='PENDING'),
        sa.Column('wallet_balance_at_auth_cents', sa.Integer(), nullable=True),
        sa.Column('auth_approved', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('auth_decline_reason', sa.String(255), nullable=True),
        sa.Column('requested_at', sa.DateTime(), nullable=False),
        sa.Column('authorized_at', sa.DateTime(), nullable=True),
        sa.Column('settled_at', sa.DateTime(), nullable=True),
        sa.Column('processor_response_code', sa.String(10), nullable=True),
        sa.Column('processor_response_message', sa.String(255), nullable=True),
        sa.Column('fallback_status', sa.String(50), nullable=True),
        sa.Column('requires_manual_review', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('manual_review_notes', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('processor_transaction_id', name='uq_processor_txn'),
        sa.UniqueConstraint('idempotency_key', name='uq_idempotency'),
    )
    op.create_index('ix_card_txn_user_status', 'card_transactions', ['user_id', 'status'])
    op.create_index('ix_card_txn_card_status', 'card_transactions', ['card_id', 'status'])
    op.create_index('ix_card_txn_wallet', 'card_transactions', ['wallet_address'])
    op.create_index('ix_card_txn_date', 'card_transactions', ['requested_at'])

    # Create card_audit_logs table
    op.create_table(
        'card_audit_logs',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('event_type', sa.Enum('CARD_ISSUED', 'CARD_ACTIVATED', 'CARD_FROZEN', 'CARD_UNFROZEN', 'CARD_SUSPENDED', 'CARD_REVOKED', 'AUTH_ATTEMPTED', 'AUTH_APPROVED', 'AUTH_DECLINED', 'AUTH_REVERSED', 'BALANCE_CHECK', 'PROCESSOR_ERROR', 'FALLBACK_MANUAL_REVIEW', name='auditeventtype'), nullable=False),
        sa.Column('entity_type', sa.String(50), nullable=False),
        sa.Column('entity_id', sa.String(36), nullable=False),
        sa.Column('user_id', sa.String(36), nullable=False),
        sa.Column('actor_type', sa.String(50), nullable=False, server_default='user'),
        sa.Column('actor_id', sa.String(36), nullable=True),
        sa.Column('details', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('retention_until', sa.DateTime(), nullable=False),
        sa.Column('processor_id', sa.String(255), nullable=True),
        sa.Column('processor_event_id', sa.String(255), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('processor_event_id', name='uq_processor_event'),
    )
    op.create_index('ix_audit_user_type', 'card_audit_logs', ['user_id', 'event_type'])
    op.create_index('ix_audit_entity', 'card_audit_logs', ['entity_type', 'entity_id'])
    op.create_index('ix_audit_retention', 'card_audit_logs', ['retention_until'])

    # Create card_rate_limits table
    op.create_table(
        'card_rate_limits',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('user_id', sa.String(36), nullable=False),
        sa.Column('cards_issued_today', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('cards_issued_today_reset_at', sa.DateTime(), nullable=False),
        sa.Column('last_reset_at', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', name='uq_rate_limit_user'),
    )
    op.create_index('ix_rate_limit_user', 'card_rate_limits', ['user_id'])

    # Create processor_webhook_logs table
    op.create_table(
        'processor_webhook_logs',
        sa.Column('id', sa.String(36), nullable=False),
        sa.Column('webhook_id', sa.String(255), nullable=False),
        sa.Column('event_type', sa.String(50), nullable=False),
        sa.Column('payload_hash', sa.String(64), nullable=False),
        sa.Column('processed', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_attempt_at', sa.DateTime(), nullable=True),
        sa.Column('last_attempt_status', sa.Integer(), nullable=True),
        sa.Column('received_at', sa.DateTime(), nullable=False),
        sa.Column('processed_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('webhook_id', name='uq_webhook_id'),
    )
    op.create_index('ix_webhook_event_type', 'processor_webhook_logs', ['event_type'])
    op.create_index('ix_webhook_received', 'processor_webhook_logs', ['received_at'])


def downgrade() -> None:
    op.drop_index('ix_webhook_received', table_name='processor_webhook_logs')
    op.drop_index('ix_webhook_event_type', table_name='processor_webhook_logs')
    op.drop_table('processor_webhook_logs')

    op.drop_index('ix_rate_limit_user', table_name='card_rate_limits')
    op.drop_table('card_rate_limits')

    op.drop_index('ix_audit_retention', table_name='card_audit_logs')
    op.drop_index('ix_audit_entity', table_name='card_audit_logs')
    op.drop_index('ix_audit_user_type', table_name='card_audit_logs')
    op.drop_table('card_audit_logs')

    op.drop_index('ix_card_txn_date', table_name='card_transactions')
    op.drop_index('ix_card_txn_wallet', table_name='card_transactions')
    op.drop_index('ix_card_txn_card_status', table_name='card_transactions')
    op.drop_index('ix_card_txn_user_status', table_name='card_transactions')
    op.drop_table('card_transactions')

    op.drop_index('ix_cards_wallet', table_name='cards')
    op.drop_index('ix_cards_user_status', table_name='cards')
    op.drop_table('cards')
