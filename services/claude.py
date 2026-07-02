"""
News service for FAWN.
Fetches headlines + summaries from major financial and world-news RSS feeds,
and (when an Anthropic key is configured) generates a cached plain-English
AI digest of what the stories mean for a college student's money.
Returns full article excerpts so the frontend never needs to open external links.
"""
import time
import httpx
import xml.etree.ElementTree as ET
import re
from config import settings

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"  # cheap + fast; digests are short

# Feeds grouped by category so the UI can offer Markets / World / Crypto tabs.
# "markets" is the default and matches the original feed list.
RSS_FEEDS_BY_CATEGORY = {
    "markets": [
        ("CNBC",          "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
        ("CNBC Markets",  "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
        ("MarketWatch",   "https://feeds.marketwatch.com/marketwatch/topstories/"),
        ("Reuters",       "https://feeds.reuters.com/reuters/businessNews"),
        ("Yahoo Finance", "https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC&region=US&lang=en-US"),
        ("Investopedia",  "https://www.investopedia.com/feedbuilder/feed/getfeed/?feedName=rss_articles"),
        ("Seeking Alpha", "https://seekingalpha.com/market_currents.xml"),
    ],
    "world": [
        ("BBC World",     "https://feeds.bbci.co.uk/news/world/rss.xml"),
        ("BBC Business",  "https://feeds.bbci.co.uk/news/business/rss.xml"),
        ("NPR World",     "https://feeds.npr.org/1004/rss.xml"),
        ("Al Jazeera",    "https://www.aljazeera.com/xml/rss/all.xml"),
        ("The Guardian",  "https://www.theguardian.com/world/rss"),
    ],
    "crypto": [
        ("Yahoo Finance", "https://feeds.finance.yahoo.com/rss/2.0/headline?s=BTC-USD&region=US&lang=en-US"),
        ("CoinDesk",      "https://www.coindesk.com/arc/outboundfeeds/rss/"),
        ("Cointelegraph", "https://cointelegraph.com/rss"),
    ],
}
VALID_CATEGORIES = tuple(RSS_FEEDS_BY_CATEGORY.keys())

# Backward-compatible flat list (markets + the BTC feed), used when no
# category is specified so existing callers see the same behavior.
RSS_FEEDS = RSS_FEEDS_BY_CATEGORY["markets"] + [RSS_FEEDS_BY_CATEGORY["crypto"][0]]

def _clean(text: str) -> str:
    """Strip HTML tags and excessive whitespace from RSS description fields."""
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&quot;', '"', text)
    text = re.sub(r'&#\d+;', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    # Truncate to ~300 chars at a sentence boundary
    if len(text) > 320:
        cut = text[:320].rfind('. ')
        text = text[:cut + 1] if cut > 100 else text[:320] + '…'
    return text


async def fetch_headlines(keywords: list[str] | None = None, limit: int = 30, category: str | None = None) -> list[dict]:
    """
    Fetch articles with title + summary passage from major RSS feeds.
    Filters by keywords if provided; category picks a feed group
    (markets/world/crypto), defaulting to the original mixed list.
    Returns list of {title, summary, source, pub_date} dicts — no external links.
    """
    kw_lower = [k.lower() for k in keywords] if keywords else []
    feeds = RSS_FEEDS_BY_CATEGORY.get(category, RSS_FEEDS) if category else RSS_FEEDS
    results = []
    seen = set()

    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
        for source_name, feed_url in feeds:
            if len(results) >= limit:
                break
            try:
                resp = await client.get(feed_url, headers={"User-Agent": "FAWN-NewsReader/1.0"})
                if resp.status_code != 200:
                    continue

                # Parse RSS — handle both standard RSS and Atom
                root = ET.fromstring(resp.text)
                ns = {"atom": "http://www.w3.org/2005/Atom",
                      "media": "http://search.yahoo.com/mrss/"}

                for item in root.iter("item"):
                    title_el   = item.find("title")
                    desc_el    = item.find("description")
                    date_el    = item.find("pubDate")

                    title = title_el.text.strip() if title_el is not None and title_el.text else ""
                    if not title or title in seen:
                        continue

                    # Keyword filter
                    if kw_lower and not any(kw in title.lower() for kw in kw_lower):
                        # Also check description
                        raw_desc = (desc_el.text or "") if desc_el is not None else ""
                        if not any(kw in raw_desc.lower() for kw in kw_lower):
                            continue

                    summary = _clean((desc_el.text or "") if desc_el is not None else "")
                    # If summary is just a repeat of the title or too short, leave a note
                    if not summary or summary.lower().strip() == title.lower().strip() or len(summary) < 30:
                        summary = "Tap to read the key takeaway from this story."

                    pub_date = (date_el.text or "").strip() if date_el is not None else ""
                    # Shorten date to just "Jun 18, 9:42 AM" style
                    try:
                        from email.utils import parsedate_to_datetime
                        dt = parsedate_to_datetime(pub_date)
                        hour12 = dt.hour % 12 or 12
                        ampm = "AM" if dt.hour < 12 else "PM"
                        pub_date = f"{dt.month}/{dt.day} · {hour12}:{dt.minute:02d} {ampm}"
                    except Exception:
                        pub_date = pub_date[:16] if pub_date else ""

                    seen.add(title)
                    results.append({
                        "title":    title,
                        "summary":  summary,
                        "source":   source_name,
                        "pub_date": pub_date,
                    })
                    if len(results) >= limit:
                        break

            except Exception:
                continue

    return results


async def summarize_financial_news(keywords: list[str] | None = None, limit: int = 30, category: str | None = None) -> dict:
    """Return filtered articles. AI summary is optional — raw summaries always work."""
    articles = await fetch_headlines(keywords=keywords, limit=limit, category=category)

    if not articles:
        articles = [{
            "title": "No stories matched your filters.",
            "summary": "Try different keywords or clear all filters to see all stories.",
            "source": "", "pub_date": "",
        }]

    return {"articles": articles, "ai_summary": None}


# --- AI digest -------------------------------------------------------------
# In-process cache: {cache_key: (expires_at_epoch, digest_text)}. Digests are
# derived from public headlines (no user data), so sharing across users is
# fine and keeps Anthropic costs bounded no matter how often the UI polls.
_DIGEST_CACHE: dict[str, tuple[float, str]] = {}
_DIGEST_TTL_SECONDS = 300
_DIGEST_CACHE_MAX = 200


def _anthropic_configured() -> bool:
    key = settings.anthropic_api_key
    return bool(key) and key != "ANTHROPIC_KEY_NOT_SET"


async def generate_news_digest(articles: list[dict], focus: str | None = None) -> str | None:
    """Plain-English 'what this means for your money' digest of the given
    headlines, aimed at a college student. Returns None when no Anthropic
    key is configured or the call fails — callers must treat the digest as
    strictly optional and never block headlines on it.
    """
    if not _anthropic_configured() or not articles:
        return None

    headline_block = "\n".join(
        f"- {a['title']} ({a['source']}): {a['summary']}" for a in articles[:12] if a.get("title")
    )
    cache_key = f"{focus or ''}|{hash(headline_block)}"
    now = time.time()
    cached = _DIGEST_CACHE.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]

    focus_line = f' The reader searched for "{focus}" — prioritize relevance to that.' if focus else ""
    prompt = (
        "You write FAWN's news digest for U.S. college students. Below are current "
        f"headlines.{focus_line}\n\n{headline_block}\n\n"
        "In 3 short bullets (under 30 words each), explain in plain English what the most "
        "important of these stories actually mean for a college student's money — rent, "
        "groceries, student loans, part-time wages, savings. No jargon, no hype, no "
        "investment advice, no emojis. If nothing meaningfully affects students, say so honestly."
    )

    try:
        async with httpx.AsyncClient(timeout=25) as client:
            resp = await client.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key": settings.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": ANTHROPIC_MODEL,
                    "max_tokens": 400,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            if resp.status_code != 200:
                print(f"[news] digest call failed: {resp.status_code} {resp.text[:300]}")
                return None
            data = resp.json()
            digest = "".join(
                block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
            ).strip()
            if not digest:
                return None
    except Exception as e:
        print(f"[news] digest call raised: {e}")
        return None

    if len(_DIGEST_CACHE) >= _DIGEST_CACHE_MAX:
        _DIGEST_CACHE.clear()  # tiny cache — wholesale reset is fine
    _DIGEST_CACHE[cache_key] = (now + _DIGEST_TTL_SECONDS, digest)
    return digest


REVIEW_MODEL = "claude-sonnet-5"  # quality matters; one call per user request, rate-limited


async def generate_money_review(
    category_totals: dict[str, float],
    transactions_sample: list[dict],
    monthly_income_dollars: float | None = None,
    goals: str | None = None,
    pasted_data: str | None = None,
) -> str | None:
    """One-shot personal money review for a college student, consolidating
    the classic '10 money prompts' into a single pass over their REAL
    spending data: budget check, overspend/wins, subscription audit,
    negotiation targets, savings & emergency-fund step, debt note, food
    spend, big-purchase rule, income ideas, and 3 specific adjustments.

    Strictly budgeting/spending analysis — the prompt forbids investment
    advice and invented numbers. Returns None if no key or the call fails.
    """
    if not _anthropic_configured():
        return None

    parts = []
    if category_totals:
        parts.append("SPENDING BY CATEGORY THIS PERIOD (from their FAWN account):\n" +
                     "\n".join(f"- {cat}: ${amt:,.2f}" for cat, amt in sorted(category_totals.items(), key=lambda x: -x[1])))
    if transactions_sample:
        parts.append("RECENT TRANSACTIONS (sample):\n" +
                     "\n".join(f"- {t.get('date','')} {t.get('description','')}: ${abs(t.get('amount',0)):,.2f}"
                               for t in transactions_sample[:40]))
    if pasted_data:
        parts.append(f"DATA THE USER PASTED THEMSELVES:\n{pasted_data[:4000]}")
    if not parts:
        return None
    if monthly_income_dollars:
        parts.append(f"STATED MONTHLY INCOME: ${monthly_income_dollars:,.2f}")
    if goals:
        parts.append(f"THEIR STATED GOALS: {goals[:500]}")

    prompt = (
        "You are FAWN's money review assistant for U.S. college students. Run a complete "
        "monthly financial review over the data below, doing ALL of the following in one pass:\n"
        "1. BUDGET CHECK: infer a reasonable simple budget from their income (or from spending "
        "if no income given) and compare actual spending to it.\n"
        "2. OVERSPENT / DID WELL: name the specific categories, with their real numbers.\n"
        "3. SUBSCRIPTION AUDIT: flag recurring charges worth cancelling or downgrading.\n"
        "4. NEGOTIATION TARGETS: bills in the data a student could realistically call and negotiate.\n"
        "5. SAVINGS STEP: one concrete emergency-fund/savings action sized to their actual numbers.\n"
        "6. DEBT NOTE: only if debt payments appear in the data — otherwise skip silently.\n"
        "7. FOOD REALITY CHECK: dining/delivery/coffee vs groceries, with the actual totals.\n"
        "8. BIG-PURCHASE RULE: one sentence of guardrail relevant to their spending pattern.\n"
        "9. EARN IDEA: one realistic student income idea connected to something in their data.\n"
        "10. THREE ADJUSTMENTS: end with exactly three specific, numbered changes for next month, "
        "each with a dollar estimate of impact.\n\n"
        "HARD RULES: Use ONLY the numbers in the data — never invent amounts, merchants, or debts. "
        "If a section has no supporting data, say 'not enough data' or skip it rather than guessing. "
        "NO investment advice of any kind (no stocks, crypto, funds, or 'invest the difference'). "
        "Plain English, direct, non-judgmental. Under 450 words. Plain text only — no markdown "
        "symbols, use SECTION HEADINGS IN CAPS.\n\n" + "\n\n".join(parts)
    )

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                ANTHROPIC_URL,
                headers={
                    "x-api-key": settings.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": REVIEW_MODEL,
                    "max_tokens": 1500,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            if resp.status_code != 200:
                print(f"[money-review] call failed: {resp.status_code} {resp.text[:300]}")
                return None
            data = resp.json()
            review = "".join(
                b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
            ).strip()
            return review or None
    except Exception as e:
        print(f"[money-review] call raised: {e}")
        return None
