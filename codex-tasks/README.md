> **SUPERSEDED (this migration):** FAWN's BaaS provider moved from Unit to
> Stripe (Connect + Treasury + Issuing). Everything below is historical —
> it describes the Unit-era integration and is kept for the record, not as
> current instructions. See `README.md` and `services/stripe_baas.py` for
> the live Stripe integration.

# Codex tasks — Unit BaaS integration push (historical, superseded by Stripe)

Four independent, scoped prompts for Codex. Each file is self-contained —
paste its whole content as the task prompt. They don't depend on each other
and can be run in parallel in separate worktrees/branches.

Repo: `fawn-backend` (FastAPI + SQLAlchemy + Postgres, deploys to Railway
from `origin/main`). Run tests with `python -m pytest tests/ -q` — all
must pass before any commit. Stage files by name (never `git add -A`),
never amend, never force-push.

What's already built and working (sandbox, `api.s.unit.sh`): KYC
application + webhook-driven account creation, deposit accounts, balance
+ transaction reads, Book Payments (instant P2P between FAWN accounts),
virtual cards (create/list/freeze/unfreeze), inline-counterparty ACH
funding (currently feature-flagged OFF via `ALLOW_UNVERIFIED_ACH_FUNDING`
pending task 1 below), and a signature-verified webhook receiver
(`routers/unit_webhook.py`, HMAC-SHA1 per Unit's scheme).

What's explicitly NOT in scope right now: Tier 2 external sends (sends to
non-FAWN banks/cards) — `services/external_send.py` is intentionally
stubbed because which settlement rail Unit's sponsor bank supports
(FedNow/RTP, push-to-card, or same-day ACH) hasn't been confirmed yet.
Don't guess at or implement a rail — that's a real money-movement
decision waiting on an answer from Unit, not a coding task.

1. `01-plaid-account-verification.md` — verify external-account ownership
   before re-enabling Add Funds (the actual blocker on the existing
   ACH-funding feature flag).
2. `02-unit-service-resilience.md` — retries/timeouts/structured error
   handling across `services/unit.py` so a transient Unit hiccup doesn't
   surface as a raw 500 or an unhandled exception.
3. `03-production-cutover-runbook.md` — docs + a config audit for the
   sandbox-to-production switch, not a feature. Low-risk, good first task.
4. `04-obp-evaluation.md` — CLOSED. Open Bank Project turned out to be
   an API-middleware layer banks deploy in front of their own core
   systems for European-style open-banking regulation, not a
   sponsor-bank-equivalent to Unit — there's no integration work here.
   Kept for the record.
