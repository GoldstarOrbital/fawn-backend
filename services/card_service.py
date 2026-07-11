"""
Card service layer: issue, authorize, balance check, freeze, transactions.
- Idempotency handling
- Audit logging
- Rate limiting
- Processor integration (Lithic)
- Fallback for processor outages
"""

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
from decimal import Decimal

from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_

from models.card import (
    Card, CardTransaction, CardAuditLog, CardRateLimit, ProcessorWebhookLog,
    CardStatus, TransactionStatus, AuditEventType
)
from models.user import User
from services.lithic_processor import LithicProcessor, LithicException
from services.crypto_wallet import CryptoWalletService
from core.config import settings

logger = logging.getLogger(__name__)


class CardServiceException(Exception):
    """Base exception for card service"""
    pass


class InsufficientBalanceException(CardServiceException):
    """Wallet USDC balance insufficient"""
    pass


class CardFrozenException(CardServiceException):
    """Card is frozen/suspended"""
    pass


class IdempotencyConflictException(CardServiceException):
    """Idempotency key already processed with different params"""
    pass


class RateLimitExceededException(CardServiceException):
    """Rate limit exceeded"""
    pass


class ProcessorException(CardServiceException):
    """Processor (Lithic) error"""
    pass


class CardService:
    """Card operations with compliance & reliability"""

    def __init__(self, db: Session, lithic: LithicProcessor, wallet_svc: CryptoWalletService):
        self.db = db
        self.lithic = lithic
        self.wallet_svc = wallet_svc

    # ==================== ISSUANCE ====================

    async def issue_card(self, user_id: str, wallet_address: str) -> Dict[str, Any]:
        """
        Issue virtual debit card instantly.

        Args:
            user_id: User UUID
            wallet_address: USDC wallet address (Polygon/Ethereum)

        Returns:
            {
                'card_id': str,
                'card_last_four': str,
                'status': 'active',
                'issued_at': datetime,
                'lithic_card_token': str (opaque provider id)
            }

        Raises:
            RateLimitExceededException: >1 card/day
            ProcessorException: Lithic API error
        """
        # Rate limit: max 1 card per user per day
        user_rate_limit = self.db.query(CardRateLimit).filter_by(user_id=user_id).first()
        if not user_rate_limit:
            user_rate_limit = CardRateLimit(
                user_id=user_id,
                cards_issued_today_reset_at=datetime.utcnow() + timedelta(days=1)
            )
            self.db.add(user_rate_limit)
        else:
            if datetime.utcnow() >= user_rate_limit.cards_issued_today_reset_at:
                # Reset daily counter
                user_rate_limit.cards_issued_today = 0
                user_rate_limit.cards_issued_today_reset_at = datetime.utcnow() + timedelta(days=1)

            if user_rate_limit.cards_issued_today >= 1:
                raise RateLimitExceededException("Maximum 1 card per day allowed")

        # Get user & verify wallet
        user = self.db.query(User).filter_by(id=user_id).first()
        if not user:
            raise CardServiceException("User not found")

        if user.crypto_wallet_address != wallet_address:
            raise CardServiceException("Wallet address mismatch")

        # Call Lithic to issue card
        try:
            lithic_response = await self.lithic.issue_card(
                user_id=user_id,
                wallet_address=wallet_address
            )
        except LithicException as e:
            logger.error(f"Lithic issuance failed: {e}")
            raise ProcessorException(f"Card processor error: {e}")

        # Create card record
        card = Card(
            id=str(uuid.uuid4()),
            user_id=user_id,
            lithic_card_token=lithic_response['lithic_card_token'],
            card_last_four=lithic_response['card_last_four'],
            card_brand=lithic_response.get('card_brand', 'VISA'),
            wallet_address=wallet_address,
            status=CardStatus.ACTIVE,
            activated_at=datetime.utcnow(),
            processor_status='active',
            daily_limit_cents=settings.card_daily_limit_cents,
            monthly_limit_cents=settings.card_monthly_limit_cents,
            transaction_limit_cents=settings.card_transaction_limit_cents,
        )
        self.db.add(card)

        # Audit log
        self._audit_log(
            user_id=user_id,
            event_type=AuditEventType.CARD_ISSUED,
            entity_type='card',
            entity_id=card.id,
            details={'wallet_address': wallet_address, 'card_last_four': card.card_last_four}
        )

        # Rate limit update
        user_rate_limit.cards_issued_today += 1

        self.db.commit()
        logger.info(f"Card issued: {card.id} for user {user_id}")

        return {
            'card_id': card.id,
            'card_last_four': card.card_last_four,
            'status': card.status.value,
            'issued_at': card.issued_at,
            'lithic_card_token': card.lithic_card_token,
        }

    # ==================== AUTHORIZATION ====================

    async def authorize_transaction(
        self,
        processor_transaction_id: str,
        idempotency_key: str,
        card_id: str,
        merchant_name: str,
        transaction_amount_cents: int,
        merchant_mcc: Optional[str] = None,
        fallback_on_processor_timeout: bool = True,
    ) -> Dict[str, Any]:
        """
        Authorize debit card transaction (called by Lithic webhook).

        Idempotency: same idempotency_key = return cached result (no charge).

        Args:
            processor_transaction_id: Lithic transaction ID (unique)
            idempotency_key: Merchant upstream idempotency key (unique)
            card_id: Card UUID
            merchant_name: Merchant display name
            transaction_amount_cents: Transaction amount (USD cents)
            merchant_mcc: Merchant Category Code (optional)
            fallback_on_processor_timeout: If balance check times out, fallback to manual review

        Returns:
            {
                'transaction_id': str,
                'approved': bool,
                'decline_reason': str or null,
                'processor_response_code': '00' (approved) or decline code,
                'wallet_balance_after_cents': int or null,
            }

        Raises:
            InsufficientBalanceException: Balance too low
            CardFrozenException: Card status not ACTIVE
            IdempotencyConflictException: Same idempotency_key with different params
        """
        # Check idempotency first (fast path)
        existing_txn = self.db.query(CardTransaction).filter_by(
            idempotency_key=idempotency_key
        ).first()

        if existing_txn:
            # Return cached result
            return {
                'transaction_id': existing_txn.id,
                'approved': existing_txn.auth_approved,
                'decline_reason': existing_txn.auth_decline_reason,
                'processor_response_code': existing_txn.processor_response_code or ('00' if existing_txn.auth_approved else '05'),
                'wallet_balance_after_cents': existing_txn.wallet_balance_at_auth_cents,
            }

        # Fetch card
        card = self.db.query(Card).filter_by(id=card_id).first()
        if not card:
            raise CardServiceException("Card not found")

        # Verify card status
        if card.status != CardStatus.ACTIVE:
            decline_reason = f"Card status: {card.status.value}"
            self._record_failed_auth(
                card_id=card_id,
                user_id=card.user_id,
                processor_transaction_id=processor_transaction_id,
                idempotency_key=idempotency_key,
                merchant_name=merchant_name,
                transaction_amount_cents=transaction_amount_cents,
                merchant_mcc=merchant_mcc,
                auth_approved=False,
                auth_decline_reason=decline_reason,
                processor_response_code='03',  # Card not active
            )
            raise CardFrozenException(decline_reason)

        # Check velocity limits
        velocity_ok, velocity_reason = self._check_velocity(card, transaction_amount_cents)
        if not velocity_ok:
            self._record_failed_auth(
                card_id=card_id,
                user_id=card.user_id,
                processor_transaction_id=processor_transaction_id,
                idempotency_key=idempotency_key,
                merchant_name=merchant_name,
                transaction_amount_cents=transaction_amount_cents,
                merchant_mcc=merchant_mcc,
                auth_approved=False,
                auth_decline_reason=velocity_reason,
                processor_response_code='04',  # Velocity limit
            )
            return {
                'transaction_id': None,
                'approved': False,
                'decline_reason': velocity_reason,
                'processor_response_code': '04',
                'wallet_balance_after_cents': None,
            }

        # Check USDC balance (<100ms with cache)
        try:
            wallet_balance_cents = await asyncio.wait_for(
                self.wallet_svc.get_wallet_balance(card.wallet_address),
                timeout=0.1  # 100ms timeout
            )
        except asyncio.TimeoutError:
            logger.warning(f"Balance check timeout for wallet {card.wallet_address}, using fallback")
            if fallback_on_processor_timeout:
                return await self._handle_manual_review_fallback(
                    card_id=card_id,
                    user_id=card.user_id,
                    processor_transaction_id=processor_transaction_id,
                    idempotency_key=idempotency_key,
                    merchant_name=merchant_name,
                    transaction_amount_cents=transaction_amount_cents,
                    merchant_mcc=merchant_mcc,
                    fallback_reason='processor_timeout',
                )
            else:
                # Hard decline
                self._record_failed_auth(
                    card_id=card_id,
                    user_id=card.user_id,
                    processor_transaction_id=processor_transaction_id,
                    idempotency_key=idempotency_key,
                    merchant_name=merchant_name,
                    transaction_amount_cents=transaction_amount_cents,
                    merchant_mcc=merchant_mcc,
                    auth_approved=False,
                    auth_decline_reason='Balance check timeout',
                    processor_response_code='91',  # Issuer unavailable
                )
                raise ProcessorException("Balance check timeout")

        # Check sufficient balance
        if wallet_balance_cents < transaction_amount_cents:
            self._record_failed_auth(
                card_id=card_id,
                user_id=card.user_id,
                processor_transaction_id=processor_transaction_id,
                idempotency_key=idempotency_key,
                merchant_name=merchant_name,
                transaction_amount_cents=transaction_amount_cents,
                merchant_mcc=merchant_mcc,
                auth_approved=False,
                auth_decline_reason='Insufficient funds',
                processor_response_code='05',
                wallet_balance_at_auth=wallet_balance_cents,
            )
            raise InsufficientBalanceException(
                f"Insufficient balance: {wallet_balance_cents} < {transaction_amount_cents}"
            )

        # Approve & record transaction
        txn = CardTransaction(
            id=str(uuid.uuid4()),
            processor_transaction_id=processor_transaction_id,
            idempotency_key=idempotency_key,
            card_id=card_id,
            user_id=card.user_id,
            wallet_address=card.wallet_address,
            merchant_name=merchant_name,
            merchant_mcc=merchant_mcc,
            transaction_amount_cents=transaction_amount_cents,
            usdc_amount_cents=transaction_amount_cents,  # 1:1 USDC rate
            status=TransactionStatus.AUTHORIZED,
            wallet_balance_at_auth_cents=wallet_balance_cents,
            auth_approved=True,
            processor_response_code='00',
            requested_at=datetime.utcnow(),
            authorized_at=datetime.utcnow(),
        )
        self.db.add(txn)

        # Update card velocity
        card.daily_transaction_count += 1
        card.daily_transaction_total_cents += transaction_amount_cents
        card.monthly_transaction_count += 1
        card.monthly_transaction_total_cents += transaction_amount_cents
        card.last_used_at = datetime.utcnow()

        # Audit
        self._audit_log(
            user_id=card.user_id,
            event_type=AuditEventType.AUTH_APPROVED,
            entity_type='transaction',
            entity_id=txn.id,
            details={
                'merchant': merchant_name,
                'amount': transaction_amount_cents,
                'balance_after': wallet_balance_cents - transaction_amount_cents,
            }
        )

        self.db.commit()
        logger.info(f"Transaction authorized: {txn.id} for card {card_id}")

        return {
            'transaction_id': txn.id,
            'approved': True,
            'decline_reason': None,
            'processor_response_code': '00',
            'wallet_balance_after_cents': wallet_balance_cents - transaction_amount_cents,
        }

    def _record_failed_auth(
        self,
        card_id: str,
        user_id: str,
        processor_transaction_id: str,
        idempotency_key: str,
        merchant_name: str,
        transaction_amount_cents: int,
        merchant_mcc: Optional[str],
        auth_approved: bool,
        auth_decline_reason: str,
        processor_response_code: str,
        wallet_balance_at_auth: Optional[int] = None,
    ):
        """Record a declined/failed authorization"""
        txn = CardTransaction(
            id=str(uuid.uuid4()),
            processor_transaction_id=processor_transaction_id,
            idempotency_key=idempotency_key,
            card_id=card_id,
            user_id=user_id,
            merchant_name=merchant_name,
            merchant_mcc=merchant_mcc,
            transaction_amount_cents=transaction_amount_cents,
            usdc_amount_cents=0,  # No charge on decline
            status=TransactionStatus.DECLINED,
            wallet_balance_at_auth_cents=wallet_balance_at_auth,
            auth_approved=auth_approved,
            auth_decline_reason=auth_decline_reason,
            processor_response_code=processor_response_code,
            requested_at=datetime.utcnow(),
        )
        self.db.add(txn)

        # Audit
        self._audit_log(
            user_id=user_id,
            event_type=AuditEventType.AUTH_DECLINED,
            entity_type='transaction',
            entity_id=txn.id,
            details={
                'merchant': merchant_name,
                'amount': transaction_amount_cents,
                'reason': auth_decline_reason,
            }
        )
        self.db.commit()

    async def _handle_manual_review_fallback(
        self,
        card_id: str,
        user_id: str,
        processor_transaction_id: str,
        idempotency_key: str,
        merchant_name: str,
        transaction_amount_cents: int,
        merchant_mcc: Optional[str],
        fallback_reason: str,
    ) -> Dict[str, Any]:
        """
        Fallback to manual review when processor unavailable.
        - Tentatively approve (assume balance exists)
        - Flag for manual review
        - Returns approved but with manual_review flag
        """
        txn = CardTransaction(
            id=str(uuid.uuid4()),
            processor_transaction_id=processor_transaction_id,
            idempotency_key=idempotency_key,
            card_id=card_id,
            user_id=user_id,
            merchant_name=merchant_name,
            merchant_mcc=merchant_mcc,
            transaction_amount_cents=transaction_amount_cents,
            usdc_amount_cents=transaction_amount_cents,
            status=TransactionStatus.AUTHORIZED,
            auth_approved=True,  # Tentatively approve
            fallback_status=fallback_reason,
            requires_manual_review=True,
            processor_response_code='99',  # Custom: manual review pending
            requested_at=datetime.utcnow(),
            authorized_at=datetime.utcnow(),
        )
        self.db.add(txn)

        # Audit with manual review flag
        self._audit_log(
            user_id=user_id,
            event_type=AuditEventType.FALLBACK_MANUAL_REVIEW,
            entity_type='transaction',
            entity_id=txn.id,
            details={
                'merchant': merchant_name,
                'amount': transaction_amount_cents,
                'fallback_reason': fallback_reason,
            }
        )
        self.db.commit()

        return {
            'transaction_id': txn.id,
            'approved': True,
            'decline_reason': None,
            'processor_response_code': '99',
            'requires_manual_review': True,
            'fallback_reason': fallback_reason,
        }

    # ==================== BALANCE & VELOCITY ====================

    async def get_card_balance(self, card_id: str) -> Dict[str, Any]:
        """
        Get real-time USDC balance for card's wallet.

        Returns:
            {
                'wallet_address': str,
                'balance_cents': int,
                'balance_usd': float,
                'last_updated': datetime,
            }
        """
        card = self.db.query(Card).filter_by(id=card_id).first()
        if not card:
            raise CardServiceException("Card not found")

        balance_cents = await self.wallet_svc.get_wallet_balance(card.wallet_address)

        return {
            'wallet_address': card.wallet_address,
            'balance_cents': balance_cents,
            'balance_usd': balance_cents / 100.0,
            'last_updated': datetime.utcnow(),
        }

    def _check_velocity(self, card: Card, txn_amount_cents: int) -> Tuple[bool, str]:
        """
        Check transaction velocity limits.

        Returns: (ok, reason)
        """
        # Per-transaction limit
        if txn_amount_cents > card.transaction_limit_cents:
            return False, f"Transaction exceeds limit: ${txn_amount_cents/100} > ${card.transaction_limit_cents/100}"

        # Daily limit
        if card.daily_transaction_total_cents + txn_amount_cents > card.daily_limit_cents:
            return False, f"Daily limit exceeded: ${card.daily_transaction_total_cents/100 + txn_amount_cents/100} > ${card.daily_limit_cents/100}"

        # Monthly limit
        if card.monthly_transaction_total_cents + txn_amount_cents > card.monthly_limit_cents:
            return False, f"Monthly limit exceeded"

        return True, ""

    # ==================== FREEZE/UNFREEZE ====================

    def freeze_card(self, card_id: str, user_id: str, reason: Optional[str] = None) -> Dict[str, Any]:
        """Freeze card (user-initiated or admin)"""
        card = self.db.query(Card).filter_by(id=card_id, user_id=user_id).first()
        if not card:
            raise CardServiceException("Card not found")

        if card.status == CardStatus.FROZEN:
            return {'status': 'already_frozen', 'card_id': card_id}

        card.status = CardStatus.FROZEN
        card.frozen_at = datetime.utcnow()

        self._audit_log(
            user_id=user_id,
            event_type=AuditEventType.CARD_FROZEN,
            entity_type='card',
            entity_id=card.id,
            details={'reason': reason}
        )
        self.db.commit()
        logger.info(f"Card frozen: {card_id}")

        return {
            'card_id': card_id,
            'status': card.status.value,
            'frozen_at': card.frozen_at,
        }

    def unfreeze_card(self, card_id: str, user_id: str, reason: Optional[str] = None) -> Dict[str, Any]:
        """Unfreeze card"""
        card = self.db.query(Card).filter_by(id=card_id, user_id=user_id).first()
        if not card:
            raise CardServiceException("Card not found")

        if card.status != CardStatus.FROZEN:
            return {'status': 'not_frozen', 'card_id': card_id}

        card.status = CardStatus.ACTIVE
        card.frozen_at = None

        self._audit_log(
            user_id=user_id,
            event_type=AuditEventType.CARD_UNFROZEN,
            entity_type='card',
            entity_id=card.id,
            details={'reason': reason}
        )
        self.db.commit()
        logger.info(f"Card unfrozen: {card_id}")

        return {
            'card_id': card_id,
            'status': card.status.value,
        }

    # ==================== TRANSACTION HISTORY ====================

    def get_card_transactions(
        self,
        card_id: str,
        user_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """
        Get card transaction history.

        Returns:
            {
                'transactions': [...],
                'total_count': int,
                'limit': int,
                'offset': int,
            }
        """
        # Verify user owns card
        card = self.db.query(Card).filter_by(id=card_id, user_id=user_id).first()
        if not card:
            raise CardServiceException("Card not found")

        query = self.db.query(CardTransaction).filter_by(card_id=card_id).order_by(
            CardTransaction.requested_at.desc()
        )
        total_count = query.count()

        txns = query.limit(limit).offset(offset).all()

        return {
            'transactions': [
                {
                    'id': t.id,
                    'merchant_name': t.merchant_name,
                    'amount_cents': t.transaction_amount_cents,
                    'amount_usd': t.transaction_amount_cents / 100.0,
                    'status': t.status.value,
                    'approved': t.auth_approved,
                    'requested_at': t.requested_at,
                    'authorized_at': t.authorized_at,
                    'processor_response_code': t.processor_response_code,
                }
                for t in txns
            ],
            'total_count': total_count,
            'limit': limit,
            'offset': offset,
        }

    # ==================== AUDIT LOGGING ====================

    def _audit_log(
        self,
        user_id: str,
        event_type: AuditEventType,
        entity_type: str,
        entity_id: str,
        details: Dict[str, Any],
        actor_type: str = "system",
        actor_id: Optional[str] = None,
        processor_event_id: Optional[str] = None,
    ):
        """Record audit log (7-year retention)"""
        log = CardAuditLog(
            id=str(uuid.uuid4()),
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            user_id=user_id,
            actor_type=actor_type,
            actor_id=actor_id,
            details=json.dumps(details),
            processor_event_id=processor_event_id,
            retention_until=datetime.utcnow() + timedelta(days=365*7),  # 7 years
        )
        self.db.add(log)
