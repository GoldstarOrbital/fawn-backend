import time
import os
try:
    import sentry_sdk
except ImportError:
    sentry_sdk = None
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from database import engine, Base, SessionLocal
from routers import auth, accounts, transactions, news, waitlist, referral, admin, email_automation, public_stats, stripe_webhook, member, deals, p2p, cards, unit_webhook, funding, unit_onboarding, podcast, money_review, investing, plaid_link, column_webhook, lithic_webhook
from config import settings

if sentry_sdk and os.environ.get("SENTRY_DSN"):
    sentry_sdk.init(
        dsn=os.environ["SENTRY_DSN"],
        traces_sample_rate=0.2,
        environment=os.environ.get("RAILWAY_ENVIRONMENT", "production"),
    )

_START_TIME = time.time()


def _init_db_schema():
    """Create tables and patch any columns added after the initial deploy.

    SQLAlchemy's create_all() only creates missing TABLES â€” it never adds
    missing COLUMNS to an existing table. On Railway the `waitlist` table
    was originally created without `source` / `referral_code`, so any query
    that selects those columns now returns 500. Patch them in idempotently.
    """
    from sqlalchemy import inspect, text

    try:
        Base.metadata.create_all(bind=engine)
    except Exception as e:
        # Don't crash boot if DB is briefly unreachable during deploy.
        print(f"[startup] create_all failed (continuing): {e}")
        return

    try:
        inspector = inspect(engine)

        def _patch(table: str, col: str, ddl: str):
            try:
                cols = {c["name"] for c in inspector.get_columns(table)}
            except Exception:
                return
            if col in cols:
                return
            try:
                with engine.begin() as conn:
                    conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {ddl}'))
                print(f"[startup] added missing column {table}.{col}")
            except Exception as e:
                print(f"[startup] failed to add {table}.{col}: {e}")

        # waitlist columns added after initial schema
        _patch("waitlist", "source", "source VARCHAR")
        _patch("waitlist", "referral_code", "referral_code VARCHAR")
        _patch("waitlist", "name", "name VARCHAR")

        # founding_members columns
        _patch("founding_members", "refunded", "refunded BOOLEAN DEFAULT FALSE NOT NULL")

        # magic_link_tokens â€” no extra patches needed (create_all handles new tables)
        # password_reset_tokens â€” no extra patches needed (create_all handles new tables)
        # stripe_events â€” no extra patches needed

        # users columns added after initial schema
        _patch("users", "referral_code", "referral_code VARCHAR")
        _patch("users", "referred_by", "referred_by VARCHAR")
        _patch("users", "referral_count", "referral_count INTEGER DEFAULT 0 NOT NULL")
        _patch("users", "phone", "phone VARCHAR")
        _patch("users", "is_student", "is_student BOOLEAN DEFAULT FALSE")
        _patch("users", "unit_application_id", "unit_application_id VARCHAR")
        _patch("users", "unit_application_form_id", "unit_application_form_id VARCHAR")
        _patch("users", "school", "school VARCHAR")
        _patch("users", "location", "location VARCHAR")
        _patch("users", "military_status", "military_status VARCHAR")

        # multi-BaaS provider ids added during the Column/Lithic/Alpaca cutover
        _patch("users", "column_entity_id", "column_entity_id VARCHAR")
        _patch("users", "column_account_id", "column_account_id VARCHAR")
        _patch("users", "lithic_account_token", "lithic_account_token VARCHAR")
        _patch("users", "alpaca_account_id", "alpaca_account_id VARCHAR")

        # cards.provider distinguishes Unit- vs Lithic-issued cards
        _patch("cards", "provider", "provider VARCHAR DEFAULT 'unit'")

        # p2p_disputes columns added after initial schema
        _patch("p2p_disputes", "payment_id", "payment_id VARCHAR")
    except Exception as e:
        print(f"[startup] schema patch pass failed (continuing): {e}")


_init_db_schema()

limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])

app = FastAPI(
    title="FAWN API",
    description="Financial AI + World News â€” banking backend",
    version="0.2.0",
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

ALLOWED_ORIGINS = settings.allowed_origins_list

app.add_middleware(GZipMiddleware, minimum_size=500)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Admin-Key"],
)


_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=(), payment=()"
    # Swagger/ReDoc load their UI from a CDN, so they need a looser policy.
    # Every other route is a JSON API with no inline scripts â€” lock it down fully.
    if request.url.path.startswith(_DOCS_PATHS):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' cdn.jsdelivr.net 'unsafe-inline'; "
            "style-src 'self' cdn.jsdelivr.net 'unsafe-inline'; img-src 'self' data: fastapi.tiangolo.com"
        )
    else:
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    return response


app.include_router(auth.router)
app.include_router(accounts.router)
app.include_router(transactions.router)
app.include_router(news.router)
app.include_router(waitlist.router)
app.include_router(referral.router)
app.include_router(admin.router)
app.include_router(email_automation.router)
app.include_router(public_stats.router)
app.include_router(stripe_webhook.router)
app.include_router(member.router)
app.include_router(deals.router)
app.include_router(p2p.router)
app.include_router(cards.router)
app.include_router(unit_webhook.router)
app.include_router(funding.router)
app.include_router(unit_onboarding.router)
app.include_router(podcast.router)
app.include_router(money_review.router)
app.include_router(investing.router)
app.include_router(plaid_link.router)
app.include_router(column_webhook.router)
app.include_router(lithic_webhook.router)


@app.on_event("startup")
async def _start_podcast_scheduler():
    """Daily 3:30 AM Pacific generation of the FAWN Daily Brief.

    A plain asyncio loop instead of an external cron: sleep until the next
    release time, generate, repeat. Safe against restarts and (unlikely)
    multiple instances because generate_episode is idempotent per Pacific
    date â€” the unique episode_date row is the lock.
    """
    import asyncio
    from services import podcast as podcast_svc

    async def _loop():
        while True:
            try:
                await asyncio.sleep(podcast_svc.seconds_until_next_release())
                db = SessionLocal()
                try:
                    await podcast_svc.generate_episode(db)
                finally:
                    db.close()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[podcast] scheduler pass failed (will retry next cycle): {e}")
                await asyncio.sleep(300)  # don't tight-loop on repeated failures

    asyncio.get_event_loop().create_task(_loop())


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.2.0"}


@app.get("/status")
def status():
    """Operational status: uptime, db connectivity, Unit API reachability, version."""
    # DB check
    db_ok = False
    try:
        db = SessionLocal()
        db.execute(__import__("sqlalchemy").text("SELECT 1"))
        db.close()
        db_ok = True
    except Exception:
        pass

    # Unit API reachability â€” attempt a connection; any HTTP response means the host is up
    unit_ok = False
    try:
        import urllib.request
        import urllib.error
        req = urllib.request.Request(
            f"{settings.unit_base_url}/",
            method="HEAD",
        )
        req.add_header("User-Agent", "fawn-status-check/1.0")
        try:
            urllib.request.urlopen(req, timeout=3)
            unit_ok = True
        except urllib.error.HTTPError:
            # Got an HTTP error response â€” host is reachable
            unit_ok = True
        except urllib.error.URLError:
            unit_ok = False
    except Exception:
        unit_ok = False

    uptime_seconds = round(time.time() - _START_TIME, 1)

    return {
        "version": "0.2.0",
        "uptime_seconds": uptime_seconds,
        "db_ok": db_ok,
        "unit_api_reachable": unit_ok,
        "unit_base_url": settings.unit_base_url,
    }
