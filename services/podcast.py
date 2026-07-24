"""FAWN Daily Brief: an AI-compiled, AI-spoken ~5-minute daily news podcast.

Pipeline: fetch current headlines (financial + world feeds) -> Claude writes
a ~750-word spoken script under hard composition rules (>=50% of the runtime
on financial news, >=33% of all stories US-related) -> edge-tts synthesizes
an MP3 -> stored as a PodcastEpisode row, one per Pacific-time date.

Every stage degrades honestly: no Anthropic key -> no episode (we never
publish a fake script); TTS failure -> episode publishes as transcript-only.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.orm import Session

from config import settings
from models import PodcastDelivery, PodcastEpisode
from services import claude as claude_svc

PACIFIC = ZoneInfo("America/Los_Angeles")
RELEASE_HOUR, RELEASE_MINUTE = 3, 30   # 3:30 AM Pacific daily
TARGET_WORDS = 750                     # ~5 minutes at a natural ~150 wpm
WORDS_PER_MINUTE = 150
KEEP_EPISODES = 14

# A clear, neutral US-English news voice from Microsoft's free edge-tts set.
TTS_VOICE = "en-US-GuyNeural"

SCRIPT_MODEL = "claude-sonnet-5"  # script quality matters; one call/day


def today_pacific() -> str:
    return datetime.now(PACIFIC).strftime("%Y-%m-%d")


def seconds_until_next_release(now: datetime | None = None) -> float:
    now = now or datetime.now(PACIFIC)
    target = now.replace(hour=RELEASE_HOUR, minute=RELEASE_MINUTE, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def _gather_headlines() -> tuple[list[dict], list[dict]]:
    financial = await claude_svc.fetch_headlines(limit=14, category="markets")
    world = await claude_svc.fetch_headlines(limit=10, category="world")
    return financial, world


def _headline_block(articles: list[dict]) -> str:
    return "\n".join(f"- {a['title']} ({a['source']}): {a['summary']}" for a in articles if a.get("title"))


async def generate_script(financial: list[dict], world: list[dict]) -> str | None:
    """Write the spoken script via Claude. Returns None if no key configured
    or the call fails — callers must not publish anything in that case."""
    if not claude_svc._anthropic_configured():
        print("[podcast] skipped: no Anthropic key configured")
        return None
    if not financial and not world:
        print("[podcast] skipped: no headlines available")
        return None

    date_line = datetime.now(PACIFIC).strftime("%A, %B %d, %Y")
    prompt = (
        "Write the complete spoken script for the FAWN Daily Brief, a ~5-minute daily news "
        f"podcast for U.S. college students, dated {date_line}. It is read aloud by a "
        "text-to-speech voice, so write ONLY the words to be spoken: no headers, no stage "
        "directions, no markdown, no bullet symbols.\n\n"
        f"TARGET LENGTH: {TARGET_WORDS} words (plus or minus 50).\n\n"
        "HARD COMPOSITION RULES:\n"
        "1. At least HALF of the script must cover financial/economic news (markets, rates, "
        "prices, jobs, student loans, banking).\n"
        "2. At least ONE THIRD of all stories covered must be about or directly involve the "
        "United States.\n"
        "3. Plain English. No jargon without a one-phrase explanation. No hype.\n"
        "4. Only report what is in the headlines below — never invent facts, numbers, or "
        "events. If a detail isn't in the material, don't say it.\n"
        "5. No investment advice. Where natural, connect one or two stories to what they "
        "mean for a student's money (rent, groceries, loans, wages).\n\n"
        "STRUCTURE: One-sentence welcome ('Good morning, this is the FAWN Daily Brief for "
        f"{date_line}...'), financial news block first, then US news, then the wider world, "
        "then a single-sentence sign-off that reminds listeners this briefing is AI-generated "
        "from news wires and is not financial advice.\n\n"
        f"FINANCIAL/MARKETS HEADLINES:\n{_headline_block(financial)}\n\n"
        f"WORLD/GENERAL HEADLINES:\n{_headline_block(world)}"
    )

    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                claude_svc.ANTHROPIC_URL,
                headers={
                    "x-api-key": settings.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": SCRIPT_MODEL,
                    "max_tokens": 2000,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            if resp.status_code != 200:
                print(f"[podcast] script call failed: {resp.status_code} {resp.text[:300]}")
                return None
            data = resp.json()
            script = "".join(
                b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
            ).strip()
            return script or None
    except Exception as e:
        print(f"[podcast] script call raised: {e}")
        return None


async def synthesize_audio(script: str) -> bytes | None:
    """Text-to-speech via edge-tts (free, no API key). Returns MP3 bytes,
    or None on any failure — the episode then ships transcript-only."""
    try:
        import edge_tts  # lazy: keeps the app importable if the dep is missing

        communicate = edge_tts.Communicate(script, TTS_VOICE)
        chunks: list[bytes] = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])
        audio = b"".join(chunks)
        return audio or None
    except Exception as e:
        print(f"[podcast] TTS failed (episode will be transcript-only): {e}")
        return None


async def generate_episode(db: Session, force: bool = False) -> PodcastEpisode | None:
    """Generate (or return) today's episode. Idempotent per Pacific date
    unless force=True, which regenerates today's episode in place."""
    episode_date = today_pacific()
    existing = db.query(PodcastEpisode).filter(PodcastEpisode.episode_date == episode_date).first()
    if existing and not force:
        return existing

    financial, world = await _gather_headlines()
    script = await generate_script(financial, world)
    if not script:
        return None

    audio = await synthesize_audio(script)
    word_count = len(script.split())

    if existing:
        episode = existing
    else:
        episode = PodcastEpisode(episode_date=episode_date)
        db.add(episode)
    episode.title = f"FAWN Daily Brief — {datetime.now(PACIFIC).strftime('%B %d, %Y')}"
    episode.script = script
    episode.audio_mp3 = audio
    episode.word_count = word_count
    episode.est_duration_seconds = int(word_count / WORDS_PER_MINUTE * 60)
    episode.source_headline_count = len(financial) + len(world)
    db.commit()
    db.refresh(episode)

    _prune_old_episodes(db)
    print(f"[podcast] episode {episode_date} ready: {word_count} words, audio={'yes' if audio else 'NO (transcript only)'}")
    return episode


async def send_episode_to_subscribers(db: Session, episode: PodcastEpisode) -> int:
    """Send today's episode link to all active users via email, once each.

    Delivery state is persisted per episode/user so restarts and retries never
    duplicate a successful send. Failed sends remain retryable on the next
    scheduler pass.
    """
    from config import settings
    from models import User
    from email_templates import build_daily_brief

    if not settings.resend_api_key:
        print(f"[podcast-email] skipped: no resend_api_key configured")
        return 0

    # Fetch all users with active wallets. Delivery rows are deliberately not
    # created when email is disabled, so enabling Resend later can catch up.
    users = db.query(User).filter(User.wallet_initialized == True).all()
    if not users:
        print(f"[podcast-email] no subscribers")
        return 0

    subject, html = build_daily_brief(
        episode.episode_date,
        episode.title,
        episode.est_duration_seconds
    )

    sent_count = 0
    async with httpx.AsyncClient(timeout=10) as client:
        for user in users:
            delivery = db.query(PodcastDelivery).filter(
                PodcastDelivery.episode_id == episode.id,
                PodcastDelivery.user_id == user.id,
            ).first()
            if delivery and delivery.status == "sent":
                continue
            if not delivery:
                delivery = PodcastDelivery(episode_id=episode.id, user_id=user.id)
                db.add(delivery)
                db.flush()
            delivery.attempts += 1
            try:
                resp = await client.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {settings.resend_api_key}", "Content-Type": "application/json"},
                    json={"from": f"FAWN <{settings.from_email}>", "to": [user.email], "subject": subject, "html": html},
                )
                if resp.status_code in (200, 201):
                    delivery.status = "sent"
                    delivery.sent_at = datetime.now(PACIFIC)
                    delivery.last_error = None
                    sent_count += 1
                else:
                    delivery.status = "failed"
                    delivery.last_error = f"Resend HTTP {resp.status_code}"[:300]
                    print(f"[podcast-email] to {user.email} failed: {resp.status_code}")
            except Exception as e:
                delivery.status = "failed"
                delivery.last_error = str(e)[:300]
                print(f"[podcast-email] to {user.email} raised: {e}")
            db.commit()

    print(f"[podcast-email] sent {sent_count}/{len(users)} daily briefs")
    return sent_count


async def publish_today(db: Session) -> PodcastEpisode | None:
    """Generate today's brief if needed, then deliver any unsent copies."""
    episode = await generate_episode(db)
    if episode:
        await send_episode_to_subscribers(db, episode)
    return episode


def _prune_old_episodes(db: Session):
    try:
        cutoff = (datetime.now(PACIFIC) - timedelta(days=KEEP_EPISODES)).strftime("%Y-%m-%d")
        stale = db.query(PodcastEpisode).filter(PodcastEpisode.episode_date < cutoff).all()
        for row in stale:
            db.delete(row)
        if stale:
            db.commit()
    except Exception as e:
        db.rollback()
        print(f"[podcast] prune failed (continuing): {e}")
