from pydantic import field_validator
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    database_url: str = "sqlite:///./fawn.db"
    unit_api_token: str = "UNIT_TOKEN_NOT_SET"
    unit_base_url: str = "https://api.s.unit.sh"

    # ---- Multi-BaaS provider stack (Column / Lithic / Alpaca / Plaid) ----
    # Unit stays the KYC/onboarding front door; these providers each own one
    # capability. Every client is guarded: an unset token raises a clear
    # "<provider> is not configured" error instead of calling out with a bad
    # key, so the stack is safe to deploy before real contracts land.
    #
    # baas_provider selects which BANKING backend money-movement routes use.
    # "unit" (default) keeps today's behavior; "column" cuts over to Column.
    baas_provider: str = "unit"

    # Column — banking (deposit accounts, ACH, book/realtime transfers)
    column_api_key: str = ""
    column_base_url: str = "https://api.column.com"
    column_webhook_secret: str = ""

    # Lithic — card issuing + real-time auth stream
    lithic_api_key: str = ""
    lithic_base_url: str = "https://sandbox.lithic.com/v1"
    lithic_webhook_secret: str = ""

    # Alpaca — brokerage / investing (Broker API)
    alpaca_api_key: str = ""
    alpaca_api_secret: str = ""
    alpaca_base_url: str = "https://broker-api.sandbox.alpaca.markets"

    # Plaid — bank-account linking (funding source switching)
    plaid_client_id: str = ""
    plaid_secret: str = ""
    plaid_env: str = "sandbox"  # sandbox | production
    plaid_base_url: str = "https://sandbox.plaid.com"

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
    unit_webhook_secret: str = ""
    allow_unverified_ach_funding: bool = False
    allow_unsigned_stripe_webhooks: bool = False
    allow_unsigned_unit_webhooks: bool = False
    allow_unsigned_column_webhooks: bool = False
    allow_unsigned_lithic_webhooks: bool = False
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
