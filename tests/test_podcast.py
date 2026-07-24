"""Tests for the FAWN Daily Brief podcast — generation idempotency,
public endpoints, admin gating, and transcript-only degradation.
All Anthropic/TTS/RSS calls are mocked; no test touches the network.
"""
import uuid

import pytest

from database import SessionLocal
from models import PodcastEpisode
from config import settings
from services import podcast as podcast_svc


FAKE_SCRIPT = ("Good morning, this is the FAWN Daily Brief. " + "Markets held steady today. " * 40).strip()


def _mock_pipeline(monkeypatch, script=FAKE_SCRIPT, audio=b"ID3fakemp3bytes"):
    async def fake_gather():
        return ([{"title": "Fed news", "summary": "s", "source": "T", "pub_date": ""}],
                [{"title": "World news", "summary": "s", "source": "T", "pub_date": ""}])

    async def fake_script(financial, world):
        return script

    async def fake_tts(text):
        return audio

    monkeypatch.setattr(podcast_svc, "_gather_headlines", fake_gather)
    monkeypatch.setattr(podcast_svc, "generate_script", fake_script)
    monkeypatch.setattr(podcast_svc, "synthesize_audio", fake_tts)


def _clear_episodes():
    db = SessionLocal()
    try:
        db.query(PodcastEpisode).delete()
        db.commit()
    finally:
        db.close()


def _admin_headers():
    return {"X-Admin-Key": settings.admin_api_key}


def test_generate_is_idempotent_per_day(client, monkeypatch):
    _clear_episodes()
    _mock_pipeline(monkeypatch)

    first = client.post("/podcast/internal/generate", headers=_admin_headers())
    assert first.status_code == 200, first.text
    second = client.post("/podcast/internal/generate", headers=_admin_headers())
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]

    db = SessionLocal()
    try:
        assert db.query(PodcastEpisode).count() == 1
    finally:
        db.close()


def test_generate_requires_admin_key(client):
    resp = client.post("/podcast/internal/generate")
    assert resp.status_code in (401, 403, 422)


def test_delivery_retry_requires_admin_and_uses_latest_episode(client, monkeypatch):
    _clear_episodes()
    db = SessionLocal()
    try:
        db.add(PodcastEpisode(
            episode_date="2026-07-24",
            title="FAWN Daily Brief",
            script="A short transcript.",
            word_count=3,
            est_duration_seconds=1,
        ))
        db.commit()
    finally:
        db.close()

    async def fake_send(db, episode):
        assert episode.episode_date == "2026-07-24"
        return 7

    monkeypatch.setattr(podcast_svc, "send_episode_to_subscribers", fake_send)
    assert client.post("/podcast/internal/deliver").status_code in (401, 403, 422)
    response = client.post("/podcast/internal/deliver", headers=_admin_headers())
    assert response.status_code == 200, response.text
    assert response.json() == {"episode_date": "2026-07-24", "sent_count": 7}


def test_latest_and_audio_endpoints(client, monkeypatch):
    _clear_episodes()
    _mock_pipeline(monkeypatch)
    client.post("/podcast/internal/generate", headers=_admin_headers())

    latest = client.get("/podcast/latest")
    assert latest.status_code == 200
    body = latest.json()
    assert body["audio_available"] is True
    assert body["ai_generated"] is True
    assert "not financial advice" in body["disclaimer"].lower()
    assert body["script"].startswith("Good morning")
    assert body["est_duration_seconds"] > 0

    audio = client.get(body["audio_url"])
    assert audio.status_code == 200
    assert audio.headers["content-type"] == "audio/mpeg"
    assert audio.content == b"ID3fakemp3bytes"


def test_latest_404_when_no_episodes(client):
    _clear_episodes()
    resp = client.get("/podcast/latest")
    assert resp.status_code == 404


def test_tts_failure_degrades_to_transcript_only(client, monkeypatch):
    _clear_episodes()
    _mock_pipeline(monkeypatch, audio=None)
    gen = client.post("/podcast/internal/generate", headers=_admin_headers())
    assert gen.status_code == 200
    body = gen.json()
    assert body["audio_available"] is False
    assert body["audio_url"] is None

    # Transcript still served; audio route 404s rather than returning junk.
    latest = client.get("/podcast/latest")
    assert latest.status_code == 200
    assert latest.json()["script"]
    audio = client.get(f"/podcast/episodes/{body['episode_date']}.mp3")
    assert audio.status_code == 404


@pytest.mark.asyncio
async def test_tts_timeout_does_not_block_transcript_publication(monkeypatch):
    _clear_episodes()
    async def slow_tts(_script):
        import asyncio
        await asyncio.sleep(1)
        return b"late-audio"

    _mock_pipeline(monkeypatch, audio=None)
    monkeypatch.setattr(podcast_svc, "synthesize_audio", slow_tts)
    monkeypatch.setattr(podcast_svc, "TTS_TIMEOUT_SECONDS", 0.01)
    db = SessionLocal()
    try:
        episode = await podcast_svc.generate_episode(db)
        assert episode is not None
        assert episode.script
        assert episode.audio_mp3 is None
    finally:
        db.close()


def test_no_script_means_no_episode(client, monkeypatch):
    _clear_episodes()
    _mock_pipeline(monkeypatch, script=None)

    async def fake_script_none(financial, world):
        return None
    monkeypatch.setattr(podcast_svc, "generate_script", fake_script_none)

    resp = client.post("/podcast/internal/generate", headers=_admin_headers())
    assert resp.status_code == 503
    db = SessionLocal()
    try:
        assert db.query(PodcastEpisode).count() == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_source_grounded_script_publishes_without_anthropic(monkeypatch):
    monkeypatch.setattr(podcast_svc.claude_svc, "_anthropic_configured", lambda: False)
    script = await podcast_svc.generate_script(
        [{"title": "Jobs report moves markets", "summary": "Investors watched new employment data.", "source": "Reuters"}],
        [{"title": "Congress considers a bill", "summary": "Lawmakers discussed the proposal.", "source": "AP"}],
    )
    assert script is not None
    assert "Jobs report moves markets" in script
    assert "Congress considers a bill" in script
    assert "automatically compiled by FAWN" in script


def test_scheduler_math_targets_330_pacific():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/Los_Angeles")
    before = datetime(2026, 7, 2, 2, 0, tzinfo=tz)   # 2:00 AM -> 90 min away
    assert abs(podcast_svc.seconds_until_next_release(before) - 90 * 60) < 1
    after = datetime(2026, 7, 2, 4, 0, tzinfo=tz)    # 4:00 AM -> tomorrow 3:30
    assert abs(podcast_svc.seconds_until_next_release(after) - 23.5 * 3600) < 1
