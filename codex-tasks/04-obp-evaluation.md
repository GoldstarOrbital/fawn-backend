# Task: Evaluate Open Bank Project (OBP) as a fit for FAWN — research only, no integration yet

## Context

FAWN currently uses Unit (unit.co) as its BaaS provider — Unit holds the
sponsor-bank relationship that lets FAWN offer FDIC-insured checking
accounts, and `services/unit.py` is built against Unit's REST API
(applications/KYC, deposit accounts, Book Payments, virtual cards, ACH).
That integration already works end-to-end in Unit's sandbox.

Someone suggested looking at Open Bank Project (OBP) — described as "a
leading open source banking API platform that enables banks and
developers to securely access accounts, transactions, payments, and
other financial services."

**Important distinction to verify, not assume**: from what's publicly
known, OBP is primarily an open-source API *abstraction/middleware
layer* that banks deploy in front of their own core banking systems —
it standardizes how a bank exposes its existing accounts/transactions/
payments as APIs. That's a different role than Unit, which itself *is*
the sponsor-bank relationship and issues real FDIC-insured deposit
accounts on FAWN's behalf. It is not yet confirmed whether OBP (a) has
its own sponsor-bank partnerships a startup like FAWN could plug into
the way it plugs into Unit, (b) only works if you already have a bank
relationship and want to expose your own APIs, or (c) something else
entirely. Do not assume either way — find out and report back.

## Goal

Produce a short written evaluation (`docs/OBP_EVALUATION.md`), **not**
any integration code, answering:

1. What is OBP's actual business model and architecture? Is it
   something a non-bank startup can integrate with directly to get
   FDIC-insured deposit accounts for its users (i.e., a Unit
   competitor), or does it require FAWN to already have its own
   chartered-bank or sponsor-bank relationship and just want a
   standardized API layer on top of it?
2. Does OBP (or its public sandbox) offer anything resembling Unit's
   KYC/account-opening/Book-Payment/card-issuance flow that FAWN
   actually uses today, or is its sandbox purely mock/demo data with no
   real path to issuing real accounts?
3. If OBP turns out to be a genuine sponsor-bank-equivalent option:
   how does its onboarding/approval process, pricing, and API surface
   compare to Unit's, concretely (cite what you find, don't speculate)?
4. If OBP turns out to be the middleware-only thing described above
   (most likely based on its public description): is there still a
   legitimate use for it in FAWN's stack — e.g., as an internal API
   standardization layer if FAWN ever adds a second BaaS/bank partner
   — or is it simply not applicable here? Say so plainly if the answer
   is "not applicable."
5. Bottom-line recommendation: stick with Unit, evaluate OBP further
   with a real sandbox account, or some third option. One paragraph,
   no hedging.

## Constraints

- **No code changes in this task.** This is research and a written
  recommendation only — explicitly do not touch `services/unit.py`,
  add an `services/obp.py`, or wire anything into routers.
- Do not write anything that implies FAWN already has, or is close to
  having, an OBP-backed banking relationship — only report what you can
  actually verify (OBP's docs, public sandbox, pricing/partner pages).
- If you can't get a clear answer to question 1 from public sources,
  say that explicitly in the evaluation rather than guessing — this is
  exactly the kind of thing covered by "don't guess on anything
  involving money movement or banking-partner status."
