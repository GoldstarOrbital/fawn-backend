from fastapi import APIRouter, Depends, Query
from typing import Optional, List
from dependencies import get_current_user
from models import User
from services import claude as claude_svc

router = APIRouter(prefix="/news", tags=["news"])


@router.get("/headlines")
async def get_headlines(
    keywords: Optional[List[str]] = Query(default=None),
    current_user: User = Depends(get_current_user),
):
    """
    Live financial news articles with inline summaries.
    Filtered by optional keywords. No external links needed — summaries are included.
    """
    result = await claude_svc.summarize_financial_news(keywords=keywords or [])
    return {
        **result,
        "disclaimer": "Financial news for informational purposes only. Not investment advice.",
    }


@router.get("/summary")
async def get_summary_compat(
    keywords: Optional[List[str]] = Query(default=None),
    current_user: User = Depends(get_current_user),
):
    """Backward-compatible alias for /news/headlines. Deprecated — use /news/headlines."""
    result = await claude_svc.summarize_financial_news(keywords=keywords or [])
    return {
        **result,
        "disclaimer": "Financial news for informational purposes only. Not investment advice.",
    }
