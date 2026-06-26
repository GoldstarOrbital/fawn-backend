"""Virtual debit card management.

Deliberately limited scope: create one virtual card, list your own cards
(masked — last4/status/expiration only), freeze, unfreeze. No full PAN/CVV
retrieval anywhere — that needs Unit's PCI-scoped "sensitive details" flow,
which is out of scope until there's an actual need for it.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from slowapi import Limiter
from slowapi.util import get_remote_address

from database import get_db
from models import User, Card
from schemas import CardOut, CardList, CardFreezeRequest
from dependencies import get_current_user
from services import unit as unit_svc

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
    card = db.query(Card).filter(Card.unit_card_id == card_id, Card.user_id == user_id).first()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found.")
    return card


@router.post("", response_model=CardOut, status_code=201)
@limiter.limit("5/minute")
async def create_card(request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not current_user.unit_account_id:
        raise HTTPException(status_code=400, detail="You need an active FAWN bank account before you can get a card.")

    existing = db.query(Card).filter(Card.user_id == current_user.id).first()
    if existing:
        raise HTTPException(status_code=409, detail="You already have a card. Multiple cards aren't supported yet.")

    idempotency_key = f"card-create:{current_user.id}"
    try:
        card_data = await unit_svc.create_virtual_card(current_user.unit_account_id, idempotency_key)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Couldn't create your card: {e}")

    attrs = card_data.get("attributes", {})
    db.add(Card(user_id=current_user.id, unit_card_id=card_data["id"]))
    db.commit()

    return CardOut(
        id=card_data["id"],
        last4_digits=attrs.get("last4Digits", ""),
        expiration_date=attrs.get("expirationDate", ""),
        status=attrs.get("status", ""),
        created_at=attrs.get("createdAt", ""),
    )


@router.get("", response_model=CardList)
async def list_my_cards(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    mine = db.query(Card).filter(Card.user_id == current_user.id).all()
    cards = []
    for c in mine:
        try:
            summary = await unit_svc.get_card(c.unit_card_id)
            cards.append(_to_out(summary))
        except Exception:
            continue  # Unit hiccup on one card shouldn't 500 the whole list
    return CardList(cards=cards)


@router.post("/{card_id}/freeze", response_model=CardOut)
@limiter.limit("20/minute")
async def freeze(request: Request, card_id: str, req: CardFreezeRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _owned_card_or_404(db, current_user.id, card_id)
    try:
        summary = await unit_svc.freeze_card(card_id, req.reason or "userRequested")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Couldn't freeze your card: {e}")
    return _to_out(summary)


@router.post("/{card_id}/unfreeze", response_model=CardOut)
@limiter.limit("20/minute")
async def unfreeze(request: Request, card_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _owned_card_or_404(db, current_user.id, card_id)
    try:
        summary = await unit_svc.unfreeze_card(card_id)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Couldn't unfreeze your card: {e}")
    return _to_out(summary)
