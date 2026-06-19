import time
import os
import sentry_sdk
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from database import engine, Base, SessionLocal
from routers import auth, accounts, transactions, news, waitlist, referral, admin, email_automation
from config import settings

if os.environ.get("SENTRY_DSN"):
    sentry_sdk.init(
        dsn=os.environ["SENTRY_DSN"],
        traces_sample_rate=0.2,
        environment=os.environ.get("RAILWAY_ENVIRONMENT", "production"),
    )

_START_TIME = time.time()


def _init_db_schema():
    """Create tables and patch any columns added after the initial deploy.

    SQLAlchemy's create_all() only creates missing TABLES — it never adds
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

        # users columns added after initial schema
        _patch("users", "referral_code", "referral_code VARCHAR")
        _patch("users", "referred_by", "referred_by VARCHAR")
        _patch("users", "referral_count", "referral_count INTEGER DEFAULT 0 NOT NULL")
        _patch("users", "phone", "phone VARCHAR")
        _patch("users", "is_student", "is_student BOOLEAN DEFAULT FALSE")
    except Exception as e:
        print(f"[startup] schema patch pass failed (continuing): {e}")


_init_db_schema()

limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])

app = FastAPI(
    title="FAWN API",
    description="Financial AI + World News — banking backend",
    version="0.2.0",
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

ALLOWED_ORIGINS = [
    "https://goldstarorbital.github.io",
    "http://localhost:3000",
    "http://localhost:8080",
    "http://127.0.0.1:5500",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    return response


app.include_router(auth.router)
app.include_router(accounts.router)
app.include_router(transactions.router)
app.include_router(news.router)
app.include_router(waitlist.router)
app.include_router(referral.router)
app.include_router(admin.router)
app.include_router(email_automation.router)


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

    # Unit API reachability — attempt a connection; any HTTP response means the host is up
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
            # Got an HTTP error response — host is reachable
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
