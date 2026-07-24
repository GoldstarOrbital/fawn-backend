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
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import secrets
import uuid
from database import get_db
from dependencies import get_current_user
from models import User, UserAuditLog
from services.webhook_delivery import dispatch_user_event, public_subscription

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


# ── MODELS ──

class WebhookSubscription(BaseModel):
    url: HttpUrl  # Webhook endpoint
    events: list[str] = Field(min_length=1, max_length=5)
    active: bool = True

    @field_validator("url")
    @classmethod
    def public_https_endpoint(cls, value: HttpUrl):
        if value.scheme != "https":
            raise ValueError("Webhook URLs must use HTTPS")
        host = (value.host or "").lower()
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            raise ValueError("Webhook URL must be publicly reachable")
        try:
            if ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback:
                raise ValueError("Webhook URL must not target a private address")
        except ValueError as exc:
            if "must not" in str(exc):
                raise
        return value

    @field_validator("events")
    @classmethod
    def allowed_events(cls, events: list[str]):
        allowed = {"price_alert.triggered", "batch_transfer.queued", "test.ping"}
        if not set(events).issubset(allowed):
            raise ValueError(f"Events must be one of: {', '.join(sorted(allowed))}")
        if len(set(events)) != len(events):
            raise ValueError("Webhook events must be unique")
        return events


class NotificationPreference(BaseModel):
    email: bool = True
    sms: bool = False
    push: bool = True
    min_amount_usd: float = 0.01  # Only notify for transfers above this


class BatchRecipient(BaseModel):
    address: str = Field(min_length=3, max_length=120)
    amount_cents: int = Field(ge=100, le=100_000)


class BatchTransfer(BaseModel):
    recipients: list[BatchRecipient] = Field(min_length=2, max_length=25)
    metadata: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def batch_limits(self):
        recipients = [item.address.strip().lower() for item in self.recipients]
        if len(set(recipients)) != len(recipients):
            raise ValueError("Each batch recipient must be unique")
        if sum(item.amount_cents for item in self.recipients) > 500_000:
            raise ValueError("Batch transfers are capped at $5,000")
        return self


# ── WEBHOOKS ──

@router.post("/subscribe")
async def subscribe_webhook(
    req: WebhookSubscription,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Subscribe to webhook events."""
    active_count = db.query(UserAuditLog).filter(
        UserAuditLog.user_id == current_user.id,
        UserAuditLog.action == "webhook_subscribed",
    ).count()
    if active_count >= 5:
        raise HTTPException(status_code=400, detail="You can have up to five webhook subscriptions")
    signing_secret = f"whsec_{secrets.token_urlsafe(24)}"
    webhook_config = {
        "url": str(req.url),
        "events": req.events,
        "active": req.active,
        "webhook_id": f"wh_{uuid.uuid4().hex}",
        "signing_secret": signing_secret,
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
        "webhook": public_subscription(webhook_config),
        "signing_secret": signing_secret,
        "signature_header": "X-FAWN-Signature: sha256=<HMAC-SHA256 of raw body>",
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
                active_webhooks.append(public_subscription(webhook))
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


@router.post("/subscriptions/{webhook_id}/test")
async def test_webhook(
    webhook_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Send a signed test event to a matching test.ping subscription."""
    rows = db.query(UserAuditLog).filter(
        UserAuditLog.user_id == current_user.id,
        UserAuditLog.action == "webhook_subscribed",
    ).all()
    for row in rows:
        try:
            config = json.loads(row.details or "{}")
            if config.get("webhook_id") == webhook_id and config.get("active"):
                if "test.ping" not in config.get("events", []):
                    raise HTTPException(status_code=400, detail="Subscribe to test.ping before sending a test")
                delivered = await dispatch_user_event(db, current_user.id, "test.ping", {"webhook_id": webhook_id})
                return {"status": "delivered" if delivered else "attempted", "deliveries": delivered}
        except HTTPException:
            raise
        except Exception:
            continue
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
    """Queue a review-only batch; no transfer is executed from this endpoint."""
    total_amount = sum(r.amount_cents for r in req.recipients)

    if total_amount > current_user.usdc_balance_cents:
        raise HTTPException(status_code=400, detail="Insufficient balance for batch")

    batch_config = {
        "batch_id": f"batch_{uuid.uuid4().hex}",
        "recipient_count": len(req.recipients),
        "total_amount_cents": total_amount,
        "status": "queued_for_review",
        "created_at": datetime.utcnow().isoformat(),
        "executed_at": None,
        "metadata": req.metadata,
    }

    audit = UserAuditLog(
        user_id=current_user.id,
        action="batch_transfer_queued",
        details=json.dumps({**batch_config, "recipients": [recipient.model_dump() for recipient in req.recipients]}),
        retention_expires_at=datetime.now(tz=timezone.utc) + timedelta(days=365*7),
    )
    db.add(audit)
    db.commit()

    delivered = await dispatch_user_event(db, current_user.id, "batch_transfer.queued", batch_config)
    return {
        "status": "queued_for_review",
        "batch": batch_config,
        "webhook_deliveries": delivered,
        "message": f"Batch saved for review: {len(req.recipients)} recipients, ${total_amount/100:.2f}",
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
