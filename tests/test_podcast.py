"""Tests for the FAWN Daily Brief podcast — generation idempotency,
public endpoints, admin gating, and transcript-only degradation.
All Anthropic/TTS/RSS calls are mocked; no test touches the network.
"""
import uuid

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


def test_scheduler_math_targets_330_pacific():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/Los_Angeles")
    before = datetime(2026, 7, 2, 2, 0, tzinfo=tz)   # 2:00 AM -> 90 min away
    assert abs(podcast_svc.seconds_until_next_release(before) - 90 * 60) < 1
    after = datetime(2026, 7, 2, 4, 0, tzinfo=tz)    # 4:00 AM -> tomorrow 3:30
    assert abs(podcast_svc.seconds_until_next_release(after) - 23.5 * 3600) < 1
