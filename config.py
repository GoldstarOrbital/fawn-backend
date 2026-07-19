from pydantic import field_validator
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    database_url: str = "sqlite:///./fawn.db"

    # ---- Third-party integrations ----
    # FAWN is self-custodial/crypto-native. The only remaining third
    # parties are Alpaca (investing) and Plaid (bank-account linking).
    # Every client is guarded: an unset key raises a clear
    # "<provider> is not configured" error instead of calling out with a
    # bad key.

    # Alpaca — brokerage / investing (Broker API)
    alpaca_api_key: str = ""
    alpaca_api_secret: str = ""
    alpaca_base_url: str = "https://broker-api.sandbox.alpaca.markets"

    # Plaid — bank-account linking (funding source switching)
    plaid_client_id: str = ""
    plaid_secret: str = ""
    plaid_env: str = "sandbox"  # sandbox | production
    plaid_base_url: str = "https://sandbox.plaid.com"

    # ---- Buy Crypto (on-ramp aggregator) ----
    # Multi-provider fiat-to-USDC on-ramp, embedded in the Add Funds modal.
    # These are PUBLISHABLE / host keys, not secrets — safe to expose to the
    # client (that's how each provider's widget SDK is designed to work).
    # A provider tab is hidden client-side if its key is unset.
    ramp_host_app_id: str = ""
    moonpay_api_key: str = ""
    transak_api_key: str = ""
    transak_env: str = "STAGING"  # STAGING | PRODUCTION

    # Coinbase Onramp is the one exception: it requires a server-signed
    # session token (CDP API key pair, kept secret) rather than a public
    # client-side key. See services/coinbase_onramp.py.
    coinbase_onramp_project_id: str = ""
    coinbase_cdp_api_key_name: str = ""
    coinbase_cdp_api_key_secret: str = ""

    anthropic_api_key: str = "ANTHROPIC_KEY_NOT_SET"
    jwt_secret: str = "dev_secret_change_in_production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60
    resend_api_key: str = ""
    from_email: str = "alex@getfawn.com"
    admin_api_key: str = ""

    # Campus Savings gas-price freshness. Prices are hand-verified (no honest
    # free per-station feed exists), so instead of faking live data we track
    # when they were last verified and flag staleness. Bump GAS_VERIFIED_DATE
    # (YYYY-MM-DD) on Railway whenever you re-verify — no code change needed.
    gas_verified_date: str = "2026-06-19"
    gas_stale_after_days: int = 14
    stripe_secret_key: str = ""
    stripe_publishable_key: str = ""
    stripe_webhook_secret: str = ""
    allow_unsigned_stripe_webhooks: bool = False
    fawn_encryption_key: str = ""  # Encryption key for custodial wallet private keys
    uniswap_api_key: str = ""  # Uniswap v3 API key for trading quotes
    alchemy_api_key: str = ""  # Alchemy RPC for blockchain monitoring (Polygon)
    gas_station_private_key: str = ""  # FAWN-controlled wallet that sponsors gas for custodial-wallet sends

    # ---- Custody hardening ----
    # A compromised session, a bug, or a leaked key should never be able to
    # drain a wallet in one shot -- hard caps contain the blast radius
    # regardless of what went wrong upstream. Defaults are generous for a
    # student P2P app but bound worst-case loss; override via env vars if
    # these ever need to flex for a specific rollout.
    max_send_cents_per_tx: int = 200_000  # $2,000 per single send
    max_send_cents_per_day: int = 500_000  # $5,000 per user per rolling 24h
    max_gas_topups_per_day: int = 200  # platform-wide, protects the gas station wallet from a runaway loop

    # ---- Fraud & risk controls ----
    # Dollar caps alone don't catch a compromised account rapidly draining
    # via many small transactions, each individually under the per-tx cap --
    # velocity limits bound transaction COUNT, independent of amount.
    max_sends_per_hour: int = 10
    max_sends_per_day: int = 30
    # A first-time send to a never-before-seen recipient, above this
    # amount, is held for manual review instead of settling immediately --
    # the classic account-takeover pattern is immediate drain to a new,
    # attacker-controlled address. Set to the per-tx cap to effectively
    # disable this (every send already under review-equivalent scrutiny
    # via the per-tx cap) if it's ever too aggressive for real usage.
    new_recipient_review_threshold_cents: int = 50_000  # $500
    allowed_origins: str = (
        "https://goldstarorbital.com,"
        "https://www.goldstarorbital.com,"
        "https://goldstarorbital.github.io,"
        "http://localhost:3000,"
        "http://localhost:3001,"
        "http://localhost:8080,"
        "http://127.0.0.1:5500"
    )

    @field_validator("database_url")
    @classmethod
    def fix_postgres_dialect(cls, v: str) -> str:
        # Railway provides postgresql:// but SQLAlchemy 2.x needs postgresql+psycopg2://
        if v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+psycopg2://", 1)
        return v

    class Config:
        env_file = ".env"

    @property
    def allowed_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]

settings = Settings()

_JWT_FORBIDDEN = (
    "dev_secret_change_in_production",
    "change_this_to_a_long_random_string_in_production",
)
if len(settings.jwt_secret) < 32 or settings.jwt_secret in _JWT_FORBIDDEN:
    raise RuntimeError(
        "JWT_SECRET is not set to a secure value. "
        "Set JWT_SECRET (32+ chars) in the environment. "
        "Generate one with: python -c \"import secrets; print(secrets.token_hex(64))\""
    )
