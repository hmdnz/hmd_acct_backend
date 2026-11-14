from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ENVIRONMENT: str = "development"
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    DATABASE_URL: str

    # Must be exactly the same value as ACCOUNTING_WEBHOOK_SECRET in hamads-dev.
    ACCOUNTING_WEBHOOK_SECRET: str

    # Comma-separated origins allowed to call this API from a browser (your accounting frontend).
    CORS_ORIGINS: str = "http://localhost:5173"

    @field_validator("DATABASE_URL")
    @classmethod
    def _use_asyncpg_driver(cls, v: str) -> str:
        # Render hands out "postgresql://..." (or "postgres://..."); this app needs the async driver.
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+asyncpg://" + v[len(prefix):]
        return v


settings = Settings()
