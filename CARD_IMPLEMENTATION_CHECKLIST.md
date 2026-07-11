# Card Authorization Implementation Checklist

Complete step-by-step guide to integrate debit card authorization into FAWN backend.

## Phase 1: Setup & Configuration

### Environment Setup

- [ ] **Create `.env.card` file** (or add to existing `.env`)
  ```bash
  # Lithic Processor
  LITHIC_API_KEY=sk_sandbox_...
  LITHIC_BASE_URL=https://api.sandbox.lithic.com
  LITHIC_WEBHOOK_SECRET=your_webhook_secret
  ALLOW_UNSIGNED_LITHIC_WEBHOOKS=false

  # Card Limits
  CARD_DAILY_LIMIT_CENTS=1000000
  CARD_MONTHLY_LIMIT_CENTS=30000000
  CARD_TRANSACTION_LIMIT_CENTS=100000

  # Timeouts
  BALANCE_CHECK_TIMEOUT_MS=100
  AUTH_RESPONSE_TIMEOUT_MS=500

  # Compliance
  AUDIT_RETENTION_DAYS=2555
  ```

- [ ] **Update `core/config.py`** to load card settings
  ```python
  from pydantic import BaseSettings
  
  class Settings(BaseSettings):
      # ... existing settings ...
      
      # Card Service
      lithic_api_key: str = ""
      lithic_base_url: str = "https://api.sandbox.lithic.com"
      lithic_webhook_secret: str = ""
      allow_unsigned_lithic_webhooks: bool = False
      
      card_daily_limit_cents: int = 1000000
      card_monthly_limit_cents: int = 30000000
      card_transaction_limit_cents: int = 100000
      
      balance_check_timeout_ms: int = 100
      auth_response_timeout_ms: int = 500
      
      audit_retention_days: int = 2555
      
      class Config:
          env_file = ".env"
  
  settings = Settings()
  ```

### Lithic Account Setup

- [ ] **Create Lithic sandbox account**
  - Visit: https://dashboard.sandbox.lithic.com
  - Sign up with email
  - Verify email

- [ ] **Create API key**
  - Dashboard > Settings > API Keys
  - Click "Create New Key"
  - Copy key to `LITHIC_API_KEY`
  - Note: Can't retrieve later, only rotate

- [ ] **Setup webhook endpoint**
  - Dashboard > Settings > Webhooks
  - Click "New Endpoint"
  - URL: `https://yourapi.fawn.app/card/authorize-transaction` (replace with prod URL)
  - Select events: "Card Transaction" > "Authorization Requested"
  - Copy webhook secret to `LITHIC_WEBHOOK_SECRET`

- [ ] **Test Lithic connectivity**
  ```bash
  curl -X GET https://api.sandbox.lithic.com/v1/accounts \
    -H "Authorization: Bearer $LITHIC_API_KEY"
  ```
  Should return 200 with accounts list.

---

## Phase 2: Database & Models

### Create Models

- [ ] **File: `models/card.py`** ✓ (provided)
  - Contains: Card, CardTransaction, CardAuditLog, CardRateLimit, ProcessorWebhookLog
  - Status enums: CardStatus, TransactionStatus, AuditEventType

- [ ] **Update `models/user.py`** (already present from crypto pivot)
  - User should have: `crypto_wallet_address`, `wallet_initialized`
  - No changes needed for card service

### Database Migration

- [ ] **Create Alembic migration**
  - File: `migrations/versions/002_add_card_tables.py` ✓ (provided)

- [ ] **Run migration locally**
  ```bash
  # From project root
  alembic upgrade head
  ```
  Should create 6 new tables: cards, card_transactions, card_audit_logs, card_rate_limits, processor_webhook_logs

- [ ] **Verify schema**
  ```bash
  sqlite3 ./fawn.db ".schema cards"
  ```

### Test Database Queries

- [ ] **Test basic CRUD**
  ```python
  from models.card import Card, CardStatus
  from sqlalchemy import create_engine
  from sqlalchemy.orm import sessionmaker
  
  db = sessionmaker(bind=engine)()
  
  # Create
  card = Card(
      id="test-id",
      user_id="user-123",
      lithic_card_token="token-123",
      card_last_four="4242",
      wallet_address="0x...",
  )
  db.add(card)
  db.commit()
  
  # Query
  found = db.query(Card).filter_by(card_last_four="4242").first()
  assert found is not None
  ```

---

## Phase 3: Service Layer

### Card Service

- [ ] **File: `services/card_service.py`** ✓ (provided)
  - CardService class with methods:
    - `issue_card()` - Issue virtual card
    - `authorize_transaction()` - Real-time auth
    - `get_card_balance()` - Balance check
    - `freeze_card()` - User freeze
    - `get_card_transactions()` - History

- [ ] **Test CardService locally**
  ```python
  from services.card_service import CardService
  from services.lithic_processor import LithicProcessor
  from services.crypto_wallet import CryptoWalletService
  
  lithic = LithicProcessor()
  wallet_svc = CryptoWalletService(db)
  card_svc = CardService(db, lithic, wallet_svc)
  
  # Test issue card (requires valid wallet)
  card = await card_svc.issue_card(
      user_id="user-123",
      wallet_address="0x742d35Cc6634C0532925a3b844Bc854e5038c58f"
  )
  assert card['status'] == 'success'
  ```

### Lithic Processor

- [ ] **File: `services/lithic_processor.py`** ✓ (provided)
  - LithicProcessor class with methods:
    - `issue_card()` - Call Lithic API
    - `verify_webhook_signature()` - Verify HMAC
    - `parse_webhook_payload()` - Extract auth request
    - `extract_auth_request()` - Get auth details

- [ ] **Test Lithic API connectivity**
  ```python
  from services.lithic_processor import LithicProcessor
  
  lithic = LithicProcessor()
  card = await lithic.issue_card(
      user_id="test-user",
      wallet_address="0x..."
  )
  # Should return: lithic_card_token, card_last_four, card_brand
  assert card['lithic_card_token']
  ```

### Manual Review Service

- [ ] **File: `services/card_manual_review.py`** ✓ (provided)
  - ManualReviewService class with methods:
    - `get_review_queue()` - Pending reviews
    - `approve_transaction()` - Admin approval
    - `decline_transaction()` - Admin decline
    - `escalate_transaction()` - Escalation
    - `get_transaction_details()` - Full context
    - `get_statistics()` - Queue stats

---

## Phase 4: API Endpoints

### Card Routes

- [ ] **File: `routers/card.py`** ✓ (provided)
  - Endpoints:
    - `POST /card/issue` - Issue card
    - `POST /card/authorize-transaction` - Auth webhook (from Lithic)
    - `GET /card/balance/{card_id}` - Real-time balance
    - `POST /card/freeze/{card_id}` - Freeze card
    - `POST /card/unfreeze/{card_id}` - Unfreeze card
    - `GET /card/transactions/{card_id}` - History
    - `GET /card/audit/{card_id}` - Audit logs (admin)
    - `GET /card/health` - Health check

- [ ] **Register routes in `main.py`**
  ```python
  from routers import card
  
  app.include_router(card.router)
  ```

### Admin Routes

- [ ] **File: `routers/card_admin.py`** ✓ (provided)
  - Endpoints:
    - `GET /admin/card/review-queue` - Manual review queue
    - `POST /admin/card/review/{id}/approve` - Approve transaction
    - `POST /admin/card/review/{id}/decline` - Decline transaction
    - `POST /admin/card/review/{id}/escalate` - Escalate transaction
    - `GET /admin/card/review/{id}` - Transaction details
    - `GET /admin/card/review/statistics` - Queue stats
    - `GET /admin/card/health` - Admin service health

- [ ] **Register routes in `main.py`**
  ```python
  from routers import card_admin
  
  app.include_router(card_admin.router)
  ```

---

## Phase 5: Testing

### Unit Tests

- [ ] **Create `tests/test_card_service.py`**
  ```python
  import pytest
  from services.card_service import CardService, InsufficientBalanceException
  
  @pytest.mark.asyncio
  async def test_issue_card(db, lithic, wallet_svc):
      card_svc = CardService(db, lithic, wallet_svc)
      result = await card_svc.issue_card("user-1", "0x...")
      assert result['status'] == 'success'
      assert result['card_id']
  
  @pytest.mark.asyncio
  async def test_authorize_insufficient_balance(db, card_svc):
      with pytest.raises(InsufficientBalanceException):
          await card_svc.authorize_transaction(
              processor_transaction_id="xyz",
              idempotency_key="abc",
              card_id="card-1",
              merchant_name="Starbucks",
              transaction_amount_cents=999999999,
          )
  ```

- [ ] **Create `tests/test_card_idempotency.py`**
  ```python
  @pytest.mark.asyncio
  async def test_idempotent_auth(db, card_svc):
      # First request
      result1 = await card_svc.authorize_transaction(
          processor_transaction_id="xyz1",
          idempotency_key="trace123",
          card_id="card-1",
          merchant_name="Merchant",
          transaction_amount_cents=1000,
      )
      
      # Second request (same idempotency_key)
      result2 = await card_svc.authorize_transaction(
          processor_transaction_id="xyz2",
          idempotency_key="trace123",  # Same!
          card_id="card-1",
          merchant_name="Merchant",
          transaction_amount_cents=1000,
      )
      
      # Should return cached result
      assert result1 == result2
      
      # Only one transaction in DB
      txns = db.query(CardTransaction).filter_by(
          idempotency_key="trace123"
      ).all()
      assert len(txns) == 1
  ```

- [ ] **Create `tests/test_lithic_webhook.py`**
  ```python
  @pytest.mark.asyncio
  async def test_webhook_authorization(db, client):
      payload = {
          "type": "card_transaction.updated",
          "data": {
              "token": "card_token",
              "events": [{
                  "type": "AUTHORIZATION",
                  "amount": 1000,
                  "merchant": {"merchant_name": "Test"},
                  "network_identifiers": {
                      "processor_transaction_id": "xyz",
                      "trace_number": "trace"
                  }
              }]
          }
      }
      
      response = client.post(
          "/card/authorize-transaction",
          json=payload,
          headers={"X-Lithic-Signature": "valid_sig"}
      )
      assert response.status_code == 200
      assert response.json()["approved"] in [True, False]
  ```

### Integration Tests

- [ ] **Test full flow**
  ```bash
  # 1. Issue card
  CARD_ID=$(curl -X POST http://localhost:8000/card/issue \
    -H "Authorization: Bearer $TOKEN" | jq -r '.data.card_id')
  
  # 2. Check balance
  curl http://localhost:8000/card/balance/$CARD_ID \
    -H "Authorization: Bearer $TOKEN"
  
  # 3. Mock auth webhook
  curl -X POST http://localhost:8000/card/authorize-transaction \
    -H "X-Lithic-Signature: $SIG" \
    -d @webhook.json
  
  # 4. Check transaction history
  curl http://localhost:8000/card/transactions/$CARD_ID \
    -H "Authorization: Bearer $TOKEN"
  ```

### Load Testing

- [ ] **Simulate 1000 auths/day**
  ```python
  import asyncio
  import time
  
  async def load_test(concurrency=10, duration_seconds=60):
      tasks = []
      start = time.time()
      
      while time.time() - start < duration_seconds:
          for _ in range(concurrency):
              tasks.append(test_auth())
          results = await asyncio.gather(*tasks, return_exceptions=True)
          success = sum(1 for r in results if r.get('approved'))
          print(f"Success: {success}/{len(results)}, "
                f"Avg: {(time.time()-start)/len(results)}s")
  
  # Run: python -m pytest tests/test_load.py -v
  ```

---

## Phase 6: Deployment

### Pre-Production

- [ ] **Update Railway env vars**
  - Railway dashboard > Variables > Add:
    - `LITHIC_API_KEY` = sandbox key (test first!)
    - `LITHIC_WEBHOOK_SECRET` = webhook secret
    - Other card config vars

- [ ] **Test on Railway staging**
  ```bash
  git push origin feature/card-auth
  # Railway auto-deploys to staging
  curl -X GET https://staging.fawn.app/card/health
  ```

- [ ] **Production Lithic setup**
  - Create production Lithic account
  - Switch `LITHIC_BASE_URL` to prod: `https://api.lithic.com`
  - Create new API key (sandbox → prod)
  - Update webhook URL to prod domain

### Production Deployment

- [ ] **Add to Railway prod vars**
  - Switch `LITHIC_API_KEY` to prod key
  - Switch `LITHIC_BASE_URL` to `https://api.lithic.com`
  - Verify `ALLOW_UNSIGNED_LITHIC_WEBHOOKS=false`

- [ ] **Run migration on prod DB**
  ```bash
  # Via Railway dashboard or local CLI
  alembic upgrade head  # Must run on prod database
  ```

- [ ] **Enable in code**
  - Verify card routes imported in `main.py`
  - Verify auth decorators on admin endpoints

- [ ] **Monitor immediately**
  - Alert on: auth success rate < 95%
  - Alert on: manual review queue > 50
  - Alert on: processor errors > 10/min
  - Alert on: avg response time > 500ms

---

## Phase 7: Compliance & Security

### PCI-DSS Compliance

- [ ] **Card data handling**
  - ✓ Never store full card numbers (only last 4)
  - ✓ Never log sensitive card data
  - ✓ Use Lithic as payment processor (we never touch PAN)

- [ ] **Network security**
  - ✓ All endpoints HTTPS only
  - ✓ Webhook signature verification (HMAC-SHA256)
  - ✓ JWT token authentication on card endpoints

- [ ] **Data encryption**
  - User wallet addresses encrypted at rest (if stored)
  - Audit logs immutable, 7-year retention

### Rate Limiting

- [ ] **Implement slowapi on card routes**
  ```python
  from slowapi import Limiter
  from slowapi.util import get_remote_address
  
  limiter = Limiter(key_func=get_remote_address)
  app.state.limiter = limiter
  
  @router.post("/issue", dependencies=[Depends(limiter.limit("10/minute"))])
  async def issue_card(...):
      ...
  ```

- [ ] **Add rate limits**
  - `POST /card/issue`: 1/day per user (enforced in service)
  - `POST /card/authorize-transaction`: 1000/day per card (velocity checks)
  - `GET /card/balance`: 100/minute per user (balance spam protection)

### Audit & Logging

- [ ] **Verify audit logs created**
  ```bash
  # Query audit table
  select count(*) from card_audit_logs where created_at > NOW() - interval 1 hour;
  ```

- [ ] **Setup log retention cleanup**
  ```python
  # Add to main.py startup
  @app.on_event("startup")
  async def cleanup_expired_audits():
      # Run daily: delete logs where retention_until < now
      db.query(CardAuditLog).filter(
          CardAuditLog.retention_until < datetime.utcnow()
      ).delete()
  ```

- [ ] **Document for auditors**
  - Audit log schema
  - Data retention policy (7 years)
  - Access controls (admin-only)

---

## Phase 8: Monitoring & Observability

### Metrics

- [ ] **Setup Prometheus/DataDog metrics**
  ```python
  from prometheus_client import Counter, Histogram
  
  auth_counter = Counter(
      'card_authorizations_total',
      'Total authorizations',
      ['status']  # approved, declined, error
  )
  
  auth_latency = Histogram(
      'card_auth_latency_ms',
      'Authorization latency',
      buckets=[50, 100, 250, 500, 1000]
  )
  ```

- [ ] **Key metrics to track**
  - Auth success rate (target: >98%)
  - Auth response time (target: <500ms)
  - Manual review queue size (alert: >100)
  - Fallback rate (processor timeouts %)
  - Card issuance rate (per day, per user)

### Alerting

- [ ] **Setup PagerDuty/Slack alerts**
  - Auth success rate < 95% → Page oncall
  - Manual review queue > 200 → Slack notification
  - Processor error rate > 5% → Page oncall
  - Database connectivity issue → Page oncall

### Logging

- [ ] **Structured logging for troubleshooting**
  ```python
  logger.info(
      "Transaction authorized",
      extra={
          'transaction_id': txn.id,
          'card_id': card.id,
          'merchant': merchant,
          'amount': amount,
          'latency_ms': elapsed_ms,
          'balance_before': balance_before,
          'balance_after': balance_after,
          'processor_response_code': code,
          'user_id': user.id,
          'correlation_id': request.headers.get('X-Correlation-ID'),
          'trace_id': request.headers.get('X-Trace-ID'),
      }
  )
  ```

---

## Troubleshooting

### Common Issues

| Issue | Cause | Solution |
|-------|-------|----------|
| Card issuance returns 503 | Lithic API unreachable | Check `LITHIC_API_KEY`, `LITHIC_BASE_URL` in Railway |
| Webhook signature invalid | Secret mismatch | Verify `LITHIC_WEBHOOK_SECRET` matches dashboard |
| Auth always times out | Balance check too slow | Check wallet service, consider Redis cache |
| Idempotency not working | Database constraint not created | Run migration: `alembic upgrade head` |
| Manual review queue stuck | Admin UI not deployed | Check `/admin/card/review-queue` endpoint registered |

### Debug Commands

```bash
# Check Railway env vars
railway env

# View Lithic API logs
curl -X GET https://api.sandbox.lithic.com/v1/transactions \
  -H "Authorization: Bearer $LITHIC_API_KEY"

# Query card transactions
sqlite3 fawn.db "SELECT * FROM card_transactions ORDER BY requested_at DESC LIMIT 10;"

# Check audit logs
sqlite3 fawn.db "SELECT event_type, COUNT(*) FROM card_audit_logs GROUP BY event_type;"

# Test webhook locally
curl -X POST http://localhost:8000/card/authorize-transaction \
  -H "Content-Type: application/json" \
  -H "X-Lithic-Signature: test" \
  -d @webhook_payload.json
```

---

## Post-Launch

### Week 1

- [ ] Monitor auth success rate (target: >98%)
- [ ] Check manual review queue (should be empty)
- [ ] Validate audit logs being recorded
- [ ] Gather user feedback on card issuance flow

### Month 1

- [ ] Analyze transaction patterns (merchants, amounts, velocities)
- [ ] Validate compliance (7-year audit retention)
- [ ] Performance tuning (cache balance checks if needed)
- [ ] Security review (PCI-DSS checklist)

### Ongoing

- [ ] Weekly: Review processor error logs
- [ ] Monthly: Audit trail integrity check
- [ ] Quarterly: Security update review
- [ ] Annually: Compliance certification renewal
