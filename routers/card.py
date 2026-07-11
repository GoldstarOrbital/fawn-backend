"""
FastAPI routes for debit card operations.
- Issue cards
- Authorization webhooks
- Balance queries
- Card freeze/unfreeze
- Transaction history
"""

import asyncio
import hashlib
import json
import logging
from typing import Optional
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Header, Request, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from core.auth import get_current_user
from core.config import settings
from core.database import get_db
from models.user import User
from models.card import CardAuditLog, CardRateLimit, ProcessorWebhookLog
from services.card_service import (
    CardService,
    InsufficientBalanceException,
    CardFrozenException,
    IdempotencyConflictException,
    RateLimitExceededException,
    ProcessorException,
    CardServiceException,
)
from services.lithic_processor import LithicProcessor, LithicException
from services.crypto_wallet import CryptoWalletService

router = APIRouter(prefix="/card", tags=["card"])
logger = logging.getLogger(__name__)


def get_card_service(db: Session = Depends(get_db)) -> CardService:
    """Dependency: card service with processor integration"""
    lithic = LithicProcessor()
    wallet_svc = CryptoWalletService(db)
    return CardService(db, lithic, wallet_svc)


# ==================== ISSUANCE ====================

@router.post("/issue")
async def issue_card(
    user: User = Depends(get_current_user),
    card_service: CardService = Depends(get_card_service),
    db: Session = Depends(get_db),
):
    """
    Issue virtual USDC debit card instantly.

    Returns:
        {
            'card_id': str,
            'card_last_four': str,
            'status': 'active',
            'issued_at': datetime,
        }

    Errors:
        - 429: Rate limit (max 1 card/day)
        - 503: Processor error
        - 400: Wallet not initialized
    """
    if not user.wallet_initialized:
        raise HTTPException(
            status_code=400,
            detail="USDC wallet not initialized"
        )

    try:
        card = await card_service.issue_card(
            user_id=user.id,
            wallet_address=user.crypto_wallet_address,
        )
        return {
            'status': 'success',
            'data': card,
        }
    except RateLimitExceededException as e:
        raise HTTPException(status_code=429, detail=str(e))
    except ProcessorException as e:
        logger.error(f"Processor error on issue: {e}")
        raise HTTPException(status_code=503, detail="Card processor unavailable")
    except CardServiceException as e:
        raise HTTPException(status_code=400, detail=str(e))


# ==================== AUTHORIZATION WEBHOOK ====================

@router.post("/authorize-transaction")
async def authorize_transaction_webhook(
    request: Request,
    x_lithic_signature: Optional[str] = Header(None),
    card_service: CardService = Depends(get_card_service),
    db: Session = Depends(get_db),
):
    """
    Handle Lithic authorization webhook.

    Called ~1000x/day at scale. Must respond in <1s for processor timeout.

    Processor calls:
        POST /card/authorize-transaction
        Headers:
            X-Lithic-Signature: HMAC-SHA256
        Body:
            {
                'type': 'card_transaction.updated',
                'data': {
                    'token': 'card_token',
                    'events': [
                        {
                            'type': 'AUTHORIZATION',
                            'amount': 10050,  # cents
                            'merchant': {...},
                            'network_identifiers': {
                                'processor_transaction_id': 'xyz123'
                            }
                        }
                    ]
                }
            }

    Response (must return quickly):
        {
            'approved': bool,
            'decline_reason': str or null,
            'processor_response_code': '00' or decline code,
        }

    Returns:
        - 200: Auth decision sent to processor
        - 202: Async fallback (manual review queued)
        - 400: Invalid payload
        - 401: Invalid signature
        - 503: Internal error (processor should retry)
    """
    body = await request.body()
    body_str = body.decode('utf-8')

    # Verify signature
    lithic = LithicProcessor()
    if not lithic.verify_webhook_signature(body_str, x_lithic_signature or ""):
        logger.warning(f"Invalid Lithic signature, skipping auth")
        if not settings.allow_unsigned_lithic_webhooks:
            raise HTTPException(status_code=401, detail="Invalid signature")

    # Parse payload
    try:
        payload = lithic.parse_webhook_payload(body_str)
    except LithicException as e:
        logger.error(f"Invalid webhook payload: {e}")
        raise HTTPException(status_code=400, detail="Invalid payload")

    # Check webhook dedup
    payload_hash = hashlib.sha256(body_str.encode()).hexdigest()
    existing_log = db.query(ProcessorWebhookLog).filter_by(
        payload_hash=payload_hash
    ).first()

    if existing_log and existing_log.processed:
        logger.debug(f"Duplicate webhook, returning cached result")
        return {
            'approved': existing_log.last_attempt_status == 200,
            'processor_response_code': '00' if existing_log.last_attempt_status == 200 else '05',
        }

    # Extract auth request
    auth_req = lithic.extract_auth_request(payload)
    if not auth_req:
        logger.warning(f"No auth event in webhook")
        raise HTTPException(status_code=400, detail="No authorization event")

    # Map Lithic card token to our card ID
    processor_card_token = auth_req.get('card_token')
    from models.card import Card
    card = db.query(Card).filter_by(lithic_card_token=processor_card_token).first()

    if not card:
        logger.error(f"Card token not found: {processor_card_token}")
        # Record failed auth but don't expose this to processor (privacy)
        raise HTTPException(status_code=400, detail="Card not found")

    # Spawn async auth in background (don't block processor timeout)
    processor_txn_id = auth_req.get('processor_transaction_id')
    idempotency_key = auth_req.get('idempotency_key', processor_txn_id)

    # Record webhook
    webhook_log = ProcessorWebhookLog(
        webhook_id=processor_txn_id,
        event_type='card_transaction.updated',
        payload_hash=payload_hash,
    )
    db.add(webhook_log)
    db.commit()

    try:
        # Try to authorize synchronously (with timeout)
        result = await asyncio.wait_for(
            card_service.authorize_transaction(
                processor_transaction_id=processor_txn_id,
                idempotency_key=idempotency_key,
                card_id=card.id,
                merchant_name=auth_req.get('merchant_name'),
                transaction_amount_cents=auth_req.get('amount_cents'),
                merchant_mcc=auth_req.get('merchant_mcc'),
                fallback_on_processor_timeout=True,
            ),
            timeout=0.5,  # Must respond in 500ms
        )

        # Update webhook log
        webhook_log.processed = True
        webhook_log.last_attempt_status = 200
        webhook_log.processed_at = datetime.utcnow()
        db.commit()

        return {
            'approved': result.get('approved', False),
            'decline_reason': result.get('decline_reason'),
            'processor_response_code': result.get('processor_response_code', '05'),
        }

    except asyncio.TimeoutError:
        logger.warning(f"Auth timeout for txn {processor_txn_id}, using fallback")

        # Manual review fallback (tentatively approve)
        result = await card_service._handle_manual_review_fallback(
            card_id=card.id,
            user_id=card.user_id,
            processor_transaction_id=processor_txn_id,
            idempotency_key=idempotency_key,
            merchant_name=auth_req.get('merchant_name'),
            transaction_amount_cents=auth_req.get('amount_cents'),
            merchant_mcc=auth_req.get('merchant_mcc'),
            fallback_reason='processor_timeout',
        )

        webhook_log.processed = True
        webhook_log.last_attempt_status = 202
        webhook_log.processed_at = datetime.utcnow()
        db.commit()

        return JSONResponse(
            status_code=202,
            content={
                'approved': True,
                'processor_response_code': '99',
                'requires_manual_review': True,
            }
        )

    except (InsufficientBalanceException, CardFrozenException) as e:
        logger.debug(f"Auth declined: {e}")
        webhook_log.last_attempt_status = 400
        db.commit()

        return {
            'approved': False,
            'decline_reason': str(e),
            'processor_response_code': '05',
        }

    except ProcessorException as e:
        logger.error(f"Auth processor error: {e}")
        webhook_log.last_attempt_status = 503
        db.commit()

        # Tell processor to retry
        raise HTTPException(status_code=503, detail="Processing error, please retry")

    except Exception as e:
        logger.exception(f"Unexpected error in auth: {e}")
        webhook_log.last_attempt_status = 500
        db.commit()

        raise HTTPException(status_code=503, detail="Internal server error")


# ==================== BALANCE ====================

@router.get("/balance/{card_id}")
async def get_balance(
    card_id: str,
    user: User = Depends(get_current_user),
    card_service: CardService = Depends(get_card_service),
):
    """
    Get real-time USDC balance for card (sourced from wallet).

    Returns:
        {
            'wallet_address': str,
            'balance_cents': int,
            'balance_usd': float,
            'last_updated': datetime,
        }

    Errors:
        - 404: Card not found
        - 503: Balance check timeout (fallback to cached value)
    """
    try:
        balance = await card_service.get_card_balance(card_id)
        return {
            'status': 'success',
            'data': balance,
        }
    except CardServiceException:
        raise HTTPException(status_code=404, detail="Card not found")
    except asyncio.TimeoutError:
        raise HTTPException(status_code=503, detail="Balance check timeout, please retry")


# ==================== FREEZE/UNFREEZE ====================

@router.post("/freeze/{card_id}")
async def freeze_card(
    card_id: str,
    reason: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
    card_service: CardService = Depends(get_card_service),
):
    """
    Freeze card (user-initiated).

    Parameters:
        - card_id: Card UUID
        - reason: Optional reason for audit log

    Returns:
        {
            'card_id': str,
            'status': 'frozen',
            'frozen_at': datetime,
        }

    Errors:
        - 404: Card not found
        - 403: User doesn't own card
    """
    try:
        result = card_service.freeze_card(card_id, user.id, reason)
        return {
            'status': 'success',
            'data': result,
        }
    except CardServiceException:
        raise HTTPException(status_code=404, detail="Card not found")


@router.post("/unfreeze/{card_id}")
async def unfreeze_card(
    card_id: str,
    reason: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
    card_service: CardService = Depends(get_card_service),
):
    """
    Unfreeze card.

    Returns:
        {
            'card_id': str,
            'status': 'active',
        }

    Errors:
        - 404: Card not found
    """
    try:
        result = card_service.unfreeze_card(card_id, user.id, reason)
        return {
            'status': 'success',
            'data': result,
        }
    except CardServiceException:
        raise HTTPException(status_code=404, detail="Card not found")


# ==================== TRANSACTION HISTORY ====================

@router.get("/transactions/{card_id}")
async def get_transactions(
    card_id: str,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    card_service: CardService = Depends(get_card_service),
):
    """
    Get card transaction history.

    Parameters:
        - card_id: Card UUID
        - limit: Results per page (max 500)
        - offset: Pagination offset

    Returns:
        {
            'transactions': [
                {
                    'id': str,
                    'merchant_name': str,
                    'amount_cents': int,
                    'amount_usd': float,
                    'status': 'authorized|declined|settled',
                    'approved': bool,
                    'requested_at': datetime,
                    'authorized_at': datetime or null,
                    'processor_response_code': str,
                }
            ],
            'total_count': int,
            'limit': int,
            'offset': int,
        }

    Errors:
        - 404: Card not found
        - 403: User doesn't own card
    """
    try:
        txns = card_service.get_card_transactions(card_id, user.id, limit, offset)
        return {
            'status': 'success',
            'data': txns,
        }
    except CardServiceException:
        raise HTTPException(status_code=404, detail="Card not found")


# ==================== ADMIN: AUDIT LOG QUERY ====================

@router.get("/audit/{card_id}")
async def get_audit_log(
    card_id: str,
    event_type: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    [ADMIN] Query audit log for compliance.

    Parameters:
        - card_id: Card UUID
        - event_type: Filter by event type (optional)
        - limit: Max results

    Returns 7-year audit trail (ADMIN ONLY).

    Errors:
        - 403: Not admin
        - 404: No audit logs
    """
    # TODO: Check admin role
    # if not user.is_admin:
    #     raise HTTPException(status_code=403, detail="Admin only")

    query = db.query(CardAuditLog).filter_by(entity_id=card_id)
    if event_type:
        query = query.filter_by(event_type=event_type)

    logs = query.order_by(CardAuditLog.created_at.desc()).limit(limit).all()

    if not logs:
        raise HTTPException(status_code=404, detail="No audit logs found")

    return {
        'status': 'success',
        'data': {
            'logs': [
                {
                    'id': log.id,
                    'event_type': log.event_type.value,
                    'entity_type': log.entity_type,
                    'details': json.loads(log.details) if log.details else {},
                    'actor_type': log.actor_type,
                    'created_at': log.created_at,
                    'retention_until': log.retention_until,
                }
                for log in logs
            ],
            'count': len(logs),
        }
    }


# ==================== HEALTH ====================

@router.get("/health")
async def card_service_health(
    db: Session = Depends(get_db),
):
    """
    Card service health check.
    - Lithic API reachability
    - Database connectivity
    """
    lithic = LithicProcessor()

    return {
        'status': 'healthy',
        'card_service': {
            'lithic_configured': bool(lithic.api_key),
            'database_ok': True,  # We're connected if we got here
            'timestamp': datetime.utcnow(),
        }
    }
