# FAWN Debit Card Authorization System

Complete backend implementation for instant USDC-backed virtual debit card issuance and real-time authorization.

## Architecture Overview

```
User Flow:
  1. POST /card/issue → Virtual card issued instantly via Lithic
  2. Card linked to USDC wallet (1:1)
  3. User makes purchase → Merchant → Lithic processor
  4. Lithic webhook → POST /card/authorize-transaction
  5. Balance check (<100ms) → Approve/Decline → Response to processor (1s timeout)
  6. Transaction recorded with idempotency key
  7. Audit log created (7-year retention)

Processor Outage Fallback:
  - Balance check timeout (>100ms) → Tentatively approve + manual review flag
  - Processor unavailable → Use manual review fallback (approve, queue review)
  - Webhook duplicates → Deduplicated by idempotency key (cached response)
```

## Database Schema

### Cards Table
```sql
-- Virtual debit card linked 1:1 to USDC wallet
CREATE TABLE cards (
  id VARCHAR(36) PRIMARY KEY,
  user_id VARCHAR(36) NOT NULL,
  lithic_card_token VARCHAR(255) UNIQUE NOT NULL,
  card_last_four VARCHAR(4) NOT NULL,
  wallet_address VARCHAR(255) NOT NULL,
  status ENUM('PENDING','ACTIVE','FROZEN','SUSPENDED','REVOKED'),
  daily_limit_cents INT DEFAULT 1000000,     -- $10,000/day
  monthly_limit_cents INT DEFAULT 30000000,  -- $300,000/month
  transaction_limit_cents INT DEFAULT 100000, -- $1,000/txn
  issued_at DATETIME NOT NULL,
  activated_at DATETIME,
  frozen_at DATETIME,
  requires_manual_review BOOLEAN DEFAULT FALSE,
  INDEX (user_id, status),
  INDEX (wallet_address),
  UNIQUE (user_id, lithic_card_token)
);
```

### Card Transactions Table (Idempotent)
```sql
-- Transaction record with duplicate prevention
CREATE TABLE card_transactions (
  id VARCHAR(36) PRIMARY KEY,
  processor_transaction_id VARCHAR(255) UNIQUE NOT NULL,  -- Lithic txn ID
  idempotency_key VARCHAR(255) UNIQUE NOT NULL,            -- Merchant trace (retry-safe)
  card_id VARCHAR(36) NOT NULL,
  merchant_name VARCHAR(255) NOT NULL,
  transaction_amount_cents INT NOT NULL,
  usdc_amount_cents INT NOT NULL,
  status ENUM('PENDING','AUTHORIZED','DECLINED','APPROVED','SETTLED'),
  wallet_balance_at_auth_cents INT,
  auth_approved BOOLEAN DEFAULT FALSE,
  auth_decline_reason VARCHAR(255),
  requested_at DATETIME NOT NULL,
  authorized_at DATETIME,
  fallback_status VARCHAR(50),  -- 'processor_timeout', 'processor_error'
  requires_manual_review BOOLEAN DEFAULT FALSE,
  INDEX (user_id, status),
  INDEX (card_id, status),
  INDEX (requested_at),
  UNIQUE (processor_transaction_id),
  UNIQUE (idempotency_key)
);
```

### Card Audit Logs Table (7-Year Retention)
```sql
-- Immutable audit trail for compliance
CREATE TABLE card_audit_logs (
  id VARCHAR(36) PRIMARY KEY,
  event_type ENUM('CARD_ISSUED','AUTH_APPROVED','AUTH_DECLINED','FALLBACK_MANUAL_REVIEW',...),
  entity_type VARCHAR(50),     -- 'card' or 'transaction'
  entity_id VARCHAR(36),
  user_id VARCHAR(36),
  actor_type VARCHAR(50),      -- 'user', 'processor', 'admin', 'system'
  details JSON,                -- Full event snapshot
  created_at DATETIME NOT NULL,
  retention_until DATETIME NOT NULL,  -- 7 years out
  processor_event_id VARCHAR(255) UNIQUE,
  INDEX (user_id, event_type),
  INDEX (retention_until)  -- For cleanup queries
);
```

## API Endpoints

### 1. POST /card/issue
**Issue virtual card instantly**

```bash
curl -X POST https://api.fawn.app/card/issue \
  -H "Authorization: Bearer <JWT_TOKEN>" \
  -H "Content-Type: application/json"
```

**Response (201):**
```json
{
  "status": "success",
  "data": {
    "card_id": "550e8400-e29b-41d4-a716-446655440000",
    "card_last_four": "4242",
    "status": "active",
    "issued_at": "2026-07-08T12:00:00Z",
    "lithic_card_token": "evm2T7p8qEiMvvS1oZnZDznVTuVkAGX3"
  }
}
```

**Error Responses:**
- `400` - Wallet not initialized
- `429` - Rate limit (max 1 card/day per user)
- `503` - Lithic processor unavailable

---

### 2. POST /card/authorize-transaction (Webhook)
**Called by Lithic processor ~1000x/day at scale**

**Lithic sends:**
```bash
POST https://yourapi.com/card/authorize-transaction
X-Lithic-Signature: d47d1c...
Content-Type: application/json

{
  "type": "card_transaction.updated",
  "data": {
    "token": "evm2T7p8qEiMvvS1oZnZDznVTuVkAGX3",
    "events": [
      {
        "type": "AUTHORIZATION",
        "amount": 10050,
        "merchant": {
          "merchant_name": "Starbucks",
          "mcc_code": "5461"
        },
        "network_identifiers": {
          "processor_transaction_id": "xyz123456",
          "trace_number": "merchant_trace_789"
        }
      }
    ]
  }
}
```

**API Response (must return <1s):**
```json
{
  "approved": true,
  "decline_reason": null,
  "processor_response_code": "00"
}
```

**Error Responses:**
- `200` - Authorization decision sent (approve/decline)
- `202` - Async fallback used (manual review queued)
- `400` - Invalid payload
- `401` - Invalid signature
- `503` - Internal error (processor will retry)

**Key Features:**
- **Idempotency**: Same `idempotency_key` = cached response (no charge)
- **Balance check (<100ms)**: Uses cached/hot USDC balance from wallet service
- **Fallback**: If timeout, tentatively approve + flag manual review
- **Compliance**: Every decision logged (7-year audit trail)

---

### 3. GET /card/balance/{card_id}
**Get real-time USDC balance**

```bash
curl -X GET https://api.fawn.app/card/balance/550e8400-e29b-41d4-a716-446655440000 \
  -H "Authorization: Bearer <JWT_TOKEN>"
```

**Response:**
```json
{
  "status": "success",
  "data": {
    "wallet_address": "0x742d35Cc6634C0532925a3b844Bc854e5038c58f",
    "balance_cents": 10050,
    "balance_usd": 100.50,
    "last_updated": "2026-07-08T12:00:05Z"
  }
}
```

---

### 4. POST /card/freeze/{card_id}
**User-initiated card freeze**

```bash
curl -X POST https://api.fawn.app/card/freeze/550e8400-e29b-41d4-a716-446655440000 \
  -H "Authorization: Bearer <JWT_TOKEN>" \
  -d "reason=Suspicious%20activity"
```

**Response:**
```json
{
  "status": "success",
  "data": {
    "card_id": "550e8400-e29b-41d4-a716-446655440000",
    "status": "frozen",
    "frozen_at": "2026-07-08T12:05:00Z"
  }
}
```

**Frozen card behavior:** All auth requests return `decline_reason: "Card status: frozen"` with code `03`.

---

### 5. GET /card/transactions/{card_id}
**Transaction history (paginated)**

```bash
curl -X GET 'https://api.fawn.app/card/transactions/550e8400-e29b-41d4-a716-446655440000?limit=50&offset=0' \
  -H "Authorization: Bearer <JWT_TOKEN>"
```

**Response:**
```json
{
  "status": "success",
  "data": {
    "transactions": [
      {
        "id": "txn_1",
        "merchant_name": "Starbucks",
        "amount_cents": 525,
        "amount_usd": 5.25,
        "status": "authorized",
        "approved": true,
        "requested_at": "2026-07-08T11:45:30Z",
        "authorized_at": "2026-07-08T11:45:31Z",
        "processor_response_code": "00"
      }
    ],
    "total_count": 42,
    "limit": 50,
    "offset": 0
  }
}
```

---

## Error Responses

| Status | Reason | Card Behavior |
|--------|--------|---------------|
| `00` | Approved | Balance deducted, auth recorded |
| `03` | Card not active (frozen/suspended) | Decline, audit logged |
| `04` | Velocity limit exceeded | Decline, no balance check |
| `05` | Insufficient funds | Decline, audit logged |
| `91` | Issuer unavailable | Decline or fallback if enabled |
| `99` | Manual review pending | Tentatively approved, flagged |

---

## Idempotency & Duplicate Prevention

**Problem:** Lithic may retry webhook (network timeout, 5xx), causing duplicate charges.

**Solution:** Unique constraint on `idempotency_key`.

```python
# Same merchant trace = same idempotency_key
if existing_txn := db.query(CardTransaction).filter_by(
    idempotency_key=idempotency_key
).first():
    # Return cached result (no DB operation)
    return existing_txn.auth_approved, existing_txn.processor_response_code
```

**Example:**
- Merchant sends: trace_number=`"abc123"`, amount=$100
- Lithic webhook 1: Processor receives → We approve → Return 200
- Network timeout: Lithic retries
- Lithic webhook 2: Same trace_number=`"abc123"`
- We find existing transaction → Return cached result → **No double charge**

---

## Processor Outage Fallback

**Scenario: Balance check times out (>100ms)**

```python
# Try fast balance check
try:
    balance = await asyncio.wait_for(
        wallet_svc.get_wallet_balance(wallet_address),
        timeout=0.1  # 100ms
    )
except asyncio.TimeoutError:
    # Fallback: tentatively approve, flag manual review
    return {
        "approved": True,
        "processor_response_code": "99",  # Manual review
        "requires_manual_review": True,
        "fallback_reason": "processor_timeout"
    }
```

**Manual Review Process:**
1. Transactions with `requires_manual_review=True` queued in admin dashboard
2. Admin reviews: wallet balance, merchant, amount, user history
3. Approve → Charge wallet
4. Decline → Reverse auth, notify user
5. Audit logged with manual action (compliance)

---

## Rate Limiting (Prevent Card Spam)

**Limit:** Max 1 card issued per user per day

```python
if user_rate_limit.cards_issued_today >= 1:
    raise RateLimitExceededException("Maximum 1 card per day allowed")
```

**Reset:** Daily counter reset at UTC midnight (configurable).

---

## Velocity Checks (Per-Transaction Limits)

**Checks run sequentially before balance check:**

1. **Per-transaction limit**: $1,000 max
2. **Daily limit**: $10,000 cumulative
3. **Monthly limit**: $300,000 cumulative

**Decline reason:**
```json
{
  "approved": false,
  "decline_reason": "Daily limit exceeded: $9500 + $1500 > $10000",
  "processor_response_code": "04"
}
```

---

## Audit Logging (7-Year Compliance)

**Every event recorded immutably:**

```python
CardAuditLog(
    event_type=AuditEventType.AUTH_APPROVED,
    entity_type='transaction',
    entity_id=txn.id,
    user_id=user.id,
    actor_type='processor',
    details={
        'merchant': 'Starbucks',
        'amount': 525,
        'balance_after': 9475,
        'processor_txn_id': 'xyz123'
    },
    retention_until=datetime.utcnow() + timedelta(days=365*7)
)
```

**Query audit trail (ADMIN):**
```bash
GET /card/audit/550e8400-e29b-41d4-a716-446655440000?event_type=AUTH_APPROVED
```

---

## Integration with Lithic

### Setup

1. **Create sandbox account:** https://dashboard.sandbox.lithic.com
2. **API Key:** Settings > API Keys > Create
3. **Webhook:** Settings > Webhooks > New Endpoint
   - URL: `https://yourapi.com/card/authorize-transaction`
   - Secret: Copy to `LITHIC_WEBHOOK_SECRET`
4. **Test:**
   ```bash
   curl -X POST https://api.sandbox.lithic.com/v1/cards \
     -H "Authorization: Bearer sk_..." \
     -d '{"type":"VIRTUAL","account_token":"user123"}'
   ```

### Webhook Verification

```python
# Lithic signs with HMAC-SHA256
signature = hmac.new(
    webhook_secret.encode(),
    request.body,
    hashlib.sha256
).hexdigest()

# Verify header matches
if not hmac.compare_digest(signature, x_lithic_signature):
    raise HTTPException(status_code=401, detail="Invalid signature")
```

---

## Environment Variables (.env)

```bash
# Lithic processor
LITHIC_API_KEY=sk_sandbox_...
LITHIC_BASE_URL=https://api.sandbox.lithic.com
LITHIC_WEBHOOK_SECRET=your_webhook_secret_here
ALLOW_UNSIGNED_LITHIC_WEBHOOKS=false  # DEV ONLY

# Card limits
CARD_DAILY_LIMIT_CENTS=1000000        # $10,000/day
CARD_MONTHLY_LIMIT_CENTS=30000000     # $300,000/month
CARD_TRANSACTION_LIMIT_CENTS=100000   # $1,000/txn

# Timeouts
BALANCE_CHECK_TIMEOUT_MS=100
AUTH_RESPONSE_TIMEOUT_MS=500

# Compliance
AUDIT_RETENTION_DAYS=2555  # 7 years
```

---

## Performance at Scale

**1000 authorizations/day target:**

| Operation | Target | Implementation |
|-----------|--------|-----------------|
| Balance check | <100ms | Cached USDC wallet balance |
| Webhook response | <500ms | Async balance check + fallback |
| Idempotency lookup | <10ms | Unique constraint on `idempotency_key` |
| Audit log write | <50ms | Async batch write (non-blocking) |
| **Total auth latency** | **<1s** | Meets Lithic processor timeout |

**Scaling recommendations:**
- Cache USDC balances in Redis (1-5 min TTL)
- Batch audit logs (write every 100 events or 5s)
- Use read replicas for transaction history queries
- Index by `(user_id, status)` for velocity checks

---

## Testing

### Unit Tests

```python
# tests/test_card_service.py
async def test_authorize_transaction_insufficient_balance():
    card_svc = CardService(db, lithic, wallet_svc)
    with pytest.raises(InsufficientBalanceException):
        await card_svc.authorize_transaction(
            processor_transaction_id="xyz123",
            idempotency_key="abc456",
            card_id=card.id,
            merchant_name="Starbucks",
            transaction_amount_cents=10000,  # $100
        )
    # Verify declined transaction recorded
    txn = db.query(CardTransaction).filter_by(
        processor_transaction_id="xyz123"
    ).first()
    assert txn.auth_approved == False
    assert txn.processor_response_code == "05"  # Insufficient funds

async def test_idempotency_no_double_charge():
    # First request
    result1 = await card_svc.authorize_transaction(
        processor_transaction_id="xyz123",
        idempotency_key="abc456",
        card_id=card.id,
        merchant_name="Starbucks",
        transaction_amount_cents=525,
    )
    
    # Second request (same key)
    result2 = await card_svc.authorize_transaction(
        processor_transaction_id="xyz999",  # Different processor ID
        idempotency_key="abc456",          # Same client trace
        card_id=card.id,
        merchant_name="Starbucks",
        transaction_amount_cents=525,
    )
    
    # Should return cached result
    assert result1 == result2
    
    # Only one transaction in DB
    txns = db.query(CardTransaction).filter_by(
        idempotency_key="abc456"
    ).all()
    assert len(txns) == 1
```

### Integration Tests

```bash
# 1. Issue card
curl -X POST http://localhost:8000/card/issue \
  -H "Authorization: Bearer $TOKEN"

# 2. Mock Lithic webhook (auth request)
curl -X POST http://localhost:8000/card/authorize-transaction \
  -H "X-Lithic-Signature: valid_signature" \
  -d @webhook_payload.json

# 3. Check transaction history
curl -X GET http://localhost:8000/card/transactions/$CARD_ID \
  -H "Authorization: Bearer $TOKEN"

# 4. Check audit trail
curl -X GET http://localhost:8000/card/audit/$CARD_ID \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

---

## Production Checklist

- [ ] Lithic production API key configured
- [ ] Webhook secret verified and rotated
- [ ] Database migration run (Alembic `upgrade`)
- [ ] HTTPS enforced on all endpoints
- [ ] Rate limiting enabled (slowapi)
- [ ] Audit log retention set to 7 years
- [ ] Card limits reviewed (daily/monthly/per-txn)
- [ ] Processor timeout fallback tested
- [ ] Monitoring: auth success rate, avg response time
- [ ] Alerting: manual review queue size, processor errors
- [ ] Compliance: PCI-DSS review, test decryption of audit logs
