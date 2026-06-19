from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr
from typing import Optional

from database import get_db
from models import WaitlistEntry

router = APIRouter(prefix="/waitlist", tags=["waitlist"])


class WaitlistJoin(BaseModel):
    email: EmailStr
    name: Optional[str] = None
    source: Optional[str] = "landing"


@router.post("/join", status_code=201)
def join_waitlist(req: WaitlistJoin, db: Session = Depends(get_db)):
    existing = db.query(WaitlistEntry).filter(WaitlistEntry.email == req.email).first()
    if existing:
        return {"message": "You're already on the list!", "position": _position(db, existing)}

    entry = WaitlistEntry(email=req.email, name=req.name, source=req.source)
    db.add(entry)
    db.commit()
    db.refresh(entry)
    count = db.query(WaitlistEntry).count()
    return {"message": "You're on the list!", "position": count}


@router.get("/count")
def waitlist_count(db: Session = Depends(get_db)):
    return {"count": db.query(WaitlistEntry).count()}


def _position(db: Session, entry: WaitlistEntry) -> int:
    return db.query(WaitlistEntry).filter(WaitlistEntry.created_at <= entry.created_at).count()
