# FAWN Backend

**Fintech All-in-One Wallet** — Banking, Card Issuing, Investing.

A Python-based fintech platform integrating:
- **Column** — Banking (ACH, wires, deposits)
- **Lithic** — Card issuing
- **Alpaca** — Fractional shares + investing
- **Alloy** — KYC verification
- **Unit21** — AML monitoring
- **Modern Treasury** — Reconciliation

## Quick Start

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m uvicorn app.main:app --reload
```

## Architecture

```
app/
├── main.py                 # FastAPI entry
├── config.py               # Environment + settings
├── db/
│   ├── models.py           # SQLAlchemy models (Account, Ledger, Card, etc.)
│   ├── session.py          # Database connection
│   └── migrations/
├── core/
│   ├── security.py         # JWT, 2FA, encryption
│   ├── ledger.py           # Internal ledger + event log
│   └── reconciliation.py    # Modern Treasury sync
├── integrations/
│   ├── column.py           # Column API (ACH, wires, deposits)
│   ├── lithic.py           # Lithic API (card issuing)
│   ├── alpaca.py           # Alpaca API (investing)
│   ├── alloy.py            # Alloy KYC
│   ├── unit21.py           # Unit21 AML
│   └── modern_treasury.py  # Reconciliation
├── api/
│   ├── auth.py             # Login, registration, 2FA
│   ├── accounts.py         # Account creation + management
│   ├── cards.py            # Card creation, tokenization, disputes
│   ├── transfers.py        # ACH, wires, book transfers
│   ├── investing.py        # Fractional shares, auto-invest
│   └── compliance.py       # KYC, AML, Reg E
├── schemas/                # Pydantic request/response models
├── utils/
│   ├── errors.py           # Custom exceptions
│   ├── logging.py          # Structured logging
│   └── helpers.py
├── tests/
│   ├── conftest.py
│   ├── test_auth.py
│   ├── test_column.py
│   └── ...
└── migrations/             # Alembic (SQLAlchemy)
```

## Launch Timeline

- **Week 1** (Jul 4–11): Column + KYC/AML integration
- **Week 2** (Jul 11–18): ACH + wire + reconciliation
- **Week 3** (Jul 18–25): Lithic card issuing
- **Week 4** (Jul 25–Aug 1): Alpaca investing
- **Week 5** (Aug 1–8): Cash access + Stripe merchant
- **Week 6** (Aug 8–13): Compliance audit + launch

## Environment Variables

See `.env.example` for the full list with inline notes. Core:

```
DATABASE_URL=postgresql://user:pass@localhost/fawn
JWT_SECRET=...                     # 32+ chars, required
UNIT_API_TOKEN=...                 # KYC/onboarding front door
ANTHROPIC_API_KEY=...
SENTRY_DSN=...                     # optional
```

Multi-BaaS provider stack (all optional — each provider stays dormant and
returns HTTP 503 until its key is set, so the app boots fine with these blank):

```
BAAS_PROVIDER=unit                 # "unit" | "column" — selects banking backend
COLUMN_API_KEY= / COLUMN_BASE_URL= / COLUMN_WEBHOOK_SECRET=       # banking
LITHIC_API_KEY= / LITHIC_BASE_URL= / LITHIC_WEBHOOK_SECRET=       # cards
ALPACA_API_KEY= / ALPACA_API_SECRET= / ALPACA_BASE_URL=          # investing
PLAID_CLIENT_ID= / PLAID_SECRET= / PLAID_ENV= / PLAID_BASE_URL=   # bank linking
```

## Status

- [x] Backend scaffold
- [x] DB schema + startup column patching
- [x] Column integration — service client + webhook (`services/column.py`, `/column/webhook`)
- [x] Lithic card issuing — service client + webhook (`services/lithic.py`, `/lithic/webhook`)
- [x] Alpaca investing — accounts, orders, positions (`services/alpaca.py`, `/investing/*`)
- [x] Plaid bank linking — link token + exchange (`services/plaid.py`, `/plaid/*`)
- [ ] Cut `BAAS_PROVIDER=column` over for live money movement (needs Column key + contract)
- [ ] Wire provider webhooks to business logic (currently record + ack)
- [ ] KYC/AML vendor (Alloy/Unit21) — TBD
- [ ] Reconciliation + ledger event mapping
- [ ] Compliance policies
- [ ] Pen test + audit

> **Note:** each provider client above is a guarded scaffold — it raises
> `<Provider>NotConfigured` when unkeyed. Real money movement requires signed
> commercial agreements and live credentials from each provider.

---

**Team:** 3 engineers | **Burn:** $18K/mo | **Launch:** Aug 13
