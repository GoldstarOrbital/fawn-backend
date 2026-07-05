"""Virtual debit card management.

Deliberately limited scope: create one virtual card, list your own cards
(masked — last4/status/expiration only), freeze, unfreeze. No full PAN/CVV
retrieval anywhere — that needs Stripe's PCI-scoped "sensitive details"
flow (Stripe.js element), which is out of scope until there's an actual
need for it.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from slowapi import Limiter
from slowapi.util import get_remote_address

from database import get_db
from models import User, Card
from schemas import CardOut, CardList, CardFreezeRequest
from dependencies import get_current_user
from services import stripe_baas as stripe_svc

router = APIRouter(prefix="/cards", tags=["cards"])
limiter = Limiter(key_func=get_remote_address)


def _to_out(summary: dict) -> CardOut:
    return CardOut(
        id=summary["id"],
        last4_digits=summary.get("last4Digits", ""),
        expiration_date=summary.get("expirationDate", ""),
        status=summary.get("status", ""),
        created_at=summary.get("createdAt", ""),
    )


def _owned_card_or_404(db: Session, user_id: str, card_id: str) -> Card:
    card = db.query(Card).filter(Card.stripe_card_id == card_id, Card.user_id == user_id).first()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found.")
    return card


@router.post("", response_model=CardOut, status_code=201)
@limiter.limit("5/minute")
async def create_card(request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not current_user.stripe_financial_account_id:
        raise HTTPException(status_code=400, detail="You need an active FAWN bank account before you can get a card.")

    existing = db.query(Card).filter(Card.user_id == current_user.id).first()
    if existing:
        raise HTTPException(status_code=409, detail="You already have a card. Multiple cards aren't supported yet.")

    idempotency_key = f"card-create:{current_user.id}"
    try:
        cardholder = await stripe_svc.create_issuing_cardholder(
            account_id=current_user.stripe_account_id,
            full_name=current_user.full_name,
            email=current_user.email,
            phone=current_user.phone or "",
        )
        card_data = await stripe_svc.create_virtual_card(
            account_id=current_user.stripe_account_id,
            cardholder_id=cardholder["id"],
            financial_account_id=current_user.stripe_financial_account_id,
            idempotency_key=idempotency_key,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Couldn't create your card: {e}")

    db.add(Card(user_id=current_user.id, stripe_card_id=card_data["id"]))
    db.commit()

    exp_month = card_data.get("exp_month")
    return CardOut(
        id=card_data["id"],
        last4_digits=card_data.get("last4", ""),
        expiration_date=f"{exp_month:0>2}/{card_data.get('exp_year', '')}" if exp_month else "",
        status=card_data.get("status", ""),
        created_at=str(card_data.get("created", "")),
    )


@router.get("", response_model=CardList)
async def list_my_cards(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    mine = db.query(Card).filter(Card.user_id == current_user.id).all()
    cards = []
    for c in mine:
        try:
            summary = await stripe_svc.get_card(current_user.stripe_account_id, c.stripe_card_id)
            cards.append(_to_out(summary))
        except Exception as e:
            print(f"[cards] failed to fetch card {c.stripe_card_id} for user {current_user.id}: {e}")
            continue  # Stripe hiccup on one card shouldn't 500 the whole list
    return CardList(cards=cards)


@router.post("/{card_id}/freeze", response_model=CardOut)
@limiter.limit("20/minute")
async def freeze(request: Request, card_id: str, req: CardFreezeRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _owned_card_or_404(db, current_user.id, card_id)
    try:
        summary = await stripe_svc.freeze_card(current_user.stripe_account_id, card_id, req.reason or "userRequested")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Couldn't freeze your card: {e}")
    return _to_out(summary)


@router.post("/{card_id}/unfreeze", response_model=CardOut)
@limiter.limit("20/minute")
async def unfreeze(request: Request, card_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _owned_card_or_404(db, current_user.id, card_id)
    try:
        summary = await stripe_svc.unfreeze_card(current_user.stripe_account_id, card_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Couldn't unfreeze your card: {e}")
    return _to_out(summary)
