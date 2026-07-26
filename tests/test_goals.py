"""Tests for the Savings Goals feature (services/goals.py).

These test the SERVICE functions directly (the router is not registered in
main.py yet), following the style of tests/test_referral.py: create User rows
via SessionLocal, call the pure service functions, and assert on the returned
dicts.

The overriding invariant under test: this feature is TRACKING ONLY. No call
here may ever change User.usdc_balance_cents or create a CryptoTransfer.
"""
import json
import uuid

import pytest

from database import SessionLocal
from models import User, UserAuditLog, CryptoTransfer
from services import goals as goals_service


def _make_user(balance_cents: int = 25_000) -> str:
    db = SessionLocal()
    try:
        user = User(
            email=f"goals_{uuid.uuid4().hex[:10]}@example.com",
            hashed_password="x",
            full_name="Goals Tester",
            usdc_balance_cents=balance_cents,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user.id
    finally:
        db.close()


def _balance_cents(user_id: str) -> int:
    db = SessionLocal()
    try:
        return db.query(User).filter(User.id == user_id).first().usdc_balance_cents or 0
    finally:
        db.close()


def _transfer_count(user_id: str) -> int:
    db = SessionLocal()
    try:
        return db.query(CryptoTransfer).filter(CryptoTransfer.sender_id == user_id).count()
    finally:
        db.close()


# ── create + list ──

def test_create_goal_returns_stable_uuid_and_zero_progress():
    user_id = _make_user()
    db = SessionLocal()
    try:
        goal = goals_service.create_goal(db, user_id, "Emergency Fund", 100_000)
    finally:
        db.close()

    assert goal["name"] == "Emergency Fund"
    assert goal["target_cents"] == 100_000
    assert goal["saved_cents"] == 0
    assert goal["progress_pct"] == 0.0
    assert goal["remaining_cents"] == 100_000
    assert goal["is_complete"] is False
    assert goal["status"] == "active"
    assert goal["tracking_only"] is True
    # id is a valid uuid
    uuid.UUID(goal["id"])


def test_list_goals_returns_created_goals():
    user_id = _make_user()
    db = SessionLocal()
    try:
        goals_service.create_goal(db, user_id, "Vacation", 50_000, deadline="2026-12-01T00:00:00+00:00")
        goals_service.create_goal(db, user_id, "New Laptop", 150_000)
        listing = goals_service.list_goals(db, user_id)
    finally:
        db.close()

    assert listing["count"] == 2
    assert listing["tracking_only"] is True
    names = {g["name"] for g in listing["goals"]}
    assert names == {"Vacation", "New Laptop"}


# ── contributions (tracking only) ──

def test_contributions_accumulate_and_update_progress():
    user_id = _make_user()
    db = SessionLocal()
    try:
        goal = goals_service.create_goal(db, user_id, "Spring Break", 40_000)
        gid = goal["id"]
        goals_service.log_contribution(db, user_id, gid, 10_000)
        result = goals_service.log_contribution(db, user_id, gid, 5_000)
    finally:
        db.close()

    assert result["saved_cents"] == 15_000
    assert result["progress_pct"] == 37.5  # 15000 / 40000
    assert result["remaining_cents"] == 25_000
    assert result["is_complete"] is False
    assert result["contributed_cents"] == 5_000
    assert result["tracking_only"] is True

    # And it survives a fresh read.
    db = SessionLocal()
    try:
        again = goals_service.get_goal(db, user_id, gid)
    finally:
        db.close()
    assert again["saved_cents"] == 15_000


def test_progress_caps_at_100_and_marks_complete():
    user_id = _make_user()
    db = SessionLocal()
    try:
        goal = goals_service.create_goal(db, user_id, "Small Goal", 10_000)
        result = goals_service.log_contribution(db, user_id, goal["id"], 25_000)
    finally:
        db.close()

    assert result["saved_cents"] == 25_000
    assert result["progress_pct"] == 100.0  # capped
    assert result["remaining_cents"] == 0
    assert result["is_complete"] is True


def test_contribution_is_audit_logged_as_tracking_only():
    user_id = _make_user()
    db = SessionLocal()
    try:
        goal = goals_service.create_goal(db, user_id, "Bike", 30_000)
        goals_service.log_contribution(db, user_id, goal["id"], 7_500)

        logs = db.query(UserAuditLog).filter(
            UserAuditLog.user_id == user_id,
            UserAuditLog.action == goals_service.CONTRIBUTION_ACTION,
        ).all()
        assert len(logs) == 1
        details = json.loads(logs[0].details)
        assert details["goal_id"] == goal["id"]
        assert details["amount_cents"] == 7_500
        assert details["tracking_only"] is True
    finally:
        db.close()


# ── the critical safety invariant: NO money movement ──

def test_contributions_never_move_real_funds():
    user_id = _make_user(balance_cents=25_000)
    balance_before = _balance_cents(user_id)
    transfers_before = _transfer_count(user_id)

    db = SessionLocal()
    try:
        goal = goals_service.create_goal(db, user_id, "Tracking Only", 100_000)
        goals_service.log_contribution(db, user_id, goal["id"], 20_000)
        goals_service.log_contribution(db, user_id, goal["id"], 50_000)
        goals_service.archive_goal(db, user_id, goal["id"])
    finally:
        db.close()

    # Real balance is completely untouched despite $700 of "contributions".
    assert _balance_cents(user_id) == balance_before == 25_000
    # No ledger transfers were created.
    assert _transfer_count(user_id) == transfers_before == 0


# ── archive / delete ──

def test_archive_removes_goal_from_default_list():
    user_id = _make_user()
    db = SessionLocal()
    try:
        goal = goals_service.create_goal(db, user_id, "Temp", 10_000)
        gid = goal["id"]
        goals_service.archive_goal(db, user_id, gid)

        active = goals_service.list_goals(db, user_id)
        with_archived = goals_service.list_goals(db, user_id, include_archived=True)
    finally:
        db.close()

    assert active["count"] == 0
    assert with_archived["count"] == 1
    assert with_archived["goals"][0]["status"] == "archived"


def test_archive_twice_raises_not_found():
    user_id = _make_user()
    db = SessionLocal()
    try:
        goal = goals_service.create_goal(db, user_id, "Once", 10_000)
        goals_service.archive_goal(db, user_id, goal["id"])
        with pytest.raises(goals_service.GoalNotFound):
            goals_service.archive_goal(db, user_id, goal["id"])
    finally:
        db.close()


# ── validation ──

def test_create_goal_rejects_nonpositive_target():
    user_id = _make_user()
    db = SessionLocal()
    try:
        with pytest.raises(goals_service.GoalError):
            goals_service.create_goal(db, user_id, "Bad", 0)
        with pytest.raises(goals_service.GoalError):
            goals_service.create_goal(db, user_id, "Bad", -500)
    finally:
        db.close()


def test_create_goal_rejects_short_name():
    user_id = _make_user()
    db = SessionLocal()
    try:
        with pytest.raises(goals_service.GoalError):
            goals_service.create_goal(db, user_id, "x", 10_000)
    finally:
        db.close()


def test_contribution_rejects_nonpositive_amount():
    user_id = _make_user()
    db = SessionLocal()
    try:
        goal = goals_service.create_goal(db, user_id, "Valid Goal", 10_000)
        with pytest.raises(goals_service.GoalError):
            goals_service.log_contribution(db, user_id, goal["id"], 0)
        with pytest.raises(goals_service.GoalError):
            goals_service.log_contribution(db, user_id, goal["id"], -100)
    finally:
        db.close()


def test_contribute_unknown_goal_raises_not_found():
    user_id = _make_user()
    db = SessionLocal()
    try:
        with pytest.raises(goals_service.GoalNotFound):
            goals_service.log_contribution(db, user_id, "no-such-goal", 1_000)
    finally:
        db.close()


# ── cross-user isolation ──

def test_goals_are_isolated_per_user():
    owner_id = _make_user()
    other_id = _make_user()
    db = SessionLocal()
    try:
        goal = goals_service.create_goal(db, owner_id, "Owner Goal", 10_000)
        gid = goal["id"]

        # The other user cannot see it...
        assert goals_service.list_goals(db, other_id)["count"] == 0
        # ...nor contribute to it (resolves only within owner's audit rows).
        with pytest.raises(goals_service.GoalNotFound):
            goals_service.log_contribution(db, other_id, gid, 1_000)
        # ...nor archive it.
        with pytest.raises(goals_service.GoalNotFound):
            goals_service.archive_goal(db, other_id, gid)

        # Owner still sees exactly their one goal.
        assert goals_service.list_goals(db, owner_id)["count"] == 1
    finally:
        db.close()
