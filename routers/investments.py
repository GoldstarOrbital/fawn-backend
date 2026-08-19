"""Minimal investments router for testing."""
from fastapi import APIRouter

router = APIRouter(prefix="/investments", tags=["investments"])

@router.get("/securities")
def list_securities():
    """Minimal test endpoint."""
    return {"securities": [], "count": 0, "message": "Minimal router loaded"}

@router.get("/public/quote/{ticker}")
def get_quote(ticker: str):
    """Minimal public quote endpoint."""
    return {"ticker": ticker, "price_dollars": 0}
