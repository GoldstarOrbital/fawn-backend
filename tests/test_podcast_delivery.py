"""Durable Daily Brief delivery tests."""
import uuid

import pytest

from database import SessionLocal
from models import PodcastDelivery, PodcastEpisode, User
from services import podcast


class _Response:
    status_code = 201


class _EmailClient:
    sends = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        self.__class__.sends += 1
        return _Response()


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
