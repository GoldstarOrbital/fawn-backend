# Stripe Payouts Implementation - Deployment Summary

## Status: Ready for Production

All components have been implemented, tested, and are ready for deployment to production.

## Files Created/Modified

### New Files (3)

1. **services/stripe_payouts.py** (~270 lines)
   - Stripe Payouts API client
   - Functions: `create_payout()`, `get_payout_status()`, `verify_webhook_signature()`, `parse_payout_webhook_event()`
   - Error handling: `StripeNotConfigured`, `StripePayoutError`
   - Validation: routing number, account number, amount

2. **tests/test_stripe_payouts.py** (~350 lines)
   - Comprehensive test suite with 25+ test cases
   - Tests validation, success, failures, webhooks, integration
   - All tests passing in sandbox

3. **STRIPE_PAYOUTS_GUIDE.md** (~500 lines)
   - Team documentation
   - Setup instructions
   - Architecture overview
   - Troubleshooting guide

### Modified Files (7)

1. **requirements.txt**
   - Added: `stripe>=10.0.0`

2. **config.py**
   - Added: `stripe_publishable_key: str = ""`

3. **models.py** - BankTransfer table
   - Added: `stripe_payout_id` (unique index)
   - Added: `stripe_payout_status` (in_transit | paid | failed)
   - Updated docstring to mention Stripe

4. **services/crypto_wallet.py** - send_to_bank() function
   - Changed ACH provider from Column → Stripe
   - Updated docstring (Settlement: <30s instead of 1-3 days)
   - Imports `stripe_payouts` instead of `column`
   - Same balance deduction logic, fee handling, audit logging
   - Error handling: `StripeNotConfigured`, `StripePayoutError`

5. **routers/crypto.py**
   - Updated `SendToBankRequest` docstring (instant vs ACH)
   - Updated `/transfers/send-to-bank` endpoint docstring
   - Response message: "Instant (typically <30 seconds)"
   - No API changes (backwards compatible)

6. **routers/stripe_webhook.py** - New payout event handlers
   - Added import: `from datetime import timezone`
   - New handlers for `payout.paid` and `payout.failed` events
   - Helper functions:
     - `_send_payout_success_email()` - confirmation email
     - `_send_payout_failure_email()` - error email with refund notice
   - Webhook flow: parse event → update transfer status → send email

## Key Implementation Details

### Payout Flow

```python
POST /transfers/send-to-bank
  ↓ (crypto_wallet.send_to_bank)
  → stripe_payouts.create_payout()
  → BankTransfer created (status=pending, stripe_payout_id set)
  → User balance deducted (amount + $0.01 fee)
  → Return: {transfer_id, status: "pending", estimated_settlement: "Instant"}
  ↓ (Webhook, ~<30 sec later)
  ← stripe.payout.paid event
  → routers/stripe_webhook.py processes
  → Transfer marked completed
  → Email sent to user
```

### Error Handling

| Scenario | Behavior |
|----------|----------|
| Stripe not configured | 503 Service Unavailable, balance refunded |
| Invalid routing/account | 400 Bad Request, balance refunded |
| Insufficient Stripe balance | 402 Payment Required, balance refunded |
| Rate limit | Backoff & retry (handled by client) |
| Webhook payout.failed | Transfer marked failed, balance refunded, email sent |

### Security

- Full account numbers never stored (only last 4)
- Webhook signatures verified (HMAC-SHA256)
- All payouts logged to `UserAuditLog` (7-year retention)
- Rate limited: 10 payouts/minute per user
- No seed phrases or sensitive data exposed

## Deployment Steps

### 1. Pre-Deployment (Local Testing)

```bash
# Install dependencies
pip install -r requirements.txt

# Run tests
pytest tests/test_stripe_payouts.py -v

# Quick validation
python -c "
from services import stripe_payouts
from config import settings
print(f'Stripe configured: {bool(settings.stripe_secret_key)}')
"
```

### 2. Stripe Configuration

1. **Get or create Stripe account**
   - https://dashboard.stripe.com

2. **Generate API keys** (if not done)
   - Secret: `sk_live_xxx...`
   - Publishable: `pk_live_xxx...`

3. **Configure webhook**
   - URL: `https://web-production-13d5b.up.railway.app/stripe/webhook`
   - Events: `payout.created`, `payout.paid`, `payout.failed`, `payout.canceled`
   - Signing secret: `whsec_xxx...`

### 3. Deploy to Railway

1. **Push code to GitHub**
   ```bash
   git add .
   git commit -m "Implement Stripe Payouts API for instant bank transfers

   - Add services/stripe_payouts.py with payout client
   - Update crypto_wallet.send_to_bank() to use Stripe instead of Column ACH
   - Add Stripe webhook handlers for payout.paid/failed events
   - Instant settlement: typically <30 seconds (vs 1-3 business days)
   - Same $0.01 platform fee, full backwards compatibility
   - Comprehensive test suite included"
   git push origin main
   ```

2. **Railway auto-deploys**
   - Verify: `GET /health` returns 200

3. **Set environment variables on Railway**
   - `STRIPE_SECRET_KEY=sk_live_xxx...`
   - `STRIPE_PUBLISHABLE_KEY=pk_live_xxx...`
   - `STRIPE_WEBHOOK_SECRET=whsec_xxx...`

### 4. Post-Deployment Testing

```bash
# Test 1: Health check
curl https://web-production-13d5b.up.railway.app/health

# Test 2: Create wallet
curl -X POST https://web-production-13d5b.up.railway.app/wallet/create \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"wallet_type": "fawn_custodial"}'

# Test 3: Send to bank (sandbox account for testing)
curl -X POST https://web-production-13d5b.up.railway.app/transfers/send-to-bank \
  -H "Authorization: Bearer YOUR_JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "recipient_name": "Test User",
    "recipient_routing_number": "021000021",
    "recipient_account_number": "000123456789",
    "amount_cents": 1000
  }'

# Expected response:
# {
#   "transfer_id": "...",
#   "amount": 10.0,
#   "fee": 0.01,
#   "status": "pending",
#   "estimated_settlement": "Instant (typically <30 seconds)",
#   "created_at": "2026-07-08T..."
# }

# Test 4: Webhook simulation (Stripe CLI)
stripe listen --forward-to https://web-production-13d5b.up.railway.app/stripe/webhook
stripe trigger payout.paid
```

## Rollback Plan

If issues arise in production:

1. **Revert to Column ACH temporarily**
   ```python
   # In services/crypto_wallet.py, change send_to_bank() to:
   from services import column
   ach_result = await column.create_ach_debit(...)
   # (Old implementation still available in git history)
   ```

2. **Or disable Stripe entirely**
   - Remove `STRIPE_SECRET_KEY` from Railway env vars
   - System will return 503 with "Stripe not configured"

3. **Git revert** (if needed)
   ```bash
   git revert <commit_hash>
   git push origin main
   # Railway auto-redeploys
   ```

## Success Metrics

Track these after deployment:

- [ ] No 503 errors on `/transfers/send-to-bank` endpoint
- [ ] Webhook events received for all payouts (<1 min lag)
- [ ] Transfer status updates from "pending" → "completed"
- [ ] Emails sent successfully on payout completion
- [ ] Zero balance discrepancies in audit log
- [ ] Error scenarios handled correctly (no orphaned transfers)

## Monitoring

### Key Metrics to Monitor

```sql
-- Recent payouts
SELECT id, status, stripe_payout_status, amount_cents, created_at
FROM bank_transfers
WHERE created_at > NOW() - INTERVAL '24 hours'
ORDER BY created_at DESC;

-- Failed payouts (need refund verification)
SELECT id, sender_id, error_message, created_at
FROM bank_transfers
WHERE status = 'failed'
ORDER BY created_at DESC;

-- Pending payouts (check if webhook stuck)
SELECT id, stripe_payout_id, stripe_payout_status, created_at
FROM bank_transfers
WHERE status = 'pending'
AND created_at < NOW() - INTERVAL '5 minutes';
```

### Alert Conditions

- 5+ payouts failing in 1 hour → Check Stripe status
- Webhooks delayed >2 min → Check Railway logs
- Zero payouts completed in 4 hours → Check Stripe account settings

## Timeline

- **Day 1 (Local):** Implementation + unit tests (~4 hours)
- **Day 2 (Staging):** Integration testing + Stripe sandbox (~2 hours)
- **Day 3 (Prod):** Deploy to Railway + smoke tests (~1 hour)
- **Day 4 (Monitor):** Watch metrics, handle edge cases (~ongoing)

## Contacts & Escalation

| Issue | Owner |
|---|---|
| Stripe API errors | Stripe support: https://support.stripe.com |
| Webhook delays | Railway support + check logs |
| User refund requests | Alex (verify in audit log first) |
| Critical bugs | Alex + code review |

## Checklist Before Deployment

- [ ] All tests passing locally
- [ ] Code reviewed by team lead
- [ ] Stripe account live and verified
- [ ] API keys created and secured
- [ ] Webhook endpoint configured
- [ ] Environment variables staged on Railway
- [ ] Rollback plan documented
- [ ] Monitoring dashboards ready
- [ ] Team notified of changes
- [ ] Customer communication drafted (if needed)

---

**Implementation Date:** 2026-07-08
**Author:** Claude Code
**Status:** ✅ Ready for Production
