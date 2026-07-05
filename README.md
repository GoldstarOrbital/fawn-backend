# FAWN Backend

Financial AI + World News — banking API for Gen Z and college students.

## Stack

- **Python 3.13** + **FastAPI** — REST API
- **SQLite** (dev) / **PostgreSQL** (prod) — user database via SQLAlchemy
- **Stripe** — Banking-as-a-Service via Connect + Treasury + Issuing (FDIC-insured accounts, ACH, virtual debit cards)
- **Anthropic Claude** — AI news summarization

## Project Structure

```
fawn-backend/
├── main.py              # App entry point, router registration
├── config.py            # Settings loaded from .env
├── database.py          # DB connection + session factory
├── models.py             # SQLAlchemy User table
├── schemas.py            # Pydantic request/response models
├── dependencies.py       # JWT auth dependency (get_current_user)
├── routers/
│   ├── auth.py                 # POST /auth/register, /auth/login, /auth/token, GET /auth/me
│   ├── accounts.py             # GET /accounts/balance, dashboard, KYC status polling
│   ├── stripe_onboarding.py    # POST /stripe/onboarding — hosted Connect + Account Link KYC
│   ├── stripe_baas_webhook.py  # POST /stripe/baas-webhook — account.updated -> Financial Account activation
│   ├── stripe_webhook.py       # POST /stripe/webhook — founding-member checkout payments (unrelated to BaaS)
│   ├── cards.py                # Stripe Issuing virtual cards
│   ├── funding.py              # Add Funds via Stripe Treasury Inbound Transfer
│   ├── p2p.py                  # FAWN-to-FAWN sends via Stripe Treasury transfers
│   ├── transactions.py         # GET /transactions/
│   └── news.py                 # POST /news/summary
└── services/
    ├── stripe_baas.py    # All Stripe Connect/Treasury/Issuing BaaS API calls
    └── claude.py         # Anthropic Claude API calls
```

## Setup

**1. Create and activate virtual environment (Python 3.13)**
```powershell
py -3.13 -m venv venv
.\venv\Scripts\Activate.ps1
```

**2. Install dependencies**
```powershell
pip install -r requirements.txt
```

**3. Configure environment**
```powershell
Copy-Item .env.example .env
# Edit .env and add your API keys
```

**4. Run the server**
```powershell
uvicorn main:app --reload --port 8001
```

**5. Open API docs**

Navigate to `http://localhost:8001/docs`

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | No | Defaults to SQLite. Use `postgresql://...` for prod. |
| `STRIPE_SECRET_KEY` | For banking + paid memberships | Stripe API key. Test-mode (`sk_test_...`) vs live (`sk_live_...`) selects sandbox vs production — there's no separate base-URL setting like the old BaaS provider had. |
| `STRIPE_BAAS_WEBHOOK_SECRET` | Yes for BaaS webhooks | Verifies `/stripe/baas-webhook` deliveries (account.updated, Treasury/Issuing events) |
| `ALLOW_UNSIGNED_BAAS_WEBHOOKS` | Local/dev only | Set `true` only for local webhook testing without Stripe signatures |
| `STRIPE_ONBOARDING_REFRESH_URL` / `STRIPE_ONBOARDING_RETURN_URL` | For onboarding | Where Stripe's hosted Account Link flow sends the user back |
| `ANTHROPIC_API_KEY` | For AI news | From console.anthropic.com |
| `JWT_SECRET` | Yes (change in prod) | Any long random string |
| `ALLOWED_ORIGINS` | Yes for launch | Comma-separated browser origins allowed by CORS |
| `RESEND_API_KEY` | For email | Password reset and lifecycle emails |
| `FROM_EMAIL` | For email | Verified Resend sender, defaults to `alex@getfawn.com` |
| `ADMIN_API_KEY` | Yes for admin routes | Required for admin and backfill endpoints |
| `STRIPE_WEBHOOK_SECRET` | Yes for founding-member webhooks | Verifies `/stripe/webhook` (checkout) events — separate endpoint/secret from the BaaS webhook above |
| `ALLOW_UNSIGNED_STRIPE_WEBHOOKS` | Local/dev only | Keep `false` outside local testing |
| `ALLOW_UNVERIFIED_ACH_FUNDING` | No for launch | Keep `false` until external-account ownership verification is live |

## API Endpoints

### Auth
| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/auth/register` | No | Create FAWN user; optionally starts direct Stripe sandbox KYC when SSN/DOB/address are supplied |
| POST | `/auth/login` | No | Get JWT token (JSON body) |
| POST | `/auth/token` | No | Get JWT token (OAuth2 form — used by Swagger) |
| GET | `/auth/me` | Yes | Get current user profile |
| POST | `/stripe/onboarding` | Yes | Create a Stripe Connect account + hosted Account Link and return its onboarding URL |

### Accounts
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/accounts/balance` | Yes | Fetch live balance from the Stripe Treasury Financial Account |
| GET | `/accounts/details` | Yes | Routing/account number for the Financial Account |
| GET | `/accounts/dashboard` | Yes | Balance + details + recent transactions in one call |
| POST | `/accounts/refresh-application-status` | Yes | Poll Stripe Connect account status and finish Financial Account setup once active |

### Cards
| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/cards` | Yes | Issue a Stripe Issuing virtual debit card |
| GET | `/cards` | Yes | List your cards (masked) |
| POST | `/cards/{id}/freeze` / `/unfreeze` | Yes | Freeze/unfreeze a card |

### Transactions
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/transactions/` | Yes | List transactions from Stripe Treasury (`?limit=20`) |

### News
| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/news/summary` | Yes | AI news summary for given topics |

### Health
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/health` | No | Server health check |

## How Registration Works

1. User submits email, password, name, phone, and optional school/location/military status
2. FAWN creates a local `User` record in the DB without storing SSN, DOB, or address
3. Production onboarding calls `POST /stripe/onboarding` to create a Stripe Connect account and a hosted Account Link
4. FAWN returns the Stripe-hosted onboarding URL and the browser opens it for the user
5. The user enters SSN/DOB/address directly inside Stripe's hosted flow — FAWN never touches it
6. The `account.updated` webhook (`routers/stripe_baas_webhook.py`) creates the Treasury Financial Account once Stripe's `treasury` capability goes active, and all balance/transaction calls go directly to Stripe from then on

For sandbox-only smoke tests, `/auth/register` still accepts DOB, SSN, and address. When all three are supplied and `STRIPE_SECRET_KEY` is configured, FAWN creates the Stripe Connect account directly with that KYC data.

## Regulatory Notes

- FAWN is **not** a bank. Deposits are held at Stripe's FDIC-member bank partners (Evolve Bank & Trust, Goldman Sachs Bank USA) via Stripe Treasury; Stripe Payments Company is a licensed money transmitter.
- The AI news feature is **informational only** — not investment advice.
- For production: use Stripe's hosted Account Link onboarding; do not collect SSN/address in FAWN-owned forms.
- Stripe Treasury and Issuing capabilities must be approved by Stripe for the platform account before card issuance/deposit accounts work in production.

## Security Notes

- Never commit `.env` to git
- Rotate `JWT_SECRET` before any real deployment
- CORS is configured from `ALLOWED_ORIGINS`; include every production web app domain before launch
- Keep unsigned Stripe checkout and Stripe BaaS webhook overrides disabled outside local development
- Rotate Stripe API keys periodically
