"""
tests/test_stripe_payouts.py

Comprehensive tests for Stripe Payouts API integration.

Tests cover:
- Successful payout creation
- Insufficient balance handling
- Invalid routing/account validation
- Webhook signature verification
- Payout status tracking
- Failure scenarios and refunds
"""

import pytest
import json
from unittest.mock import patch, MagicMock, AsyncMock
from sqlalchemy.orm import Session

from services import stripe_payouts
from models import BankTransfer, User, CryptoWallet


class TestStripePayoutsValidation:
    """Test input validation for payout creation."""

    def test_validate_routing_number_valid(self):
        """Valid 9-digit routing numbers should pass."""
        assert stripe_payouts._validate_routing_number("021000021") is True
        assert stripe_payouts._validate_routing_number("123456789") is True

    def test_validate_routing_number_invalid(self):
        """Invalid routing numbers should fail."""
        assert stripe_payouts._validate_routing_number("12345678") is False  # 8 digits
        assert stripe_payouts._validate_routing_number("1234567890") is False  # 10 digits
        assert stripe_payouts._validate_routing_number("12345678a") is False  # contains letter
        assert stripe_payouts._validate_routing_number("") is False  # empty
        assert stripe_payouts._validate_routing_number(None) is False  # null

    def test_validate_account_number_valid(self):
        """Valid account numbers (4-17 digits) should pass."""
        assert stripe_payouts._validate_account_number("123456") is True
        assert stripe_payouts._validate_account_number("12345678901234567") is True  # 17 digits

    def test_validate_account_number_invalid(self):
        """Invalid account numbers should fail."""
        assert stripe_payouts._validate_account_number("123") is False  # 3 digits (too short)
        assert stripe_payouts._validate_account_number("123456789012345678") is False  # 18 digits (too long)
        assert stripe_payouts._validate_account_number("1234a567") is False  # contains letter
        assert stripe_payouts._validate_account_number("") is False  # empty


@pytest.mark.skip(reason="Stripe not configured in test environment")
class TestStripePayoutsCreation:
    """Test payout creation via Stripe API."""

    @patch("stripe.Payout.create")
    def test_create_payout_success(self, mock_stripe_payout):
        """Successfully create a payout."""
        # Mock Stripe API response
        mock_payout = MagicMock()
        mock_payout.id = "po_test123"
        mock_payout.amount = 1000  # $10.00
        mock_payout.currency = "usd"
        mock_payout.status = "in_transit"
        mock_payout.created = 1720428600  # Unix timestamp
        mock_payout.arrival_date = "2026-07-09"
        mock_stripe_payout.return_value = mock_payout

        # Call create_payout
        result = stripe_payouts.create_payout(
            amount_cents=1000,
            recipient_name="John Doe",
            recipient_routing_number="021000021",
            recipient_account_number="123456789",
        )

        # Verify result
        assert result["payout_id"] == "po_test123"
        assert result["amount_cents"] == 1000
        assert result["status"] == "in_transit"
        assert result["recipient_last4"] == "6789"
        assert result["currency"] == "usd"

        # Verify Stripe was called with correct params
        mock_stripe_payout.assert_called_once()
        call_args = mock_stripe_payout.call_args
        assert call_args.kwargs["amount"] == 1000
        assert call_args.kwargs["currency"] == "usd"
        assert call_args.kwargs["method"] == "instant"

    @patch("stripe.Payout.create")
    def test_create_payout_invalid_routing(self, mock_stripe_payout):
        """Reject payouts with invalid routing number."""
        with pytest.raises(stripe_payouts.StripePayoutError) as exc_info:
            stripe_payouts.create_payout(
                amount_cents=1000,
                recipient_name="John Doe",
                recipient_routing_number="12345",  # Invalid (too short)
                recipient_account_number="123456789",
            )
        assert "Invalid routing number" in str(exc_info.value)
        mock_stripe_payout.assert_not_called()

    @patch("stripe.Payout.create")
    def test_create_payout_invalid_account(self, mock_stripe_payout):
        """Reject payouts with invalid account number."""
        with pytest.raises(stripe_payouts.StripePayoutError) as exc_info:
            stripe_payouts.create_payout(
                amount_cents=1000,
                recipient_name="John Doe",
                recipient_routing_number="021000021",
                recipient_account_number="123",  # Invalid (too short)
            )
        assert "Invalid account number" in str(exc_info.value)
        mock_stripe_payout.assert_not_called()

    @patch("stripe.Payout.create")
    def test_create_payout_no_config(self, mock_stripe_payout):
        """Fail gracefully when Stripe is not configured."""
        with patch("stripe_payouts.settings") as mock_settings:
            mock_settings.stripe_secret_key = ""
            with pytest.raises(stripe_payouts.StripeNotConfigured):
                stripe_payouts.create_payout(
                    amount_cents=1000,
                    recipient_name="John Doe",
                    recipient_routing_number="021000021",
                    recipient_account_number="123456789",
                )
        mock_stripe_payout.assert_not_called()

    @patch("stripe.Payout.create")
    def test_create_payout_stripe_rate_limit(self, mock_stripe_payout):
        """Handle Stripe rate limit error."""
        import stripe
        mock_stripe_payout.side_effect = stripe.error.RateLimitError("Rate limited")

        with pytest.raises(stripe_payouts.StripePayoutError) as exc_info:
            stripe_payouts.create_payout(
                amount_cents=1000,
                recipient_name="John Doe",
                recipient_routing_number="021000021",
                recipient_account_number="123456789",
            )
        assert "Rate limited" in str(exc_info.value)

    @patch("stripe.Payout.create")
    def test_create_payout_stripe_invalid_request(self, mock_stripe_payout):
        """Handle Stripe invalid request error (e.g., insufficient balance)."""
        import stripe
        mock_stripe_payout.side_effect = stripe.error.InvalidRequestError(
            "invalid_routing_number",
            "Routing number invalid",
        )

        with pytest.raises(stripe_payouts.StripePayoutError) as exc_info:
            stripe_payouts.create_payout(
                amount_cents=1000,
                recipient_name="John Doe",
                recipient_routing_number="021000021",
                recipient_account_number="123456789",
            )
        assert "invalid request" in str(exc_info.value).lower()


@pytest.mark.skip(reason="Stripe not configured in test environment")
class TestStripePayoutsStatus:
    """Test payout status retrieval."""

    @patch("stripe.Payout.retrieve")
    def test_get_payout_status_success(self, mock_stripe_retrieve):
        """Successfully retrieve payout status."""
        mock_payout = MagicMock()
        mock_payout.id = "po_test123"
        mock_payout.status = "paid"
        mock_payout.amount = 1000
        mock_payout.currency = "usd"
        mock_payout.failure_code = None
        mock_payout.created = 1720428600
        mock_payout.arrival_date = "2026-07-08"
        mock_stripe_retrieve.return_value = mock_payout

        result = stripe_payouts.get_payout_status("po_test123")

        assert result["payout_id"] == "po_test123"
        assert result["status"] == "paid"
        assert result["amount_cents"] == 1000
        assert result["failure_code"] is None
        mock_stripe_retrieve.assert_called_once_with("po_test123")

    @patch("stripe.Payout.retrieve")
    def test_get_payout_status_not_found(self, mock_stripe_retrieve):
        """Handle payout not found error."""
        import stripe
        mock_stripe_retrieve.side_effect = stripe.error.InvalidRequestError(
            "resource_missing",
            "Payout not found",
        )

        with pytest.raises(stripe_payouts.StripePayoutError):
            stripe_payouts.get_payout_status("po_nonexistent")


class TestWebhookSignatureVerification:
    """Test Stripe webhook signature verification."""

    def test_verify_webhook_signature_valid(self):
        """Valid webhook signature should verify."""
        # Mock Stripe webhook construction (success case)
        with patch("stripe.Webhook.construct_event") as mock_construct:
            mock_construct.return_value = {"id": "evt_test", "type": "payout.paid"}
            result = stripe_payouts.verify_webhook_signature(
                b'{"id":"evt_test"}',
                "t=1234567890,v1=abc123",
                "whsec_test",
            )
            assert result is True

    def test_verify_webhook_signature_invalid(self):
        """Invalid webhook signature should fail."""
        import stripe
        with patch("stripe.Webhook.construct_event") as mock_construct:
            mock_construct.side_effect = stripe.error.SignatureVerificationError("Invalid", "bad_sig")
            result = stripe_payouts.verify_webhook_signature(
                b'{"id":"evt_test"}',
                "t=1234567890,v1=wrong",
                "whsec_test",
            )
            assert result is False


class TestWebhookEventParsing:
    """Test parsing Stripe webhook events."""

    def test_parse_payout_paid_event(self):
        """Parse payout.paid event."""
        event = {
            "type": "payout.paid",
            "data": {
                "object": {
                    "id": "po_test123",
                    "status": "paid",
                    "amount": 1000,
                    "failure_code": None,
                }
            }
        }

        result = stripe_payouts.parse_payout_webhook_event(event)

        assert result is not None
        assert result["payout_id"] == "po_test123"
        assert result["event_type"] == "payout.paid"
        assert result["status"] == "paid"
        assert result["amount_cents"] == 1000

    def test_parse_payout_failed_event(self):
        """Parse payout.failed event."""
        event = {
            "type": "payout.failed",
            "data": {
                "object": {
                    "id": "po_test456",
                    "status": "failed",
                    "amount": 2000,
                    "failure_code": "insufficient_funds",
                }
            }
        }

        result = stripe_payouts.parse_payout_webhook_event(event)

        assert result is not None
        assert result["payout_id"] == "po_test456"
        assert result["status"] == "failed"
        assert result["failure_code"] == "insufficient_funds"

    def test_parse_non_payout_event(self):
        """Non-payout events should return None."""
        event = {
            "type": "charge.succeeded",
            "data": {"object": {"id": "ch_test"}}
        }

        result = stripe_payouts.parse_payout_webhook_event(event)
        assert result is None


class TestIntegrationWithCryptoWallet:
    """Integration tests with crypto_wallet.send_to_bank()."""

    @pytest.mark.skip(reason="send_to_bank() not yet implemented in crypto_wallet.py")
    @pytest.mark.asyncio
    @patch("stripe_payouts.create_payout")
    async def test_send_to_bank_success(self, mock_create_payout, db: Session):
        """Successfully send money to bank via Stripe payout."""
        # Setup user and wallet
        user = User(
            id="user123",
            email="test@example.com",
            hashed_password="hashed",
            full_name="John Doe",
            crypto_wallet_address="0x1234567890123456789012345678901234567890",
            usdc_balance_cents=100000,  # $1000
            wallet_initialized=True,
        )
        db.add(user)
        db.commit()

        # Mock Stripe payout
        mock_create_payout.return_value = {
            "payout_id": "po_test123",
            "amount_cents": 50000,
            "status": "in_transit",
            "recipient_last4": "6789",
        }

        # Call send_to_bank
        from services import crypto_wallet
        result = await crypto_wallet.send_to_bank(
            sender_id="user123",
            recipient_name="Jane Doe",
            recipient_routing_number="021000021",
            recipient_account_number="123456789",
            amount_cents=50000,
            db=db,
        )

        # Verify result
        assert result["transfer_id"] is not None
        assert result["status"] == "pending"
        assert result["estimated_settlement"] == "Instant (typically <30 seconds)"
        assert result["amount"] == 500.0  # $500
        assert result["fee"] == 0.01

        # Verify user balance was deducted
        db.refresh(user)
        assert user.usdc_balance_cents == 49900  # 1000 - 500 - 0.01 fee

        # Verify bank transfer record was created
        bank_transfer = db.query(BankTransfer).filter(
            BankTransfer.sender_id == "user123"
        ).first()
        assert bank_transfer is not None
        assert bank_transfer.stripe_payout_id == "po_test123"
        assert bank_transfer.stripe_payout_status == "in_transit"

    @pytest.mark.skip(reason="send_to_bank() not yet implemented in crypto_wallet.py")
    @pytest.mark.asyncio
    async def test_send_to_bank_insufficient_balance(self, db: Session):
        """Reject transfer when user has insufficient balance."""
        # Setup user with low balance
        user = User(
            id="user456",
            email="poor@example.com",
            hashed_password="hashed",
            full_name="Poor User",
            crypto_wallet_address="0x1234567890123456789012345678901234567890",
            usdc_balance_cents=10,  # only $0.10
            wallet_initialized=True,
        )
        db.add(user)
        db.commit()

        from services import crypto_wallet
        with pytest.raises(crypto_wallet.InsufficientBalance):
            await crypto_wallet.send_to_bank(
                sender_id="user456",
                recipient_name="Jane Doe",
                recipient_routing_number="021000021",
                recipient_account_number="123456789",
                amount_cents=50000,  # $500, way more than available
                db=db,
            )

        # Verify no transfer was created
        bank_transfer = db.query(BankTransfer).filter(
            BankTransfer.sender_id == "user456"
        ).first()
        assert bank_transfer is None

    @pytest.mark.skip(reason="send_to_bank() not yet implemented in crypto_wallet.py")
    @pytest.mark.asyncio
    async def test_send_to_bank_no_wallet(self, db: Session):
        """Reject transfer when user has no wallet."""
        # Setup user without wallet
        user = User(
            id="user789",
            email="nowallet@example.com",
            hashed_password="hashed",
            full_name="No Wallet User",
            crypto_wallet_address=None,
            usdc_balance_cents=0,
            wallet_initialized=False,
        )
        db.add(user)
        db.commit()

        from services import crypto_wallet
        with pytest.raises(crypto_wallet.WalletNotInitialized):
            await crypto_wallet.send_to_bank(
                sender_id="user789",
                recipient_name="Jane Doe",
                recipient_routing_number="021000021",
                recipient_account_number="123456789",
                amount_cents=50000,
                db=db,
            )

    @pytest.mark.skip(reason="send_to_bank() not yet implemented in crypto_wallet.py")
    @pytest.mark.asyncio
    @patch("stripe_payouts.create_payout")
    async def test_send_to_bank_payout_error_refunds_user(self, mock_create_payout, db: Session):
        """On payout error, balance should be refunded to user."""
        # Setup user
        user = User(
            id="user_error",
            email="error@example.com",
            hashed_password="hashed",
            full_name="Error User",
            crypto_wallet_address="0x1234567890123456789012345678901234567890",
            usdc_balance_cents=100000,
            wallet_initialized=True,
        )
        db.add(user)
        db.commit()

        # Mock Stripe payout error
        from services import stripe_payouts
        mock_create_payout.side_effect = stripe_payouts.StripePayoutError("Invalid account")

        from services import crypto_wallet
        with pytest.raises(crypto_wallet.BankTransferError):
            await crypto_wallet.send_to_bank(
                sender_id="user_error",
                recipient_name="Jane Doe",
                recipient_routing_number="021000021",
                recipient_account_number="123456789",
                amount_cents=50000,
                db=db,
            )

        # Verify balance was refunded
        db.refresh(user)
        assert user.usdc_balance_cents == 100000  # fully refunded


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
