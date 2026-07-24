"""Trust, support, feedback, card readiness, and product scorecard APIs."""
from __future__ import annotations

import json
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from dependencies import get_current_user
from models import CardRequest, ProductMetric, SupportTicket, User, UserFeedback
from services.product_metrics import record_metric

router = APIRouter(prefix="/experience", tags=["experience"])
ADMIN_HEADER = APIKeyHeader(name="X-Admin-Key", auto_error=False)


def _admin_key(key: Optional[str] = Security(ADMIN_HEADER)) -> str:
    if not settings.admin_api_key or not key or key != settings.admin_api_key:
        raise HTTPException(status_code=403, detail="Invalid or missing admin key")
    return key


class TelemetryRequest(BaseModel):
    event_name: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9_.-]+$")
    anonymous_id: str = Field(min_length=8, max_length=120)
    duration_ms: float | None = Field(default=None, ge=0, le=600000)
    success: bool | None = None
    path: str | None = Field(default=None, max_length=200)
    metadata: dict = Field(default_factory=dict)


@router.get("/trust")
def trust_disclosures():
    return {
        "custody": {
            "model": "custodial",
            "summary": "FAWN holds encrypted signing keys for FAWN wallets; users do not receive seed phrases.",
            "important": "FAWN wallet balances are not the same as a bank deposit and are not represented as FDIC-insured deposits.",
        },
        "safeguards": [
            "Envelope encryption for custodial wallet keys",
            "Immutable audit records for significant account actions",
            "Server-side transfer caps, velocity checks, and sanctions screening",
            "Visible confirmation, status, and fee breakdowns before settlement",
        ],
        "fees": {"internal_transfer": 0.01, "external_wallet_transfer": 0.50, "currency": "USD"},
        "limits": {"single_send_max": 2000.0, "daily_send_max": 5000.0, "investing_order_max": 1000.0},
        "funding": {"bank_linking_configured": bool(settings.plaid_client_id and settings.plaid_secret), "wallet_receive": True},
        "recovery": "Use password reset and contact support for account-access or wallet-recovery assistance. Never share a password or recovery code.",
        "disputes": "Open a support ticket with the transfer, funding, or card context. FAWN retains the transaction audit trail while the issue is reviewed.",
        "last_updated": "2026-07-23",
    }


@router.post("/telemetry", status_code=202)
def receive_telemetry(req: TelemetryRequest, request: Request, db: Session = Depends(get_db)):
    # Anonymous telemetry is deliberately limited to product-quality signals.
    safe_metadata = {str(k)[:40]: str(v)[:200] for k, v in req.metadata.items() if str(k)[:40] not in {"email", "password", "token", "address"}}
    record_metric(db, req.event_name, duration_ms=req.duration_ms, success=req.success,
                  path=req.path or request.url.path, metadata={"anonymous_id": req.anonymous_id, **safe_metadata})
    return {"accepted": True}


class FeedbackRequest(BaseModel):
    score: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=1200)
    context: str | None = Field(default=None, max_length=80)


@router.post("/feedback", status_code=201)
def submit_feedback(req: FeedbackRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = UserFeedback(user_id=current_user.id, score=req.score, comment=req.comment, context=req.context)
    db.add(row)
    db.commit()
    record_metric(db, "feedback_submitted", user_id=current_user.id, success=True, metadata={"score": req.score, "context": req.context or ""})
    return {"status": "received", "message": "Thanks — your feedback helps shape FAWN."}


class SupportRequest(BaseModel):
    category: str = Field(pattern=r"^(transfer|funding|card|dispute|account|other)$")
    subject: str = Field(min_length=3, max_length=120)
    message: str = Field(min_length=5, max_length=4000)
    priority: str = Field(default="normal", pattern=r"^(normal|urgent)$")


class SupportUpdate(BaseModel):
    status: str = Field(pattern=r"^(in_progress|resolved)$")
    resolution_notes: str | None = Field(default=None, max_length=4000)


@router.post("/support/tickets", status_code=201)
def create_ticket(req: SupportRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ticket = SupportTicket(user_id=current_user.id, category=req.category, subject=req.subject,
                           message=req.message, priority=req.priority)
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    record_metric(db, "support_ticket_created", user_id=current_user.id, success=True, metadata={"category": req.category})
    return {"id": ticket.id, "status": ticket.status, "created_at": ticket.created_at.isoformat() if ticket.created_at else None}


@router.get("/support/tickets")
def list_tickets(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.query(SupportTicket).filter(SupportTicket.user_id == current_user.id).order_by(SupportTicket.created_at.desc()).limit(50).all()
    return {"tickets": [{"id": x.id, "category": x.category, "subject": x.subject, "status": x.status, "created_at": x.created_at.isoformat() if x.created_at else None, "resolved_at": x.resolved_at.isoformat() if x.resolved_at else None} for x in rows]}


@router.patch("/admin/support/tickets/{ticket_id}", dependencies=[Depends(_admin_key)])
def update_ticket(ticket_id: str, req: SupportUpdate, db: Session = Depends(get_db)):
    ticket = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Support ticket not found")
    ticket.status = req.status
    ticket.resolution_notes = req.resolution_notes
    ticket.resolved_at = datetime.now(timezone.utc) if req.status == "resolved" else None
    db.commit()
    record_metric(db, "support_ticket_resolved" if req.status == "resolved" else "support_ticket_updated", success=True, metadata={"category": ticket.category})
    return {"id": ticket.id, "status": ticket.status, "resolved_at": ticket.resolved_at.isoformat() if ticket.resolved_at else None}


@router.get("/cards/status")
def card_status(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.query(CardRequest).filter(CardRequest.user_id == current_user.id).first()
    configured = bool(getattr(settings, "lithic_api_key", ""))
    return {"issuer_configured": configured, "status": row.status if row else "not_requested", "card_type": row.card_type if row else "virtual_debit", "message": "Card issuance is being prepared; FAWN will show availability before collecting any card details." if not configured else "Card issuance is available for eligible users."}


@router.post("/cards/request", status_code=201)
def request_card(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.query(CardRequest).filter(CardRequest.user_id == current_user.id).first()
    if not row:
        row = CardRequest(user_id=current_user.id, status="interest")
        db.add(row)
        db.commit()
    record_metric(db, "card_interest_requested", user_id=current_user.id, success=True)
    return {"status": row.status, "message": "You're on the FAWN card list. We'll notify you when issuance opens."}


@router.get("/admin/scorecard", dependencies=[Depends(_admin_key)])
def scorecard(db: Session = Depends(get_db)):
    """Return measured values only; null means insufficient telemetry."""
    since = datetime.now(timezone.utc) - timedelta(days=30)
    def count(event):
        return db.query(func.count(ProductMetric.id)).filter(ProductMetric.event_name == event, ProductMetric.created_at >= since).scalar() or 0
    starts, completes = count("onboarding_started"), count("onboarding_completed")
    transfer_ok, transfer_fail = count("transfer_succeeded"), count("transfer_failed")
    loads = [float(x[0]) for x in db.query(ProductMetric.duration_ms).filter(ProductMetric.event_name == "page_load", ProductMetric.duration_ms.isnot(None), ProductMetric.created_at >= since).all()]
    resolutions = []
    for created, resolved in db.query(SupportTicket.created_at, SupportTicket.resolved_at).filter(SupportTicket.resolved_at.isnot(None), SupportTicket.created_at >= since).all():
        if created and resolved:
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if resolved.tzinfo is None:
                resolved = resolved.replace(tzinfo=timezone.utc)
            resolutions.append(max(0, (resolved - created).total_seconds() / 3600))
    errors, requests = count("client_error"), count("api_request")
    return {"window_days": 30, "measured_at": datetime.now(timezone.utc).isoformat(),
            "onboarding_completion_rate": round(completes / starts, 4) if starts else None,
            "onboarding_started": starts, "onboarding_completed": completes,
            "transfer_success_rate": round(transfer_ok / (transfer_ok + transfer_fail), 4) if transfer_ok + transfer_fail else None,
            "transfer_succeeded": transfer_ok, "transfer_failed": transfer_fail,
            "median_page_load_ms": statistics.median(loads) if loads else None,
            "page_load_samples": len(loads),
            "client_error_rate": round(errors / requests, 4) if requests else None,
            "client_errors": errors, "api_requests": requests,
            "median_support_resolution_hours": statistics.median(resolutions) if resolutions else None,
            "resolved_support_tickets": len(resolutions)}
