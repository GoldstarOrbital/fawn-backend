# Task: Make services/unit.py resilient to Unit API failures

## Context

FAWN is a pre-launch fintech app on Unit BaaS (sandbox: `api.s.unit.sh`).
Every call to Unit in `services/unit.py` follows the same shape:

```python
async with httpx.AsyncClient(timeout=N) as client:
    resp = await client.post(...)
    resp.raise_for_status()
    return resp.json()["data"]
```

No retries, no distinction between "Unit is down/slow" (retry-safe) vs
"Unit rejected this request" (not retry-safe, surface to the user), and
inconsistent error handling upstream — some routers catch and convert to
a clean `502` (`cards.py`'s create/freeze/unfreeze), one logs-and-skips
per-item (`cards.py`'s `list_my_cards`), but most callers just let the
`httpx.HTTPStatusError` propagate as an unhandled 500. As FAWN moves
toward real production banking traffic, an unhandled Unit timeout or a
transient 503 from their side shouldn't surface as a raw 500 to a user
mid-payment, and silent failures need to be loggable/debuggable.

## Goal

Add consistent, conservative resilience to `services/unit.py` without
changing any function's external behavior/return shape on the success
path, and without introducing retry logic on non-idempotent calls that
could cause a double-send.

## Constraints (read carefully — this touches money movement)

- **Never retry `create_book_payment`, `create_ach_funding_payment`,
  `create_virtual_card`, or `create_application` on ambiguous failures**
  (timeout, connection error) **unless the call already has an
  Idempotency-Key header** (the payment/card calls do — confirm before
  retrying) **and you're retrying the literal same payload**. A
  timeout doesn't mean the request didn't land at Unit's side — retrying
  a non-idempotent call on timeout risks a double-charge. Read-only
  calls (`get_account_balance`, `list_transactions`, `get_card`,
  `list_cards`, `get_application`, `get_customer_accounts`,
  `get_account_details`) are always safe to retry.
- Distinguish in the raised exception (or a typed return) between: Unit
  rejected the request (4xx — don't retry, this is a real validation
  failure to surface to the user) vs. a transient failure (timeout,
  connection error, 5xx — safe to retry idempotent calls, log loudly on
  non-idempotent ones).
- Add a short bounded retry (e.g. 2 retries, exponential backoff,
  total budget under ~10s) only for the read-only calls listed above.
- Every Unit call should log the failure with enough context to debug
  in production (endpoint, status code if any, truncated body) — mirror
  the `[prefix] ...` print-logging pattern already used in
  `routers/auth.py`, `routers/cards.py`, `routers/email_automation.py`
  in this codebase. Don't introduce a new logging library/pattern.
- Don't swallow exceptions silently anywhere they currently propagate —
  if a caller needs the error to convert it into an HTTP response (like
  `cards.py` does), it must still be able to catch a real exception.

## Suggested approach

1. Add a small internal helper (e.g. `_request_with_retry(client, method,
   url, *, json=None, headers=None, retryable: bool)`) used by every
   function in the file, replacing the repeated
   `async with httpx.AsyncClient... resp.raise_for_status()` blocks.
2. Define a `UnitAPIError(Exception)` (carries status_code + body) so
   callers can distinguish "Unit said no" from a generic exception if
   they want to (optional — only add this if it doesn't require touching
   every router; a clean log + re-raise of the underlying httpx error is
   an acceptable minimal version).
3. Apply retry=True only to the read-only functions listed above;
   retry=False (single attempt, just better logging) to everything else.
4. Update/add tests in `tests/test_*.py` (there's likely no dedicated
   `test_unit_service.py` yet — check, add one if not) that monkeypatch
   `httpx.AsyncClient` or the new helper to simulate a timeout-then-success
   for a read-only call, and confirm a payment call does NOT get retried
   on timeout.
5. Run `python -m pytest tests/ -q` — must be 100% green.

## Out of scope

- Don't change any router's business logic, only how `services/unit.py`
  surfaces failures.
- Don't add a circuit breaker, queueing, or async job system — that's a
  bigger architectural change than this task; flag it as a follow-up
  idea in your PR description if you think it's warranted, don't build it.
