from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # SQLite by default — no installation needed. Change to postgresql://... when ready.
    database_url: str = "sqlite:///./fawn.db"
    unit_api_token: str = "UNIT_TOKEN_NOT_SET"
    unit_base_url: str = "https://api.s.unit.sh"
    anthropic_api_key: str = "ANTHROPIC_KEY_NOT_SET"
    jwt_secret: str = "dev_secret_change_in_production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    class Config:
        env_file = ".env"

settings = Settings()

assert (
    len(settings.jwt_secret) >= 32
    and settings.jwt_secret not in (
        "dev_secret_change_in_production",
        "change_this_to_a_long_random_string_in_production",
    )
), "Set a real JWT_SECRET in .env (use: python -c \"import secrets; print(secrets.token_hex(64))\")"
