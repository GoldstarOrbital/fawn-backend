> **SUPERSEDED:** written during the Unit era; FAWN's BaaS provider is now
> Stripe (Connect + Treasury + Issuing). Kept for historical record — see
> `README.md` and `services/stripe_baas.py` for the live integration.

# Task: Verify external bank account ownership before ACH funding

## Context

FAWN is a pre-launch fintech app (FDIC-insured student checking via Unit
BaaS, sandbox only — not production banking yet). `routers/funding.py`'s
`POST /funding/add-funds` pulls money from a user-entered external bank
account into their FAWN account via Unit's inline-counterparty ACH
payment (`services/unit.py::create_ach_funding_payment`). The user types
in a routing number, account number, and account type — there is
currently **no proof they actually own that external account**. That's a
real fraud surface (unauthorized ACH debit is a known abuse pattern), so
the endpoint is hard-disabled behind a feature flag:

```python
# routers/funding.py
if not settings.allow_unverified_ach_funding:
    raise HTTPException(status_code=403, detail="Add Funds is temporarily disabled until external bank account ownership verification is enabled.")
```

`ALLOW_UNVERIFIED_ACH_FUNDING` defaults to `false` everywhere (see
`.env.example`, `config.py`). Re-enabling this flag without verification
is not an option — do not just flip it to `true`.

## Goal

Add Plaid Link as an account-ownership verification step before Add
Funds will accept a transfer, then make the flag conditional on
verification actually having happened (not just unconditionally true).

## Suggested approach (adjust as needed — use your judgment on the Plaid API specifics, but the constraints below are non-negotiable)

1. Add a `LinkedBankAccount` model: `user_id`, `plaid_item_id`,
   `plaid_account_id`, `mask` (last4 only), `institution_name`,
   `verified_at`. **Never store full account/routing numbers from Plaid
   — Unit needs them per-call, not persisted.** Mirror how SSN and raw
   ACH numbers are already handled elsewhere in this codebase (sent
   directly to the provider, discarded after the call).
2. New router `routers/plaid.py`:
   - `POST /plaid/link-token` — create a Plaid Link token for the
     current user (Plaid `/link/token/create`, products: `auth`).
   - `POST /plaid/exchange` — exchange the public token from Plaid Link
     for an access token server-side, fetch account/routing numbers via
     `/auth/get`, store a `LinkedBankAccount` row (masked only), and
     securely cache what's needed to call Unit's ACH payment **at
     send-time** (e.g. re-fetch via Plaid's access token right before
     calling Unit, rather than ever persisting the raw numbers).
3. `POST /funding/add-funds` requires `linked_account_id` instead of
   raw routing/account numbers, looks up the verified `LinkedBankAccount`
   owned by `current_user`, fetches numbers from Plaid just-in-time, and
   only then calls `unit_svc.create_ach_funding_payment`.
4. Remove the raw-routing/account-number fields from
   `schemas.AddFundsRequest` entirely once the Plaid path replaces them
   — don't leave a parallel unverified path alive.
5. Add `plaid_client_id` / `plaid_secret` / `plaid_env` to `config.py`
   and `.env.example` (sandbox/development/production tiers, matching
   the existing `UNIT_BASE_URL` sandbox/production pattern).
6. Tests (new `tests/test_plaid.py` + updates to `tests/test_funding.py`):
   monkeypatch the Plaid client the same way `unit_svc` calls are
   mocked elsewhere in this test suite. Cover: link-token creation,
   exchange + verified-account creation, add-funds succeeding with a
   verified linked account, add-funds rejecting an unverified or
   someone-else's linked account.
7. Run `python -m pytest tests/ -q` — must be 100% green. Validate no
   plaintext routing/account numbers are ever written to the DB (grep
   your own diff for it before committing).

## Out of scope

- Don't touch `services/external_send.py` (Tier 2) — unrelated, still
  blocked on a separate decision.
- Don't change the `$500`/`$1,000` (or whatever current) funding caps in
  `_check_limits` unless you have a concrete reason tied to this task.
- Don't enable `ALLOW_UNVERIFIED_ACH_FUNDING` as a way to "test it
  works" — Add Funds should work because verification now exists, not
  because the safety flag was bypassed.
