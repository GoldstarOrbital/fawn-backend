"""Master list of investable securities available via DSPP or ETF providers.

Pre-populated with:
- ~50 major US companies with active Computershare DSPPs
- ~20 popular, low-cost ETFs (index funds suitable for students)

In production, this would sync daily with SEC filings, DSPP provider catalogs,
and ETF issuer data. For MVP, it's a static curated list.
"""

# Major US companies with active Computershare DSPPs
# Source: https://www.computershare.com/us/online (representative sample)
DSPP_STOCKS = [
    # Tech
    {"ticker": "AAPL", "name": "Apple Inc.", "sector": "Technology", "industry": "Consumer Electronics"},
    {"ticker": "MSFT", "name": "Microsoft Corporation", "sector": "Technology", "industry": "Software"},
    {"ticker": "NVDA", "name": "NVIDIA Corporation", "sector": "Technology", "industry": "Semiconductors"},
    {"ticker": "TSLA", "name": "Tesla Inc.", "sector": "Consumer Cyclical", "industry": "Automotive"},
    {"ticker": "META", "name": "Meta Platforms Inc.", "sector": "Technology", "industry": "Internet"},
    {"ticker": "GOOGL", "name": "Alphabet Inc.", "sector": "Technology", "industry": "Internet"},
    # Finance
    {"ticker": "JPM", "name": "JPMorgan Chase & Co.", "sector": "Financials", "industry": "Banking"},
    {"ticker": "BAC", "name": "Bank of America Corp.", "sector": "Financials", "industry": "Banking"},
    {"ticker": "GS", "name": "The Goldman Sachs Group Inc.", "sector": "Financials", "industry": "Investment Banking"},
    {"ticker": "BLK", "name": "BlackRock Inc.", "sector": "Financials", "industry": "Asset Management"},
    # Healthcare
    {"ticker": "JNJ", "name": "Johnson & Johnson", "sector": "Healthcare", "industry": "Pharmaceuticals"},
    {"ticker": "UNH", "name": "UnitedHealth Group Inc.", "sector": "Healthcare", "industry": "Healthcare Services"},
    {"ticker": "PFE", "name": "Pfizer Inc.", "sector": "Healthcare", "industry": "Pharmaceuticals"},
    {"ticker": "ABBV", "name": "AbbVie Inc.", "sector": "Healthcare", "industry": "Pharmaceuticals"},
    # Industrial
    {"ticker": "BA", "name": "The Boeing Company", "sector": "Industrials", "industry": "Aerospace & Defense"},
    {"ticker": "CAT", "name": "Caterpillar Inc.", "sector": "Industrials", "industry": "Heavy Machinery"},
    {"ticker": "GE", "name": "General Electric Company", "sector": "Industrials", "industry": "Conglomerates"},
    # Consumer
    {"ticker": "WMT", "name": "Walmart Inc.", "sector": "Consumer Defensive", "industry": "Retail"},
    {"ticker": "PG", "name": "Procter & Gamble Co.", "sector": "Consumer Defensive", "industry": "Household Products"},
    {"ticker": "KO", "name": "The Coca-Cola Company", "sector": "Consumer Defensive", "industry": "Beverages"},
    {"ticker": "MCD", "name": "McDonald's Corporation", "sector": "Consumer Cyclical", "industry": "Food & Beverage"},
    # Energy
    {"ticker": "XOM", "name": "Exxon Mobil Corporation", "sector": "Energy", "industry": "Oil & Gas"},
    {"ticker": "CVX", "name": "Chevron Corporation", "sector": "Energy", "industry": "Oil & Gas"},
    # Utilities
    {"ticker": "NEE", "name": "NextEra Energy Inc.", "sector": "Utilities", "industry": "Electric Utilities"},
    {"ticker": "DUK", "name": "Duke Energy Corporation", "sector": "Utilities", "industry": "Electric Utilities"},
    # Real Estate
    {"ticker": "DLR", "name": "Digital Realty Trust Inc.", "sector": "Real Estate", "industry": "REITs"},
    # Communication
    {"ticker": "VZ", "name": "Verizon Communications Inc.", "sector": "Communication Services", "industry": "Telecom"},
    {"ticker": "T", "name": "AT&T Inc.", "sector": "Communication Services", "industry": "Telecom"},
    # Additional blue chips
    {"ticker": "V", "name": "Visa Inc.", "sector": "Financials", "industry": "Payment Processors"},
    {"ticker": "MA", "name": "Mastercard Incorporated", "sector": "Financials", "industry": "Payment Processors"},
    {"ticker": "AMZN", "name": "Amazon.com Inc.", "sector": "Consumer Cyclical", "industry": "E-commerce"},
    {"ticker": "IBM", "name": "International Business Machines", "sector": "Technology", "industry": "IT Services"},
    {"ticker": "AMD", "name": "Advanced Micro Devices Inc.", "sector": "Technology", "industry": "Semiconductors"},
    {"ticker": "INTC", "name": "Intel Corporation", "sector": "Technology", "industry": "Semiconductors"},
    {"ticker": "F", "name": "Ford Motor Company", "sector": "Consumer Cyclical", "industry": "Automotive"},
    {"ticker": "GM", "name": "General Motors Company", "sector": "Consumer Cyclical", "industry": "Automotive"},
    {"ticker": "CSCO", "name": "Cisco Systems Inc.", "sector": "Technology", "industry": "Networking"},
    {"ticker": "PYPL", "name": "PayPal Holdings Inc.", "sector": "Financials", "industry": "Payment Processors"},
    {"ticker": "NFLX", "name": "Netflix Inc.", "sector": "Communication Services", "industry": "Entertainment"},
    {"ticker": "DIS", "name": "The Walt Disney Company", "sector": "Communication Services", "industry": "Entertainment"},
    # More DSPPs (expanded coverage)
    {"ticker": "CRM", "name": "Salesforce Inc.", "sector": "Technology", "industry": "Software"},
    {"ticker": "ADBE", "name": "Adobe Inc.", "sector": "Technology", "industry": "Software"},
    {"ticker": "QCOM", "name": "Qualcomm Inc.", "sector": "Technology", "industry": "Semiconductors"},
    {"ticker": "MU", "name": "Micron Technology Inc.", "sector": "Technology", "industry": "Semiconductors"},
    {"ticker": "TXN", "name": "Texas Instruments Inc.", "sector": "Technology", "industry": "Semiconductors"},
    {"ticker": "AMAT", "name": "Applied Materials Inc.", "sector": "Technology", "industry": "Semiconductors"},
    {"ticker": "LRCX", "name": "Lam Research Corporation", "sector": "Technology", "industry": "Semiconductors"},
    {"ticker": "ASML", "name": "ASML Holding N.V.", "sector": "Technology", "industry": "Semiconductors"},
    {"ticker": "TSM", "name": "Taiwan Semiconductor Manufacturing", "sector": "Technology", "industry": "Semiconductors"},
    {"ticker": "SNPS", "name": "Synopsys Inc.", "sector": "Technology", "industry": "Software"},
    {"ticker": "CDNS", "name": "Cadence Design Systems Inc.", "sector": "Technology", "industry": "Software"},
    {"ticker": "CRWD", "name": "CrowdStrike Holdings Inc.", "sector": "Technology", "industry": "Cybersecurity"},
    {"ticker": "NET", "name": "Cloudflare Inc.", "sector": "Technology", "industry": "Cybersecurity"},
    {"ticker": "ACN", "name": "Accenture plc", "sector": "Technology", "industry": "IT Services"},
    {"ticker": "INTU", "name": "Intuit Inc.", "sector": "Technology", "industry": "Software"},
    {"ticker": "OKTA", "name": "Okta Inc.", "sector": "Technology", "industry": "Cybersecurity"},
    {"ticker": "TWLO", "name": "Twilio Inc.", "sector": "Technology", "industry": "Software"},
    {"ticker": "HAS", "name": "Hasbro Inc.", "sector": "Consumer Cyclical", "industry": "Toys & Games"},
    {"ticker": "LEGO", "name": "LEGO Group", "sector": "Consumer Cyclical", "industry": "Toys & Games"},
    {"ticker": "RY", "name": "Royal Bank of Canada", "sector": "Financials", "industry": "Banking"},
    {"ticker": "TD", "name": "Toronto-Dominion Bank", "sector": "Financials", "industry": "Banking"},
    {"ticker": "WFC", "name": "Wells Fargo & Company", "sector": "Financials", "industry": "Banking"},
    {"ticker": "WU", "name": "The Western Union Company", "sector": "Financials", "industry": "Money Transfer"},
    {"ticker": "SQ", "name": "Square Inc.", "sector": "Financials", "industry": "Payment Processors"},
    {"ticker": "PAYX", "name": "Paychex Inc.", "sector": "Technology", "industry": "Software"},
    {"ticker": "ADP", "name": "Automatic Data Processing Inc.", "sector": "Technology", "industry": "Software"},
    {"ticker": "STX", "name": "Seagate Technology Holdings", "sector": "Technology", "industry": "Storage"},
    {"ticker": "WDC", "name": "Western Digital Corporation", "sector": "Technology", "industry": "Storage"},
    {"ticker": "KEYS", "name": "Keysight Technologies Inc.", "sector": "Technology", "industry": "Test & Measurement"},
    {"ticker": "ENPH", "name": "Enphase Energy Inc.", "sector": "Industrials", "industry": "Clean Energy"},
    {"ticker": "SEDG", "name": "SolarEdge Technologies Inc.", "sector": "Industrials", "industry": "Clean Energy"},
    {"ticker": "RUN", "name": "Sunrun Inc.", "sector": "Utilities", "industry": "Renewable Energy"},
    {"ticker": "PLUG", "name": "Plug Power Inc.", "sector": "Industrials", "industry": "Fuel Cells"},
    {"ticker": "FCEL", "name": "FuelCell Energy Inc.", "sector": "Industrials", "industry": "Fuel Cells"},
    {"ticker": "BLNK", "name": "Blink Charging Co.", "sector": "Industrials", "industry": "EV Charging"},
    {"ticker": "CCIV", "name": "Lucid Group Inc.", "sector": "Consumer Cyclical", "industry": "Automotive"},
    {"ticker": "XPEV", "name": "XPeng Inc.", "sector": "Consumer Cyclical", "industry": "Automotive"},
    {"ticker": "NIO", "name": "NIO Inc.", "sector": "Consumer Cyclical", "industry": "Automotive"},
    {"ticker": "LI", "name": "Li Auto Inc.", "sector": "Consumer Cyclical", "industry": "Automotive"},
]

# Major ETFs suitable for college students (low cost, diversified, long-term)
ETF_LIST = [
    # Broad market index ETFs
    {"ticker": "VTI", "name": "Vanguard Total Stock Market ETF", "expense_ratio": 0.0003, "holdings_count": 3500},
    {"ticker": "ITOT", "name": "iShares Core S&P Total US Stock Mkt ETF", "expense_ratio": 0.0003, "holdings_count": 3400},
    {"ticker": "SWTSX", "name": "Schwab US Total Stock Market ETF", "expense_ratio": 0.0003, "holdings_count": 3400},
    # S&P 500 index ETFs
    {"ticker": "VOO", "name": "Vanguard S&P 500 ETF", "expense_ratio": 0.0003, "holdings_count": 500},
    {"ticker": "IVV", "name": "iShares Core S&P 500 ETF", "expense_ratio": 0.0003, "holdings_count": 500},
    {"ticker": "SPY", "name": "SPDR S&P 500 ETF Trust", "expense_ratio": 0.0009, "holdings_count": 500},
    # Small/mid cap
    {"ticker": "VB", "name": "Vanguard Small-Cap ETF", "expense_ratio": 0.0005, "holdings_count": 1400},
    {"ticker": "IJH", "name": "iShares Core S&P Mid-Cap ETF", "expense_ratio": 0.0005, "holdings_count": 700},
    # International
    {"ticker": "VXUS", "name": "Vanguard Total International Stock ETF", "expense_ratio": 0.0008, "holdings_count": 7000},
    {"ticker": "IXUS", "name": "iShares Core MSCI Intl Developed Mkt ETF", "expense_ratio": 0.0008, "holdings_count": 3200},
    # Bonds
    {"ticker": "BND", "name": "Vanguard Total Bond Market ETF", "expense_ratio": 0.0003, "holdings_count": 9000},
    {"ticker": "AGG", "name": "iShares Core US Aggregate Bond ETF", "expense_ratio": 0.0003, "holdings_count": 9000},
    # Target date (student friendly — auto-rebalances)
    {"ticker": "VFFVX", "name": "Vanguard Freedom 2050 ETF", "expense_ratio": 0.0008, "holdings_count": None},
    {"ticker": "VFTAX", "name": "Vanguard Freedom 2045 ETF", "expense_ratio": 0.0008, "holdings_count": None},
    # Tech-heavy (higher risk, student interest)
    {"ticker": "QQQ", "name": "Invesco QQQ Trust (Nasdaq-100)", "expense_ratio": 0.0020, "holdings_count": 100},
    {"ticker": "XLK", "name": "Technology Select Sector SPDR", "expense_ratio": 0.0010, "holdings_count": 65},
    # Dividend aristocrats (passive income appeal)
    {"ticker": "NOBL", "name": "ProShares S&P 500 Dividend Aristocrats ETF", "expense_ratio": 0.0035, "holdings_count": 65},
    {"ticker": "SCHD", "name": "Schwab US Dividend Equity ETF", "expense_ratio": 0.0006, "holdings_count": 300},
    # Growth vs Value
    {"ticker": "VUG", "name": "Vanguard Growth ETF", "expense_ratio": 0.0004, "holdings_count": 600},
    {"ticker": "VTV", "name": "Vanguard Value ETF", "expense_ratio": 0.0004, "holdings_count": 600},
]


def get_master_securities() -> list[dict]:
    """Return all available securities (stocks + ETFs) for the app.

    Each security is marked with its provider and DSPP availability.
    """
    # Stocks with DSPP
    stocks = [
        {
            **s,
            "asset_type": "stock",
            "has_dspp": True,
            "dspp_provider": "computershare",
        }
        for s in DSPP_STOCKS
    ]

    # ETFs (no DSPP, but can be purchased through ETF provider)
    etfs = [
        {
            **e,
            "asset_type": "etf",
            "has_dspp": False,
            "dspp_provider": None,
        }
        for e in ETF_LIST
    ]

    return stocks + etfs


def search_securities(query: str, asset_type: str | None = None) -> list[dict]:
    """Search for a security by ticker or name."""
    all_secs = get_master_securities()
    query_lower = query.lower()

    results = [
        s for s in all_secs
        if (query_lower in s["ticker"].lower() or query_lower in s["name"].lower())
        and (asset_type is None or s["asset_type"] == asset_type)
    ]
    return results[:20]  # return top 20


def get_security(ticker: str) -> dict | None:
    """Fetch a single security by ticker."""
    results = [s for s in get_master_securities() if s["ticker"] == ticker.upper()]
    return results[0] if results else None
