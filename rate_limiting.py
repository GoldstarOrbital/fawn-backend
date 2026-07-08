"""
Rate limiting configuration for FAWN API.

Per-endpoint limits protect against:
- Account enumeration (wallet creation)
- Transfer spam (transfers/send)
- Data export DOS (user/export)
- Brute force account deletion (user/delete)
- Fee collection manipulation (fees/collect)
"""
from slowapi import Limiter
from slowapi.util import get_remote_address

# Global limiter instance (shared across all routers)
limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])

# Per-endpoint rate limit configurations
RATE_LIMITS = {
    # Wallet operations
    "wallet_create": "5/hour",  # Creating wallet should be rare (max 5 per hour per IP)
    "wallet_balance": "60/minute",  # Reading balance is safe (frequent checks OK)

    # Transfer operations
    "transfer_send": "30/hour",  # Prevent transfer spam (max 30 per hour per IP)
    "transfer_history": "60/minute",  # Reading history is safe

    # User data operations
    "user_export": "1/day",  # GDPR export once per day per IP
    "user_delete": "1/day",  # Account deletion max once per day per IP

    # Admin operations
    "fees_collect": "6/hour",  # Fee collection max 6x per hour (every 10 min)
}
