# FAWN Backend

Financial AI + World News — banking API for Gen Z and college students.

## Stack

- **Python 3.13** + **FastAPI** — REST API
- **SQLite** (dev) / **PostgreSQL** (prod) — user database via SQLAlchemy
- **Unit** — Banking-as-a-Service (FDIC-insured accounts, ACH, debit cards)
- **Anthropic Claude** — AI news summarization

## Project Structure

```
fawn-backend/
├── main.py              # App entry point, router registration
├── config.py            # Settings loaded from .env
├── database.py          # DB connection + session factory
├── models.py            # SQLAlchemy User table
├── schemas.py           # Pydantic request/response models
├── dependencies.py      # JWT auth dependency (get_current_user)
├── routers/
│   ├── auth.py          # POST /auth/register, /auth/login, /auth/token, GET /auth/me
│   ├── accounts.py      # GET /accounts/balance
│   ├── transactions.py  # GET /transactions/
│   └── news.py          # POST /news/summary
└── services/
    ├── unit.py          # All Unit BaaS API calls
    └── claude.py        # Anthropic Claude API calls
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
| `UNIT_API_TOKEN` | For banking features | From app.s.unit.co sandbox dashboard |
| `UNIT_BASE_URL` | No | Defaults to `https://api.s.unit.sh` (sandbox) |
| `UNIT_WEBHOOK_SECRET` | Yes for Unit webhooks | Rejects unsigned Unit webhook deliveries unless local override is enabled |
| `ALLOW_UNSIGNED_UNIT_WEBHOOKS` | Local/dev only | Set `true` only for local webhook testing without Unit signatures |
| `ANTHROPIC_API_KEY` | For AI news | From console.anthropic.com |
| `JWT_SECRET` | Yes (change in prod) | Any long random string |
| `ALLOWED_ORIGINS` | Yes for launch | Comma-separated browser origins allowed by CORS |
| `RESEND_API_KEY` | For email | Password reset and lifecycle emails |
| `FROM_EMAIL` | For email | Verified Resend sender, defaults to `alex@getfawn.com` |
| `ADMIN_API_KEY` | Yes for admin routes | Required for admin and backfill endpoints |
| `STRIPE_SECRET_KEY` | For paid memberships | Stripe API key |
| `STRIPE_WEBHOOK_SECRET` | Yes for Stripe webhooks | Verifies paid-member webhook events |
| `ALLOW_UNSIGNED_STRIPE_WEBHOOKS` | Local/dev only | Keep `false` outside local testing |
| `ALLOW_UNVERIFIED_ACH_FUNDING` | No for launch | Keep `false` until external-account ownership verification is live |

## API Endpoints

### Auth
| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/auth/register` | No | Create FAWN user; optionally starts direct Unit sandbox KYC when SSN/DOB/address are supplied |
| POST | `/auth/login` | No | Get JWT token (JSON body) |
| POST | `/auth/token` | No | Get JWT token (OAuth2 form — used by Swagger) |
| GET | `/auth/me` | Yes | Get current user profile |
| POST | `/unit/application-form` | Yes | Create a Unit-hosted KYC application form and return its URL |
| GET | `/unit/application-form-prefill` | Yes | Unit hosted application-form config/prefill endpoint |

### Accounts
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/accounts/balance` | Yes | Fetch live balance from Unit |

### Transactions
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/transactions/` | Yes | List transactions from Unit (`?limit=20`) |

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
3. Production onboarding calls `POST /unit/application-form` to create a Unit-hosted KYC form
4. FAWN returns the Unit-hosted form URL and the browser opens it for the user
5. FAWN tags the Unit form/application with `fawnUserId`
6. Unit webhooks finish account activation once KYC is approved, then all balance/transaction calls go directly to Unit

For sandbox-only smoke tests, `/auth/register` still accepts DOB, SSN, and address. When all three are supplied and `UNIT_API_TOKEN` is configured, FAWN creates the Unit individual application directly. Sandbox SSN `721074426` is the happy-path approval value.

## Regulatory Notes

- FAWN is **not** a bank. Deposits are held at Unit's FDIC-member bank partner.
- The AI news feature is **informational only** — not investment advice.
- For production: use Unit's hosted application form; do not collect SSN/address in FAWN-owned forms.
- For production: switch `UNIT_BASE_URL` to `https://api.unit.co`

## Security Notes

- Never commit `.env` to git
- Rotate `JWT_SECRET` before any real deployment
- CORS is configured from `ALLOWED_ORIGINS`; include every production web app domain before launch
- Keep unsigned Stripe and Unit webhook overrides disabled outside local development
- The sandbox Unit token in `.env` should be rotated periodically
