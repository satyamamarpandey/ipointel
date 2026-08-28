from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_name: str = "IPO Intelligence Terminal"
    app_env: str = "development"
    database_url: str = "sqlite:///./data/ipo.db"
    public_base_url: str = "http://localhost:8000"
    sec_user_agent: str = "IPOIntelligence/2.0 research@example.com"
    admin_token: str = "change-me-in-production"
    worker_interval_seconds: int = 900
    data_stale_after_minutes: int = 60
    allow_secondary_market_data: bool = True
    enable_email: bool = False
    email_provider: str = "mailpit"  # mailpit | smtp | freeresend | resend - zero-credential default, see EmailProvider.get_provider
    email_from: str = ""  # generic sender used by mailpit/smtp/freeresend; falls back to resend_from if unset
    smtp_host: str = "127.0.0.1"
    smtp_port: int = 1025
    smtp_tls: bool = False
    smtp_user: str = ""
    smtp_password: str = ""
    resend_api_key: str = ""
    resend_from: str = "IPO Intelligence <updates@example.com>"
    resend_webhook_secret: str = ""
    freeresend_base_url: str = ""
    freeresend_api_key: str = ""
    email_daily_soft_limit: int = 90
    email_monthly_soft_limit: int = 2800
    email_max_attempts: int = 5
    openrouter_api_key: str = ""
    secondary_enrichment_url: str = ""
    secondary_enrichment_token: str = ""
    strict_reliability: bool = True
    min_recommendation_confidence: float = 70.0

@lru_cache
def get_settings() -> Settings:
    return Settings()
