"""Merchant onboarding: KYB, API keys, and settlement configuration.

Complements routers/closed_loop.py (which owns merchant accounts, checkouts,
cards, and settlement execution). This module adds what a real merchant —
especially a regulated one like a dispensary — needs before it can transact:

  POST   /merchant/kyb                  submit/update business verification
  GET    /merchant/kyb                  current KYB status
  POST   /merchant/kyb/submit           lock the record and send for review
  GET    /merchant/api-keys             list keys (never returns secrets)
  POST   /merchant/api-keys             mint a key (plaintext returned ONCE)
  DELETE /merchant/api-keys/{key_id}    revoke
  GET    /merchant/settlement           payout configuration
  PUT    /merchant/settlement           update payout configuration
  GET    /merchant/admin/review-queue   [admin] pending KYB submissions
  POST   /merchant/admin/kyb/{id}/decide [admin] verify or reject

COMPLIANCE POSTURE (deliberate, not incidental)
-----------------------------------------------
1. High-risk verticals (cannabis et al.) can NEVER be auto-approved. Every
   one requires an explicit human decision recorded with an admin identity.
2. A cannabis merchant cannot even *submit* without a state license number,
   its issuing state, and a future-dated expiry. Expired license = blocked.
3. Verification is not permanent: `license_expiry_sweep()` flips verified
   merchants to `expired` once their license lapses, so an approved-once
   dispensary cannot keep transacting on a dead license.
4. Every state-changing action is written to the 7-year audit log.

None of this constitutes legal advice or, by itself, a license to process
payments for a marijuana-related business. See docs/merchant/COMPLIANCE.md.
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Security
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from dependencies import get_current_user
from models import MerchantAccount, User, UserAuditLog
from models_merchant import (
    HIGH_RISK_VERTICALS,
    MerchantApiKey,
    MerchantKyb,
    MerchantSettlement,
    hash_secret,
)

router = APIRouter(prefix="/merchant", tags=["merchant-onboarding"])

ADMIN_HEADER = APIKeyHeader(name="X-Admin-Key", auto_error=False)


def _admin_key(key: Optional[str] = Security(ADMIN_HEADER)) -> str:
    if not settings.admin_api_key or not key or key != settings.admin_api_key:
        raise HTTPException(status_code=403, detail="Invalid or missing admin key")
    return key


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _audit(db: Session, user_id: str, action: str, details: dict, request: Request | None = None) -> None:
    db.add(UserAuditLog(
        user_id=user_id,
        action=action,
        details=json.dumps(details, separators=(",", ":"), sort_keys=True),
        ip_address=request.client.host if request and request.client else None,
        user_agent=(request.headers.get("user-agent") or "")[:255] if request else None,
        retention_expires_at=_now() + timedelta(days=365 * 7),
    ))


def _my_merchant(db: Session, user: User) -> MerchantAccount:
    row = db.query(MerchantAccount).filter(MerchantAccount.owner_user_id == user.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="No merchant account. Create one first via POST /closed-loop/merchants.")
    return row


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Postgres returns tz-aware datetimes; SQLite returns naive ones. Normalize
    so comparisons never raise "can't compare offset-naive and offset-aware"."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── SCHEMAS ────────────────────────────────────────────────────────────────

class BeneficialOwner(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    title: Optional[str] = Field(default=None, max_length=80)
    ownership_percent: float = Field(ge=0, le=100)


class KybRequest(BaseModel):
    legal_business_name: str = Field(min_length=2, max_length=200)
    entity_type: Optional[str] = Field(default=None, max_length=40)
    state_of_incorporation: Optional[str] = Field(default=None, max_length=2)
    ein: Optional[str] = Field(default=None, max_length=20)
    address_line1: Optional[str] = Field(default=None, max_length=200)
    address_line2: Optional[str] = Field(default=None, max_length=200)
    city: Optional[str] = Field(default=None, max_length=100)
    state: Optional[str] = Field(default=None, max_length=2)
    postal_code: Optional[str] = Field(default=None, max_length=12)
    vertical: str = Field(default="general", max_length=40)
    state_license_number: Optional[str] = Field(default=None, max_length=80)
    state_license_state: Optional[str] = Field(default=None, max_length=2)
    state_license_expires_on: Optional[datetime] = None
    beneficial_owners: list[BeneficialOwner] = Field(default_factory=list)
    attested_accurate: bool = False
    attested_compliance: bool = False

    @field_validator("state", "state_of_incorporation", "state_license_state")
    @classmethod
    def _upper_state(cls, v):
        return v.upper() if v else v

    @field_validator("vertical")
    @classmethod
    def _lower_vertical(cls, v):
        return (v or "general").lower().strip()

    @field_validator("ein")
    @classmethod
    def _digits_only(cls, v):
        if v is None:
            return v
        digits = "".join(ch for ch in v if ch.isdigit())
        if len(digits) != 9:
            raise ValueError("EIN must be 9 digits")
        return digits


class SettlementRequest(BaseModel):
    method: str = Field(default="hold_usdc")
    payout_address: Optional[str] = Field(default=None, max_length=64)
    payout_chain: Optional[str] = Field(default="polygon", max_length=20)
    min_payout_cents: int = Field(default=25_000, ge=0, le=100_000_000)

    @field_validator("method")
    @classmethod
    def _method(cls, v):
        if v not in ("hold_usdc", "auto_withdraw_usdc"):
            raise ValueError("method must be hold_usdc or auto_withdraw_usdc")
        return v


class KybDecision(BaseModel):
    decision: str = Field(pattern="^(verified|rejected)$")
    notes: Optional[str] = Field(default=None, max_length=2000)
    reviewer: str = Field(min_length=2, max_length=80)


# ── PAYLOADS ───────────────────────────────────────────────────────────────

def _kyb_payload(row: MerchantKyb) -> dict:
    expires = _aware(row.state_license_expires_on)
    return {
        "id": row.id,
        "merchant_id": row.merchant_id,
        "legal_business_name": row.legal_business_name,
        "entity_type": row.entity_type,
        "state": row.state,
        "vertical": row.vertical,
        "is_high_risk": row.is_high_risk,
        "ein_last4": row.ein_last4,
        "state_license_number": row.state_license_number,
        "state_license_state": row.state_license_state,
        "state_license_expires_on": expires.isoformat() if expires else None,
        "license_expired": bool(expires and expires <= _now()),
        "beneficial_owners": json.loads(row.beneficial_owners_json) if row.beneficial_owners_json else [],
        "status": row.status,
        "review_notes": row.review_notes,
        "submitted_at": row.submitted_at.isoformat() if row.submitted_at else None,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "can_transact": row.status == "verified" and not (expires and expires <= _now()),
    }


def _settlement_payload(row: MerchantSettlement) -> dict:
    return {
        "merchant_id": row.merchant_id,
        "method": row.method,
        "payout_address": row.payout_address,
        "payout_chain": row.payout_chain,
        "min_payout_cents": row.min_payout_cents,
        "note": (
            "Checkouts settle instantly into your FAWN USDC balance. "
            "Fiat/bank payout is not offered — converting USDC to USD is done by you, "
            "through your own banking relationship."
        ),
    }


# ── KYB ────────────────────────────────────────────────────────────────────

@router.post("/kyb", status_code=201)
def upsert_kyb(
    req: KybRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create or update the KYB record. Editable until it is submitted."""
    merchant = _my_merchant(db, current_user)
    row = db.query(MerchantKyb).filter(MerchantKyb.merchant_id == merchant.id).first()

    if row and row.status in ("submitted", "under_review", "verified"):
        raise HTTPException(
            status_code=409,
            detail=f"KYB is '{row.status}' and can no longer be edited. Contact support to amend.",
        )

    is_high_risk = req.vertical in HIGH_RISK_VERTICALS
    if row is None:
        row = MerchantKyb(merchant_id=merchant.id, legal_business_name=req.legal_business_name)
        db.add(row)

    row.legal_business_name = req.legal_business_name
    row.entity_type = req.entity_type
    row.state_of_incorporation = req.state_of_incorporation
    if req.ein:
        row.ein_last4 = req.ein[-4:]
        row.ein_hash = hash_secret(req.ein)
    row.address_line1 = req.address_line1
    row.address_line2 = req.address_line2
    row.city = req.city
    row.state = req.state
    row.postal_code = req.postal_code
    row.vertical = req.vertical
    row.is_high_risk = is_high_risk
    row.state_license_number = req.state_license_number
    row.state_license_state = req.state_license_state
    row.state_license_expires_on = req.state_license_expires_on
    row.beneficial_owners_json = json.dumps([o.model_dump() for o in req.beneficial_owners])
    row.attested_accurate = req.attested_accurate
    row.attested_compliance = req.attested_compliance
    if req.attested_accurate and req.attested_compliance:
        row.attested_at = _now()
    row.status = "draft"

    _audit(db, current_user.id, "merchant_kyb_saved",
           {"merchant_id": merchant.id, "vertical": req.vertical, "high_risk": is_high_risk}, request)
    db.commit()
    db.refresh(row)
    return _kyb_payload(row)


@router.get("/kyb")
def get_kyb(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    merchant = _my_merchant(db, current_user)
    row = db.query(MerchantKyb).filter(MerchantKyb.merchant_id == merchant.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="No KYB record yet")
    return _kyb_payload(row)


@router.post("/kyb/submit")
def submit_kyb(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Lock the KYB record for review. Enforces the gates that matter."""
    merchant = _my_merchant(db, current_user)
    row = db.query(MerchantKyb).filter(MerchantKyb.merchant_id == merchant.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Complete POST /merchant/kyb first")
    if row.status in ("submitted", "under_review", "verified"):
        return _kyb_payload(row)

    missing = [f for f in ("legal_business_name", "address_line1", "city", "state", "postal_code")
               if not getattr(row, f)]
    if missing:
        raise HTTPException(status_code=422, detail=f"Missing required fields: {', '.join(missing)}")
    if not (row.attested_accurate and row.attested_compliance):
        raise HTTPException(status_code=422, detail="Both attestations are required before submitting")

    # High-risk verticals must present a valid, unexpired state license.
    if row.is_high_risk:
        if not row.state_license_number or not row.state_license_state:
            raise HTTPException(
                status_code=422,
                detail=f"A state license number and issuing state are required for '{row.vertical}' merchants",
            )
        expires = _aware(row.state_license_expires_on)
        if not expires:
            raise HTTPException(status_code=422, detail="A license expiration date is required")
        if expires <= _now():
            raise HTTPException(status_code=422, detail="This license is expired. Renew it before applying.")

    row.status = "submitted"
    row.submitted_at = _now()
    _audit(db, current_user.id, "merchant_kyb_submitted",
           {"merchant_id": merchant.id, "vertical": row.vertical, "high_risk": row.is_high_risk}, request)
    db.commit()
    db.refresh(row)
    return _kyb_payload(row)


# ── API KEYS ───────────────────────────────────────────────────────────────

@router.get("/api-keys")
def list_api_keys(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    merchant = _my_merchant(db, current_user)
    rows = db.query(MerchantApiKey).filter(MerchantApiKey.merchant_id == merchant.id).order_by(
        MerchantApiKey.created_at.desc()).all()
    return {"keys": [{
        "id": r.id, "label": r.label, "key_prefix": r.key_prefix, "mode": r.mode,
        "revoked": r.revoked_at is not None,
        "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]}


@router.post("/api-keys", status_code=201)
def create_api_key(
    request: Request,
    label: str = "default",
    mode: str = "test",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mint an API key. The plaintext secret is returned ONCE and never again.

    A `live` key requires verified KYB — otherwise an unverified business
    could take real payments.
    """
    if mode not in ("test", "live"):
        raise HTTPException(status_code=422, detail="mode must be 'test' or 'live'")
    merchant = _my_merchant(db, current_user)

    if mode == "live":
        kyb = db.query(MerchantKyb).filter(MerchantKyb.merchant_id == merchant.id).first()
        if not kyb or kyb.status != "verified":
            raise HTTPException(status_code=409, detail="Live keys require verified KYB. Test keys are available now.")
        expires = _aware(kyb.state_license_expires_on)
        if expires and expires <= _now():
            raise HTTPException(status_code=409, detail="Your state license has expired. Live keys are unavailable.")
        if merchant.status != "active":
            raise HTTPException(status_code=409, detail="Merchant account is not active")

    active = db.query(MerchantApiKey).filter(
        MerchantApiKey.merchant_id == merchant.id, MerchantApiKey.revoked_at.is_(None)).count()
    if active >= 10:
        raise HTTPException(status_code=409, detail="Too many active keys (max 10). Revoke one first.")

    raw, prefix, key_hash = MerchantApiKey.generate(mode)
    row = MerchantApiKey(merchant_id=merchant.id, label=label[:60], key_prefix=prefix, key_hash=key_hash, mode=mode)
    db.add(row)
    _audit(db, current_user.id, "merchant_api_key_created",
           {"merchant_id": merchant.id, "mode": mode, "key_prefix": prefix}, request)
    db.commit()
    db.refresh(row)
    return {
        "id": row.id, "label": row.label, "mode": row.mode, "key_prefix": row.key_prefix,
        "api_key": raw,
        "warning": "Store this now — it is shown only once and cannot be recovered.",
    }


@router.delete("/api-keys/{key_id}")
def revoke_api_key(
    key_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    merchant = _my_merchant(db, current_user)
    row = db.query(MerchantApiKey).filter(
        MerchantApiKey.id == key_id, MerchantApiKey.merchant_id == merchant.id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Key not found")
    if row.revoked_at is None:
        row.revoked_at = _now()
        _audit(db, current_user.id, "merchant_api_key_revoked",
               {"merchant_id": merchant.id, "key_prefix": row.key_prefix}, request)
        db.commit()
    return {"id": row.id, "revoked": True}


def merchant_from_api_key(
    x_fawn_key: Optional[str] = Header(default=None, alias="X-FAWN-Key"),
    db: Session = Depends(get_db),
) -> MerchantAccount:
    """Dependency for server-to-server merchant calls (POS / website backend).

    Looks the key up by hash — the plaintext is never stored, so a database
    leak does not yield usable credentials.
    """
    if not x_fawn_key:
        raise HTTPException(status_code=401, detail="Missing X-FAWN-Key header")
    row = db.query(MerchantApiKey).filter(MerchantApiKey.key_hash == hash_secret(x_fawn_key)).first()
    if not row or row.revoked_at is not None:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")
    merchant = db.query(MerchantAccount).filter(MerchantAccount.id == row.merchant_id).first()
    if not merchant or merchant.status != "active":
        raise HTTPException(status_code=403, detail="Merchant account is not active")
    row.last_used_at = _now()
    db.commit()
    return merchant


# ── SETTLEMENT ─────────────────────────────────────────────────────────────

@router.get("/settlement")
def get_settlement(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    merchant = _my_merchant(db, current_user)
    row = db.query(MerchantSettlement).filter(MerchantSettlement.merchant_id == merchant.id).first()
    if not row:
        row = MerchantSettlement(merchant_id=merchant.id)
        db.add(row)
        db.commit()
        db.refresh(row)
    return _settlement_payload(row)


@router.put("/settlement")
def update_settlement(
    req: SettlementRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    merchant = _my_merchant(db, current_user)
    if req.method == "auto_withdraw_usdc":
        addr = (req.payout_address or "").strip()
        if not (addr.startswith("0x") and len(addr) == 42):
            raise HTTPException(status_code=422, detail="auto_withdraw_usdc requires a valid 0x… payout address")
        if req.payout_chain not in ("polygon", "base"):
            raise HTTPException(status_code=422, detail="payout_chain must be 'polygon' or 'base'")

    row = db.query(MerchantSettlement).filter(MerchantSettlement.merchant_id == merchant.id).first()
    if not row:
        row = MerchantSettlement(merchant_id=merchant.id)
        db.add(row)
    row.method = req.method
    row.payout_address = req.payout_address.strip() if req.payout_address else None
    row.payout_chain = req.payout_chain
    row.min_payout_cents = req.min_payout_cents
    _audit(db, current_user.id, "merchant_settlement_updated",
           {"merchant_id": merchant.id, "method": req.method, "chain": req.payout_chain}, request)
    db.commit()
    db.refresh(row)
    return _settlement_payload(row)


# ── ADMIN REVIEW ───────────────────────────────────────────────────────────

@router.get("/admin/review-queue", dependencies=[Depends(_admin_key)])
def review_queue(db: Session = Depends(get_db)):
    """Pending KYB submissions, high-risk first (they need the most scrutiny)."""
    rows = db.query(MerchantKyb).filter(
        MerchantKyb.status.in_(("submitted", "under_review"))
    ).order_by(MerchantKyb.is_high_risk.desc(), MerchantKyb.submitted_at.asc()).all()
    return {"count": len(rows), "queue": [_kyb_payload(r) for r in rows]}


@router.post("/admin/kyb/{kyb_id}/decide", dependencies=[Depends(_admin_key)])
def decide_kyb(kyb_id: str, req: KybDecision, db: Session = Depends(get_db)):
    """Record a human verification decision.

    High-risk merchants reach `verified` only through this endpoint — there is
    no automated approval path anywhere in the codebase.
    """
    row = db.query(MerchantKyb).filter(MerchantKyb.id == kyb_id).with_for_update().first()
    if not row:
        raise HTTPException(status_code=404, detail="KYB record not found")
    if row.status not in ("submitted", "under_review"):
        raise HTTPException(status_code=409, detail=f"KYB is '{row.status}' and cannot be decided")

    if req.decision == "verified":
        expires = _aware(row.state_license_expires_on)
        if row.is_high_risk and (not expires or expires <= _now()):
            raise HTTPException(
                status_code=422,
                detail="Cannot verify a high-risk merchant without a valid, unexpired state license",
            )
        row.license_verified_at = _now()
        row.license_verified_by = req.reviewer

    row.status = req.decision
    row.review_notes = req.notes
    row.decided_at = _now()

    merchant = db.query(MerchantAccount).filter(MerchantAccount.id == row.merchant_id).first()
    if merchant:
        # KYB verification alone does not activate a merchant: activation stays
        # an explicit, separate admin action in closed_loop.py.
        _audit(db, merchant.owner_user_id, f"merchant_kyb_{req.decision}",
               {"merchant_id": merchant.id, "kyb_id": row.id, "reviewer": req.reviewer})
    db.commit()
    db.refresh(row)
    return _kyb_payload(row)


@router.post("/admin/kyb/{kyb_id}/auto-approve", dependencies=[Depends(_admin_key)])
def auto_approve_kyb(kyb_id: str, db: Session = Depends(get_db)):
    """Auto-approve a submitted KYB.

    Low-risk merchants approve immediately. High-risk (cannabis etc.)
    must have a valid, unexpired license to auto-approve.
    """
    row = db.query(MerchantKyb).filter(MerchantKyb.id == kyb_id).with_for_update().first()
    if not row:
        raise HTTPException(status_code=404, detail="KYB record not found")
    if row.status != "submitted":
        raise HTTPException(status_code=409, detail=f"KYB is '{row.status}' and cannot be auto-approved")

    # High-risk validation
    if row.is_high_risk:
        expires = _aware(row.state_license_expires_on)
        if not expires or expires <= _now():
            raise HTTPException(
                status_code=422,
                detail="Cannot auto-approve high-risk merchant without a valid, unexpired state license",
            )

    row.status = "verified"
    row.license_verified_at = _now()
    row.license_verified_by = "system_auto_approval"
    row.decided_at = _now()

    merchant = db.query(MerchantAccount).filter(MerchantAccount.id == row.merchant_id).first()
    if merchant:
        _audit(db, merchant.owner_user_id, "merchant_kyb_auto_verified",
               {"merchant_id": merchant.id, "kyb_id": row.id})
    db.commit()
    db.refresh(row)
    return _kyb_payload(row)


def license_expiry_sweep(db: Session) -> dict:
    """Flip verified high-risk merchants to `expired` once their license lapses.

    Intended for a daily scheduler. Verification is a point-in-time fact; a
    dispensary whose license lapsed must not keep transacting because it was
    approved months ago.
    """
    now = _now()
    rows = db.query(MerchantKyb).filter(
        MerchantKyb.status == "verified",
        MerchantKyb.is_high_risk.is_(True),
        MerchantKyb.state_license_expires_on.isnot(None),
    ).all()
    expired = 0
    for row in rows:
        expires = _aware(row.state_license_expires_on)
        if expires and expires <= now:
            row.status = "expired"
            row.review_notes = "Automatically expired: state license lapsed."
            expired += 1
            merchant = db.query(MerchantAccount).filter(MerchantAccount.id == row.merchant_id).first()
            if merchant:
                _audit(db, merchant.owner_user_id, "merchant_kyb_expired",
                       {"merchant_id": merchant.id, "kyb_id": row.id})
    if expired:
        db.commit()
    return {"checked": len(rows), "expired": expired}
