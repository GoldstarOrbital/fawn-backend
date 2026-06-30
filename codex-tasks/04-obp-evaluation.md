# Closed: Open Bank Project (OBP) is not a fit for FAWN

## Status: answered, no further work needed

Someone suggested Open Bank Project (OBP) as a possible banking
partner alongside/instead of Unit. Verdict, based on OBP's own public
description: **it isn't a Unit alternative and there's nothing to
integrate.**

## Why

OBP is an open-source API *middleware/abstraction layer* that an
existing bank deploys in front of its own core banking system, to
standardize how it exposes accounts/transactions/payments —
particularly for European-style regulatory regimes (Berlin Group, UK
Open Banking, PSD2, plus Mexico/Brazil/Bahrain/Saudi/Australia). It
gives developers a consistent API to *read and act on accounts that
already exist at a bank that has deployed OBP*. It does not:

- hold a sponsor-bank relationship of its own,
- issue new FDIC-insured deposit accounts to a fintech's end users,
- run KYC/account-opening, Book Payments, or card issuance the way
  Unit does for FAWN today.

It's closer in category to Plaid/Tink/TrueLayer/Yodlee/MX (account
access/aggregation tools) than to Unit/Synapse/Treasury Prime
(BaaS/sponsor-bank platforms). Unit is what gives FAWN the actual
ability to open real, FDIC-insured checking accounts for students —
OBP doesn't replace that role, and there's no evidence it operates in
the US market FAWN needs (its named standards are all
European/UK/LatAm/Middle East/Australia-focused; no US mention).

## What to actually do with this

Nothing — FAWN already has the right tool in this category queued up:
`codex-tasks/01-plaid-account-verification.md` (verifying ownership of
a user's *external* bank account before pulling funds via ACH) is the
correct use of an account-access API like this, and Plaid is the
standard choice for that in the US. Don't substitute OBP for it.

If FAWN ever expands into a market where OBP's standards apply (EU/UK
Open Banking, etc.) and needs to read balances/transactions at a
partner bank that has deployed OBP, this could become relevant again
then — not before.
