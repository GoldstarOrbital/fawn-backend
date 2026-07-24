"""Durable Daily Brief delivery tests."""
import uuid

import pytest

from database import SessionLocal
from models import PodcastDelivery, PodcastEpisode, User, WaitlistEntry
from services import podcast
from email_templates import build_daily_brief


class _Response:
    status_code = 201


class _EmailClient:
    sends = 0
    recipients = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        self.__class__.sends += 1
        self.__class__.recipients.append(kwargs["json"]["to"][0])
        return _Response()


def test_daily_brief_email_links_to_its_landing_episode():
    _subject, html = build_daily_brief("2026-07-24", "FAWN Daily Brief", 300)
    assert "https://goldstarorbital.github.io/fawn-landing/podcast/?episode=2026-07-24" in html
    assert "app.goldstarorbital.com" not in html


@pytest.mark.asyncio
async def test_daily_brief_delivery_is_idempotent_per_user(monkeypatch):
    db = SessionLocal()
    try:
        suffix = uuid.uuid4().hex[:10]
        user = User(
            email=f"brief_{suffix}@example.com", hashed_password="x", full_name="Brief Tester",
            wallet_initialized=True,
        )
        episode = PodcastEpisode(
            episode_date=f"2099-01-{suffix[:2]}", title="Test Brief", script="A short test brief.",
            word_count=4, est_duration_seconds=2, source_headline_count=1,
        )
        db.add_all([user, episode])
        db.commit()
        _EmailClient.sends = 0
        _EmailClient.recipients = []
        monkeypatch.setattr(podcast.settings, "resend_api_key", "test-key")
        monkeypatch.setattr(podcast.httpx, "AsyncClient", lambda **kwargs: _EmailClient())

        first_pass = await podcast.send_episode_to_subscribers(db, episode)
        assert first_pass >= 1
        assert await podcast.send_episode_to_subscribers(db, episode) == 0
        assert _EmailClient.sends == first_pass
        delivery = db.query(PodcastDelivery).filter(PodcastDelivery.episode_id == episode.id, PodcastDelivery.user_id == user.id).one()
        assert delivery.status == "sent"
        assert delivery.attempts == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_daily_brief_reaches_all_accounts_and_waitlist_signups_once(monkeypatch):
    db = SessionLocal()
    try:
        suffix = uuid.uuid4().hex[:10]
        shared_email = f"shared_{suffix}@example.com"
        account_only = f"account_{suffix}@example.com"
        signup_only = f"signup_{suffix}@example.com"
        db.add_all([
            User(email=shared_email, hashed_password="x", full_name="Shared Account"),
            User(email=account_only, hashed_password="x", full_name="Account Only"),
            WaitlistEntry(email=shared_email, name="Shared Signup"),
            WaitlistEntry(email=signup_only, name="Signup Only"),
        ])
        episode = PodcastEpisode(
            episode_date=f"2098-02-{suffix[:2]}", title="All Recipients", script="Brief.",
            word_count=1, est_duration_seconds=1, source_headline_count=1,
        )
        db.add(episode)
        db.commit()

        _EmailClient.sends = 0
        _EmailClient.recipients = []
        monkeypatch.setattr(podcast.settings, "resend_api_key", "test-key")
        monkeypatch.setattr(podcast.httpx, "AsyncClient", lambda **kwargs: _EmailClient())

        first_pass = await podcast.send_episode_to_subscribers(db, episode)
        assert first_pass >= 3
        assert {shared_email, account_only, signup_only}.issubset(set(_EmailClient.recipients))
        assert await podcast.send_episode_to_subscribers(db, episode) == 0
        assert db.query(PodcastDelivery).filter(PodcastDelivery.episode_id == episode.id).count() >= 3
    finally:
        db.close()
