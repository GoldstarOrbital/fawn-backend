# Bank Transfer Implementation Checklist

## Code Changes (COMPLETED)

### 1. Database Models ✅
- **File**: `models.py`
- **Changes**:
  - Added `BankTransfer` table (lines 497-530)
  - Stores ACH transfer records with:
    - Sender ID, recipient name, routing last 4, account last 4
    - Amount, fee, status, memo
    - ACH provider ID, idempotency key, error message
    - Created/completed timestamps
    - Indexes for efficient querying

### 2. API Endpoint ✅
- **File**: `routers/crypto.py`
- **Endpoint**: `POST /transfers/send-to-bank` (line 224)
- **Changes**:
  - Added `SendToBankRequest` Pydantic model (lines 72-100) with validation:
    - Recipient name: 1-100 chars, alphanumeric + punctuation
    - Routing number: exactly 9 digits
    - Account number: 4-17 digits
    - Amount: positive integer, max $99,999,999.99
  - Added `BankTransferResponse` Pydantic model (lines 102-113)
  - Added endpoint handler (lines 224-272) with:
    - Rate limiting per user
    - Error handling (404, 402, 503)
    - Analytics capture
    - Proper HTTP status codes

### 3. Service Logic ✅
- **File**: `services/crypto_wallet.py`
- **Changes**:
  - Added `BankTransferError` exception class (line 61-62)
  - Added `send_to_bank()` async function (lines 388-501) with:
    - Balance validation (amount + $0.01 fee)
    - Pessimistic balance deduction (before ACH settlement)
    - Bank transfer record creation
    - Audit log with 7-year retention
    - Column ACH debit API call
    - Error handling with balance refund on failure
    - Idempotency key generation

### 4. Banking Provider Integration ✅
- **File**: `services/column.py`
- **Changes**:
  - Added `create_ach_debit()` async function (lines 164-203)
  - Sends ACH debit request to Column API
  - Maps FAWN transfer to Column's ACH format
  - Handles counterparty bank details
  - Returns Column transfer ID and status

## Integration Steps (TODO - Next Phase)

### 1. Frontend Implementation
- [ ] Create bank transfer form UI
  - Recipient name input
  - Routing number input (9 digits, validation)
  - Account number input (4-17 digits, validation)
  - Amount slider/input
  - Memo field (optional)
- [ ] Create confirmation screen
  - Show recipient details (name + last 4)
  - Show amount + $0.01 fee
  - Show "1-3 business days" settlement estimate
- [ ] Add API call in JavaScript/React
  - POST to `/transfers/send-to-bank`
  - Include Bearer token
  - Handle 402 (insufficient balance), 503 (service unavailable)
- [ ] Update balance display after transfer
- [ ] Show pending transfer status

### 2. ACH Settlement Webhook Handler
- [ ] Implement Column webhook handler in `routers/column_webhook.py`
- [ ] Listen for ACH settlement events
- [ ] Update `BankTransfer.status` to "completed" or "failed"
- [ ] Update `BankTransfer.completed_at` timestamp
- [ ] Handle ACH returns (failed transactions)
- [ ] Send user notification email on completion

### 3. Environment Configuration
- [ ] Ensure `COLUMN_API_KEY` is set on Railway
- [ ] Set `COLUMN_BASE_URL` (https://api.column.com for production)
- [ ] Verify Column sandbox credentials are valid
- [ ] Test ACH calls in Column sandbox environment

### 4. Testing
- [ ] Unit tests in `tests/test_bank_transfers.py` ✅ (written, not run yet)
- [ ] Integration tests (e2e ACH call to Column)
- [ ] Manual testing:
  - [ ] Create user with USDC balance
  - [ ] Call `/transfers/send-to-bank` with valid bank details
  - [ ] Verify balance decreases immediately
  - [ ] Verify `BankTransfer` record created with status="pending"
  - [ ] Verify audit log entry created
  - [ ] Check Column dashboard for ACH record
  - [ ] Test insufficient balance error
  - [ ] Test invalid routing number error
  - [ ] Test Column unavailable (graceful failure + refund)

### 5. Deployment Steps
- [ ] Create git branch: `feature/bank-transfers`
- [ ] Push code changes to fawn-backend
- [ ] Verify CI/CD pipeline passes
- [ ] Deploy to Railway staging environment
- [ ] Run integration tests against staging
- [ ] Test e2e flow (UI → API → Column sandbox)
- [ ] Deploy to Railway production
- [ ] Monitor logs for errors
- [ ] Verify endpoint is live on production

### 6. Launch Preparation
- [ ] Update API documentation (Swagger/OpenAPI)
- [ ] Add bank transfer use case to onboarding flow
- [ ] Update landing page (getfawn.com) to mention bank transfers
- [ ] Create FAQ: "What happens if my ACH fails?"
- [ ] Create FAQ: "Why does it take 1-3 days?"
- [ ] Prepare customer support runbook
- [ ] Draft launch announcement/email
- [ ] Coordinate with marketing team

## Files Changed

| File | Lines | Changes |
|------|-------|---------|
| `models.py` | 497-533 | Added `BankTransfer` model class |
| `routers/crypto.py` | 72-113, 224-272 | Added request/response models + endpoint |
| `services/crypto_wallet.py` | 61-62, 388-501 | Added `BankTransferError` exception + `send_to_bank()` function |
| `services/column.py` | 164-203 | Added `create_ach_debit()` function |
| `tests/test_bank_transfers.py` | (new file) | Comprehensive test suite |
| `BANK_TRANSFERS_IMPLEMENTATION.md` | (new file) | Full documentation |

## Files NOT Changed (as expected)

- `main.py` - No changes needed, routers auto-discovered
- `database.py` - No changes needed, SQLAlchemy handles migrations
- `schemas.py` - Pydantic models are in `routers/crypto.py`
- Frontend files - Not part of backend implementation

## Verification Steps

Before deploying to production:

1. **Code review**:
   ```bash
   git diff main feature/bank-transfers
   # Check that models, routes, and services are logically sound
   ```

2. **Lint check**:
   ```bash
   cd fawn-backend
   black . --check
   flake8 . --max-line-length=120
   ```

3. **Type check**:
   ```bash
   mypy services/crypto_wallet.py routers/crypto.py
   ```

4. **Syntax validation**:
   ```bash
   python -m py_compile models.py routers/crypto.py services/crypto_wallet.py services/column.py
   ```

5. **Unit tests**:
   ```bash
   python -m pytest tests/test_bank_transfers.py -v
   ```

6. **Manual API test** (using curl or Swagger UI):
   ```bash
   # Create test user + wallet first
   curl -X POST https://api.fawn.app/transfers/send-to-bank \
     -H "Authorization: Bearer {token}" \
     -H "Content-Type: application/json" \
     -d '{
       "recipient_name": "John Doe",
       "recipient_routing_number": "011000015",
       "recipient_account_number": "123456789",
       "amount_cents": 10000,
       "memo": "Test transfer"
     }'
   ```

## Known Limitations / TODOs

1. **Column account mapping**: The code passes an empty string for `column_account_id`:
   ```python
   ach_result = await column.create_ach_debit(
       column_account_id="",  # TODO: map sender to their Column account
       ...
   )
   ```
   **Fix**: Map each FAWN user to their Column account ID (from `User.column_account_id`). This requires that users have been onboarded to Column separately.

2. **Webhook handler not implemented**: ACH settlement events won't update transfer status automatically.
   **Fix**: Implement `handle_column_ach_event()` in `routers/column_webhook.py` to listen for transfer updates.

3. **No scheduled status checks**: If Column webhooks fail, transfers will stuck in "pending" forever.
   **Fix**: Add daily scheduled job to poll Column for settlement status.

4. **Account type hardcoded to "checking"**: Assumes all recipients have checking accounts.
   **Fix**: Add optional `account_type` parameter to request model ("checking" | "savings").

5. **No email notifications**: User won't get notified when transfer completes or fails.
   **Fix**: Send email via Resend when `status` changes from "pending" to "completed" or "failed".

## Success Metrics

After launch, monitor:

- ✅ **Transfer volume**: How many students send to bank accounts?
- ✅ **Fee revenue**: Each transfer = $0.01 fee → total fees collected
- ✅ **Success rate**: % of ACH transfers that complete vs. fail/return
- ✅ **Settlement time**: Actual ACH settlement time vs. "1-3 business days" estimate
- ✅ **Error rate**: % of API calls that fail due to validation or Column issues
- ✅ **User retention**: Do bank transfer users stay engaged?

## Support Contacts

- **Column support**: [Column dashboard](https://dashboard.column.com/)
- **FAWN team**: Alex (alexmarcusgoldsmith@gmail.com)
- **Internal runbook**: See deployment logs on Railway dashboard

---

**Status**: Code implementation COMPLETE. Awaiting frontend + webhook handler + testing before production launch.

**Last updated**: 2026-07-08
**Implemented by**: Claude Code
