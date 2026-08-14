import uuid

from models import BuckCredit, BuckFundingPayment, BuckLedgerEntry, User
from routers.bucks import handle_buck_event


def _event(user_id, session_id=None, payment_intent=None, count=3):
    session_id = session_id or f"cs_{uuid.uuid4().hex}"
    return {"id": f"evt_{uuid.uuid4().hex}", "type": "checkout.session.completed", "data": {"object": {
        "id": session_id, "payment_intent": payment_intent or f"pi_{uuid.uuid4().hex}", "payment_status": "paid",
        "metadata": {"fawn_product": "bucks", "user_id": user_id, "amount_cents": str(count * 100),
                      "fee_cents": str(count), "buck_count": str(count)},
    }}}


def test_bucks_issue_once_with_serials_and_ledger(db):
    user = User(email=f"bucks-{uuid.uuid4().hex}@example.com", hashed_password="x", full_name="Bucks Tester")
    db.add(user)
    db.commit()
    event = _event(user.id)
    first = handle_buck_event(event, db)
    second = handle_buck_event(event, db)
    assert first["bucks_issued"] == 3
    assert second["duplicate"] is True
    assert db.query(BuckCredit).filter(BuckCredit.user_id == user.id).count() == 3
    assert db.query(BuckLedgerEntry).filter(BuckLedgerEntry.user_id == user.id).one().bucks_delta == 3


def test_bucks_refund_reverses_active_serials(db):
    user = User(email=f"refund-{uuid.uuid4().hex}@example.com", hashed_password="x", full_name="Refund Tester")
    db.add(user)
    db.commit()
    event = _event(user.id, payment_intent="pi_refund")
    handle_buck_event(event, db)
    refund = {"type": "charge.refunded", "data": {"object": {"payment_intent": "pi_refund", "amount_refunded": 303}}}
    result = handle_buck_event(refund, db)
    assert result["reversed"] == 3
    assert db.query(BuckCredit).filter(BuckCredit.user_id == user.id, BuckCredit.status == "active").count() == 0
    assert db.query(BuckLedgerEntry).filter(BuckLedgerEntry.user_id == user.id).count() == 2
