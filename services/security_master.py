"""Master list of investable securities with performance data."""

DSPP_STOCKS = [
    {"ticker": "AAPL", "name": "Apple Inc.", "sector": "Technology", "industry": "Consumer Electronics", "pe_ratio": 28.5, "dividend_yield": 0.44, "market_cap_b": 3200, "52w_high": 199.62, "52w_low": 164.08, "ytd_return": 18.5, "one_year_return": 28.3, "five_year_return": 324.5},
    {"ticker": "MSFT", "name": "Microsoft Corporation", "sector": "Technology", "industry": "Software", "pe_ratio": 35.2, "dividend_yield": 0.75, "market_cap_b": 3100, "52w_high": 420.88, "52w_low": 309.33, "ytd_return": 22.4, "one_year_return": 35.2, "five_year_return": 420.8},
    {"ticker": "NVDA", "name": "NVIDIA Corporation", "sector": "Technology", "industry": "Semiconductors", "pe_ratio": 68.3, "dividend_yield": 0.03, "market_cap_b": 1850, "52w_high": 875.44, "52w_low": 311.97, "ytd_return": 95.2, "one_year_return": 188.5, "five_year_return": 2850.3},
    {"ticker": "TSLA", "name": "Tesla Inc.", "sector": "Consumer Cyclical", "industry": "Automotive", "pe_ratio": 52.1, "dividend_yield": 0.0, "market_cap_b": 880, "52w_high": 278.00, "52w_low": 138.80, "ytd_return": 8.2, "one_year_return": 14.5, "five_year_return": 580.2},
    {"ticker": "META", "name": "Meta Platforms Inc.", "sector": "Technology", "industry": "Internet", "pe_ratio": 22.5, "dividend_yield": 0.0, "market_cap_b": 1200, "52w_high": 601.02, "52w_low": 278.00, "ytd_return": 35.8, "one_year_return": 126.3, "five_year_return": 185.5},
    {"ticker": "GOOGL", "name": "Alphabet Inc.", "sector": "Technology", "industry": "Internet", "pe_ratio": 24.8, "dividend_yield": 0.0, "market_cap_b": 1750, "52w_high": 191.27, "52w_low": 98.86, "ytd_return": 28.5, "one_year_return": 68.5, "five_year_return": 285.3},
    {"ticker": "JPM", "name": "JPMorgan Chase & Co.", "sector": "Financials", "industry": "Banking", "pe_ratio": 12.5, "dividend_yield": 2.55, "market_cap_b": 520, "52w_high": 210.20, "52w_low": 160.55, "ytd_return": 32.5, "one_year_return": 42.3, "five_year_return": 95.2},
    {"ticker": "BAC", "name": "Bank of America Corp.", "sector": "Financials", "industry": "Banking", "pe_ratio": 11.2, "dividend_yield": 2.82, "market_cap_b": 320, "52w_high": 38.98, "52w_low": 28.51, "ytd_return": 28.4, "one_year_return": 35.8, "five_year_return": 42.5},
    {"ticker": "JNJ", "name": "Johnson & Johnson", "sector": "Healthcare", "industry": "Pharmaceuticals", "pe_ratio": 23.4, "dividend_yield": 3.18, "market_cap_b": 360, "52w_high": 161.73, "52w_low": 144.74, "ytd_return": 8.5, "one_year_return": 12.3, "five_year_return": 85.2},
    {"ticker": "UNH", "name": "UnitedHealth Group Inc.", "sector": "Healthcare", "industry": "Healthcare Services", "pe_ratio": 28.9, "dividend_yield": 1.28, "market_cap_b": 490, "52w_high": 558.17, "52w_low": 370.80, "ytd_return": 15.2, "one_year_return": 38.5, "five_year_return": 185.5},
    {"ticker": "PFE", "name": "Pfizer Inc.", "sector": "Healthcare", "industry": "Pharmaceuticals", "pe_ratio": 12.8, "dividend_yield": 6.18, "market_cap_b": 165, "52w_high": 32.13, "52w_low": 20.60, "ytd_return": -8.5, "one_year_return": -22.3, "five_year_return": 25.2},
    {"ticker": "BA", "name": "The Boeing Company", "sector": "Industrials", "industry": "Aerospace & Defense", "pe_ratio": 18.5, "dividend_yield": 0.0, "market_cap_b": 130, "52w_high": 212.87, "52w_low": 155.18, "ytd_return": 35.2, "one_year_return": 48.5, "five_year_return": -15.3},
    {"ticker": "WMT", "name": "Walmart Inc.", "sector": "Consumer Defensive", "industry": "Retail", "pe_ratio": 31.2, "dividend_yield": 0.73, "market_cap_b": 480, "52w_high": 98.50, "52w_low": 73.35, "ytd_return": 8.2, "one_year_return": 28.5, "five_year_return": 65.2},
    {"ticker": "KO", "name": "The Coca-Cola Company", "sector": "Consumer Defensive", "industry": "Beverages", "pe_ratio": 26.5, "dividend_yield": 3.06, "market_cap_b": 280, "52w_high": 63.22, "52w_low": 46.10, "ytd_return": 12.5, "one_year_return": 18.5, "five_year_return": 32.5},
    {"ticker": "XOM", "name": "Exxon Mobil Corporation", "sector": "Energy", "industry": "Oil & Gas", "pe_ratio": 9.8, "dividend_yield": 3.45, "market_cap_b": 440, "52w_high": 120.51, "52w_low": 75.02, "ytd_return": 18.5, "one_year_return": 42.3, "five_year_return": 85.2},
    {"ticker": "VZ", "name": "Verizon Communications Inc.", "sector": "Communication Services", "industry": "Telecom", "pe_ratio": 8.2, "dividend_yield": 6.82, "market_cap_b": 240, "52w_high": 42.50, "52w_low": 35.83, "ytd_return": 2.5, "one_year_return": 8.5, "five_year_return": 15.2},
    {"ticker": "T", "name": "AT&T Inc.", "sector": "Communication Services", "industry": "Telecom", "pe_ratio": 7.5, "dividend_yield": 7.12, "market_cap_b": 135, "52w_high": 21.51, "52w_low": 16.62, "ytd_return": -5.2, "one_year_return": 2.5, "five_year_return": -8.5},
    {"ticker": "V", "name": "Visa Inc.", "sector": "Financials", "industry": "Payment Processors", "pe_ratio": 42.3, "dividend_yield": 0.67, "market_cap_b": 680, "52w_high": 308.25, "52w_low": 198.37, "ytd_return": 18.5, "one_year_return": 42.3, "five_year_return": 285.5},
    {"ticker": "MA", "name": "Mastercard Incorporated", "sector": "Financials", "industry": "Payment Processors", "pe_ratio": 38.5, "dividend_yield": 0.48, "market_cap_b": 600, "52w_high": 528.33, "52w_low": 314.81, "ytd_return": 22.5, "one_year_return": 45.8, "five_year_return": 325.2},
    {"ticker": "AMZN", "name": "Amazon.com Inc.", "sector": "Consumer Cyclical", "industry": "E-commerce", "pe_ratio": 52.8, "dividend_yield": 0.0, "market_cap_b": 1950, "52w_high": 198.88, "52w_low": 101.26, "ytd_return": 42.5, "one_year_return": 85.3, "five_year_return": 285.5},
    {"ticker": "IBM", "name": "International Business Machines", "sector": "Technology", "industry": "Software", "pe_ratio": 14.5, "dividend_yield": 3.25, "market_cap_b": 225, "52w_high": 238.42, "52w_low": 168.51, "ytd_return": 12.5, "one_year_return": 28.5, "five_year_return": 15.2},
    {"ticker": "AMD", "name": "Advanced Micro Devices Inc.", "sector": "Technology", "industry": "Semiconductors", "pe_ratio": 185.3, "dividend_yield": 0.0, "market_cap_b": 215, "52w_high": 227.41, "52w_low": 107.80, "ytd_return": 68.5, "one_year_return": 112.3, "five_year_return": 285.2},
    {"ticker": "INTC", "name": "Intel Corporation", "sector": "Technology", "industry": "Semiconductors", "pe_ratio": 8.9, "dividend_yield": 4.75, "market_cap_b": 190, "52w_high": 43.89, "52w_low": 20.18, "ytd_return": -25.5, "one_year_return": -42.8, "five_year_return": -65.2},
    {"ticker": "CRM", "name": "Salesforce Inc.", "sector": "Technology", "industry": "Software", "pe_ratio": 124.5, "dividend_yield": 0.0, "market_cap_b": 310, "52w_high": 368.97, "52w_low": 238.64, "ytd_return": 28.5, "one_year_return": 42.5, "five_year_return": 185.2},
    {"ticker": "ADBE", "name": "Adobe Inc.", "sector": "Technology", "industry": "Software", "pe_ratio": 42.8, "dividend_yield": 0.0, "market_cap_b": 190, "52w_high": 662.80, "52w_low": 425.42, "ytd_return": 22.5, "one_year_return": 35.8, "five_year_return": 125.2},
    {"ticker": "QCOM", "name": "QUALCOMM Incorporated", "sector": "Technology", "industry": "Semiconductors", "pe_ratio": 15.2, "dividend_yield": 1.85, "market_cap_b": 250, "52w_high": 235.99, "52w_low": 141.78, "ytd_return": 28.5, "one_year_return": 48.5, "five_year_return": 85.2},
]

ETF_LIST = [
    {"ticker": "VTI", "name": "Vanguard Total Stock Market ETF", "asset_type": "etf", "expense_ratio": 0.0003, "holdings_count": 3500, "ytd_return": 18.5, "one_year_return": 28.5, "five_year_return": 95.2},
    {"ticker": "VOO", "name": "Vanguard S&P 500 ETF", "asset_type": "etf", "expense_ratio": 0.0003, "holdings_count": 500, "ytd_return": 22.4, "one_year_return": 32.5, "five_year_return": 105.3},
    {"ticker": "QQQ", "name": "Invesco QQQ Trust", "asset_type": "etf", "expense_ratio": 0.0020, "holdings_count": 100, "ytd_return": 35.2, "one_year_return": 62.5, "five_year_return": 385.2},
    {"ticker": "BND", "name": "Vanguard Total Bond Market ETF", "asset_type": "etf", "expense_ratio": 0.0003, "holdings_count": 9000, "ytd_return": -2.5, "one_year_return": 5.2, "five_year_return": 18.5},
    {"ticker": "AGG", "name": "iShares Core US Aggregate Bond ETF", "asset_type": "etf", "expense_ratio": 0.0003, "holdings_count": 9000, "ytd_return": -2.8, "one_year_return": 4.8, "five_year_return": 16.2},
    {"ticker": "SCHD", "name": "Schwab US Dividend Equity ETF", "asset_type": "etf", "expense_ratio": 0.0006, "holdings_count": 300, "ytd_return": 15.2, "one_year_return": 25.8, "five_year_return": 85.2},
    {"ticker": "SPY", "name": "SPDR S&P 500 ETF Trust", "asset_type": "etf", "expense_ratio": 0.0009, "holdings_count": 500, "ytd_return": 22.5, "one_year_return": 32.8, "five_year_return": 105.8},
    {"ticker": "XLK", "name": "Technology Select Sector SPDR", "asset_type": "etf", "expense_ratio": 0.0010, "holdings_count": 65, "ytd_return": 28.5, "one_year_return": 52.5, "five_year_return": 285.2},
    {"ticker": "NOBL", "name": "ProShares S&P 500 Dividend Aristocrats ETF", "asset_type": "etf", "expense_ratio": 0.0035, "holdings_count": 65, "ytd_return": 12.5, "one_year_return": 22.8, "five_year_return": 65.2},
    {"ticker": "VUG", "name": "Vanguard Growth ETF", "asset_type": "etf", "expense_ratio": 0.0004, "holdings_count": 600, "ytd_return": 32.5, "one_year_return": 58.5, "five_year_return": 285.2},
]

def get_master_securities() -> list[dict]:
    """Return all available securities with full data."""
    stocks = [
        {
            **s,
            "asset_type": "stock",
            "has_dspp": True,
            "dspp_provider": "computershare",
        }
        for s in DSPP_STOCKS
    ]

    etfs = [
        {
            **e,
            "has_dspp": False,
            "dspp_provider": None,
        }
        for e in ETF_LIST
    ]

    return stocks + etfs

def search_securities(query: str, asset_type: str | None = None) -> list[dict]:
    """Search for securities by ticker or name."""
    all_secs = get_master_securities()
    query_lower = query.lower()

    results = [
        s for s in all_secs
        if (query_lower in s["ticker"].lower() or query_lower in s["name"].lower())
        and (asset_type is None or s["asset_type"] == asset_type)
    ]
    return results[:20]

def get_security(ticker: str) -> dict | None:
    """Fetch a single security by ticker with full data."""
    results = [s for s in get_master_securities() if s["ticker"] == ticker.upper()]
    return results[0] if results else None
