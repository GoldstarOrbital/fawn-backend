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

```
DATABASE_URL=postgresql://user:pass@localhost/fawn
COLUMN_API_KEY=...
COLUMN_SANDBOX=true
LITHIC_API_KEY=...
ALPACA_API_KEY=...
ALLOY_API_KEY=...
UNIT21_API_KEY=...
MODERN_TREASURY_API_KEY=...
JWT_SECRET=...
SENTRY_DSN=...
```

## Status

- [ ] Backend scaffold
- [ ] DB schema + migrations
- [ ] Column integration
- [ ] Alloy + Unit21 KYC/AML
- [ ] Ledger + event mapping
- [ ] ACH origination + returns
- [ ] Wire support
- [ ] Lithic card issuing
- [ ] Alpaca investing
- [ ] Modern Treasury reconciliation
- [ ] Compliance policies
- [ ] Pen test + audit

---

**Team:** 3 engineers | **Burn:** $18K/mo | **Launch:** Aug 13
