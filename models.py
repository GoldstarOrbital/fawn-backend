from sqlalchemy import Column, String, DateTime, Boolean, Numeric, Integer
from sqlalchemy.sql import func
from database import Base
import uuid

def new_id():
    return str(uuid.uuid4())

class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=new_id)
    email = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    full_name = Column(String, nullable=False)
    phone = Column(String, nullable=True)
    is_student = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Unit identifiers — set after BaaS account creation
    unit_customer_id = Column(String, nullable=True)
    unit_account_id = Column(String, nullable=True)

    # Referral
    referral_code = Column(String, unique=True, nullable=True, index=True)
    referred_by = Column(String, nullable=True)   # referral_code of inviter
    referral_count = Column(Integer, default=0, nullable=False)


class WaitlistEntry(Base):
    __tablename__ = "waitlist"

    id = Column(String, primary_key=True, default=new_id)
    email = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, nullable=True)
    source = Column(String, nullable=True, default="landing")   # where they signed up
    created_at = Column(DateTime(timezone=True), server_default=func.now())
