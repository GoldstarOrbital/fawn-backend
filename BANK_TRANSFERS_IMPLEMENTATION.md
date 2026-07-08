# Bank Account Transfer Implementation

## Overview

FAWN users can now send USDC to **any traditional bank account** via ACH (Automated Clearing House). This enables students to:
- Send money to friends on FAWN (existing P2P USDC)
- Send money to **any bank account** (NEW - traditional banking)
- Same $0.01 flat fee on both paths
- Instant settlement for FAWN P2P, standard ACH 1-3 business days for banks

## Architecture

### New Endpoint: `POST /transfers/send-to-bank`

```
POST https://api.fawn.app/transfers/send-to-bank
Authorization: Bearer {user_token}

{
  "recipient_name": "John Doe",
  "recipient_routing_number": "011000015",
  "recipient_account_number": "123456789",
  "amount_cents": 10000,
  "memo": "Rent payment"
}

Response (201 Created):
{
  "transfer_id": "uuid",
  "amount": 100.00,
  "fee": 0.01,
  "total_debited": 100.01,
  "recipient_name": "John Doe",
  "recipient_last4": "6789",
  "status": "pending",
  "estimated_settlement": "1-3 business days",
  "created_at": "2026-07-08T12:34:56Z"
}
```

### Flow

1. **User sends USDC** from FAWN wallet
2. **Validate balance**: amount + $0.01 fee must be available
3. **Deduct balance immediately**: pessimistic (balance drops before settlement)
4. **Initiate ACH via Column**:
   - Convert USDC → USD (1:1, no slippage)
   - Call Column's ACH debit endpoint
   - Column handles routing/account validation
5. **Mark status pending**: settle in 1-3 business days
6. **Log audit trail**: 7-year retention per compliance

### Error Handling

| Scenario | HTTP Status | Message |
|----------|------------|---------|
| User has no wallet | 404 Not Found | "No stablecoin wallet" |
| Insufficient USDC | 402 Payment Required | "Insufficient balance. Have $X, need $Y" |
| Column not configured | 503 Service Unavailable | "Banking service unavailable" |
| ACH API error | 503 Service Unavailable | "ACH transfer failed: {detail}" |
| Invalid inputs | 422 Unprocessable Entity | Validation errors |

**Refund Logic**: If ACH initiation fails (Column error), the balance is **refunded**:
- Restores user's balance to pre-request state
- Transfer marked `status=failed`
- Audit logged with error detail

## Database Changes

### New Table: `BankTransfer`

```python
class BankTransfer(Base):
    __tablename__ = "bank_transfers"
    
    id: str  # UUID
    sender_id: str  # FK to users
    recipient_name: str  # e.g., "John Doe"
    recipient_routing_number: str  # e.g., "011000015"
    recipient_account_last4: str  # "6789" (ONLY last 4 persisted)
    amount_cents: int  # e.g., 10000 = $100
    fee_cents: int  # 100 = $0.01
    status: str  # "pending" | "completed" | "failed"
    memo: str  # Optional payment reference
    ach_id: str  # Column's transfer ID (nullable until success)
    idempotency_key: str  # Prevents retry duplicates
    error_message: str  # If status=failed
    created_at: datetime
    completed_at: datetime  # Set when Column confirms
```

**Security**: Recipient account/routing numbers are **NOT persisted**. Only the last 4 digits of the account number are stored for user reference. Full numbers are sent directly to Column and discarded immediately after the API call (same model as ACH funding in the banking era).

### Updated Table: `User`

- Existing `usdc_balance_cents` tracks combined balance (used for both P2P and bank transfers)
- Existing `total_fees_paid_cents` includes bank transfer fees

### Updated Table: `UserAuditLog`

- New action type: `sent_bank_transfer`
- Details include: recipient name, routing last 4, account last 4, amount, fee
- 7-year retention per compliance

## Service Layer

### New Function: `crypto_wallet.send_to_bank()`

**File**: `services/crypto_wallet.py`

```python
async def send_to_bank(
    sender_id: str,
    recipient_name: str,
    recipient_routing_number: str,
    recipient_account_number: str,
    amount_cents: int,
    db: Session,
    memo: str = None,
) -> dict:
    """
    Send USDC to traditional bank account via ACH.
    
    Args:
        sender_id: FAWN user ID
        recipient_name: Name on receiving bank account
        recipient_routing_number: 9-digit US routing number
        recipient_account_number: Bank account (4-17 digits)
        amount_cents: Amount in cents (e.g., 1000 = $10)
        db: Database session
        memo: Optional payment reference
    
    Returns:
        {
            "transfer_id": "...",
            "amount": 100.00,
            "fee": 0.01,
            "total_debited": 100.01,
            "recipient_name": "John Doe",
            "recipient_last4": "6789",
            "status": "pending",
            "estimated_settlement": "1-3 business days",
            "created_at": "2026-07-08T...",
        }
    
    Raises:
        WalletNotInitialized: User has no wallet
        InsufficientBalance: Can't cover amount + fee
        BankTransferError: Column is unavailable or API error
    """
```

**Key logic**:
1. Validate sender has wallet + sufficient balance
2. Create `BankTransfer` record (status="pending")
3. Deduct balance immediately (amount + $0.01 fee)
4. Log audit entry (7-year retention)
5. Call Column ACH debit API
6. On success: populate `ach_id`, mark ready for settlement
7. On failure: refund balance, mark `status=failed`

### New Function: `column.create_ach_debit()`

**File**: `services/column.py`

```python
async def create_ach_debit(
    column_account_id: str,
    routing_number: str,
    account_number: str,
    account_type: str,
    account_holder_name: str,
    amount_cents: int,
    idempotency_key: str,
) -> dict:
    """
    Send funds from FAWN Column account to external bank account.
    
    This is the reverse of create_ach_credit() (which pulls funds IN).
    Funds flow OUT to the recipient's bank via ACH.
    
    Settlement: 1-3 business days (standard ACH).
    
    Args:
        column_account_id: FAWN's Column account (source)
        routing_number: 9-digit US routing number of recipient bank
        account_number: Recipient bank account number
        account_type: "checking" | "savings"
        account_holder_name: Name on recipient account
        amount_cents: Amount in cents (e.g., 1000 = $10)
        idempotency_key: For deduplication
    
    Returns:
        {
            "id": "ach_...",
            "type": "ACH_DEBIT",
            "amount": 1000,
            "status": "pending",
            "created_at": "2026-07-08T...",
            ...
        }
    
    Raises:
        ColumnNotConfigured: Column API key not set
        ColumnError: API error (invalid routing, etc.)
    """
```

Column API call:
```json
POST /transfers/ach
{
  "bank_account_id": "{column_account_id}",
  "type": "DEBIT",
  "amount": 1000,
  "currency_code": "USD",
  "description": "FAWN Send to Bank",
  "counterparty": {
    "routing_number": "011000015",
    "account_number": "123456789",
    "account_type": "checking",
    "name": "John Doe"
  }
}
```

## Input Validation

### `SendToBankRequest` Pydantic model

- **recipient_name**: 1-100 chars, alphanumeric + spaces and common punctuation
- **recipient_routing_number**: exactly 9 digits (regex: `^\d{9}$`)
- **recipient_account_number**: 4-17 digits (regex: `^\d{4,17}$`)
- **amount_cents**: integer > 0, max $99,999,999.99
- **memo**: optional, max 100 chars

Validation prevents SQL injection, XSS, and malformed bank details.

## Fee Model

- **Platform fee**: $0.01 (100 cents) per transfer
- **ACH fees**: $0 (Column absorbs or includes in interchange)
- **Gas fees**: $0 (not a blockchain transaction)
- **Total for $100 transfer**: $100.01

Fee is charged to sender, deducted along with transfer amount. Lifetime fees tracked in `User.total_fees_paid_cents` for analytics.

## Settlement & Status

### Pending State

- User's balance decreases immediately (pessimistic)
- `BankTransfer.status = "pending"`
- ACH is in-flight (1-3 business days)

### Completed State

When Column webhook confirms ACH settlement:
- `BankTransfer.status = "completed"`
- `BankTransfer.completed_at` is set
- User's balance remains decreased (already deducted)
- Audit log notes completion

### Failed State

If ACH returns/bounces:
- `BankTransfer.status = "failed"`
- `BankTransfer.error_message` explains why
- User's balance is **refunded** (if not already done at initiation)
- Audit log notes failure

**TODO**: Implement Column webhook handler in `routers/column_webhook.py` to listen for settlement/failure events and update `BankTransfer.status` and `BankTransfer.completed_at`.

## Audit & Compliance

Every bank transfer creates an immutable audit log entry:

```python
UserAuditLog(
    user_id=sender_id,
    action="sent_bank_transfer",
    details=json.dumps({
        "recipient_name": "John Doe",
        "routing_last4": "0015",  # Last 4 only
        "account_last4": "6789",
        "amount_cents": 10000,
        "fee_cents": 100,
        "bank_transfer_id": transfer_id,
    }),
    retention_expires_at=datetime.now(tz=timezone.utc) + timedelta(days=365*7),
)
```

**Retention**: 7 years (2,555 days) per FinCEN / federal compliance.

## Testing

**File**: `tests/test_bank_transfers.py`

Test coverage:
- ✅ Successful bank transfer with valid inputs
- ✅ Insufficient balance rejection
- ✅ No wallet error
- ✅ Column not configured (graceful failure + refund)
- ✅ ACH API error (graceful failure + refund)
- ✅ Input validation (bad routing number, etc.)
- ✅ Idempotency (each call is unique)
- ✅ Audit retention (7-year expiry)

Run tests:
```bash
cd fawn-backend
python -m pytest tests/test_bank_transfers.py -v
```

## Deployment Checklist

- [ ] Deploy backend with new models + endpoint
- [ ] Run migrations (automatic via `_init_db_schema()`)
- [ ] Test endpoint locally with Swagger UI
- [ ] Verify Column API is configured (`COLUMN_API_KEY` set)
- [ ] Update frontend to call `/transfers/send-to-bank`
- [ ] Add UI form for bank account entry (routing, account, name)
- [ ] Add confirmation screen ("Send $X to bank ending in 6789")
- [ ] Monitor Column ACH webhook for settlement callbacks
- [ ] Implement Column webhook handler for status updates

## Frontend Integration (Not Included)

The frontend (fawn-frontend) needs:

1. **New form screen**: Recipient bank details
   - Name on account
   - Routing number (9 digits)
   - Account number (4-17 digits)
   - Amount
   - Memo (optional)

2. **Validation**: Client-side checks on inputs

3. **Confirmation screen**: Show total fee + settlement time before confirming

4. **API call**:
   ```javascript
   const response = await fetch('https://api.fawn.app/transfers/send-to-bank', {
     method: 'POST',
     headers: {
       'Authorization': `Bearer ${token}`,
       'Content-Type': 'application/json',
     },
     body: JSON.stringify({
       recipient_name: "John Doe",
       recipient_routing_number: "011000015",
       recipient_account_number: "123456789",
       amount_cents: 10000,
       memo: "Rent"
     })
   });
   ```

5. **Status display**: Show "pending" status with expected settlement date

6. **Success screen**: Confirm transfer ID + settlement estimate

## Future Enhancements

1. **Webhook handler**: Listen to Column ACH settlement events, update transfer status
2. **Scheduled checks**: Poll for ACH settlement status if webhooks fail
3. **On-chain settlement**: Direct USDC transfers to crypto wallets (vs. bank routing)
4. **Multiple bank accounts**: Let users save favorite bank accounts for quick sends
5. **International wire transfers**: Extend beyond ACH to SWIFT
6. **Instapay**: Faster settlement (same-day or better) via FedNow or RTP
7. **Batch transfers**: Send to multiple recipients in one operation
8. **Deferred payments**: Schedule a transfer for future date

## Support & Troubleshooting

### "Banking service unavailable"
- Column API key not set (`COLUMN_API_KEY` missing in env vars)
- Column API is temporarily down
- Fix: Set `COLUMN_API_KEY` on Railway dashboard and redeploy

### "Insufficient balance"
- User doesn't have enough USDC for amount + $0.01 fee
- Fix: User needs to fund their wallet first (via P2P receive or other means)

### "Invalid routing number"
- 9-digit US routing number failed Column validation
- Fix: Verify routing number is correct (can use https://www.frbservices.org/search for verification)

### "ACH transfer failed"
- Recipient account details are invalid or account is closed
- Will show in error_message for that transfer
- User's balance is refunded

### Transfer stuck in pending
- Standard ACH can take 1-3 business days
- Check Column dashboard for settlement status
- If > 3 business days, contact Column support

## Code References

- **Models**: `models.py` - `BankTransfer` class
- **Router**: `routers/crypto.py` - `POST /transfers/send-to-bank` endpoint
- **Service**: `services/crypto_wallet.py` - `send_to_bank()` function
- **Banking provider**: `services/column.py` - `create_ach_debit()` function
- **Tests**: `tests/test_bank_transfers.py`
