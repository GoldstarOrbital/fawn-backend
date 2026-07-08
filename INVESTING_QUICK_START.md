# FAWN Investing — Quick Start Guide

## What's New

FAWN now supports investing! Students can:
- Search for stocks, ETFs, and crypto by symbol
- See real-time quotes (bid, ask, last price)
- Buy/sell with as little as $1 (fractional shares)
- Track their portfolio in one place
- Save symbols to a watchlist
- View complete order history
- Enforce $50k position limits (student safety)

## Quick Test

### 1. Open Frontend
```bash
cd fawn-frontend
# Open index.html in browser
# Or: python -m http.server 8000
```

### 2. Sign Up / Log In
- Create account or use existing credentials
- Frontend at: http://localhost:8000 (or GitHub Pages)

### 3. Go to "Investing" Tab
- Click the 📈 icon in the sidebar
- Should see "Open investing account" button (if no account yet)

### 4. Open Account
- Check the agreements checkbox
- Click "Open investing account"
- Account status → SUBMITTED (or APPROVED)

### 5. Get a Quote
- Type "AAPL" in the symbol box
- Click "Quote"
- See bid/ask prices and last trade price

### 6. Add to Watchlist
- Click "Save" button (💾) on the quote
- Check "Watchlist" tab
- Symbol appears in your saved list

### 7. Place an Order
- Enter symbol: AAPL
- Enter amount: $10
- Click "Buy"
- Confirm in dialog
- Order executes (market order, instant)

### 8. Check Portfolio
- "Positions" tab shows your holdings
- See qty, avg cost, market value, P&L
- "History" tab shows all past orders

## API Endpoints (Backend)

All require Bearer token auth (except public quotes).

### Public (No Auth)
```bash
curl https://api.fawn.app/investing/quotes/AAPL
```

### Requires Auth
```bash
# Get account
curl -H "Authorization: Bearer $TOKEN" https://api.fawn.app/investing/account

# Place order
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"symbol":"AAPL","side":"buy","notional":100}' \
  https://api.fawn.app/investing/orders

# Get positions
curl -H "Authorization: Bearer $TOKEN" https://api.fawn.app/investing/positions

# Get order history
curl -H "Authorization: Bearer $TOKEN" https://api.fawn.app/investing/orders

# Get watchlist
curl -H "Authorization: Bearer $TOKEN" https://api.fawn.app/investing/watchlist

# Add to watchlist
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"symbol":"SPY"}' \
  https://api.fawn.app/investing/watchlist

# Remove from watchlist
curl -X DELETE -H "Authorization: Bearer $TOKEN" \
  https://api.fawn.app/investing/watchlist/SPY
```

## Key Features

### Real-Time Quotes
- Type symbol (AAPL, SPY, BTC, ETH, etc.)
- See live bid/ask prices from Alpaca
- No auth needed (public market data)

### Smart Order Entry
- Dollar amount or share quantity
- Position limit: $50k max per order (student safety)
- Fractional shares: buy $1, $2.50, $99.99, etc.
- Market orders (instant execution, no pending limits)

### Portfolio Tracking
- All positions in one view
- Unrealized gain/loss (colored: green/red)
- Order history (20 recent orders)
- Filled price, status, timestamp

### Watchlist
- Save symbols to check later
- Click any watchlist item to load quote
- Delete to remove
- Stored server-side (survives logout)

### Security
- Rate limited: 50 orders/hour per user
- Position limit: $50k per trade
- Fractional shares prevent minimum-buy abuse
- All trades audit-logged for compliance

## Limits

| Action | Limit | Per |
|--------|-------|-----|
| Quote lookups | 120/minute | IP |
| Place order | 50/hour | User |
| View history | 60/minute | IP |
| View positions | 60/minute | IP |
| Save to watchlist | 100/hour | IP |

Rate limit exceeded? You'll get HTTP 429. Wait a minute and retry.

## Troubleshooting

### "Investing isn't available yet" (503)
→ Alpaca not configured. Check `ALPACA_API_KEY` and `ALPACA_API_SECRET` env vars on Railway.

### "Quote not found" (502)
→ Invalid symbol or Alpaca down. Try AAPL or SPY. Check Alpaca status page.

### "Order exceeds student limit" (400)
→ You tried to buy/sell more than $50,000 in one order. Split into smaller trades.

### "Duplicate order" (409)
→ Same order (symbol, side, amount) placed twice in quick succession. It's already pending.

### "No investing account yet" (404)
→ Open an account first via the frontend button. Takes ~10 seconds.

## Funding Your Account

To trade real money, you need to fund your Alpaca account:

1. Log into Alpaca (alpaca.markets)
2. Go to Account → Funding
3. Set up ACH transfer from your bank
4. Wait 1-3 business days for deposit
5. Money appears in your account; start trading

(Currently, FAWN doesn't auto-convert USDC to Alpaca cash — that's a future feature.)

## Support

- API docs: See `/INVESTING_FEATURE.md`
- Issues: Report on GitHub
- Feature requests: Email alex@getfawn.com

## Examples

### Buy 5 shares of Apple
```
Symbol: AAPL
Amount: $750  (5 × $150/share)
Side: Buy
→ Executed instantly
```

### Sell 2.5 shares of SPY
```
Symbol: SPY
Quantity: 2.5
Side: Sell
→ Closes 2.5 shares of existing position
```

### Buy $10 of Bitcoin
```
Symbol: BTC
Amount: $10
Side: Buy
→ Gets ~0.00025 BTC (fractional)
```

---

**You're ready to invest. Go to the Investing tab and start exploring! 🚀**
