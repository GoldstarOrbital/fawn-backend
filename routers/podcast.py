"""Public endpoints for the FAWN Daily Brief podcast, plus the admin
generation trigger. Episodes are public — they contain only news content,
never user data."""
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from database import get_db
from models import PodcastEpisode
from routers.admin import require_admin_key
from services import podcast as podcast_svc

router = APIRouter(prefix="/podcast", tags=["podcast"])


def _episode_meta(e: PodcastEpisode) -> dict:
    return {
        "id": e.id,
        "episode_date": e.episode_date,
        "title": e.title,
        "word_count": e.word_count,
        "est_duration_seconds": e.est_duration_seconds,
        "audio_available": e.audio_mp3 is not None,
        "audio_url": f"/podcast/episodes/{e.episode_date}.mp3" if e.audio_mp3 is not None else None,
        "created_at": e.created_at.isoformat() if e.created_at else "",
        "ai_generated": True,
        "disclaimer": "AI-generated news briefing compiled from public news wires. Not financial advice.",
    }


@router.get("/latest")
def get_latest(db: Session = Depends(get_db)):
    episode = db.query(PodcastEpisode).order_by(PodcastEpisode.episode_date.desc()).first()
    if not episode:
        raise HTTPException(status_code=404, detail="No episodes yet.")
    return {**_episode_meta(episode), "script": episode.script}


@router.get("/episodes")
def list_episodes(db: Session = Depends(get_db)):
    episodes = db.query(PodcastEpisode).order_by(PodcastEpisode.episode_date.desc()).limit(14).all()
    return {"episodes": [_episode_meta(e) for e in episodes]}


@router.get("/episodes/{episode_date}.mp3")
def get_episode_audio(episode_date: str, db: Session = Depends(get_db)):
    episode = db.query(PodcastEpisode).filter(PodcastEpisode.episode_date == episode_date).first()
    if not episode or episode.audio_mp3 is None:
        raise HTTPException(status_code=404, detail="Episode audio not found.")
    return Response(
        content=episode.audio_mp3,
        media_type="audio/mpeg",
        headers={
            "Content-Disposition": f'inline; filename="fawn-daily-brief-{episode_date}.mp3"',
            "Cache-Control": "public, max-age=3600",
        },
    )


@router.get("/episodes/{episode_date}")
def get_episode(episode_date: str, db: Session = Depends(get_db)):
    episode = db.query(PodcastEpisode).filter(PodcastEpisode.episode_date == episode_date).first()
    if not episode:
        raise HTTPException(status_code=404, detail="Episode not found.")
    return {**_episode_meta(episode), "script": episode.script}


@router.post("/internal/generate", dependencies=[Depends(require_admin_key)])
async def generate_now(force: bool = False, db: Session = Depends(get_db)):
    """Manual/cron trigger. Idempotent per Pacific date; force=true
    regenerates today's episode in place."""
    episode = await podcast_svc.generate_episode(db, force=force)
    if not episode:
        raise HTTPException(
            status_code=503,
            detail="Episode generation unavailable (missing Anthropic key or no headlines).",
        )
    return _episode_meta(episode)
