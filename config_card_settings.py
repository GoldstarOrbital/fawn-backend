"""
Card service configuration.

Add these to core/config.py or .env file:
"""

# ==================== LITHIC PROCESSOR ====================

# Lithic API credentials (get from https://dashboard.sandbox.lithic.com)
LITHIC_API_KEY = ""  # Bearer token for API calls
LITHIC_BASE_URL = "https://api.sandbox.lithic.com"  # Use sandbox for testing
LITHIC_WEBHOOK_SECRET = ""  # Secret for webhook signature verification (HMAC-SHA256)

# Allow unsigned webhooks (DEV ONLY)
ALLOW_UNSIGNED_LITHIC_WEBHOOKS = False  # Set True in dev, False in prod

# ==================== CARD LIMITS ====================

# Velocity limits per card
CARD_DAILY_LIMIT_CENTS = 1000000  # $10,000/day
CARD_MONTHLY_LIMIT_CENTS = 30000000  # $300,000/month
CARD_TRANSACTION_LIMIT_CENTS = 100000  # $1,000 per transaction

# Rate limiting
CARD_MAX_ISSUANCE_PER_DAY = 1  # Max cards per user per day

# ==================== AUTHORIZATION TIMEOUTS ====================

# Balance check timeout (must complete in <100ms for 1000x/day scale)
BALANCE_CHECK_TIMEOUT_MS = 100

# Authorization webhook response timeout (processor expects <1s)
AUTH_RESPONSE_TIMEOUT_MS = 500

# ==================== AUDIT & COMPLIANCE ====================

# Audit log retention (7 years for regulatory compliance)
AUDIT_RETENTION_DAYS = 365 * 7  # 2555 days

# ==================== EXAMPLE .env ====================
"""
# .env file entries:

LITHIC_API_KEY=sk_...your_sandbox_key...
LITHIC_BASE_URL=https://api.sandbox.lithic.com
LITHIC_WEBHOOK_SECRET=your_webhook_secret_here
ALLOW_UNSIGNED_LITHIC_WEBHOOKS=false

CARD_DAILY_LIMIT_CENTS=1000000
CARD_MONTHLY_LIMIT_CENTS=30000000
CARD_TRANSACTION_LIMIT_CENTS=100000
CARD_MAX_ISSUANCE_PER_DAY=1

BALANCE_CHECK_TIMEOUT_MS=100
AUTH_RESPONSE_TIMEOUT_MS=500

AUDIT_RETENTION_DAYS=2555
"""

# ==================== LITHIC SANDBOX SETUP ====================
"""
1. Create Lithic account: https://dashboard.sandbox.lithic.com
2. Create API key in Settings > API Keys > Create New Key
3. Copy key and add to LITHIC_API_KEY
4. Create webhook in Settings > Webhooks > New Endpoint
   - Endpoint URL: https://yourapi.com/card/authorize-transaction
   - Events: Card Transaction - Authorization Requested
   - Copy Webhook Secret to LITHIC_WEBHOOK_SECRET
5. Test with: curl -X POST https://api.sandbox.lithic.com/v1/cards ... (requires Bearer token)
"""

# ==================== TESTING CARD ISSUANCE ====================
"""
Test flow:
1. POST /card/issue (requires auth token + initialized USDC wallet)
2. Lithic responds with card_token, last_four
3. Card stored in DB with status=ACTIVE
4. User can now authorize transactions

Sample response:
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
"""
