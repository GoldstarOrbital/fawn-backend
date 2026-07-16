"""
Webhook & Real-time Notification APIs for FAWN automation.

Services:
1. Webhooks - Real-time transaction events
2. Notifications - Email/SMS on events
3. Event History - Audit trail of all events
4. Batch Operations - Execute multiple operations
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, HttpUrl
from datetime import datetime, timedelta, timezone
import json
import httpx
from database import get_db
from dependencies import get_current_user
from models import User, UserAuditLog

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# ── MODELS ──

class WebhookSubscription(BaseModel):
    url: HttpUrl  # Webhook endpoint
    events: list[str]  # ["transfer.completed", "trade.executed", "alert.triggered"]
    active: bool = True


class NotificationPreference(BaseModel):
    email: bool = True
    sms: bool = False
    push: bool = True
    min_amount_usd: float = 0.01  # Only notify for transfers above this


class BatchTransfer(BaseModel):
    recipients: list[dict]  # [{address: "0x...", amount_cents: 1000}, ...]
    metadata: dict = {}


# ── WEBHOOKS ──

@router.post("/subscribe")
async def subscribe_webhook(
    req: WebhookSubscription,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Subscribe to webhook events."""
    webhook_config = {
        "url": str(req.url),
        "events": req.events,
        "active": req.active,
        "webhook_id": f"wh_{current_user.id[:8]}_{int(datetime.utcnow().timestamp())}",
        "created_at": datetime.utcnow().isoformat(),
        "last_triggered": None,
        "delivery_success_count": 0,
        "delivery_failure_count": 0,
    }

    audit = UserAuditLog(
        user_id=current_user.id,
        action="webhook_subscribed",
        details=json.dumps(webhook_config),
        retention_expires_at=datetime.now(tz=timezone.utc) + timedelta(days=365*7),
    )
    db.add(audit)
    db.commit()

    return {
        "status": "subscribed",
        "webhook": webhook_config,
        "message": f"Webhook subscribed to events: {', '.join(req.events)}",
    }


@router.get("/subscriptions")
async def list_webhooks(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all webhook subscriptions."""
    webhooks = db.query(UserAuditLog).filter(
        UserAuditLog.user_id == current_user.id,
        UserAuditLog.action == "webhook_subscribed",
    ).all()

    active_webhooks = []
    for log in webhooks:
        try:
            webhook = json.loads(log.details)
            if webhook.get("active"):
                active_webhooks.append(webhook)
        except:
            pass

    return {"webhooks": active_webhooks, "count": len(active_webhooks)}


@router.delete("/subscriptions/{webhook_id}")
async def unsubscribe_webhook(
    webhook_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Unsubscribe from webhook."""
    webhooks = db.query(UserAuditLog).filter(
        UserAuditLog.user_id == current_user.id,
        UserAuditLog.action == "webhook_subscribed",
    ).all()

    for log in webhooks:
        try:
            webhook = json.loads(log.details)
            if webhook.get("webhook_id") == webhook_id:
                webhook["active"] = False
                log.details = json.dumps(webhook)
                db.commit()
                return {"status": "unsubscribed", "webhook_id": webhook_id}
        except:
            pass

    raise HTTPException(status_code=404, detail="Webhook not found")


# ── NOTIFICATIONS ──

@router.post("/notifications/preferences")
async def set_notification_preferences(
    req: NotificationPreference,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Set notification preferences."""
    prefs = {
        "email": req.email,
        "sms": req.sms,
        "push": req.push,
        "min_amount_usd": req.min_amount_usd,
        "updated_at": datetime.utcnow().isoformat(),
    }

    audit = UserAuditLog(
        user_id=current_user.id,
        action="notification_preferences_updated",
        details=json.dumps(prefs),
        retention_expires_at=datetime.now(tz=timezone.utc) + timedelta(days=365*7),
    )
    db.add(audit)
    db.commit()

    return {
        "status": "updated",
        "preferences": prefs,
        "message": "Notification preferences saved",
    }


@router.get("/notifications/preferences")
async def get_notification_preferences(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get current notification preferences."""
    prefs_log = db.query(UserAuditLog).filter(
        UserAuditLog.user_id == current_user.id,
        UserAuditLog.action == "notification_preferences_updated",
    ).order_by(UserAuditLog.created_at.desc()).first()

    if prefs_log:
        return json.loads(prefs_log.details)

    return {
        "email": True,
        "sms": False,
        "push": True,
        "min_amount_usd": 0.01,
    }


# ── BATCH OPERATIONS ──

@router.post("/batch/transfers")
async def batch_transfer(
    req: BatchTransfer,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Execute batch transfers to multiple recipients."""
    total_amount = sum(r["amount_cents"] for r in req.recipients)

    if total_amount > current_user.usdc_balance_cents:
        raise HTTPException(status_code=400, detail="Insufficient balance for batch")

    batch_config = {
        "batch_id": f"batch_{current_user.id[:8]}_{int(datetime.utcnow().timestamp())}",
        "recipient_count": len(req.recipients),
        "total_amount_cents": total_amount,
        "status": "queued",
        "created_at": datetime.utcnow().isoformat(),
        "executed_at": None,
        "metadata": req.metadata,
    }

    audit = UserAuditLog(
        user_id=current_user.id,
        action="batch_transfer_queued",
        details=json.dumps({**batch_config, "recipients": req.recipients}),
        retention_expires_at=datetime.now(tz=timezone.utc) + timedelta(days=365*7),
    )
    db.add(audit)
    db.commit()

    return {
        "status": "queued",
        "batch": batch_config,
        "message": f"Batch transfer queued: {len(req.recipients)} recipients, ${total_amount/100:.2f}",
    }


# ── EVENT HISTORY ──

@router.get("/events")
async def get_event_history(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = 50,
):
    """Get event history for automation actions."""
    events = db.query(UserAuditLog).filter(
        UserAuditLog.user_id == current_user.id,
        UserAuditLog.action.in_([
            "webhook_subscribed",
            "webhook_triggered",
            "notification_sent",
            "recurring_transfer_executed",
            "batch_transfer_queued",
            "batch_transfer_executed",
            "dca_purchase_executed",
            "price_alert_triggered",
        ])
    ).order_by(UserAuditLog.created_at.desc()).limit(limit).all()

    event_history = []
    for log in events:
        try:
            event_history.append({
                "action": log.action,
                "timestamp": log.created_at.isoformat(),
                "details": json.loads(log.details),
            })
        except:
            pass

    return {"events": event_history, "count": len(event_history)}


@router.get("/events/{event_type}")
async def get_events_by_type(
    event_type: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = 20,
):
    """Get events filtered by type."""
    events = db.query(UserAuditLog).filter(
        UserAuditLog.user_id == current_user.id,
        UserAuditLog.action == event_type,
    ).order_by(UserAuditLog.created_at.desc()).limit(limit).all()

    filtered_events = []
    for log in events:
        try:
            filtered_events.append({
                "timestamp": log.created_at.isoformat(),
                "details": json.loads(log.details),
            })
        except:
            pass

    return {"event_type": event_type, "events": filtered_events, "count": len(filtered_events)}
