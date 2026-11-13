from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ENVIRONMENT: str = "development"
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    DATABASE_URL: str
    REDIS_URL: str

    AWS_ACCESS_KEY_ID: str
    AWS_SECRET_ACCESS_KEY: str
    AWS_REGION: str
    S3_BUCKET_NAME: str

    TERMII_API_KEY: str
    TERMII_SENDER_ID: str

    PAYSTACK_SECRET_KEY: str
    OPAY_SECRET_KEY: str | None = None
    SQUAD_SECRET_KEY: str | None = None
    ACCOUNTING_INTEGRATION_KEY: str | None = None
    ACCOUNTING_WEBHOOK_SECRET: str | None = None

    CORS_ORIGINS: list[str] = ["*"]
    
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USERNAME: str
    SMTP_PASSWORD: str
    SMTP_FROM_EMAIL: str

    ACCOUNTING_WEBHOOK_SECRET: str | None = None

@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()