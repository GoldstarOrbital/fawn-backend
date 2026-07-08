# Stripe Payouts API Integration Guide

## Overview

FAWN now supports **instant bank transfers** via Stripe Payouts API, replacing the slower Column ACH system (1-3 business days).

**Key Benefits:**
- Settlement in <30 seconds (vs 1-3 business days)
- Same $0.01 platform fee
- Fully backwards-compatible with existing wallet architecture
- Webhook-driven status tracking

## Architecture

### Data Flow

```
User calls POST /transfers/send-to-bank
    ↓
API validates routing/account
    ↓
services/crypto_wallet.send_to_bank() called
    ↓
services/stripe_payouts.create_payout() initiates Stripe transfer
    ↓
BankTransfer record created (status=pending, stripe_payout_id set)
    ↓
User's USDC balance deducted (amount + $0.01 fee)
    ↓
Webhook: Stripe sends payout.paid or payout.failed event
    ↓
routers/stripe_webhook.py processes event
    ↓
Transfer marked completed or failed + email sent to user
```

### Database Schema

**BankTransfer table** now includes:

```python
stripe_payout_id: str = Column(String, nullable=True, unique=True, index=True)
stripe_payout_status: str = Column(String, nullable=True)  # in_transit | paid | failed
```

Status lifecycle:
- `pending` → `in_transit` (Stripe processing)
- `in_transit` → `paid` (money arrived, via webhook)
- `pending` → `failed` (error, via webhook)

## Implementation Details

### Key Files

| File | Purpose |
|---|---|
| `services/stripe_payouts.py` | Stripe Payouts API client (new) |
| `services/crypto_wallet.py` | Updated `send_to_bank()` to use Stripe |
| `routers/crypto.py` | Updated response messages |
| `routers/stripe_webhook.py` | Webhook handler for payout events (new handlers) |
| `models.py` | BankTransfer fields for Stripe tracking |
| `config.py` | Stripe configuration (already added) |
| `requirements.txt` | Added `stripe>=10.0.0` |

### stripe_payouts.py API

```python
# Create a payout
result = await stripe_payouts.create_payout(
    amount_cents=50000,  # $500
    recipient_name="John Doe",
    recipient_routing_number="021000021",
    recipient_account_number="123456789",
    metadata={"fawn_transfer_id": "..."}
)
# Returns: {"payout_id": "po_...", "status": "in_transit", ...}

# Check payout status
status = stripe_payouts.get_payout_status("po_...")
# Returns: {"payout_id": "po_...", "status": "paid", ...}

# Verify webhook signature
is_valid = stripe_payouts.verify_webhook_signature(
    payload,
    signature_header,
    webhook_secret,
)

# Parse webhook event
event_data = stripe_payouts.parse_payout_webhook_event(event_dict)
# Returns: {"payout_id": "po_...", "event_type": "payout.paid", ...}
```

## Setup & Configuration

### 1. Stripe Account Setup

1. **Upgrade Sandbox to Live** (or use existing live account)
   - https://dashboard.stripe.com/settings/account
   - Verify: Account capabilities → Payouts enabled

2. **Create API Keys** (if not already done)
   - https://dashboard.stripe.com/apikeys
   - Copy **Secret Key** (starts with `sk_live_`)
   - Copy **Publishable Key** (starts with `pk_live_`)

3. **Configure Webhook Endpoint**
   - https://dashboard.stripe.com/webhooks
   - URL: `https://web-production-13d5b.up.railway.app/stripe/webhook`
   - Events: `payout.created`, `payout.paid`, `payout.failed`, `payout.canceled`
   - Copy **Signing Secret** (starts with `whsec_`)

### 2. Update Environment Variables (Railway)

Set these in Railway project variables:

```env
STRIPE_SECRET_KEY=sk_live_xxxxxxx
STRIPE_PUBLISHABLE_KEY=pk_live_xxxxxxx
STRIPE_WEBHOOK_SECRET=whsec_xxxxxxx
```

Or locally in `.env`:

```env
STRIPE_SECRET_KEY=sk_test_xxxxxxx
STRIPE_PUBLISHABLE_KEY=pk_test_xxxxxxx
STRIPE_WEBHOOK_SECRET=whsec_test_xxxxxxx
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
# or just:
pip install stripe>=10.0.0
```

### 4. Test in Sandbox

```python
import stripe
from services import stripe_payouts

stripe.api_key = "sk_test_..."

# Test successful payout
result = stripe_payouts.create_payout(
    amount_cents=1000,  # $10
    recipient_name="Test User",
    recipient_routing_number="021000021",  # valid routing
    recipient_account_number="000123456789",  # test success account
)
print(f"Payout ID: {result['payout_id']}")
print(f"Status: {result['status']}")

# Test failed payout
try:
    result = stripe_payouts.create_payout(
        amount_cents=1000,
        recipient_name="Test User",
        recipient_routing_number="021000021",
        recipient_account_number="000111111116",  # test failure account
    )
except stripe_payouts.StripePayoutError as e:
    print(f"Expected error: {e}")
```

**Test Bank Account Numbers** (Stripe Sandbox):
- `000123456789` → **Success** (any routing)
- `000111111116` → **Fails** (decline)

See: https://stripe.com/docs/testing#bank-account-numbers

## Error Handling

### Common Error Scenarios

| Error | Cause | User Experience |
|---|---|---|
| `StripeNotConfigured` | Stripe keys missing | 503 Service Unavailable |
| `StripePayoutError("Invalid routing")` | Bad routing format | 400 Bad Request |
| `StripePayoutError("Rate limited")` | Too many requests | Backoff & retry |
| Webhook `payout.failed` | Invalid account, insufficient funds | Refund to wallet + email |

### Webhook Failure Handling

If a webhook fails (network timeout, etc.):
- Stripe retries for 3 days
- Our idempotency check (via event ID) prevents duplicates
- Manual recovery: check `BankTransfer.stripe_payout_status` in DB

## Security Considerations

### PII Protection

- **Never log full account numbers** — only last 4 stored in DB
- Full details sent directly to Stripe (never touch our servers beyond initial validation)
- Routing number stored for audit trail (only last 4 displayed to user)

### Webhook Security

- All Stripe webhook signatures verified via HMAC-SHA256
- `verify_webhook_signature()` must be called before processing
- If verification fails, return 400 (Stripe will retry)

### Rate Limiting

Existing rate limiter applies: `10 payouts/minute` per user (configured in `rate_limiting.py`)

### Audit Trail

Every payout logged to `UserAuditLog` with:
- Recipient name (last 4 only)
- Routing/account last 4
- Amount & fee
- BankTransfer ID
- 7-year retention period

## Monitoring & Debugging

### Check Payout Status

Query DB for recent payouts:

```python
# In Flask/FastAPI shell
from models import BankTransfer
from database import get_db

db = next(get_db())
recent = db.query(BankTransfer).filter(
    BankTransfer.created_at > datetime.now() - timedelta(hours=1)
).all()

for bt in recent:
    print(f"{bt.id}: {bt.status} (Stripe: {bt.stripe_payout_status})")
    print(f"  Amount: ${bt.amount_cents / 100:.2f}")
    print(f"  Payout ID: {bt.stripe_payout_id}")
```

### Manual Payout Status Check

```python
from services import stripe_payouts

status = stripe_payouts.get_payout_status("po_xxx")
print(f"Stripe status: {status['status']}")
print(f"Amount: ${status['amount_cents'] / 100:.2f}")
if status['failure_code']:
    print(f"Error: {status['failure_code']}")
```

### Webhook Testing

Use Stripe CLI to forward webhooks locally:

```bash
# Install: https://stripe.com/docs/stripe-cli
stripe login
stripe listen --forward-to localhost:8000/stripe/webhook

# In another terminal, send test event:
stripe trigger payout.paid
```

## Migration Notes

### From Column ACH to Stripe

- **Existing transfers on ACH remain** (no migration needed)
- **New transfers use Stripe Payouts** (via `send_to_bank()`)
- **No user-facing changes** — same endpoint, instant vs slow

### Backwards Compatibility

- Column ACH code still exists (unused)
- Can revert to Column by changing `send_to_bank()` import
- Both systems can run in parallel during transition

## Troubleshooting

### Webhook not received

1. Check Stripe dashboard → Webhooks → Recent deliveries
2. Verify webhook URL is correct and accessible
3. Check for timeouts (Railway may need config)
4. Ensure `STRIPE_WEBHOOK_SECRET` is set

### Payout marked failed but user not refunded

1. Check `UserAuditLog` for the payout event
2. Verify webhook was processed (`routers/stripe_webhook.py` logs)
3. Manual refund:
   ```python
   bank_transfer = db.query(BankTransfer).filter(
       BankTransfer.id == "transfer_id"
   ).first()
   user = db.query(User).filter(User.id == bank_transfer.sender_id).first()
   total_refund = bank_transfer.amount_cents + bank_transfer.fee_cents
   user.usdc_balance_cents += total_refund
   user.total_fees_paid_cents -= bank_transfer.fee_cents
   db.commit()
   ```

### Rate limit errors

- Stripe enforces limits per account (typically 1000 req/sec)
- User sees 429 Too Many Requests
- Backoff and retry in 60 seconds

## Testing

Run the test suite:

```bash
pytest tests/test_stripe_payouts.py -v

# Test specific scenario:
pytest tests/test_stripe_payouts.py::TestStripePayoutsCreation::test_create_payout_success -v

# With coverage:
pytest tests/test_stripe_payouts.py --cov=services.stripe_payouts
```

## Deployment Checklist

- [ ] Stripe live account created and verified
- [ ] API keys generated and set in Railway
- [ ] Webhook endpoint configured in Stripe dashboard
- [ ] `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET` set on Railway
- [ ] Tested with sandbox account first
- [ ] Tested end-to-end: send → webhook → email
- [ ] Failure scenario tested (invalid account)
- [ ] Audit logging verified in DB
- [ ] Rate limiting tested
- [ ] Documentation reviewed with team
- [ ] Rollback plan in place (revert to Column if needed)

## Support & Escalation

| Issue | Contact |
|---|---|
| Stripe API limits | Stripe support portal |
| Webhook delays | Check Railway logs |
| User refund needed | Check `UserAuditLog` + manual refund |
| Account issues | Alex (alexmarcusgoldsmith@gmail.com) |

## References

- [Stripe Payouts API](https://stripe.com/docs/api/payouts)
- [Stripe Testing](https://stripe.com/docs/testing)
- [Webhook Security](https://stripe.com/docs/webhooks/signatures)
- [Bank Account Tokens](https://stripe.com/docs/api/bank_accounts)
