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
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from rate_limiting import limiter
from database import engine, Base, SessionLocal
from routers import auth, accounts, transactions, news, waitlist, referral, admin, email_automation, public_stats, stripe_webhook, member, deals, p2p, cards, unit_webhook, funding, unit_onboarding, podcast, money_review, investing, plaid_link, column_webhook, lithic_webhook, crypto
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

        # crypto wallet columns — new crypto-native architecture (2026-07-08)
        # Note: crypto_wallet_address is VARCHAR (not UNIQUE) to allow schema patching on existing tables
        # Uniqueness is enforced at the model level + in CryptoWallet table
        _patch("users", "crypto_wallet_address", "crypto_wallet_address VARCHAR")
        _patch("users", "wallet_type", "wallet_type VARCHAR")  # non_custodial | fawn_custodial
        _patch("users", "usdc_balance_cents", "usdc_balance_cents INTEGER DEFAULT 0 NOT NULL")
        _patch("users", "wallet_initialized", "wallet_initialized BOOLEAN DEFAULT FALSE NOT NULL")
        _patch("users", "total_fees_paid_cents", "total_fees_paid_cents INTEGER DEFAULT 0 NOT NULL")

        # user_audit_log columns
        _patch("user_audit_log", "retention_expires_at", "retention_expires_at TIMESTAMP WITH TIME ZONE")

        # crypto_wallets table - ensure encrypted_private_key column exists
        try:
            with engine.begin() as conn:
                # Check if table exists
                result = conn.execute(text("SELECT 1 FROM information_schema.tables WHERE table_name='crypto_wallets'"))
                table_exists = result.fetchone() is not None

                if not table_exists:
                    # Create table if it doesn't exist
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS crypto_wallets (
                            id VARCHAR PRIMARY KEY,
                            user_id VARCHAR NOT NULL UNIQUE,
                            wallet_address VARCHAR NOT NULL UNIQUE,
                            wallet_type VARCHAR NOT NULL,
                            chain VARCHAR NOT NULL DEFAULT 'polygon',
                            usdc_balance_cents INTEGER NOT NULL DEFAULT 0,
                            encrypted_private_key BYTEA,
                            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                        )
                    """))
                    print("[startup] created crypto_wallets table")
                else:
                    # Table exists, ensure encrypted_private_key column exists
                    cols = {c["name"] for c in inspector.get_columns("crypto_wallets")}
                    if "encrypted_private_key" not in cols:
                        conn.execute(text("ALTER TABLE crypto_wallets ADD COLUMN encrypted_private_key BYTEA"))
                        print("[startup] added encrypted_private_key column to crypto_wallets")
        except Exception as e:
            print(f"[startup] crypto_wallets setup failed (continuing): {e}")

        # audit logging (user_audit_log table is created automatically via create_all)
    except Exception as e:
        print(f"[startup] schema patch pass failed (continuing): {e}")


_init_db_schema()

app = FastAPI(
    title="FAWN API",
    description="Student-focused banking platform. Send money instantly to anyone - FAWN users or traditional bank accounts. No monthly fees.",
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

# Crypto-native stablecoin wallet & transfers
app.include_router(crypto.router)
app.include_router(crypto.transfer_router)
app.include_router(crypto.user_router)  # user data export, deletion
app.include_router(crypto.admin_router)


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
                    episode = await podcast_svc.generate_episode(db)
                    if episode:
                        await podcast_svc.send_episode_to_subscribers(db, episode)
                finally:
                    db.close()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[podcast] scheduler pass failed (will retry next cycle): {e}")
                await asyncio.sleep(300)  # don't tight-loop on repeated failures

    asyncio.get_event_loop().create_task(_loop())


@app.on_event("startup")
async def _start_gas_freshness_check():
    """Daily check of Campus Savings gas-price freshness.

    Gas prices are hand-verified (no honest free per-station feed exists), so
    this doesn't fetch prices — it re-evaluates staleness once a day and logs a
    re-verify reminder when they exceed the threshold, so stale prices never go
    unnoticed. The user-facing freshness badge reads the same source live via
    GET /deals/gas-status.
    """
    import asyncio
    from routers.deals import gas_freshness

    async def _loop():
        while True:
            try:
                f = gas_freshness()
                if f.get("stale"):
                    print(
                        f"[gas] prices are {f['days_old']} days old "
                        f"(verified {f['verified_date']}, threshold "
                        f"{f['threshold_days']}d) — re-verify and bump "
                        f"GAS_VERIFIED_DATE."
                    )
                else:
                    print(f"[gas] freshness ok ({f.get('days_old')} days old)")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[gas] freshness check failed (will retry): {e}")
            await asyncio.sleep(24 * 60 * 60)  # once a day

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


@app.get("/status/unit-auth")
async def unit_auth_status():
    """Deep check: does the currently-configured UNIT_API_TOKEN authenticate?

    Distinct from /status's `unit_api_reachable` (which only pings the host).
    This makes an authenticated Unit call and returns only a boolean — no
    token or account data is exposed. Purpose-built to verify token rotation:
    after swapping UNIT_API_TOKEN, hit this and expect `unit_auth_ok: true`.
    """
    from services import unit as unit_svc
    result = await unit_svc.verify_auth()
    return {
        "unit_auth_ok": result["ok"],
        "unit_http_status": result["status"],
        "detail": result["detail"],
        "reason": result["reason"],
        "unit_base_url": settings.unit_base_url,
    }


@app.get("/status/egress-ip")
async def egress_ip():
    """Report this deployment's OUTBOUND public IP.

    Unit API tokens can be IP-allowlisted; authenticated calls from a
    non-allowlisted IP are silently dropped (they time out rather than 401).
    This returns the IP Unit sees for our requests, so it can be added to the
    token's allowlist (or so we can confirm the allowlist should be cleared).
    Queries a couple of echo services and returns whichever answers first.
    """
    import httpx
    for url in ("https://api.ipify.org?format=json", "https://ifconfig.me/all.json"):
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                r = await client.get(url)
            if r.status_code < 300:
                data = r.json()
                ip = data.get("ip") or data.get("ip_addr") or data.get("remote_addr")
                if ip:
                    return {"egress_ip": ip, "source": url}
        except Exception:
            continue
    return {"egress_ip": None, "error": "could not determine egress IP"}


@app.get("/status/net-diag")
async def net_diag():
    """Isolate ReadTimeout root cause: MTU/egress blackhole vs Unit-specific block.

    Sends a request with a ~2.1KB Authorization header (mimicking Unit's large
    token) to a NON-Unit echo host, and separately a small request. If the
    large request to a neutral host also hangs, the fault is Railway's egress
    network (large packets dropped), not Unit and not the token.
    """
    import httpx
    big_header = "Bearer " + ("x" * 2100)
    out = {}
    # 1) large-header request to a neutral host
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://httpbin.org/headers", headers={"Authorization": big_header})
        out["large_header_neutral_host"] = f"ok http_{r.status_code}"
    except Exception as e:
        out["large_header_neutral_host"] = f"FAILED {type(e).__name__}"
    # 2) small request to same neutral host (control)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://httpbin.org/get")
        out["small_neutral_host"] = f"ok http_{r.status_code}"
    except Exception as e:
        out["small_neutral_host"] = f"FAILED {type(e).__name__}"
    return out
