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
| `ANTHROPIC_API_KEY` | For AI news | From console.anthropic.com |
| `JWT_SECRET` | Yes (change in prod) | Any long random string |

## API Endpoints

### Auth
| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/auth/register` | No | Create account + Unit BaaS onboarding |
| POST | `/auth/login` | No | Get JWT token (JSON body) |
| POST | `/auth/token` | No | Get JWT token (OAuth2 form — used by Swagger) |
| GET | `/auth/me` | Yes | Get current user profile |

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

1. User submits email, password, name, phone
2. FAWN creates a local `User` record in the DB
3. FAWN calls Unit's `/applications` endpoint to create an individual application
4. Unit instantly approves (sandbox SSN `721074426` always approves)
5. A Unit customer ID and deposit account ID are saved back to the User record
6. All future balance/transaction calls go directly to Unit using those IDs

## Regulatory Notes

- FAWN is **not** a bank. Deposits are held at Unit's FDIC-member bank partner.
- The AI news feature is **informational only** — not investment advice.
- For production: replace sandbox SSN/address with real KYC collection via Unit's hosted form.
- For production: switch `UNIT_BASE_URL` to `https://api.unit.co`

## Security Notes

- Never commit `.env` to git
- Rotate `JWT_SECRET` before any real deployment
- `allow_origins=["*"]` in CORS is fine for dev — tighten to your frontend domain in prod
- The sandbox Unit token in `.env` should be rotated periodically
