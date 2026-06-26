from pydantic import field_validator
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    database_url: str = "sqlite:///./fawn.db"
    unit_api_token: str = "UNIT_TOKEN_NOT_SET"
    unit_base_url: str = "https://api.s.unit.sh"
    anthropic_api_key: str = "ANTHROPIC_KEY_NOT_SET"
    jwt_secret: str = "dev_secret_change_in_production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60
    resend_api_key: str = ""
    from_email: str = "alex@getfawn.com"
    admin_api_key: str = ""
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    unit_webhook_secret: str = ""

    @field_validator("database_url")
    @classmethod
    def fix_postgres_dialect(cls, v: str) -> str:
        # Railway provides postgresql:// but SQLAlchemy 2.x needs postgresql+psycopg2://
        if v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+psycopg2://", 1)
        return v

    class Config:
        env_file = ".env"

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
