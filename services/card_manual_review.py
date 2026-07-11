"""
Manual review workflow for processor outages and fallback scenarios.
- Queue management
- Admin interface for review/approval
- Audit trail for compliance
"""

import logging
import json
from datetime import datetime
from typing import Optional, Dict, Any, List
from enum import Enum as PyEnum

from sqlalchemy.orm import Session
from sqlalchemy import and_, func

from models.card import CardTransaction, CardAuditLog, TransactionStatus, AuditEventType

logger = logging.getLogger(__name__)


class ManualReviewStatus(str, PyEnum):
    """Manual review lifecycle"""
    PENDING = "pending"
    APPROVED = "approved"
    DECLINED = "declined"
    ESCALATED = "escalated"


class ManualReviewService:
    """
    Manage card transactions requiring manual review.
    - Processor timeouts
    - Balance check failures
    - Suspicious patterns (flagged for review)
    """

    def __init__(self, db: Session):
        self.db = db

    def get_review_queue(
        self,
        priority_order: str = "recent",
        limit: int = 50,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get transactions pending manual review.

        Args:
            priority_order: 'recent' | 'amount_desc' | 'oldest'
            limit: Max results
            status: Filter by review status

        Returns:
            List of review items
        """
        query = self.db.query(CardTransaction).filter(
            CardTransaction.requires_manual_review == True
        )

        if priority_order == "recent":
            query = query.order_by(CardTransaction.requested_at.desc())
        elif priority_order == "amount_desc":
            query = query.order_by(CardTransaction.transaction_amount_cents.desc())
        elif priority_order == "oldest":
            query = query.order_by(CardTransaction.requested_at.asc())

        txns = query.limit(limit).all()

        return [
            {
                'transaction_id': t.id,
                'card_id': t.card_id,
                'user_id': t.user_id,
                'merchant_name': t.merchant_name,
                'amount_cents': t.transaction_amount_cents,
                'amount_usd': t.transaction_amount_cents / 100.0,
                'fallback_reason': t.fallback_status,
                'auth_approved': t.auth_approved,
                'wallet_balance_at_auth_cents': t.wallet_balance_at_auth_cents,
                'requested_at': t.requested_at,
                'notes': t.manual_review_notes,
                'status': t.status.value,
            }
            for t in txns
        ]

    def approve_transaction(
        self,
        transaction_id: str,
        admin_id: str,
        reason: Optional[str] = None,
        force_charge: bool = False,
    ) -> Dict[str, Any]:
        """
        Admin approves transaction for settlement.

        Args:
            transaction_id: Transaction UUID
            admin_id: Admin user ID (for audit)
            reason: Approval reason
            force_charge: If True, charge wallet even if balance is low
                          (use only if wallet updated after auth)

        Returns:
            {
                'transaction_id': str,
                'status': 'approved',
                'charged': bool,
                'error': str or null,
            }
        """
        txn = self.db.query(CardTransaction).filter_by(id=transaction_id).first()
        if not txn:
            raise ValueError("Transaction not found")

        if not txn.requires_manual_review:
            raise ValueError("Transaction does not require manual review")

        # Verify wallet still has funds (if not forcing)
        if not force_charge and txn.wallet_balance_at_auth_cents:
            if txn.wallet_balance_at_auth_cents < txn.transaction_amount_cents:
                return {
                    'transaction_id': transaction_id,
                    'status': 'declined',
                    'charged': False,
                    'error': f"Insufficient balance: {txn.wallet_balance_at_auth_cents} < {txn.transaction_amount_cents}",
                }

        # Mark approved
        txn.status = TransactionStatus.APPROVED
        txn.requires_manual_review = False
        txn.manual_review_notes = f"Admin approved: {reason}" if reason else "Admin approved"

        # Audit
        audit = CardAuditLog(
            id=str(__import__('uuid').uuid4()),
            event_type=AuditEventType.AUTH_APPROVED,
            entity_type='transaction',
            entity_id=txn.id,
            user_id=txn.user_id,
            actor_type='admin',
            actor_id=admin_id,
            details=json.dumps({
                'merchant': txn.merchant_name,
                'amount': txn.transaction_amount_cents,
                'approval_reason': reason,
                'force_charge': force_charge,
            }),
        )
        self.db.add(audit)
        self.db.commit()

        logger.info(f"Transaction approved: {transaction_id} by admin {admin_id}")

        return {
            'transaction_id': transaction_id,
            'status': 'approved',
            'charged': True,
            'error': None,
        }

    def decline_transaction(
        self,
        transaction_id: str,
        admin_id: str,
        reason: str,
        reverse_auth: bool = True,
    ) -> Dict[str, Any]:
        """
        Admin declines transaction.

        Args:
            transaction_id: Transaction UUID
            admin_id: Admin user ID
            reason: Decline reason (user-facing)
            reverse_auth: If True, mark as REVERSED (funds returned to wallet)

        Returns:
            {
                'transaction_id': str,
                'status': 'declined',
                'reversed': bool,
            }
        """
        txn = self.db.query(CardTransaction).filter_by(id=transaction_id).first()
        if not txn:
            raise ValueError("Transaction not found")

        if not txn.requires_manual_review:
            raise ValueError("Transaction does not require manual review")

        # Mark declined
        txn.status = TransactionStatus.REVERSED if reverse_auth else TransactionStatus.DECLINED
        txn.requires_manual_review = False
        txn.auth_decline_reason = reason
        txn.manual_review_notes = f"Admin declined: {reason}"

        # Audit
        audit = CardAuditLog(
            id=str(__import__('uuid').uuid4()),
            event_type=AuditEventType.AUTH_DECLINED,
            entity_type='transaction',
            entity_id=txn.id,
            user_id=txn.user_id,
            actor_type='admin',
            actor_id=admin_id,
            details=json.dumps({
                'merchant': txn.merchant_name,
                'amount': txn.transaction_amount_cents,
                'decline_reason': reason,
                'reversed': reverse_auth,
            }),
        )
        self.db.add(audit)
        self.db.commit()

        logger.info(f"Transaction declined: {transaction_id} by admin {admin_id}")

        return {
            'transaction_id': transaction_id,
            'status': 'declined',
            'reversed': reverse_auth,
        }

    def escalate_transaction(
        self,
        transaction_id: str,
        admin_id: str,
        reason: str,
    ) -> Dict[str, Any]:
        """
        Escalate transaction to senior review.

        Args:
            transaction_id: Transaction UUID
            admin_id: Admin user ID
            reason: Escalation reason

        Returns:
            {
                'transaction_id': str,
                'escalated': bool,
            }
        """
        txn = self.db.query(CardTransaction).filter_by(id=transaction_id).first()
        if not txn:
            raise ValueError("Transaction not found")

        txn.manual_review_notes = f"Escalated by {admin_id}: {reason}"

        # Audit
        audit = CardAuditLog(
            id=str(__import__('uuid').uuid4()),
            event_type=AuditEventType.BALANCE_CHECK,  # Custom: escalation
            entity_type='transaction',
            entity_id=txn.id,
            user_id=txn.user_id,
            actor_type='admin',
            actor_id=admin_id,
            details=json.dumps({
                'escalation_reason': reason,
                'merchant': txn.merchant_name,
            }),
        )
        self.db.add(audit)
        self.db.commit()

        logger.warning(f"Transaction escalated: {transaction_id} - {reason}")

        return {
            'transaction_id': transaction_id,
            'escalated': True,
        }

    def get_transaction_details(self, transaction_id: str) -> Dict[str, Any]:
        """
        Get full transaction details for review.

        Returns all context needed for manual decision.
        """
        txn = self.db.query(CardTransaction).filter_by(id=transaction_id).first()
        if not txn:
            raise ValueError("Transaction not found")

        # Get related audit events
        audits = self.db.query(CardAuditLog).filter_by(entity_id=transaction_id).all()

        # Get user's other transactions (pattern analysis)
        from models.card import Card
        card = self.db.query(Card).filter_by(id=txn.card_id).first()

        other_txns = self.db.query(CardTransaction).filter(
            and_(
                CardTransaction.card_id == txn.card_id,
                CardTransaction.id != txn.id,
            )
        ).order_by(CardTransaction.requested_at.desc()).limit(10).all()

        return {
            'transaction': {
                'id': txn.id,
                'merchant_name': txn.merchant_name,
                'merchant_mcc': txn.merchant_mcc,
                'amount_cents': txn.transaction_amount_cents,
                'amount_usd': txn.transaction_amount_cents / 100.0,
                'status': txn.status.value,
                'requested_at': txn.requested_at,
                'authorized_at': txn.authorized_at,
                'fallback_reason': txn.fallback_status,
                'wallet_balance_at_auth_cents': txn.wallet_balance_at_auth_cents,
                'auth_approved': txn.auth_approved,
                'processor_transaction_id': txn.processor_transaction_id,
                'processor_response_code': txn.processor_response_code,
            },
            'card': {
                'id': card.id,
                'last_four': card.card_last_four,
                'status': card.status.value,
                'daily_total_cents': card.daily_transaction_total_cents,
                'monthly_total_cents': card.monthly_transaction_total_cents,
                'last_used_at': card.last_used_at,
            },
            'card_history': [
                {
                    'merchant': t.merchant_name,
                    'amount_usd': t.transaction_amount_cents / 100.0,
                    'status': t.status.value,
                    'requested_at': t.requested_at,
                }
                for t in other_txns
            ],
            'audit_trail': [
                {
                    'event_type': a.event_type.value,
                    'actor_type': a.actor_type,
                    'details': json.loads(a.details) if a.details else {},
                    'created_at': a.created_at,
                }
                for a in audits
            ],
        }

    def get_statistics(self) -> Dict[str, Any]:
        """
        Get manual review queue statistics.

        Returns:
            {
                'pending_count': int,
                'avg_age_hours': float,
                'total_amount_pending_cents': int,
                'fallback_reasons': {reason: count},
                'top_merchants': [{name: str, count: int, total_cents: int}],
            }
        """
        pending = self.db.query(CardTransaction).filter(
            CardTransaction.requires_manual_review == True
        ).all()

        if not pending:
            return {
                'pending_count': 0,
                'avg_age_hours': 0,
                'total_amount_pending_cents': 0,
                'fallback_reasons': {},
                'top_merchants': [],
            }

        # Age calculation
        now = datetime.utcnow()
        ages = [(now - t.requested_at).total_seconds() / 3600 for t in pending]
        avg_age = sum(ages) / len(ages) if ages else 0

        # Fallback reasons
        fallback_reasons = {}
        for t in pending:
            reason = t.fallback_status or 'unknown'
            fallback_reasons[reason] = fallback_reasons.get(reason, 0) + 1

        # Top merchants
        merchant_stats = {}
        for t in pending:
            if t.merchant_name not in merchant_stats:
                merchant_stats[t.merchant_name] = {'count': 0, 'total_cents': 0}
            merchant_stats[t.merchant_name]['count'] += 1
            merchant_stats[t.merchant_name]['total_cents'] += t.transaction_amount_cents

        top_merchants = sorted(
            [{'name': k, **v} for k, v in merchant_stats.items()],
            key=lambda x: x['total_cents'],
            reverse=True
        )[:10]

        return {
            'pending_count': len(pending),
            'avg_age_hours': round(avg_age, 2),
            'total_amount_pending_cents': sum(t.transaction_amount_cents for t in pending),
            'total_amount_pending_usd': sum(t.transaction_amount_cents for t in pending) / 100.0,
            'fallback_reasons': fallback_reasons,
            'top_merchants': top_merchants,
        }
