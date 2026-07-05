"""AI Money Review: the '10 money prompts in one button' feature.

Twitter threads sell 10 separate ChatGPT prompts (budget, expense audit,
subscription cull, bill negotiation, savings plan, debt payoff, food
budget, big purchases, side income, monthly review) that all start with
'[paste your data]'. FAWN already HAS the user's real transaction data,
so this endpoint runs the entire review in one authenticated call — no
copy-pasting bank statements into a chatbot.

Strictly budgeting/spending analysis. The model is explicitly forbidden
from giving investment advice or inventing numbers, and the response
carries a not-financial-advice disclaimer.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from database import get_db
from dependencies import get_current_user
from models import User
from services import claude as claude_svc
from services import stripe_baas as stripe_svc
from services.categorize import categorize

router = APIRouter(prefix="/ai", tags=["ai"])
limiter = Limiter(key_func=get_remote_address)

DISCLAIMER = (
    "AI-generated budgeting analysis based on your transaction data. "
    "Educational only — not financial, investment, tax, or legal advice."
)


class MoneyReviewRequest(BaseModel):
    monthly_income_dollars: Optional[float] = Field(default=None, ge=0, le=1_000_000)
    goals: Optional[str] = Field(default=None, max_length=500)
    # For users without an active account yet (pre-KYC), the classic
    # paste-your-data mode still works.
    pasted_data: Optional[str] = Field(default=None, max_length=4000)


@router.post("/money-review")
@limiter.limit("5/hour")
async def run_money_review(
    request: Request,
    req: MoneyReviewRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    transactions: list[dict] = []
    category_totals: dict[str, float] = {}

    if current_user.stripe_financial_account_id:
        try:
            transactions = await stripe_svc.list_transactions(current_user.stripe_account_id, current_user.stripe_financial_account_id, limit=100)
        except Exception as e:
            print(f"[money-review] transaction fetch failed for user {current_user.id}: {e}")
        for t in transactions:
            amount = t.get("amount", 0)
            if amount >= 0:
                continue  # spending only; income is stated separately
            cat, _emoji = categorize(t.get("description", ""))
            category_totals[cat] = round(category_totals.get(cat, 0) + abs(amount), 2)

    if not transactions and not (req.pasted_data and req.pasted_data.strip()):
        raise HTTPException(
            status_code=400,
            detail=(
                "No transaction data to review yet. Once your FAWN account has activity "
                "this runs automatically — or paste your income/spending from another bank."
            ),
        )

    review = await claude_svc.generate_money_review(
        category_totals=category_totals,
        transactions_sample=transactions,
        monthly_income_dollars=req.monthly_income_dollars,
        goals=req.goals,
        pasted_data=req.pasted_data,
    )
    if not review:
        raise HTTPException(status_code=503, detail="The review assistant is unavailable right now. Try again soon.")

    return {
        "review": review,
        "category_totals": category_totals,
        "transaction_count": len(transactions),
        "used_pasted_data": bool(req.pasted_data and req.pasted_data.strip()),
        "ai_generated": True,
        "disclaimer": DISCLAIMER,
    }
