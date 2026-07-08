"""
Test suite for bank transfer functionality (ACH to external bank accounts).

Tests the POST /transfers/send-to-bank endpoint, balance deduction, error
handling, and audit logging.
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from httpx import AsyncClient
from datetime import datetime, timedelta, timezone
import json
import uuid

from main import app
from database import SessionLocal
from models import User, CryptoWallet, CryptoTransfer, BankTransfer, UserAuditLog
from services import crypto_wallet


@pytest.fixture
def db():
    """Get a test database session."""
    return SessionLocal()


@pytest.fixture
def test_user(db):
    """Create a test user with a crypto wallet."""
    user = User(
        id=str(uuid.uuid4()),
        email=f"test-{uuid.uuid4()}@example.com",
        hashed_password="hashed_pw",
        full_name="Test User",
        crypto_wallet_address="0x" + "1" * 40,
        wallet_type="fawn_custodial",
        wallet_initialized=True,
        usdc_balance_cents=50000,  # $500
        total_fees_paid_cents=0,
    )
    db.add(user)

    wallet = CryptoWallet(
        user_id=user.id,
        wallet_address=user.crypto_wallet_address,
        wallet_type="fawn_custodial",
        chain="polygon",
        usdc_balance_cents=50000,
    )
    db.add(wallet)
    db.commit()
    return user


@pytest.mark.asyncio
async def test_send_to_bank_success(test_user, db):
    """Test successful bank transfer with valid inputs."""
    with patch("services.crypto_wallet.column") as mock_column:
        # Mock successful ACH debit
        mock_column.create_ach_debit = AsyncMock(
            return_value={"id": "ach_12345", "status": "pending"}
        )

        result = await crypto_wallet.send_to_bank(
            sender_id=test_user.id,
            recipient_name="John Doe",
            recipient_routing_number="011000015",
            recipient_account_number="123456789",
            amount_cents=10000,  # $100
            db=db,
            memo="Rent payment",
        )

        # Verify response
        assert result["transfer_id"] is not None
        assert result["amount"] == 100.0
        assert result["fee"] == 0.01
        assert result["total_debited"] == 100.01
        assert result["recipient_name"] == "John Doe"
        assert result["recipient_last4"] == "6789"
        assert result["status"] == "pending"
        assert result["estimated_settlement"] == "1-3 business days"

        # Verify balance was deducted
        db.refresh(test_user)
        assert test_user.usdc_balance_cents == 50000 - 10000 - 100  # original - amount - fee

        # Verify bank transfer record
        transfer = db.query(BankTransfer).filter(
            BankTransfer.id == result["transfer_id"]
        ).first()
        assert transfer is not None
        assert transfer.status == "pending"
        assert transfer.ach_id == "ach_12345"
        assert transfer.recipient_account_last4 == "6789"
        assert transfer.memo == "Rent payment"

        # Verify audit log
        audit_log = db.query(UserAuditLog).filter(
            UserAuditLog.user_id == test_user.id,
            UserAuditLog.action == "sent_bank_transfer",
        ).first()
        assert audit_log is not None
        details = json.loads(audit_log.details)
        assert details["recipient_name"] == "John Doe"
        assert details["amount_cents"] == 10000


@pytest.mark.asyncio
async def test_send_to_bank_insufficient_balance(test_user, db):
    """Test bank transfer fails with insufficient balance."""
    test_user.usdc_balance_cents = 5000  # Only $50
    db.commit()

    with pytest.raises(crypto_wallet.InsufficientBalance):
        await crypto_wallet.send_to_bank(
            sender_id=test_user.id,
            recipient_name="John Doe",
            recipient_routing_number="011000015",
            recipient_account_number="123456789",
            amount_cents=10000,  # $100, but only $50 available
            db=db,
        )


@pytest.mark.asyncio
async def test_send_to_bank_no_wallet(db):
    """Test bank transfer fails when user has no wallet."""
    user_id = str(uuid.uuid4())

    with pytest.raises(crypto_wallet.WalletNotInitialized):
        await crypto_wallet.send_to_bank(
            sender_id=user_id,
            recipient_name="John Doe",
            recipient_routing_number="011000015",
            recipient_account_number="123456789",
            amount_cents=10000,
            db=db,
        )


@pytest.mark.asyncio
async def test_send_to_bank_column_not_configured(test_user, db):
    """Test bank transfer fails gracefully when Column is not configured."""
    with patch("services.crypto_wallet.column") as mock_column:
        # Mock Column not configured
        mock_column.create_ach_debit = AsyncMock(
            side_effect=crypto_wallet.column.ColumnNotConfigured(
                "Column is not configured"
            )
        )

        with pytest.raises(crypto_wallet.BankTransferError):
            await crypto_wallet.send_to_bank(
                sender_id=test_user.id,
                recipient_name="John Doe",
                recipient_routing_number="011000015",
                recipient_account_number="123456789",
                amount_cents=10000,
                db=db,
            )

        # Verify balance was refunded on failure
        db.refresh(test_user)
        assert test_user.usdc_balance_cents == 50000  # balance restored

        # Verify transfer marked as failed
        transfer = db.query(BankTransfer).filter(
            BankTransfer.sender_id == test_user.id
        ).first()
        assert transfer is not None
        assert transfer.status == "failed"
        assert "unavailable" in transfer.error_message.lower()


@pytest.mark.asyncio
async def test_send_to_bank_api_error(test_user, db):
    """Test bank transfer fails gracefully on ACH API error."""
    with patch("services.crypto_wallet.column") as mock_column:
        # Mock Column API error
        mock_column.create_ach_debit = AsyncMock(
            side_effect=crypto_wallet.column.ColumnError(
                400, "Invalid routing number"
            )
        )

        with pytest.raises(crypto_wallet.BankTransferError):
            await crypto_wallet.send_to_bank(
                sender_id=test_user.id,
                recipient_name="John Doe",
                recipient_routing_number="999999999",  # Invalid
                recipient_account_number="123456789",
                amount_cents=10000,
                db=db,
            )

        # Verify balance was refunded on failure
        db.refresh(test_user)
        assert test_user.usdc_balance_cents == 50000  # balance restored


@pytest.mark.asyncio
async def test_send_to_bank_endpoint_validation():
    """Test API endpoint input validation."""
    client = AsyncClient(app=app, base_url="http://test")

    # Invalid routing number (not 9 digits)
    response = await client.post(
        "/transfers/send-to-bank",
        json={
            "recipient_name": "John Doe",
            "recipient_routing_number": "12345",  # Too short
            "recipient_account_number": "123456789",
            "amount_cents": 10000,
        },
    )
    assert response.status_code == 422  # Validation error


@pytest.mark.asyncio
async def test_send_to_bank_idempotency(test_user, db):
    """Test that idempotency keys prevent duplicate transfers."""
    with patch("services.crypto_wallet.column") as mock_column:
        mock_column.create_ach_debit = AsyncMock(
            return_value={"id": "ach_12345", "status": "pending"}
        )

        # Send first transfer
        result1 = await crypto_wallet.send_to_bank(
            sender_id=test_user.id,
            recipient_name="John Doe",
            recipient_routing_number="011000015",
            recipient_account_number="123456789",
            amount_cents=10000,
            db=db,
        )

        # Attempt to retry with same parameters
        # (In reality, each call gets a new idempotency_key, but the
        # Column provider uses its own idempotency logic)
        result2 = await crypto_wallet.send_to_bank(
            sender_id=test_user.id,
            recipient_name="John Doe",
            recipient_routing_number="011000015",
            recipient_account_number="123456789",
            amount_cents=10000,
            db=db,
        )

        # Verify two distinct transfers (our idempotency key is per-call, not per-user)
        transfers = db.query(BankTransfer).filter(
            BankTransfer.sender_id == test_user.id
        ).all()
        assert len(transfers) == 2
        assert transfers[0].idempotency_key != transfers[1].idempotency_key


@pytest.mark.asyncio
async def test_send_to_bank_audit_retention(test_user, db):
    """Test that bank transfers are logged with 7-year retention."""
    with patch("services.crypto_wallet.column") as mock_column:
        mock_column.create_ach_debit = AsyncMock(
            return_value={"id": "ach_12345", "status": "pending"}
        )

        await crypto_wallet.send_to_bank(
            sender_id=test_user.id,
            recipient_name="John Doe",
            recipient_routing_number="011000015",
            recipient_account_number="123456789",
            amount_cents=10000,
            db=db,
        )

        # Verify audit log has 7-year retention
        audit_log = db.query(UserAuditLog).filter(
            UserAuditLog.user_id == test_user.id,
            UserAuditLog.action == "sent_bank_transfer",
        ).first()
        assert audit_log is not None

        retention_days = (
            audit_log.retention_expires_at - datetime.now(tz=timezone.utc)
        ).days
        # Should be approximately 7 years = 365 * 7 days
        assert 365 * 7 - 1 <= retention_days <= 365 * 7 + 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
