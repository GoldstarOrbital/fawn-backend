# FAWN Investing Feature

## Overview

FAWN's investing feature allows students to buy and sell stocks, ETFs, and crypto directly from their USDC wallet via Alpaca Broker API. The feature includes real-time quotes, order history, portfolio tracking, and a personal watchlist.

## Architecture

### Backend Stack
- **FastAPI**: REST API with JWT authentication
- **Alpaca Broker API**: Brokerage operations (accounts, orders, positions, quotes)
- **SQLAlchemy + SQLite/Postgres**: Order and watchlist persistence
- **Rate Limiting**: slowapi per-endpoint rate limiting

### Frontend
- **Vanilla JavaScript**: No frameworks, ~500 lines for investing UI
- **Responsive Design**: Mobile-first, dark theme
- **Real-time Quotes**: 5-second poll updates

## API Endpoints

### Account Management

#### GET /investing/account
Get portfolio summary (balance, cash, buying power, account status).

**Auth**: Bearer token required
**Rate Limit**: 60/minute

**Response** (200 OK):
```json
{
  "account_id": "abc123...",
  "status": "ACTIVE",
  "cash": 5000.00,
  "equity": 12500.00,
  "buying_power": 12500.00,
  "currency": "USD"
}
```

#### POST /investing/account
Open a new brokerage account (one per user, requires signed agreements).

**Auth**: Bearer token required
**Rate Limit**: 5/hour

**Request Body**:
```json
{
  "agreements": [
    {
      "id": "customer_agreement",
      "signed_at": "2026-07-08T15:30:00Z",
      "ip_address": "203.0.113.42"
    }
  ]
}
```

**Response** (201 Created):
```json
{
  "account_id": "abc123...",
  "status": "SUBMITTED",
  "cash": 0.00,
  "equity": 0.00,
  "buying_power": 0.00,
  "currency": "USD"
}
```

### Trading

#### POST /investing/orders
Place a market buy or sell order.

**Auth**: Bearer token required
**Rate Limit**: 50/hour per user

**Security**:
- Max position size: $50,000 per order (student limit)
- Fractional shares enabled (minimum $1)
- Market orders only (day execution)

**Request Body**:
```json
{
  "symbol": "AAPL",
  "side": "buy",
  "notional": 250.00
}
```

Or:
```json
{
  "symbol": "SPY",
  "side": "sell",
  "qty": 5.5
}
```

**Response** (201 Created):
```json
{
  "order_id": "order123...",
  "status": "accepted",
  "symbol": "AAPL",
  "side": "buy"
}
```

**Errors**:
- 400: Invalid input, order exceeds position limit, or account not open
- 409: Duplicate order (same symbol, side, amount within recent window)
- 503: Alpaca not configured or unavailable

#### GET /investing/orders
List recent orders (up to 100, most recent first).

**Auth**: Bearer token required
**Rate Limit**: 60/minute

**Query Params**:
- `status` (optional): `all` | `open` | `closed` (default: `all`)
- `limit` (optional): 1-100 (default: 100)

**Response** (200 OK):
```json
{
  "orders": [
    {
      "order_id": "order123...",
      "symbol": "AAPL",
      "qty": 10.0,
      "notional": 1500.00,
      "side": "buy",
      "type": "market",
      "status": "filled",
      "filled_qty": 10.0,
      "filled_avg_price": 150.25,
      "created_at": "2026-07-08T14:30:00Z",
      "updated_at": "2026-07-08T14:30:05Z"
    }
  ]
}
```

### Positions & Portfolio

#### GET /investing/positions
List current holdings and their market value.

**Auth**: Bearer token required
**Rate Limit**: 60/minute

**Response** (200 OK):
```json
{
  "positions": [
    {
      "symbol": "AAPL",
      "qty": 10.5,
      "market_value": 1680.00,
      "unrealized_pl": 130.50,
      "avg_entry_price": 147.50
    }
  ]
}
```

### Market Data

#### GET /investing/quotes/{symbol}
Get real-time quote for a stock, ETF, or crypto symbol.

**Auth**: Not required (public market data)
**Rate Limit**: 120/minute

**Path Params**:
- `symbol` (string): Stock ticker (AAPL, SPY, BTC, ETH, etc.)

**Response** (200 OK):
```json
{
  "symbol": "AAPL",
  "bid": 150.20,
  "ask": 150.25,
  "last": 150.22,
  "bid_size": 1500,
  "ask_size": 2000,
  "timestamp": "2026-07-08T16:00:00Z"
}
```

**Errors**:
- 400: Invalid symbol format
- 502: Alpaca unavailable or symbol not found
- 503: Alpaca not configured

### Watchlist Management

#### GET /investing/watchlist
List symbols the user is tracking.

**Auth**: Bearer token required
**Rate Limit**: 60/minute

**Response** (200 OK):
```json
{
  "watchlist": [
    {
      "symbol": "AAPL",
      "created_at": "2026-07-08T12:00:00Z"
    },
    {
      "symbol": "SPY",
      "created_at": "2026-07-07T18:30:00Z"
    }
  ]
}
```

#### POST /investing/watchlist
Add a symbol to the user's watchlist.

**Auth**: Bearer token required
**Rate Limit**: 100/hour

**Request Body**:
```json
{
  "symbol": "AAPL"
}
```

**Response** (201 Created):
```json
{
  "symbol": "AAPL",
  "status": "added"
}
```

**Errors**:
- 400: Invalid symbol format
- 409: Symbol already in watchlist

#### DELETE /investing/watchlist/{symbol}
Remove a symbol from the watchlist.

**Auth**: Bearer token required
**Rate Limit**: 100/hour

**Path Params**:
- `symbol` (string): Stock ticker to remove

**Response** (204 No Content)

**Errors**:
- 404: Symbol not in watchlist

## Database Models

### User (existing, enhanced)
```python
class User:
    alpaca_account_id: str | None  # Alpaca account identifier
```

### InvestingOrder
Tracks all buy/sell orders placed through FAWN.

```python
class InvestingOrder:
    id: str                          # Primary key
    user_id: str                     # FK to User
    alpaca_order_id: str | None      # Alpaca's order ID
    symbol: str                      # Ticker (e.g., AAPL)
    side: str                        # buy | sell
    notional_cents: int | None       # Dollar amount (in cents)
    qty: float | None                # Share count
    status: str                      # pending | accepted | filled | failed
    idempotency_key: str             # Unique constraint (prevent duplicates)
    error_message: str | None        # If failed
    created_at: datetime
```

### InvestingWatchlist
User-saved list of symbols to track.

```python
class InvestingWatchlist:
    id: str                          # Primary key
    user_id: str                     # FK to User
    symbol: str                      # Ticker (e.g., AAPL)
    created_at: datetime
    # Unique constraint: (user_id, symbol)
```

## Security & Limits

### Rate Limiting
Enforced per endpoint to prevent abuse:

| Endpoint | Limit | Scope |
|----------|-------|-------|
| GET /investing/quotes/{symbol} | 120/minute | IP address |
| POST /investing/orders | 50/hour | User (JWT) |
| GET /investing/orders | 60/minute | IP address |
| GET /investing/positions | 60/minute | IP address |
| GET /investing/watchlist | 60/minute | IP address |
| POST /investing/watchlist | 100/hour | IP address |
| DELETE /investing/watchlist/{symbol} | 100/hour | IP address |

**Note**: Rate limits are enforced by slowapi at the transport layer and return HTTP 429 Too Many Requests when exceeded.

### Position Size Limits

- **Student max per order**: $50,000 USD
- **Minimum order**: $1 USD
- **Fractional shares**: Enabled (buy $1, $2.50, $99.99, etc.)

Limits are enforced server-side in the `POST /investing/orders` endpoint.

### Authentication

All endpoints except `/investing/quotes/{symbol}` require:
- **Header**: `Authorization: Bearer <jwt_token>`
- **Token source**: Provided on login via `/auth/login`
- **Scope**: User can only access their own account/orders/watchlist

### Idempotency

Order placement is idempotent:
- **Key**: `order:{user_id}:{symbol}:{side}:{amount}`
- **Collision handling**: If key matches, return existing order (HTTP 409)
- **Window**: Unlimited (based on DB uniqueness constraint)

## Frontend Implementation

### UI Components

#### Investing Tab
The investing tab includes:

1. **Portfolio Card**: Shows account equity, cash, and buying power
2. **Order Entry Form**:
   - Symbol search with live quote display
   - Dollar amount input (min $1)
   - Buy/Sell buttons
3. **Tab Navigation**:
   - Positions: Current holdings
   - History: Recent orders
   - Watchlist: Saved symbols

#### Quote Display
When user enters a symbol:
1. Type symbol (debounced search)
2. Click "Quote" button or press Enter
3. API call to `GET /investing/quotes/{symbol}`
4. Display bid, ask, last price
5. Show "Save to Watchlist" button

#### Order Confirmation
Before placing order:
1. Display symbol, side (buy/sell), amount
2. Show confirmation dialog
3. Execute `POST /investing/orders` on confirm
4. Show success/error toast
5. Refresh positions and order history

#### Position Card
Each position displays:
- Symbol name
- Quantity (to 4 decimals)
- Average entry price
- Current market value
- Unrealized P&L ($ and %)
- Color-coded: green for gains, red for losses

#### Order History
Shows recent orders (up to 20):
- Order date/time
- Symbol
- Side (buy 📥 / sell 📤)
- Quantity or dollar amount
- Execution price (if filled)
- Status badge (filled, pending, canceled)

#### Watchlist
Shows saved symbols:
- Click symbol to load quote and buy form
- Delete button to remove
- "Add to Watchlist" form for new additions

### JavaScript Functions

Key functions in `index.html`:

```javascript
// Load entire investing tab
loadInvesting()

// Get real-time quote
getQuote()

// Place order
placeInvestOrder(side: 'buy' | 'sell')

// Load positions
loadPositions()

// Load order history
loadOrders()

// Watchlist operations
loadWatchlist()
saveToWatchlist()
removeFromWatchlist(symbol)
loadSymbol(symbol)  // Click watchlist item to buy

// Tab switching
showInvestTab(tab: 'positions' | 'history' | 'watchlist')
```

## Error Handling

### Common Errors

**503 Service Unavailable** — Alpaca not configured or unreachable
```json
{
  "detail": "Investing isn't available yet."
}
```

**502 Bad Gateway** — Alpaca API error
```json
{
  "detail": "Investing provider error: ..."
}
```

**400 Bad Request** — Invalid input
```json
{
  "detail": "Amount must be positive."
}
```

**404 Not Found** — No account or item not found
```json
{
  "detail": "No investing account yet."
}
```

**409 Conflict** — Duplicate order or item already exists
```json
{
  "detail": "Duplicate order."
}
```

**429 Too Many Requests** — Rate limit exceeded
```json
{
  "detail": "rate limit exceeded"
}
```

### Frontend Error Recovery

- Quote fetch fails → Toast message, disable buy button
- Order placement fails → Show error toast, keep form state
- Rate limit hit → Toast "Too many requests, try again later"
- Network error → Retry with exponential backoff

## Alpaca Integration

### Account Lifecycle

1. **No Account** (initial state)
   - User sees "Open investing account" button
   - Frontend collects agreement checkboxes
   - Call `POST /investing/account` with agreements
   - Alpaca creates account (status: SUBMITTED or APPROVED)

2. **Pending Approval**
   - Account exists but status != ACTIVE
   - Trading disabled (returns 400 from POST /orders)
   - UI shows "Account under review"

3. **Active**
   - Account status = ACTIVE
   - User can place orders, view positions
   - Full trading access

### Funding

Users must fund their Alpaca account separately:
- Via ACH (1-3 business days)
- From external bank account
- Currently out of scope for FAWN (no bridge available yet)

### Data Sync

- **Quotes**: Real-time via Alpaca Quotes API (fresh on each request)
- **Orders**: Real-time via Alpaca Trading API
- **Positions**: Real-time via Alpaca Positions API
- **Account**: Real-time via Alpaca Account API

FAWN doesn't cache market data; all reads hit Alpaca directly.

## Configuration

### Environment Variables

```bash
# Alpaca Broker API credentials (required for investing feature)
ALPACA_API_KEY=<key>
ALPACA_API_SECRET=<secret>
ALPACA_BASE_URL=https://broker-api.sandbox.alpaca.markets  # Sandbox
# Production: https://broker-api.alpaca.markets
```

### Runtime Configuration

In `config.py`:
```python
alpaca_api_key: str = ""           # From env or .env file
alpaca_api_secret: str = ""        # From env or .env file
alpaca_base_url: str = "https://broker-api.sandbox.alpaca.markets"
```

## Deployment

### Railway Deployment

1. **Set env vars** in Railway project:
   - `ALPACA_API_KEY`
   - `ALPACA_API_SECRET`
   - `ALPACA_BASE_URL`

2. **Database migration** (automatic):
   - On app startup, `_init_db_schema()` creates `investing_watchlist` table
   - Existing tables auto-migrated (new columns appended)

3. **Verify**:
   ```bash
   curl https://api.fawn.app/investing/quotes/AAPL
   # Should return 200 (no auth needed for quotes)
   ```

### Local Development

1. Copy `.env.example` to `.env`
2. Add Alpaca sandbox credentials
3. Run: `python -m uvicorn main:app --reload`
4. Frontend on `http://localhost:3000` (if running)
5. Backend on `http://localhost:8000`

## Testing

### Unit Tests

Located in `tests/test_investing.py`:

```bash
pytest tests/test_investing.py -v
```

Test coverage:
- Quote fetching (valid symbol, invalid symbol, API error)
- Order placement (valid, invalid amount, rate limit, duplicate)
- Position listing (account exists, account missing)
- Watchlist operations (add, remove, duplicate, not found)
- Rate limiting (verify 429 after limit exceeded)

### Integration Testing

Against Alpaca Sandbox:

1. Create test account
2. Fund with $1,000 via Alpaca dashboard
3. Place $10 buy order (AAPL)
4. Verify order appears in history
5. Check position reflects holding
6. Sell $10 of position
7. Verify closed order

### Load Testing

Use `ab` or `locust`:

```bash
ab -n 1000 -c 10 \
  -H "Authorization: Bearer <token>" \
  http://localhost:8000/investing/quotes/AAPL

# Should return ~99% success (rate limiting triggers at 120/min per IP)
```

## Analytics & Monitoring

### Events Captured

Sent to PostHog (if `POSTHOG_API_KEY` set):

- `investing_order_placed`: Order placement success
  - Properties: symbol, side, notional, qty
- `investing_order_failed`: Order placement failed
  - Properties: symbol, side, error
- `investing_watchlist_add`: Added symbol to watchlist
  - Properties: symbol
- `investing_watchlist_delete`: Removed symbol from watchlist
  - Properties: symbol

### Logging

All trading activity logged to database table `investing_audit_log` for compliance.

Query recent trades:
```sql
SELECT * FROM investing_orders
  WHERE user_id = ?
  ORDER BY created_at DESC
  LIMIT 20;
```

## Future Enhancements

- [ ] WebSocket quotes for real-time price updates (vs 5s polling)
- [ ] Advanced orders (limit, stop-loss, trailing stop)
- [ ] Options trading
- [ ] Margin trading (after compliance review)
- [ ] Dividend/income tracking
- [ ] Tax-loss harvesting insights
- [ ] Portfolio rebalancing alerts
- [ ] Integration with USDC wallet for automatic funding

## Troubleshooting

### Quote Endpoint Returns 502

**Cause**: Alpaca API unreachable or symbol not found

**Fix**:
1. Verify `ALPACA_API_KEY` and `ALPACA_API_SECRET` are set
2. Check `ALPACA_BASE_URL` is correct (sandbox vs production)
3. Try another symbol (AAPL, SPY, BTC)
4. Check Alpaca status page

### Orders Not Appearing in History

**Cause**: Account not funded or recent orders not yet synced

**Fix**:
1. Verify account shows in `GET /investing/account` with `status: ACTIVE`
2. Fund account via ACH (1-3 days) or Alpaca dashboard
3. Refresh order history (manual or auto-refresh every 30s)

### Rate Limit 429 Errors

**Cause**: Exceeded per-endpoint limits

**Fix**:
- Wait for rate limit window to reset (per minute or per hour)
- Reduce frequency of API calls
- Batch requests where possible

### Watchlist Not Persisting

**Cause**: User logged out or browser cache cleared

**Fix**:
1. Watchlist is server-side, survives logout
2. Reload page to refresh from `GET /investing/watchlist`
3. Check browser console for API errors

## References

- [Alpaca Broker API Docs](https://broker-api.alpaca.markets)
- [Alpaca Account Setup](https://alpaca.markets/docs/account-setup/)
- [FastAPI Documentation](https://fastapi.tiangolo.com)
- [slowapi Rate Limiting](https://github.com/laurents/slowapi)
