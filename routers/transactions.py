from fastapi import APIRouter, Depends
from models import User
from dependencies import get_current_user

router = APIRouter(prefix="/transactions", tags=["transactions"])


@router.get("/summary")
async def spending_summary(current_user: User = Depends(get_current_user)):
    """Return spending totals grouped by category.

    FAWN is crypto-native and has no linked bank account transaction
    history to categorize spending from, so this returns an empty
    breakdown until on-chain transaction categorization is built.
    """
    return {"categories": [], "total_spent": 0.0}
