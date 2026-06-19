"""
Auto-categorize transactions by description keyword matching.
No ML needed — rule-based is fast, free, and accurate enough for MVP.
"""

RULES: list[tuple[list[str], str, str]] = [
    # (keywords, category, emoji)
    (["uber", "lyft", "taxi", "grab", "waymo", "transit", "subway", "metro", "bus", "train", "amtrak", "delta", "united", "southwest", "american airlines", "spirit", "jetblue"], "Transport", "🚗"),
    (["doordash", "grubhub", "uber eats", "postmates", "instacart", "seamless", "caviar"], "Food Delivery", "🛵"),
    (["starbucks", "dunkin", "coffee", "espresso", "boba", "tea"], "Coffee", "☕"),
    (["mcdonald", "chipotle", "subway", "domino", "pizza", "burger", "taco", "wendy", "chick-fil", "shake shack", "five guys", "popeye", "kfc", "restaurant", "diner", "cafe", "bistro", "sushi", "ramen", "thai", "chinese", "indian"], "Dining", "🍔"),
    (["walmart", "target", "costco", "kroger", "whole foods", "trader joe", "aldi", "publix", "safeway", "wegman", "grocery", "supermarket", "market"], "Groceries", "🛒"),
    (["netflix", "hulu", "spotify", "disney", "hbo", "paramount", "apple tv", "youtube", "twitch", "gaming", "steam", "xbox", "playstation", "nintendo"], "Entertainment", "🎮"),
    (["amazon", "ebay", "etsy", "shein", "zara", "h&m", "nike", "adidas", "apple store", "best buy", "walmart.com", "shop", "store"], "Shopping", "🛍"),
    (["rent", "lease", "apartment", "housing", "landlord"], "Rent", "🏠"),
    (["electric", "gas", "water", "internet", "comcast", "xfinity", "verizon", "at&t", "t-mobile", "utility"], "Utilities", "💡"),
    (["tuition", "university", "college", "course", "textbook", "chegg", "coursera", "udemy", "khan"], "Education", "📚"),
    (["cvs", "walgreens", "pharmacy", "doctor", "hospital", "clinic", "health", "dental", "vision", "insurance"], "Health", "🏥"),
    (["venmo", "cashapp", "zelle", "paypal", "transfer", "payment to", "sent to"], "Transfer", "💸"),
    (["salary", "payroll", "deposit", "direct deposit", "income", "paycheck"], "Income", "💰"),
    (["atm", "withdrawal", "cash"], "Cash", "💵"),
    (["gym", "planet fitness", "la fitness", "equinox", "anytime fitness", "workout", "yoga", "crossfit"], "Fitness", "💪"),
    (["apple", "google", "microsoft", "adobe", "dropbox", "slack", "notion", "subscription"], "Subscriptions", "🔄"),
]

DEFAULT = ("Other", "📎")


def categorize(description: str) -> tuple[str, str]:
    """Return (category, emoji) for a transaction description."""
    lower = description.lower()
    for keywords, category, emoji in RULES:
        if any(kw in lower for kw in keywords):
            return category, emoji
    return DEFAULT
