# Task: Unit sandbox-to-production cutover runbook + config audit

## Context

FAWN currently runs entirely against Unit's sandbox environment
(`UNIT_BASE_URL=https://api.s.unit.sh`). Going live on real banking
requires Unit's own underwriting/partner-approval process (a business
process outside this codebase — don't attempt to automate or fake any
part of that), but the **codebase itself** needs to be cutover-ready so
the only things that change on launch day are environment variables and
a couple of one-time manual steps. This task is about producing that
readiness, not about flipping the switch.

## Goal

Produce two things:

1. `docs/UNIT_PRODUCTION_CUTOVER.md` — a step-by-step runbook a non-coder
   (Alex) can follow on launch day. Structure:
   - **Pre-launch (Unit-side, do once approval lands)**: register the
     production webhook URL in Unit's dashboard, generate a production
     webhook signing secret, generate a production API token, confirm
     which deposit product/customer types are approved.
   - **Railway env var changes**: exact list of every env var that needs
     a new value for production vs. sandbox (audit `config.py` and
     `.env.example` yourself — list every Unit-related setting plus
     `ALLOW_UNVERIFIED_ACH_FUNDING`, `ALLOW_UNSIGNED_UNIT_WEBHOOKS`,
     `ALLOW_UNSIGNED_STRIPE_WEBHOOKS`, confirming each defaults safely
     and what its production value must be).
   - **Pre-flight checklist**: things to manually verify before sending
     real traffic (webhook signature actually verifies against a real
     Unit-sent test event, `approve_application_sandbox` is never
     reachable/callable against a non-sandbox `UNIT_BASE_URL` — confirm
     this guard exists or add one, KYC flow tested against a real
     low-dollar test identity per Unit's guidance if they provide one).
   - **Rollback plan**: what to do if production Unit calls start
     failing after cutover (how to point `UNIT_BASE_URL` back to
     sandbox, what user-facing state that leaves behind, whether
     in-flight FundingRequests/P2PTransfers need manual reconciliation).

2. A config-safety audit as actual code changes (small, low-risk):
   - Find every place `settings.unit_base_url` is checked or assumed and
     confirm sandbox-only code paths (e.g.
     `unit_svc.approve_application_sandbox`, any other
     sandbox-simulation endpoint) are hard-blocked when
     `unit_base_url` doesn't look like a sandbox URL — add the guard if
     missing, with a test proving it 403s/blocks in a simulated
     production config.
   - Confirm `ALLOW_UNSIGNED_UNIT_WEBHOOKS`,
     `ALLOW_UNSIGNED_STRIPE_WEBHOOKS`, and
     `ALLOW_UNVERIFIED_ACH_FUNDING` all default to `false` (they
     currently do per `config.py` — just re-verify, this is a
     regression-proofing pass) and add a single test per flag asserting
     the default is `false` if one doesn't already exist, so a future
     change can't silently flip a safety default.

## Constraints

- Don't touch any actual money-movement logic (`p2p.py`, `funding.py`,
  `unit.py`'s payment functions) beyond the sandbox-guard addition
  described above.
- Don't write anything that pretends Unit production approval has
  happened, or that the app is production-ready today — the runbook
  should be honest that it documents a *future* step, not a completed one.
- Run `python -m pytest tests/ -q` after any code change — must be 100%
  green.

## Out of scope

- Tasks 01 (Plaid) and 02 (Unit service resilience) in this same
  directory — don't duplicate or depend on their work, this task should
  produce value standalone even if those haven't landed yet.
