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
from asgi_correlation_id import CorrelationIdMiddleware, correlation_id
import structlog
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from rate_limiting import limiter
from database import engine, Base, SessionLocal
from routers import auth, accounts, transactions, news, waitlist, referral, admin, email_automation, public_stats, stripe_webhook, member, deals, podcast, money_review, investing, plaid_link, onramp, crypto, trading, admin_credit, automation, webhooks, revenue, snaptrade
from config import settings
from logging_config import configure_logging

configure_logging()

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
        _patch("users", "username", "username VARCHAR UNIQUE")
        _patch("users", "avatar_url", "avatar_url TEXT")
        _patch("users", "referral_count", "referral_count INTEGER DEFAULT 0 NOT NULL")
        _patch("users", "phone", "phone VARCHAR")
        _patch("users", "is_student", "is_student BOOLEAN DEFAULT FALSE")
        _patch("users", "school", "school VARCHAR")
        _patch("users", "location", "location VARCHAR")
        _patch("users", "military_status", "military_status VARCHAR")

        # Alpaca is the one remaining third-party account id on User
        # (investing) — Plaid's link data lives on its own PlaidItem table.
        _patch("users", "alpaca_account_id", "alpaca_account_id VARCHAR")

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
                    # Create table if it doesn't exist. user_id is nullable
                    # (not NOT NULL) to allow the one treasury-wallet row,
                    # which has no owning user -- see
                    # services/crypto_wallet.py::get_or_create_treasury_wallet.
                    # Postgres allows multiple NULLs under a UNIQUE column.
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS crypto_wallets (
                            id VARCHAR PRIMARY KEY,
                            user_id VARCHAR UNIQUE,
                            wallet_address VARCHAR NOT NULL UNIQUE,
                            wallet_type VARCHAR NOT NULL,
                            chain VARCHAR NOT NULL DEFAULT 'polygon',
                            usdc_balance_cents INTEGER NOT NULL DEFAULT 0,
                            encrypted_private_key BYTEA,
                            wrapped_dek BYTEA,
                            key_version VARCHAR,
                            pending_fee_cents INTEGER NOT NULL DEFAULT 0,
                            is_treasury BOOLEAN NOT NULL DEFAULT FALSE,
                            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                        )
                    """))
                    print("[startup] created crypto_wallets table")
                else:
                    # Table exists, ensure newer columns exist. BYTEA
                    # columns are added here rather than via _patch() below
                    # -- _patch()'s single DDL string has to work on both
                    # Postgres and SQLite, and "BYTEA" isn't a real SQLite
                    # type (this whole block is Postgres-only already, per
                    # the information_schema.tables check above).
                    cols = {c["name"] for c in inspector.get_columns("crypto_wallets")}
                    if "encrypted_private_key" not in cols:
                        conn.execute(text("ALTER TABLE crypto_wallets ADD COLUMN encrypted_private_key BYTEA"))
                        print("[startup] added encrypted_private_key column to crypto_wallets")
                    if "wrapped_dek" not in cols:
                        conn.execute(text("ALTER TABLE crypto_wallets ADD COLUMN wrapped_dek BYTEA"))
                        print("[startup] added wrapped_dek column to crypto_wallets")
        except Exception as e:
            print(f"[startup] crypto_wallets setup failed (continuing): {e}")

        # crypto_wallets: fee-sweep tracking, treasury flag, envelope-
        # encryption key version (services/crypto_wallet.py's hardened
        # custodial key storage). VARCHAR/INTEGER/BOOLEAN are safe DDL
        # across both Postgres and SQLite, unlike the BYTEA columns above.
        _patch("crypto_wallets", "pending_fee_cents", "pending_fee_cents INTEGER DEFAULT 0 NOT NULL")
        _patch("crypto_wallets", "is_treasury", "is_treasury BOOLEAN DEFAULT FALSE NOT NULL")
        _patch("crypto_wallets", "key_version", "key_version VARCHAR")
        _patch("crypto_wallets", "status", "status VARCHAR DEFAULT 'active' NOT NULL")
        _patch("crypto_wallets", "superseded_by", "superseded_by VARCHAR")
        _patch("crypto_wallets", "deactivated_at", "deactivated_at TIMESTAMP WITH TIME ZONE")
        _patch("crypto_wallets", "deactivation_reason", "deactivation_reason VARCHAR")

        # Treasury wallet (services/crypto_wallet.py::get_or_create_treasury_wallet)
        # has no owning user, so user_id must be nullable. Idempotent:
        # dropping an already-dropped NOT NULL constraint is a silent
        # no-op on Postgres; not fatal if it fails (e.g. SQLite, which
        # has no ALTER COLUMN support at all -- SQLite tables created
        # fresh via Base.metadata.create_all() already get the nullable
        # column straight from the CryptoWallet model).
        try:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE crypto_wallets ALTER COLUMN user_id DROP NOT NULL"))
                print("[startup] made crypto_wallets.user_id nullable (for treasury wallet)")
        except Exception as e:
            print(f"[startup] crypto_wallets.user_id nullable patch skipped/failed (continuing): {e}")

        # chain_scan_checkpoints columns added after initial schema
        _patch("chain_scan_checkpoints", "pre_ledger_baseline_cents", "pre_ledger_baseline_cents INTEGER DEFAULT 0 NOT NULL")

        # crypto_transfers columns added after initial schema
        _patch("crypto_transfers", "chain", "chain VARCHAR")

        # crypto_transfers.status CHECK constraint widened to allow
        # 'pending_review' (held for admin approval -- large first-time-
        # recipient sends) and 'rejected' (held send an admin declined).
        # create_all() never touches an existing table's constraints, and
        # the original constraint was unnamed, so Postgres auto-generated
        # an unpredictable name -- it has to be looked up by inspecting
        # the actual CHECK definition, not assumed, before it can be
        # dropped and replaced with the new (explicitly named) one.
        try:
            with engine.begin() as conn:
                existing = conn.execute(text("""
                    SELECT con.conname
                    FROM pg_constraint con
                    JOIN pg_class rel ON rel.oid = con.conrelid
                    WHERE rel.relname = 'crypto_transfers'
                      AND con.contype = 'c'
                      AND pg_get_constraintdef(con.oid) ILIKE '%status%'
                """)).fetchall()
                for row in existing:
                    conname = row[0]
                    if conname != "ck_crypto_transfers_status":
                        conn.execute(text(f'ALTER TABLE crypto_transfers DROP CONSTRAINT "{conname}"'))
                        print(f"[startup] dropped old crypto_transfers status constraint: {conname}")

                already_correct = conn.execute(text(
                    "SELECT 1 FROM pg_constraint WHERE conname = 'ck_crypto_transfers_status'"
                )).fetchone()
                if not already_correct:
                    conn.execute(text(
                        "ALTER TABLE crypto_transfers ADD CONSTRAINT ck_crypto_transfers_status "
                        "CHECK (status IN ('pending', 'completed', 'failed', 'pending_review', 'approving', 'rejected'))"
                    ))
                    print("[startup] added ck_crypto_transfers_status constraint")
        except Exception as e:
            # Not fatal -- e.g. running on SQLite locally, which has no
            # pg_constraint catalog at all.
            print(f"[startup] crypto_transfers status constraint patch skipped/failed (continuing): {e}")

        # pending_fee_cents >= 0 CHECK constraint. models.py's CryptoWallet
        # declares it, but create_all() never alters an existing table's
        # constraints -- crypto_wallets already exists in production, so
        # this has to be added explicitly (same reasoning as the
        # crypto_transfers.status block above, just a brand-new constraint
        # rather than one being replaced). services/crypto_wallet.py's
        # collect_fees() docstring documents this constraint as a backstop
        # against a negative pending_fee_cents -- without this block that
        # backstop only exists in application code, never in the DB, on
        # any table that existed before this patch was added.
        try:
            with engine.begin() as conn:
                already_correct = conn.execute(text(
                    "SELECT 1 FROM pg_constraint WHERE conname = 'ck_crypto_wallets_pending_fee_cents'"
                )).fetchone()
                if not already_correct:
                    conn.execute(text(
                        "ALTER TABLE crypto_wallets ADD CONSTRAINT ck_crypto_wallets_pending_fee_cents "
                        "CHECK (pending_fee_cents >= 0)"
                    ))
                    print("[startup] added ck_crypto_wallets_pending_fee_cents constraint")
        except Exception as e:
            print(f"[startup] crypto_wallets pending_fee_cents constraint patch skipped/failed (continuing): {e}")

        # At most one treasury wallet, enforced at the DB level -- matches
        # the partial unique Index in models.py's CryptoWallet.__table_args__.
        # get_or_create_treasury_wallet (services/crypto_wallet.py) does a
        # plain check-then-create with no row lock; this index is what
        # actually stops two concurrent first-ever calls (the daily
        # scheduler racing a manual admin call, or Railway scaled to >1
        # replica) from each creating a different treasury wallet -- the
        # second INSERT fails with a real IntegrityError instead of silently
        # succeeding. A fresh SQLite test DB gets this from the model
        # directly via create_all(); this block is only for the
        # already-existing production Postgres table.
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_treasury_wallet "
                    "ON crypto_wallets (is_treasury) WHERE is_treasury = true"
                ))
                print("[startup] ensured idx_one_treasury_wallet unique index exists")
        except Exception as e:
            print(f"[startup] idx_one_treasury_wallet patch skipped/failed (continuing): {e}")

        # audit logging (user_audit_log table is created automatically via create_all)
    except Exception as e:
        print(f"[startup] schema patch pass failed (continuing): {e}")


_init_db_schema()

app = FastAPI(
    title="FAWN API",
    description="Student-focused banking platform. Send money instantly to anyone - FAWN users or traditional bank accounts. No monthly fees.",
    version="0.2.0",
)


@app.on_event("startup")
async def _ensure_user_profiles():
    """Ensure every account has a username and a payment handle."""
    from services.username_service import ensure_all_user_profiles
    db = SessionLocal()
    try:
        result = ensure_all_user_profiles(db)
        print(f"[profiles] usernames/handles ready: {result}")
    except Exception as e:
        db.rollback()
        print(f"[profiles] backfill failed (continuing): {e}")
    finally:
        db.close()

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

ALLOWED_ORIGINS = settings.allowed_origins_list

# Added first so it's outermost -- every other middleware and every
# request handler runs with the correlation ID already bound to
# structlog's contextvars, so any log emitted anywhere during this
# request (including nested calls into OFAC screening, address-risk
# checks, or on-chain settlement) carries the same ID automatically.
app.add_middleware(CorrelationIdMiddleware)


@app.middleware("http")
async def bind_correlation_id_to_structlog(request: Request, call_next):
    """asgi-correlation-id stores the request ID in its own contextvar;
    structlog's merge_contextvars processor reads from structlog's own
    contextvars, so this copies one into the other for the request's
    duration -- otherwise every log line from services/onchain_send.py,
    services/sanctions_screening.py, etc. would have no request_id at all."""
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=correlation_id.get())
    return await call_next(request)


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
app.include_router(podcast.router)
app.include_router(money_review.router)
app.include_router(investing.router)
app.include_router(snaptrade.router)
app.include_router(plaid_link.router)
app.include_router(onramp.router)

# Crypto-native stablecoin wallet & transfers
app.include_router(crypto.router)
app.include_router(crypto.transfer_router)
app.include_router(crypto.user_router)  # user data export, deletion
app.include_router(crypto.admin_router)

# Trading (Uniswap swaps on Polygon)
app.include_router(trading.router)

# Admin utilities (manual balance credit for deposits, fee collection)
app.include_router(admin_credit.router)

# Automation APIs (recurring transfers, price alerts, DCA, savings goals, etc)
app.include_router(automation.router)

# Webhooks & Notifications (real-time event delivery, webhooks, batch ops)
app.include_router(webhooks.router)

# Revenue Intelligence (admin-only revenue tracking and analytics)
app.include_router(revenue.router)


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
async def _start_blockchain_monitor():
    """Start autonomous blockchain settlement layer.

    Detects USDC transfers on Polygon and auto-credits user balances.
    Uses Alchemy (primary) + fallback to public RPCs for resilience.
    """
    import asyncio
    from services.blockchain_monitor import start_blockchain_monitor

    task = start_blockchain_monitor()
    print("[blockchain] Settlement layer started")


@app.on_event("startup")
async def _start_sanctions_screening():
    """Start the OFAC sanctions-list refresh loop (services/sanctions_screening.py).

    Blocks sends to recipient addresses on OFAC's SDN list -- a legal
    requirement, not optional. Refreshes daily; screening still works
    against the last successfully fetched list across restarts.
    """
    from services.sanctions_screening import start_sanctions_screening

    start_sanctions_screening()
    print("[sanctions] Screening loop started")


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


@app.on_event("startup")
async def _start_fee_sweep_scheduler():
    """Daily automatic sweep of accumulated platform fees to FAWN's
    treasury wallet (services/crypto_wallet.py::collect_fees).

    Off by default (ENABLE_FEE_SWEEP_SCHEDULER) -- this signs and
    broadcasts real on-chain transactions with no human in the loop, a
    meaningfully bigger step than the other startup loops in this file
    (which only read/log). Until it's turned on, POST /fees/collect
    (admin-key-gated, routers/crypto.py) is the only way fees get swept;
    turning this on just means that same operation also runs unattended
    once a day.
    """
    import asyncio

    if not settings.enable_fee_sweep_scheduler:
        print("[fees] automatic daily sweep disabled (ENABLE_FEE_SWEEP_SCHEDULER=false) -- use POST /fees/collect")
        return

    async def _loop():
        while True:
            try:
                db = SessionLocal()
                try:
                    from services import crypto_wallet
                    result = await crypto_wallet.collect_fees(db)
                    if result["status"] != "noop":
                        print(
                            f"[fees] swept ${result['total_fees']/100:.2f} from "
                            f"{result['transfers_settled']} wallet(s) to {result['treasury_wallet']} "
                            f"(status: {result['status']}, failures: {len(result['failures'])})"
                        )
                        if result["failures"]:
                            print(f"[fees] failures this run: {result['failures']}")
                        if result.get("treasury_seed_phrase"):
                            print("[fees] NEW TREASURY WALLET CREATED -- back up its seed phrase via the /fees/collect response NOW, it will not be logged again.")
                finally:
                    db.close()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[fees] daily sweep pass failed (will retry next cycle): {e}")
            await asyncio.sleep(24 * 60 * 60)  # once a day

    asyncio.get_event_loop().create_task(_loop())
    print("[fees] automatic daily sweep enabled")


@app.get("/health")
def health():
    return {"status": "ok", "version": "0.2.0"}


@app.get("/status")
def status():
    """Operational status: uptime, db connectivity, version."""
    # DB check
    db_ok = False
    try:
        db = SessionLocal()
        db.execute(__import__("sqlalchemy").text("SELECT 1"))
        db.close()
        db_ok = True
    except Exception:
        pass

    uptime_seconds = round(time.time() - _START_TIME, 1)

    return {
        "version": "0.2.0",
        "uptime_seconds": uptime_seconds,
        "db_ok": db_ok,
    }


@app.get("/status/gas-station")
async def gas_station_status():
    """Report the gas-station wallet's ADDRESS and native balances.

    This is the FAWN-controlled wallet that sponsors gas for custodial
    sends (services/onchain_send.py) — it must hold native gas tokens
    (POL on Polygon, ETH on Base) for on-chain sends and fee sweeps to
    work. Only the PUBLIC address and on-chain balances are exposed here
    (both are public information on-chain); the private key never leaves
    the environment.
    """
    from eth_account import Account
    from services import blockchain_monitor as bm
    from services import onchain_send

    if not settings.gas_station_private_key:
        return {"configured": False, "address": None,
                "detail": "GAS_STATION_PRIVATE_KEY is not set — gas sponsorship (and therefore on-chain sends and fee sweeps) is disabled."}

    address = Account.from_key(settings.gas_station_private_key).address
    balances = {}
    for chain in bm.CHAINS:
        try:
            wei = await onchain_send._get_native_balance(chain, address)
            balances[chain] = {
                "wei": wei,
                "native": round(wei / 1e18, 6) if wei is not None else None,
            }
        except Exception as e:
            balances[chain] = {"wei": None, "native": None, "error": str(e)[:80]}

    return {
        "configured": True,
        "address": address,
        "balances": balances,
        "note": "Fund this address with native gas tokens (POL on Polygon, ETH on Base). It sponsors gas top-ups for custodial user wallets.",
    }


@app.get("/status/egress-ip")
async def egress_ip():
    """Report this deployment's OUTBOUND public IP.

    Third-party APIs (Alpaca, Plaid) can be IP-allowlisted; authenticated
    calls from a non-allowlisted IP are silently dropped (they time out
    rather than 401). This returns the IP those providers see for our
    requests, so it can be added to an allowlist if needed.
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
    """Isolate ReadTimeout root cause: MTU/egress blackhole vs a specific
    provider block.

    Sends a request with a ~2.1KB Authorization header (mimicking an
    oversized token) to a neutral echo host, and separately a small
    request. If the large request to a neutral host also hangs, the fault
    is Railway's egress network (large packets dropped), not any one
    provider or token.
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
