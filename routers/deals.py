"""
routers/deals.py

Community-submitted Campus Savings deal suggestions.

Flow:
  POST /deals/suggest        -> public, stores a pending suggestion
  GET  /deals/suggestions    -> admin-key protected, review queue
  POST /deals/suggestions/{id}/review -> admin-key protected, approve/reject

Approved suggestions are not auto-published — Alex reviews and manually
folds verified ones into the SCHOOLS data array in index.html. This keeps
the public-facing prices honest (community-sourced, human-verified) rather
than auto-publishing unverified claims.
"""

import hmac
import json
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Security, Request, status
from fastapi.security.api_key import APIKeyHeader
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy.orm import Session
from slowapi import Limiter
from slowapi.util import get_remote_address

from database import get_db
from models import DealSuggestion
from services.analytics import capture, EVENTS

router = APIRouter(prefix="/deals", tags=["deals"])
limiter = Limiter(key_func=get_remote_address)

API_KEY_HEADER = APIKeyHeader(name="X-Admin-Key", auto_error=False)

VALID_SCHOOLS = {
    "berkeley", "stanford", "scu", "sjsu", "usf", "sfstate", "smc", "uoregon",
    "sierra", "sacstate", "yubacollege", "calpoly", "ucdavis", "csumb", "ucsb",
    "oregonstate", "unlv", "unr", "ucsc",
    "ucla", "ucsd", "sdsu", "uci", "ucr", "ucmerced", "csulb", "csuf",
    "fresnostate", "chicostate", "humboldt", "uw", "seattleu", "wsu", "wwu", "psu",
}
VALID_CATEGORIES = {"gas", "food", "coffee", "housing", "bars", "bulk", "coupons"}

_SCHOOLS_DATA_PATH = Path(__file__).resolve().parent.parent / "schools_data.json"
with open(_SCHOOLS_DATA_PATH, encoding="utf-8") as f:
    _SCHOOLS_DATA = json.load(f)
_SCHOOLS_BY_KEY = {s["key"]: s for s in _SCHOOLS_DATA["SCHOOLS"]}


def require_admin_key(api_key: Optional[str] = Security(API_KEY_HEADER)) -> str:
    expected = os.environ.get("ADMIN_API_KEY", "")
    if not expected:
        raise HTTPException(status_code=500, detail="ADMIN_API_KEY not configured.")
    if not api_key or not hmac.compare_digest(api_key, expected):
        raise HTTPException(status_code=403, detail="Invalid or missing X-Admin-Key header.")
    return api_key


class SuggestionIn(BaseModel):
    school: str
    category: str
    suggestion: str
    submitter_email: Optional[EmailStr] = None

    @field_validator("school")
    @classmethod
    def check_school(cls, v):
        if v not in VALID_SCHOOLS:
            raise ValueError(f"school must be one of {sorted(VALID_SCHOOLS)}")
        return v

    @field_validator("category")
    @classmethod
    def check_category(cls, v):
        if v not in VALID_CATEGORIES:
            raise ValueError(f"category must be one of {sorted(VALID_CATEGORIES)}")
        return v

    @field_validator("suggestion")
    @classmethod
    def check_suggestion_length(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("suggestion cannot be empty")
        if len(v) > 500:
            raise ValueError("suggestion must be 500 characters or fewer")
        return v


class SuggestionOut(BaseModel):
    id: str
    school: str
    category: str
    suggestion: str
    submitter_email: Optional[str]
    status: str

    class Config:
        from_attributes = True


@router.post("/suggest", status_code=201)
@limiter.limit("10/minute")
def submit_suggestion(request: Request, body: SuggestionIn, db: Session = Depends(get_db)):
    entry = DealSuggestion(
        school=body.school,
        category=body.category,
        suggestion=body.suggestion,
        submitter_email=body.submitter_email,
    )
    db.add(entry)
    db.commit()
    capture(
        EVENTS["DEAL_SUGGESTION_SUBMITTED"],
        body.submitter_email or "anonymous",
        {"school": body.school, "category": body.category},
    )
    return {"message": "Thanks — we'll review it and add it if it checks out.", "id": entry.id}


@router.get("/schools")
def list_schools():
    """All campus savings data — same dataset rendered on the marketing site's
    Campus Savings hub, served here so the actual banking dashboard can pull
    a single user's school without embedding 16 schools of data client-side.
    """
    return {
        "schools": [
            {"key": s["key"], "name": s["name"]} for s in _SCHOOLS_DATA["SCHOOLS"]
        ],
        "categoryMeta": _SCHOOLS_DATA["CAT_META"],
    }


@router.get("/schools/{school_key}")
def get_school_deals(school_key: str):
    """Full deal data for one school — what the dashboard's Campus Savings
    card fetches based on the logged-in user's `school` field.
    """
    school = _SCHOOLS_BY_KEY.get(school_key)
    if not school:
        raise HTTPException(status_code=404, detail=f"Unknown school '{school_key}'")
    return {
        "key": school["key"],
        "name": school["name"],
        "categories": school["categories"],
        "categoryMeta": _SCHOOLS_DATA["CAT_META"],
    }


@router.get("/suggestions", response_model=list[SuggestionOut])
def list_suggestions(
    status_filter: Optional[str] = None,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin_key),
):
    q = db.query(DealSuggestion)
    if status_filter:
        q = q.filter(DealSuggestion.status == status_filter)
    return q.order_by(DealSuggestion.created_at.desc()).limit(200).all()


@router.post("/suggestions/{suggestion_id}/review")
def review_suggestion(
    suggestion_id: str,
    new_status: str,
    db: Session = Depends(get_db),
    _: str = Depends(require_admin_key),
):
    if new_status not in {"approved", "rejected", "pending"}:
        raise HTTPException(status_code=400, detail="new_status must be approved, rejected, or pending")
    entry = db.query(DealSuggestion).filter(DealSuggestion.id == suggestion_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    entry.status = new_status
    db.commit()
    return {"id": entry.id, "status": entry.status}
