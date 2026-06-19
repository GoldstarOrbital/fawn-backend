from fastapi import APIRouter, Depends, HTTPException, Query
from models import User
from schemas import TransactionList
from dependencies import get_current_user
from services import unit as unit_svc
from services.categorize import categorize

router = APIRouter(prefix="/transactions", tags=["transactions"])


@router.get("/", response_model=TransactionList)
async def list_transactions(
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
):
    if not current_user.unit_account_id:
        raise HTTPException(status_code=404, detail="No bank account linked yet.")

    txns = await unit_svc.list_transactions(current_user.unit_account_id, limit=limit)

    # Attach category + emoji to each transaction
    for t in txns:
        cat, emoji = categorize(t.get("description", ""))
        t["category"] = cat
        t["emoji"] = emoji

    return TransactionList(transactions=txns)


@router.get("/summary")
async def spending_summary(current_user: User = Depends(get_current_user)):
    """Return spending totals grouped by category for the last 100 transactions."""
    if not current_user.unit_account_id:
        raise HTTPException(status_code=404, detail="No bank account linked yet.")

    txns = await unit_svc.list_transactions(current_user.unit_account_id, limit=100)
    totals: dict[str, dict] = {}

    for t in txns:
        amount = t.get("amount", 0)
        if amount >= 0:
            continue  # skip income/credits for spending breakdown
        cat, emoji = categorize(t.get("description", ""))
        if cat not in totals:
            totals[cat] = {"category": cat, "emoji": emoji, "total": 0.0, "count": 0}
        totals[cat]["total"] = round(totals[cat]["total"] + abs(amount), 2)
        totals[cat]["count"] += 1

    sorted_cats = sorted(totals.values(), key=lambda x: x["total"], reverse=True)
    grand_total = sum(c["total"] for c in sorted_cats)

    for c in sorted_cats:
        c["pct"] = round((c["total"] / grand_total * 100) if grand_total else 0, 1)

    return {"categories": sorted_cats, "total_spent": round(grand_total, 2)}
