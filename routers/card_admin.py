"""
Admin endpoints for card management and manual review.
- Manual review queue
- Transaction approval/decline
- Queue statistics
- Audit log query
"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query

from sqlalchemy.orm import Session

from core.auth import get_current_user
from core.database import get_db
from models.user import User
from services.card_manual_review import ManualReviewService

router = APIRouter(prefix="/admin/card", tags=["admin-card"])


def get_manual_review_service(db: Session = Depends(get_db)) -> ManualReviewService:
    """Dependency: manual review service"""
    return ManualReviewService(db)


# ==================== QUEUE MANAGEMENT ====================

@router.get("/review-queue")
async def get_review_queue(
    priority_order: str = Query("recent", enum=["recent", "amount_desc", "oldest"]),
    limit: int = Query(50, ge=1, le=500),
    user: User = Depends(get_current_user),
    review_svc: ManualReviewService = Depends(get_manual_review_service),
    db: Session = Depends(get_db),
):
    """
    [ADMIN] Get manual review queue.

    Parameters:
        - priority_order: Sort by 'recent', 'amount_desc', 'oldest'
        - limit: Max results (default 50)

    Returns:
        {
            'queue': [
                {
                    'transaction_id': str,
                    'merchant_name': str,
                    'amount_usd': float,
                    'fallback_reason': 'processor_timeout' | 'processor_error',
                    'wallet_balance_at_auth_cents': int,
                    'requested_at': datetime,
                }
            ],
            'total_pending': int,
        }
    """
    # TODO: Verify admin role
    # if not user.is_admin:
    #     raise HTTPException(status_code=403, detail="Admin only")

    queue = review_svc.get_review_queue(priority_order, limit)

    from models.card import CardTransaction
    pending_count = db.query(CardTransaction).filter(
        CardTransaction.requires_manual_review == True
    ).count()

    return {
        'status': 'success',
        'data': {
            'queue': queue,
            'total_pending': pending_count,
            'priority_order': priority_order,
            'returned_count': len(queue),
        }
    }


# ==================== TRANSACTION APPROVAL ====================

@router.post("/review/{transaction_id}/approve")
async def approve_transaction(
    transaction_id: str,
    reason: Optional[str] = Query(None),
    force_charge: bool = Query(False),
    user: User = Depends(get_current_user),
    review_svc: ManualReviewService = Depends(get_manual_review_service),
):
    """
    [ADMIN] Approve transaction for settlement.

    Parameters:
        - transaction_id: Transaction UUID
        - reason: Approval reason (optional, for audit)
        - force_charge: If True, charge wallet even if balance is now insufficient
                        (use if wallet was updated after auth timeout)

    Returns:
        {
            'status': 'success',
            'data': {
                'transaction_id': str,
                'action': 'approved',
                'charged': bool,
                'error': str or null,
            }
        }

    Errors:
        - 403: Not admin
        - 404: Transaction not found or not pending review
        - 400: Insufficient balance (and force_charge=False)
    """
    # TODO: Verify admin
    try:
        result = review_svc.approve_transaction(
            transaction_id=transaction_id,
            admin_id=user.id,
            reason=reason,
            force_charge=force_charge,
        )
        return {
            'status': 'success',
            'data': result,
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/review/{transaction_id}/decline")
async def decline_transaction(
    transaction_id: str,
    reason: str = Query(..., min_length=3, max_length=255),
    reverse_auth: bool = Query(True),
    user: User = Depends(get_current_user),
    review_svc: ManualReviewService = Depends(get_manual_review_service),
):
    """
    [ADMIN] Decline transaction.

    Parameters:
        - transaction_id: Transaction UUID
        - reason: Decline reason (user-facing, required)
        - reverse_auth: If True, mark as REVERSED (funds returned to wallet)

    Returns:
        {
            'status': 'success',
            'data': {
                'transaction_id': str,
                'action': 'declined',
                'reversed': bool,
            }
        }

    Errors:
        - 403: Not admin
        - 404: Transaction not found
    """
    # TODO: Verify admin
    try:
        result = review_svc.decline_transaction(
            transaction_id=transaction_id,
            admin_id=user.id,
            reason=reason,
            reverse_auth=reverse_auth,
        )
        return {
            'status': 'success',
            'data': result,
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/review/{transaction_id}/escalate")
async def escalate_transaction(
    transaction_id: str,
    reason: str = Query(..., min_length=3, max_length=500),
    user: User = Depends(get_current_user),
    review_svc: ManualReviewService = Depends(get_manual_review_service),
):
    """
    [ADMIN] Escalate transaction to senior review.

    Used for suspicious patterns or edge cases requiring specialist judgment.

    Parameters:
        - transaction_id: Transaction UUID
        - reason: Escalation reason (audit trail)

    Returns:
        {
            'status': 'success',
            'data': {
                'transaction_id': str,
                'escalated': bool,
            }
        }
    """
    # TODO: Verify admin
    try:
        result = review_svc.escalate_transaction(
            transaction_id=transaction_id,
            admin_id=user.id,
            reason=reason,
        )
        return {
            'status': 'success',
            'data': result,
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ==================== TRANSACTION DETAILS ====================

@router.get("/review/{transaction_id}")
async def get_transaction_details(
    transaction_id: str,
    user: User = Depends(get_current_user),
    review_svc: ManualReviewService = Depends(get_manual_review_service),
):
    """
    [ADMIN] Get full transaction details for review.

    Returns complete context:
    - Transaction details
    - Card info & status
    - Recent card history (pattern analysis)
    - Full audit trail
    - User activity

    Returns:
        {
            'status': 'success',
            'data': {
                'transaction': {...},
                'card': {...},
                'card_history': [...],
                'audit_trail': [...],
            }
        }

    Errors:
        - 403: Not admin
        - 404: Transaction not found
    """
    # TODO: Verify admin
    try:
        details = review_svc.get_transaction_details(transaction_id)
        return {
            'status': 'success',
            'data': details,
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ==================== STATISTICS ====================

@router.get("/review/statistics")
async def get_queue_statistics(
    user: User = Depends(get_current_user),
    review_svc: ManualReviewService = Depends(get_manual_review_service),
):
    """
    [ADMIN] Get manual review queue statistics.

    Returns:
        {
            'status': 'success',
            'data': {
                'pending_count': int,
                'avg_age_hours': float,
                'total_amount_pending_usd': float,
                'fallback_reasons': {
                    'processor_timeout': 5,
                    'processor_error': 2,
                },
                'top_merchants': [
                    {
                        'name': str,
                        'count': int,
                        'total_cents': int,
                    }
                ],
            }
        }

    Use for:
    - Queue health monitoring (alert if >100 pending)
    - SLA tracking (alert if avg_age > 4 hours)
    - Processor reliability analysis (fallback reason distribution)
    """
    # TODO: Verify admin
    stats = review_svc.get_statistics()
    return {
        'status': 'success',
        'data': stats,
    }


# ==================== HEALTH & MONITORING ====================

@router.get("/health")
async def card_admin_health(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    [ADMIN] Card admin service health.

    Returns:
        {
            'status': 'healthy',
            'data': {
                'database_ok': bool,
                'manual_review_queue_size': int,
                'timestamp': datetime,
            }
        }
    """
    from models.card import CardTransaction

    pending_count = db.query(CardTransaction).filter(
        CardTransaction.requires_manual_review == True
    ).count()

    return {
        'status': 'healthy' if pending_count < 500 else 'warning',
        'data': {
            'database_ok': True,
            'manual_review_queue_size': pending_count,
            'queue_status': 'healthy' if pending_count < 100 else 'backlog',
        }
    }
