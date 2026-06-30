from fastapi import APIRouter, Depends, Query
from typing import Optional, List
from dependencies import get_current_user
from models import User
from services import claude as claude_svc

router = APIRouter(prefix="/news", tags=["news"])


@router.get("/public-headlines")
async def get_public_headlines(
    q: Optional[str] = Query(default=None, max_length=120),
    keywords: Optional[List[str]] = Query(default=None),
    limit: int = Query(default=12, ge=1, le=20),
):
    """
    Public landing-page preview for live financial news.
    Does not expose user data or require an account.
    """
    search_terms = _build_search_terms(q, keywords)
    result = await claude_svc.summarize_financial_news(keywords=search_terms, limit=limit)
    return {
        **result,
        "query": q or "",
        "keywords": search_terms,
        "refresh_seconds": 1,
        "disclaimer": "Financial news for informational purposes only. Not investment advice.",
    }


@router.get("/headlines")
async def get_headlines(
    q: Optional[str] = Query(default=None, max_length=120),
    keywords: Optional[List[str]] = Query(default=None),
    limit: int = Query(default=30, ge=1, le=50),
    current_user: User = Depends(get_current_user),
):
    """
    Live financial news articles with inline summaries.
    Filtered by optional keywords. No external links needed — summaries are included.
    """
    search_terms = _build_search_terms(q, keywords)
    result = await claude_svc.summarize_financial_news(keywords=search_terms, limit=limit)
    return {
        **result,
        "query": q or "",
        "keywords": search_terms,
        "refresh_seconds": 1,
        "disclaimer": "Financial news for informational purposes only. Not investment advice.",
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
        "disclaimer": "Financial news for informational purposes only. Not investment advice.",
    }


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
