"""FAWN-owned closed-loop balance card and merchant checkout network.

This is deliberately not a Visa/Mastercard program and never creates a PAN or
CVV. A customer authorizes a FAWN ledger transfer at a FAWN-controlled
checkout. Both sides pay exactly one cent per completed purchase.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Security
from fastapi.security.api_key import APIKeyHeader
import jwt
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from dependencies import get_current_user
from models import (
    ClosedLoopCard,
    ClosedLoopCheckout,
    ClosedLoopTapToken,
    CryptoWallet,
    MerchantAccount,
    MerchantApplication,
    User,
    UserAuditLog,
)
from rate_limiting import RATE_LIMITS, limiter

router = APIRouter(prefix="/closed-loop", tags=["closed-loop"])
ADMIN_HEADER = APIKeyHeader(name="X-Admin-Key", auto_error=False)
USER_FEE_CENTS = 1
MERCHANT_FEE_CENTS = 1
CHECKOUT_TTL_MINUTES = 15
TAP_TOKEN_TTL_SECONDS = 60


def _admin_key(key: Optional[str] = Security(ADMIN_HEADER)) -> str:
    if not settings.admin_api_key or not key or key != settings.admin_api_key:
        raise HTTPException(status_code=403, detail="Invalid or missing admin key")
    return key


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _audit(db: Session, user_id: str, action: str, details: dict, request: Request | None = None) -> None:
    db.add(UserAuditLog(
        user_id=user_id,
        action=action,
        details=json.dumps(details, separators=(",", ":"), sort_keys=True),
        ip_address=request.client.host if request and request.client else None,
        user_agent=(request.headers.get("user-agent") or "")[:255] if request else None,
        retention_expires_at=_now() + timedelta(days=365 * 7),
    ))


def _merchant_payload(row: MerchantAccount) -> dict:
    return {
        "id": row.id,
        "business_name": row.business_name,
        "display_name": row.display_name,
        "merchant_slug": row.merchant_slug,
        "website": row.website,
        "support_email": row.support_email,
        "status": row.status,
        "transaction_fee_cents": row.transaction_fee_cents,
        "total_volume_cents": row.total_volume_cents,
        "total_fees_paid_cents": row.total_fees_paid_cents,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
    }


def _card_payload(row: ClosedLoopCard) -> dict:
    google_wallet_ready = bool(
        settings.google_wallet_issuer_id
        and settings.google_wallet_service_account_email
        and settings.google_wallet_private_key
    )
    return {
        "id": row.id,
        "public_id": row.public_id,
        "last_four": row.last_four,
        "status": row.status,
        "card_type": "fawn_closed_loop_balance",
        "network": "FAWN",
        "per_transaction_limit_cents": row.per_transaction_limit_cents,
        "daily_limit_cents": row.daily_limit_cents,
        "issued_at": row.issued_at.isoformat() if row.issued_at else None,
        "phone_wallet": {
            "fawn_app": True,
            "dynamic_qr": True,
            "dynamic_tap_token": True,
            "google_wallet_pass_available": google_wallet_ready,
            "apple_pay_payment_card": False,
            "google_pay_payment_card": False,
            "smart_tap_enabled": False,
            "note": "The optional Google Wallet pass is visual only. Payment still requires a checkout-bound FAWN authorization.",
        },
    }


def _google_wallet_add_url(card: ClosedLoopCard, current_user: User) -> str:
    if not (
        settings.google_wallet_issuer_id
        and settings.google_wallet_service_account_email
        and settings.google_wallet_private_key
    ):
        raise HTTPException(status_code=503, detail="Google Wallet pass issuance is not configured")

    issuer_id = settings.google_wallet_issuer_id.strip()
    suffix = hashlib.sha256(card.id.encode()).hexdigest()[:32]
    class_id = f"{issuer_id}.fawn_balance_{suffix}"
    object_id = f"{issuer_id}.fawn_card_{suffix}"
    class_payload = {"id": class_id}
    object_payload = {
        "id": object_id,
        "classId": class_id,
        "state": "ACTIVE",
        "cardTitle": {"defaultValue": {"language": "en-US", "value": "FAWN"}},
        "header": {"defaultValue": {"language": "en-US", "value": "Balance card"}},
        "subheader": {"defaultValue": {"language": "en-US", "value": f"FAWN •••• {card.last_four}"}},
        "hexBackgroundColor": "#0B4D3B",
        "barcode": {
            "type": "QR_CODE",
            "value": f"fawn://card?card={card.public_id}",
            "alternateText": "Open FAWN to pay",
        },
        "textModulesData": [
            {
                "id": "holder",
                "header": "CARDHOLDER",
                "body": (current_user.full_name or current_user.username or "FAWN member")[:120],
            },
            {
                "id": "usage",
                "header": "WHERE IT WORKS",
                "body": "FAWN-controlled checkout only. Open FAWN to confirm the merchant, amount, and 1¢ user fee.",
            },
            {
                "id": "network",
                "header": "NETWORK",
                "body": "FAWN closed loop — not Visa, Mastercard, or Google Pay.",
            },
        ],
    }
    claims = {
        "iss": settings.google_wallet_service_account_email.strip(),
        "aud": "google",
        "typ": "savetowallet",
        "iat": int(_now().timestamp()),
        "origins": ["https://goldstarorbital.github.io"],
        "payload": {
            "genericClasses": [class_payload],
            "genericObjects": [object_payload],
        },
    }
    headers = {"typ": "JWT"}
    if settings.google_wallet_private_key_id:
        headers["kid"] = settings.google_wallet_private_key_id.strip()
    private_key = settings.google_wallet_private_key.replace("\\n", "\n")
    try:
        token = jwt.encode(claims, private_key, algorithm="RS256", headers=headers)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Google Wallet signing credentials are invalid") from exc
    return f"https://pay.google.com/gp/v/save/{token}"


def _checkout_payload(row: ClosedLoopCheckout, merchant: MerchantAccount | None = None) -> dict:
    return {
        "id": row.id,
        "checkout_token": row.checkout_token,
        "merchant_id": row.merchant_id,
        "merchant_name": merchant.display_name if merchant else None,
        "amount_cents": row.amount_cents,
        "user_fee_cents": row.user_fee_cents,
        "merchant_fee_cents": row.merchant_fee_cents,
        "payer_total_cents": row.amount_cents + row.user_fee_cents,
        "merchant_net_cents": row.merchant_net_cents,
        "order_reference": row.order_reference,
        "status": row.status,
        "expires_at": row.expires_at.isoformat(),
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
    }


class MerchantApplicationRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    contact_name: str = Field(min_length=2, max_length=100)
    business_name: str = Field(min_length=2, max_length=120)
    website: str | None = Field(default=None, max_length=300)
    category: str | None = Field(default=None, max_length=80)


@router.post("/merchant-applications", status_code=201)
@limiter.limit(RATE_LIMITS["merchant_application"])
def apply_as_merchant(req: MerchantApplicationRequest, request: Request, db: Session = Depends(get_db)):
    email = req.email.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise HTTPException(status_code=422, detail="Enter a valid business email")
    row = MerchantApplication(
        email=email,
        contact_name=req.contact_name.strip(),
        business_name=req.business_name.strip(),
        website=req.website.strip() if req.website else None,
        category=req.category.strip() if req.category else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "status": row.status, "message": "Application received. FAWN will contact you before payment access is enabled."}


class MerchantSignupRequest(BaseModel):
    business_name: str = Field(min_length=2, max_length=120)
    display_name: str = Field(min_length=2, max_length=80)
    website: str | None = Field(default=None, max_length=300)
    support_email: str = Field(min_length=5, max_length=254)


def _slug(value: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:48] or "merchant"
    return f"{base}-{secrets.token_hex(3)}"


@router.post("/merchants", status_code=201)
def create_merchant(
    req: MerchantSignupRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    existing = db.query(MerchantAccount).filter(MerchantAccount.owner_user_id == current_user.id).first()
    if existing:
        return _merchant_payload(existing)
    if not current_user.wallet_initialized:
        raise HTTPException(status_code=409, detail="Create your FAWN custodial wallet before applying as a merchant")
    active_wallet = db.query(CryptoWallet).filter(
        CryptoWallet.user_id == current_user.id,
        CryptoWallet.status == "active",
        CryptoWallet.wallet_type == "fawn_custodial",
    ).first()
    if not active_wallet:
        raise HTTPException(status_code=409, detail="An active custodial wallet is required for merchant settlement")
    row = MerchantAccount(
        owner_user_id=current_user.id,
        business_name=req.business_name.strip(),
        display_name=req.display_name.strip(),
        merchant_slug=_slug(req.display_name),
        website=req.website.strip() if req.website else None,
        support_email=req.support_email.strip().lower(),
        transaction_fee_cents=MERCHANT_FEE_CENTS,
    )
    db.add(row)
    try:
        db.flush()
        _audit(db, current_user.id, "closed_loop_merchant_applied", {"merchant_id": row.id}, request)
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.query(MerchantAccount).filter(MerchantAccount.owner_user_id == current_user.id).first()
        if existing:
            return _merchant_payload(existing)
        raise HTTPException(status_code=409, detail="Merchant account could not be created")
    db.refresh(row)
    return _merchant_payload(row)


@router.get("/merchants/me")
def get_my_merchant(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.query(MerchantAccount).filter(MerchantAccount.owner_user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="No merchant account")
    return _merchant_payload(row)


@router.post("/admin/merchants/{merchant_id}/approve", dependencies=[Depends(_admin_key)])
def approve_merchant(merchant_id: str, db: Session = Depends(get_db)):
    row = db.query(MerchantAccount).filter(MerchantAccount.id == merchant_id).with_for_update().first()
    if not row:
        raise HTTPException(status_code=404, detail="Merchant not found")
    row.status = "active"
    row.approved_at = _now()
    _audit(db, row.owner_user_id, "closed_loop_merchant_approved", {"merchant_id": row.id})
    db.commit()
    return _merchant_payload(row)


@router.post("/cards", status_code=201)
@limiter.limit(RATE_LIMITS["closed_loop_card_issue"])
def issue_card(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not current_user.wallet_initialized or not current_user.crypto_wallet_address:
        raise HTTPException(status_code=409, detail="Create your custodial wallet before issuing a FAWN card")
    active_wallet = db.query(CryptoWallet).filter(
        CryptoWallet.user_id == current_user.id,
        CryptoWallet.status == "active",
        CryptoWallet.wallet_type == "fawn_custodial",
    ).first()
    if not active_wallet:
        raise HTTPException(status_code=409, detail="An active custodial wallet is required before issuing a FAWN card")
    row = db.query(ClosedLoopCard).filter(ClosedLoopCard.user_id == current_user.id).first()
    if not row:
        row = ClosedLoopCard(user_id=current_user.id)
        db.add(row)
        try:
            db.flush()
            _audit(db, current_user.id, "closed_loop_card_issued", {"card_id": row.id}, request)
            db.commit()
        except IntegrityError:
            db.rollback()
            row = db.query(ClosedLoopCard).filter(ClosedLoopCard.user_id == current_user.id).first()
            if not row:
                raise HTTPException(status_code=409, detail="FAWN card could not be issued")
        db.refresh(row)
    return _card_payload(row)


@router.get("/cards/me")
def get_my_card(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.query(ClosedLoopCard).filter(ClosedLoopCard.user_id == current_user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="No FAWN closed-loop card")
    return _card_payload(row)


@router.post("/cards/me/google-wallet")
@limiter.limit(RATE_LIMITS["closed_loop_wallet_pass"])
def create_google_wallet_pass(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    card = db.query(ClosedLoopCard).filter(ClosedLoopCard.user_id == current_user.id).first()
    if not card:
        raise HTTPException(status_code=404, detail="Issue your FAWN card before adding a wallet pass")
    if card.status == "closed":
        raise HTTPException(status_code=409, detail="A closed FAWN card cannot be added to a phone wallet")
    add_url = _google_wallet_add_url(card, current_user)
    _audit(db, current_user.id, "closed_loop_google_wallet_pass_created", {"card_id": card.id}, request)
    db.commit()
    return {
        "wallet": "google_wallet",
        "pass_type": "generic",
        "add_url": add_url,
        "smart_tap_enabled": False,
        "payment_card": False,
        "note": "This wallet pass identifies the FAWN card. Payment still requires checkout-bound authorization in FAWN.",
    }


class CardControlsRequest(BaseModel):
    status: str | None = Field(default=None, pattern=r"^(active|frozen)$")
    per_transaction_limit_cents: int | None = Field(default=None, ge=100, le=200_000)
    daily_limit_cents: int | None = Field(default=None, ge=100, le=500_000)


@router.patch("/cards/me")
def update_card_controls(
    req: CardControlsRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = db.query(ClosedLoopCard).filter(ClosedLoopCard.user_id == current_user.id).with_for_update().first()
    if not row:
        raise HTTPException(status_code=404, detail="No FAWN closed-loop card")
    if row.status == "closed":
        raise HTTPException(status_code=409, detail="Closed cards cannot be changed")
    if req.status is not None:
        row.status = req.status
    if req.per_transaction_limit_cents is not None:
        row.per_transaction_limit_cents = req.per_transaction_limit_cents
    if req.daily_limit_cents is not None:
        row.daily_limit_cents = req.daily_limit_cents
    if row.daily_limit_cents < row.per_transaction_limit_cents:
        raise HTTPException(status_code=422, detail="Daily limit must be at least the per-transaction limit")
    _audit(db, current_user.id, "closed_loop_card_controls_updated", {"card_id": row.id, "status": row.status}, request)
    db.commit()
    return _card_payload(row)


class TapTokenRequest(BaseModel):
    checkout_token: str = Field(min_length=20, max_length=120)


@router.post("/cards/me/tap-token", status_code=201)
@limiter.limit(RATE_LIMITS["closed_loop_tap_token"])
def create_tap_token(req: TapTokenRequest, request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    card = db.query(ClosedLoopCard).filter(ClosedLoopCard.user_id == current_user.id).first()
    if not card or card.status != "active":
        raise HTTPException(status_code=409, detail="An active FAWN card is required")
    checkout = db.query(ClosedLoopCheckout).filter(ClosedLoopCheckout.checkout_token == req.checkout_token).first()
    if not checkout or checkout.status != "open" or _aware(checkout.expires_at) <= _now():
        raise HTTPException(status_code=409, detail="An open, unexpired FAWN checkout is required")
    merchant = db.query(MerchantAccount).filter(MerchantAccount.id == checkout.merchant_id).first()
    if not merchant or merchant.status != "active":
        raise HTTPException(status_code=409, detail="Merchant is not active")
    if merchant.owner_user_id == current_user.id:
        raise HTTPException(status_code=400, detail="A merchant cannot pay its own checkout")
    raw = secrets.token_urlsafe(32)
    expires = _now() + timedelta(seconds=TAP_TOKEN_TTL_SECONDS)
    db.add(ClosedLoopTapToken(
        card_id=card.id,
        user_id=current_user.id,
        checkout_id=checkout.id,
        merchant_id=merchant.id,
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        expires_at=expires,
    ))
    db.commit()
    return {
        "tap_token": raw,
        "tap_payload": f"fawn://tap/{raw}",
        "qr_payload": f"fawn://tap/{raw}",
        "expires_at": expires.isoformat(),
        "single_use": True,
        "merchant_name": merchant.display_name,
        "amount_cents": checkout.amount_cents,
        "payer_total_cents": checkout.amount_cents + USER_FEE_CENTS,
    }


class CheckoutCreateRequest(BaseModel):
    amount_cents: int = Field(ge=2, le=200_000)
    order_reference: str | None = Field(default=None, max_length=100)


@router.post("/merchant/checkouts", status_code=201)
@limiter.limit(RATE_LIMITS["closed_loop_checkout_create"])
def create_checkout(
    req: CheckoutCreateRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    merchant = db.query(MerchantAccount).filter(MerchantAccount.owner_user_id == current_user.id).first()
    if not merchant:
        raise HTTPException(status_code=404, detail="No merchant account")
    if merchant.status != "active":
        raise HTTPException(status_code=409, detail="Merchant review must be completed before accepting payments")
    stored_idempotency_key = None
    if idempotency_key:
        if len(idempotency_key) < 8 or len(idempotency_key) > 200:
            raise HTTPException(status_code=422, detail="Idempotency-Key must be 8 to 200 characters")
        stored_idempotency_key = hashlib.sha256(f"{merchant.id}:{idempotency_key}".encode()).hexdigest()
        existing = db.query(ClosedLoopCheckout).filter(
            ClosedLoopCheckout.idempotency_key == stored_idempotency_key
        ).first()
        if existing:
            base = settings.frontend_base_url.rstrip("/") + "/"
            return _checkout_payload(existing, merchant) | {
                "checkout_url": f"{base}?checkout={existing.checkout_token}",
                "idempotent_replay": True,
            }
    row = ClosedLoopCheckout(
        merchant_id=merchant.id,
        idempotency_key=stored_idempotency_key,
        amount_cents=req.amount_cents,
        user_fee_cents=USER_FEE_CENTS,
        merchant_fee_cents=MERCHANT_FEE_CENTS,
        order_reference=req.order_reference.strip() if req.order_reference else None,
        expires_at=_now() + timedelta(minutes=CHECKOUT_TTL_MINUTES),
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if stored_idempotency_key:
            existing = db.query(ClosedLoopCheckout).filter(
                ClosedLoopCheckout.idempotency_key == stored_idempotency_key
            ).first()
            if existing:
                base = settings.frontend_base_url.rstrip("/") + "/"
                return _checkout_payload(existing, merchant) | {
                    "checkout_url": f"{base}?checkout={existing.checkout_token}",
                    "idempotent_replay": True,
                }
        raise HTTPException(status_code=409, detail="Checkout could not be created")
    db.refresh(row)
    base = settings.frontend_base_url.rstrip("/") + "/"
    return _checkout_payload(row, merchant) | {"checkout_url": f"{base}?checkout={row.checkout_token}"}


@router.get("/checkouts/{checkout_token}")
def get_checkout(checkout_token: str, db: Session = Depends(get_db)):
    row = db.query(ClosedLoopCheckout).filter(ClosedLoopCheckout.checkout_token == checkout_token).first()
    if not row:
        raise HTTPException(status_code=404, detail="Checkout not found")
    if row.status == "open" and _aware(row.expires_at) <= _now():
        row.status = "expired"
        db.commit()
    merchant = db.query(MerchantAccount).filter(MerchantAccount.id == row.merchant_id).first()
    return _checkout_payload(row, merchant)


def _settle(
    db: Session,
    checkout_token: str,
    payer_id: str,
    card_id: str,
    request: Request | None = None,
) -> dict:
    checkout = db.query(ClosedLoopCheckout).filter(
        ClosedLoopCheckout.checkout_token == checkout_token
    ).with_for_update().first()
    if not checkout:
        raise HTTPException(status_code=404, detail="Checkout not found")
    merchant = db.query(MerchantAccount).filter(MerchantAccount.id == checkout.merchant_id).with_for_update().first()
    if not merchant or merchant.status != "active":
        raise HTTPException(status_code=409, detail="Merchant is not active")
    if checkout.status == "completed":
        if checkout.payer_user_id == payer_id:
            return _checkout_payload(checkout, merchant) | {"idempotent_replay": True}
        raise HTTPException(status_code=409, detail="Checkout has already been paid")
    if checkout.status != "open":
        raise HTTPException(status_code=409, detail=f"Checkout is {checkout.status}")
    if _aware(checkout.expires_at) <= _now():
        checkout.status = "expired"
        db.commit()
        raise HTTPException(status_code=410, detail="Checkout expired")
    if merchant.owner_user_id == payer_id:
        raise HTTPException(status_code=400, detail="A merchant cannot pay its own checkout")

    card = db.query(ClosedLoopCard).filter(
        ClosedLoopCard.id == card_id,
        ClosedLoopCard.user_id == payer_id,
    ).with_for_update().first()
    if not card or card.status != "active":
        raise HTTPException(status_code=409, detail="An active FAWN card is required")
    if checkout.amount_cents > card.per_transaction_limit_cents:
        raise HTTPException(status_code=403, detail="Purchase exceeds the card's per-transaction limit")

    since = _now() - timedelta(hours=24)
    spent = db.query(func.coalesce(func.sum(ClosedLoopCheckout.amount_cents + ClosedLoopCheckout.user_fee_cents), 0)).filter(
        ClosedLoopCheckout.payer_user_id == payer_id,
        ClosedLoopCheckout.status == "completed",
        ClosedLoopCheckout.completed_at >= since,
    ).scalar() or 0
    payer_total = checkout.amount_cents + USER_FEE_CENTS
    if int(spent) + payer_total > card.daily_limit_cents:
        raise HTTPException(status_code=403, detail="Purchase exceeds the card's rolling 24-hour limit")

    # Lock users in stable primary-key order to prevent A-pays-B/B-pays-A
    # deadlocks under concurrent Postgres settlement.
    locked_users = db.query(User).filter(User.id.in_([payer_id, merchant.owner_user_id])).order_by(User.id).with_for_update().all()
    users_by_id = {row.id: row for row in locked_users}
    payer = users_by_id.get(payer_id)
    merchant_owner = users_by_id.get(merchant.owner_user_id)
    if not payer or not merchant_owner:
        raise HTTPException(status_code=409, detail="Payment account is unavailable")
    if payer.usdc_balance_cents < payer_total:
        raise HTTPException(status_code=402, detail="Insufficient FAWN balance")

    merchant_net = checkout.amount_cents - MERCHANT_FEE_CENTS
    payer.usdc_balance_cents -= payer_total
    payer.total_fees_paid_cents += USER_FEE_CENTS
    merchant_owner.usdc_balance_cents += merchant_net
    merchant_owner.total_fees_paid_cents += MERCHANT_FEE_CENTS
    merchant.total_volume_cents += checkout.amount_cents
    merchant.total_fees_paid_cents += MERCHANT_FEE_CENTS

    locked_wallets = db.query(CryptoWallet).filter(
        CryptoWallet.user_id.in_([payer.id, merchant_owner.id]),
        CryptoWallet.status == "active",
        CryptoWallet.wallet_type == "fawn_custodial",
    ).order_by(CryptoWallet.user_id).with_for_update().all()
    wallets_by_user = {row.user_id: row for row in locked_wallets}
    payer_wallet = wallets_by_user.get(payer.id)
    merchant_wallet = wallets_by_user.get(merchant_owner.id)
    if not payer_wallet or not merchant_wallet:
        raise HTTPException(status_code=409, detail="Both sides need an active custodial wallet before settlement")
    payer_wallet.usdc_balance_cents = payer.usdc_balance_cents
    merchant_wallet.usdc_balance_cents = merchant_owner.usdc_balance_cents
    # Closed-loop settlement is ledger-only: both fee cents remain in FAWN's
    # custody on the payer side until the existing treasury sweep moves them
    # on-chain. Recording the full two-cent claim prevents aggregate balances
    # from shrinking without an offsetting fee receivable.
    payer_wallet.pending_fee_cents += USER_FEE_CENTS + MERCHANT_FEE_CENTS

    checkout.status = "completed"
    checkout.payer_user_id = payer.id
    checkout.card_id = card.id
    checkout.merchant_net_cents = merchant_net
    checkout.completed_at = _now()
    _audit(db, payer.id, "closed_loop_purchase_completed", {
        "checkout_id": checkout.id,
        "merchant_id": merchant.id,
        "amount_cents": checkout.amount_cents,
        "fee_cents": USER_FEE_CENTS,
    }, request)
    _audit(db, merchant_owner.id, "closed_loop_sale_completed", {
        "checkout_id": checkout.id,
        "amount_cents": checkout.amount_cents,
        "net_cents": merchant_net,
        "fee_cents": MERCHANT_FEE_CENTS,
    }, request)
    db.commit()
    return _checkout_payload(checkout, merchant) | {
        "payer_balance_cents": payer.usdc_balance_cents,
        "merchant_balance_cents": merchant_owner.usdc_balance_cents,
        "idempotent_replay": False,
    }


@router.post("/checkouts/{checkout_token}/authorize")
@limiter.limit(RATE_LIMITS["closed_loop_checkout_pay"])
def authorize_checkout(
    checkout_token: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    card = db.query(ClosedLoopCard).filter(ClosedLoopCard.user_id == current_user.id).first()
    if not card:
        raise HTTPException(status_code=409, detail="Issue your FAWN card before paying")
    return _settle(db, checkout_token, current_user.id, card.id, request)


class TapPaymentRequest(BaseModel):
    tap_token: str = Field(min_length=32, max_length=200)


@router.post("/merchant/checkouts/{checkout_token}/tap")
@limiter.limit(RATE_LIMITS["closed_loop_checkout_pay"])
def accept_tap(
    checkout_token: str,
    req: TapPaymentRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    merchant = db.query(MerchantAccount).filter(MerchantAccount.owner_user_id == current_user.id).first()
    if not merchant or merchant.status != "active":
        raise HTTPException(status_code=403, detail="An active merchant account is required")
    checkout = db.query(ClosedLoopCheckout).filter(ClosedLoopCheckout.checkout_token == checkout_token).first()
    if not checkout or checkout.merchant_id != merchant.id:
        raise HTTPException(status_code=404, detail="Checkout not found")
    digest = hashlib.sha256(req.tap_token.encode()).hexdigest()
    token = db.query(ClosedLoopTapToken).filter(ClosedLoopTapToken.token_hash == digest).with_for_update().first()
    if not token or token.used_at is not None or _aware(token.expires_at) <= _now():
        raise HTTPException(status_code=401, detail="Tap credential is invalid or expired")
    if token.checkout_id != checkout.id or token.merchant_id != merchant.id:
        raise HTTPException(status_code=403, detail="Tap credential was created for a different checkout")
    token.used_at = _now()
    result = _settle(db, checkout_token, token.user_id, token.card_id, request)
    return result | {"acceptance_method": "dynamic_tap_token"}


@router.get("/purchases")
def list_purchases(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.query(ClosedLoopCheckout).filter(
        ClosedLoopCheckout.payer_user_id == current_user.id,
        ClosedLoopCheckout.status == "completed",
    ).order_by(ClosedLoopCheckout.completed_at.desc()).limit(100).all()
    merchant_ids = {row.merchant_id for row in rows}
    merchants = {m.id: m for m in db.query(MerchantAccount).filter(MerchantAccount.id.in_(merchant_ids)).all()} if merchant_ids else {}
    return {"purchases": [_checkout_payload(row, merchants.get(row.merchant_id)) for row in rows]}


@router.get("/activity")
def list_closed_loop_activity(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    payer_rows = db.query(ClosedLoopCheckout).filter(
        ClosedLoopCheckout.payer_user_id == current_user.id,
        ClosedLoopCheckout.status == "completed",
    ).all()
    merchant = db.query(MerchantAccount).filter(MerchantAccount.owner_user_id == current_user.id).first()
    sale_rows = db.query(ClosedLoopCheckout).filter(
        ClosedLoopCheckout.merchant_id == merchant.id,
        ClosedLoopCheckout.status == "completed",
    ).all() if merchant else []
    merchant_ids = {row.merchant_id for row in payer_rows}
    merchants = {m.id: m for m in db.query(MerchantAccount).filter(MerchantAccount.id.in_(merchant_ids)).all()} if merchant_ids else {}
    activity = [{
        "id": row.id,
        "type": "purchase",
        "amount_cents": -(row.amount_cents + row.user_fee_cents),
        "principal_cents": row.amount_cents,
        "fee_cents": row.user_fee_cents,
        "counterparty": merchants.get(row.merchant_id).display_name if merchants.get(row.merchant_id) else "FAWN merchant",
        "status": row.status,
        "created_at": row.completed_at.isoformat() if row.completed_at else row.created_at.isoformat(),
    } for row in payer_rows]
    activity.extend({
        "id": row.id,
        "type": "merchant_sale",
        "amount_cents": row.merchant_net_cents,
        "principal_cents": row.amount_cents,
        "fee_cents": row.merchant_fee_cents,
        "counterparty": "FAWN customer",
        "status": row.status,
        "created_at": row.completed_at.isoformat() if row.completed_at else row.created_at.isoformat(),
    } for row in sale_rows)
    activity.sort(key=lambda item: item["created_at"], reverse=True)
    return {"activity": activity[:100]}


@router.get("/merchant/checkouts")
def list_merchant_checkouts(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    merchant = db.query(MerchantAccount).filter(MerchantAccount.owner_user_id == current_user.id).first()
    if not merchant:
        raise HTTPException(status_code=404, detail="No merchant account")
    rows = db.query(ClosedLoopCheckout).filter(ClosedLoopCheckout.merchant_id == merchant.id).order_by(
        ClosedLoopCheckout.created_at.desc()
    ).limit(100).all()
    return {"checkouts": [_checkout_payload(row, merchant) for row in rows]}
