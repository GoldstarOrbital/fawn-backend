from datetime import datetime, timezone
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from database import get_db
from dependencies import get_current_user
from models import User, NewsAlert
from services import claude as claude_svc
from services.claude import VALID_CATEGORIES
from rate_limiting import limiter

router = APIRouter(prefix="/news", tags=["news"])

MAX_ALERTS_PER_USER = 10

_DISCLAIMER = "Financial news for informational purposes only. Not investment advice."


def _validate_category(category: Optional[str]) -> Optional[str]:
    if category and category not in VALID_CATEGORIES:
        raise HTTPException(status_code=422, detail=f"category must be one of: {', '.join(VALID_CATEGORIES)}")
    return category


@router.get("/public-headlines")
async def get_public_headlines(
    q: Optional[str] = Query(default=None, max_length=120),
    keywords: Optional[List[str]] = Query(default=None),
    limit: int = Query(default=12, ge=1, le=20),
    category: Optional[str] = Query(default=None),
):
    """
    Public landing-page preview for live financial news.
    Does not expose user data or require an account.
    """
    _validate_category(category)
    search_terms = _build_search_terms(q, keywords)
    result = await claude_svc.summarize_financial_news(keywords=search_terms, limit=limit, category=category)
    return {
        **result,
        "query": q or "",
        "keywords": search_terms,
        "category": category or "",
        "refresh_seconds": 1,
        "disclaimer": _DISCLAIMER,
    }


@router.get("/headlines")
async def get_headlines(
    q: Optional[str] = Query(default=None, max_length=120),
    keywords: Optional[List[str]] = Query(default=None),
    limit: int = Query(default=30, ge=1, le=50),
    category: Optional[str] = Query(default=None),
    current_user: User = Depends(get_current_user),
):
    """
    Live news articles with inline summaries, filtered by optional keywords
    and category (markets/world/crypto). No external links needed.
    """
    _validate_category(category)
    search_terms = _build_search_terms(q, keywords)
    result = await claude_svc.summarize_financial_news(keywords=search_terms, limit=limit, category=category)
    return {
        **result,
        "query": q or "",
        "keywords": search_terms,
        "category": category or "",
        "refresh_seconds": 1,
        "disclaimer": _DISCLAIMER,
    }


@router.get("/summary")
async def get_summary_compat(
    q: Optional[str] = Query(default=None, max_length=120),
    keywords: Optional[List[str]] = Query(default=None),
    limit: int = Query(default=30, ge=1, le=50),
    current_user: User = Depends(get_current_user),
):
    """Backward-compatible alias for /news/headlines. Deprecated — use /news/headlines."""
    search_terms = _build_search_terms(q, keywords)
    result = await claude_svc.summarize_financial_news(keywords=search_terms, limit=limit)
    return {
        **result,
        "query": q or "",
        "keywords": search_terms,
        "refresh_seconds": 1,
        "disclaimer": _DISCLAIMER,
    }


@router.get("/digest")
@limiter.limit("10/minute")
async def get_ai_digest(
    request: Request,
    q: Optional[str] = Query(default=None, max_length=120),
    category: Optional[str] = Query(default=None),
    current_user: User = Depends(get_current_user),
):
    """Plain-English AI digest of what current headlines mean for a college
    student's money. Digests are generated from public headlines only (no
    user data goes to the model) and cached server-side, so polling is cheap.
    Returns available=false when no Anthropic key is configured — the UI
    should simply hide the digest panel.
    """
    _validate_category(category)
    search_terms = _build_search_terms(q, None)
    articles = await claude_svc.fetch_headlines(keywords=search_terms, limit=12, category=category)
    digest = await claude_svc.generate_news_digest(articles, focus=q or None)
    return {
        "available": digest is not None,
        "digest": digest,
        "query": q or "",
        "category": category or "",
        "article_count": len(articles),
        "disclaimer": _DISCLAIMER,
    }


# --- AI alerts: saved news-watch queries ------------------------------------

class AlertCreate(BaseModel):
    query: str
    category: Optional[str] = None

    @field_validator("query")
    @classmethod
    def _clean_query(cls, v: str) -> str:
        v = " ".join(v.split())[:60]
        if len(v) < 2:
            raise ValueError("Alert query must be at least 2 characters.")
        return v

    @field_validator("category")
    @classmethod
    def _check_category(cls, v: Optional[str]) -> Optional[str]:
        if v and v not in VALID_CATEGORIES:
            raise ValueError(f"category must be one of: {', '.join(VALID_CATEGORIES)}")
        return v


def _alert_out(a: NewsAlert) -> dict:
    return {
        "id": a.id,
        "query": a.query,
        "category": a.category or "",
        "created_at": a.created_at.isoformat() if a.created_at else "",
        "last_checked_at": a.last_checked_at.isoformat() if a.last_checked_at else "",
    }


@router.post("/alerts", status_code=201)
@limiter.limit("20/minute")
def create_alert(request: Request, req: AlertCreate, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    count = db.query(NewsAlert).filter(NewsAlert.user_id == current_user.id).count()
    if count >= MAX_ALERTS_PER_USER:
        raise HTTPException(status_code=400, detail=f"You can save up to {MAX_ALERTS_PER_USER} alerts. Delete one first.")
    dupe = db.query(NewsAlert).filter(
        NewsAlert.user_id == current_user.id,
        NewsAlert.query == req.query,
        NewsAlert.category == req.category,
    ).first()
    if dupe:
        return _alert_out(dupe)
    alert = NewsAlert(user_id=current_user.id, query=req.query, category=req.category)
    db.add(alert)
    db.commit()
    db.refresh(alert)
    return _alert_out(alert)


@router.get("/alerts")
def list_alerts(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    alerts = db.query(NewsAlert).filter(NewsAlert.user_id == current_user.id).order_by(NewsAlert.created_at.asc()).all()
    return {"alerts": [_alert_out(a) for a in alerts]}


@router.delete("/alerts/{alert_id}", status_code=204)
def delete_alert(alert_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    alert = db.query(NewsAlert).filter(NewsAlert.id == alert_id, NewsAlert.user_id == current_user.id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found.")
    db.delete(alert)
    db.commit()


@router.get("/alerts/check")
@limiter.limit("15/minute")
async def check_alerts(request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Run every saved alert against current feeds and return matches per
    alert. Matching is live (feeds change constantly), so this is a read —
    the only write is stamping last_checked_at.
    """
    alerts = db.query(NewsAlert).filter(NewsAlert.user_id == current_user.id).order_by(NewsAlert.created_at.asc()).all()
    results = []
    now = datetime.now(timezone.utc)
    for alert in alerts:
        terms = _build_search_terms(alert.query, None)
        articles = await claude_svc.fetch_headlines(keywords=terms, limit=5, category=alert.category or None)
        results.append({
            **_alert_out(alert),
            "matches": articles,
            "match_count": len(articles),
        })
        alert.last_checked_at = now
    db.commit()
    return {"alerts": results, "checked_at": now.isoformat(), "disclaimer": _DISCLAIMER}


def _build_search_terms(q: Optional[str], keywords: Optional[List[str]]) -> list[str]:
    terms: list[str] = []
    if q:
        terms.extend(part.strip() for part in q.replace(",", " ").split())
    if keywords:
        terms.extend(k.strip() for k in keywords)
    deduped: list[str] = []
    seen = set()
    for term in terms:
        clean = " ".join(term.split())[:60]
        key = clean.lower()
        if clean and key not in seen:
            deduped.append(clean)
            seen.add(key)
    return deduped[:10]
