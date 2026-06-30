"""
News service for FAWN.
Fetches headlines + summaries from major financial RSS feeds.
Returns full article excerpts so the frontend never needs to open external links.
"""
import httpx
import xml.etree.ElementTree as ET
import re
from config import settings

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# Major financial RSS feeds with descriptions/summaries
RSS_FEEDS = [
    ("CNBC",          "https://www.cnbc.com/id/10000664/device/rss/rss.html"),
    ("CNBC Markets",  "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("MarketWatch",   "https://feeds.marketwatch.com/marketwatch/topstories/"),
    ("Reuters",       "https://feeds.reuters.com/reuters/businessNews"),
    ("Yahoo Finance", "https://feeds.finance.yahoo.com/rss/2.0/headline?s=^GSPC&region=US&lang=en-US"),
    ("Yahoo Finance", "https://feeds.finance.yahoo.com/rss/2.0/headline?s=BTC-USD&region=US&lang=en-US"),
    ("Investopedia",  "https://www.investopedia.com/feedbuilder/feed/getfeed/?feedName=rss_articles"),
    ("Seeking Alpha", "https://seekingalpha.com/market_currents.xml"),
]

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


async def fetch_headlines(keywords: list[str] | None = None, limit: int = 30) -> list[dict]:
    """
    Fetch articles with title + summary passage from major RSS feeds.
    Filters by keywords if provided.
    Returns list of {title, summary, source, pub_date} dicts — no external links.
    """
    kw_lower = [k.lower() for k in keywords] if keywords else []
    results = []
    seen = set()

    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
        for source_name, feed_url in RSS_FEEDS:
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


async def summarize_financial_news(keywords: list[str] | None = None, limit: int = 30) -> dict:
    """Return filtered articles. AI summary is optional — raw summaries always work."""
    articles = await fetch_headlines(keywords=keywords, limit=limit)

    if not articles:
        articles = [{
            "title": "No stories matched your filters.",
            "summary": "Try different keywords or clear all filters to see all stories.",
            "source": "", "pub_date": "",
        }]

    return {"articles": articles, "ai_summary": None}
