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
    resend_api_key: str = ""
    resend_from: str = "IPO Intelligence <updates@example.com>"
    openrouter_api_key: str = ""
    secondary_enrichment_url: str = ""
    secondary_enrichment_token: str = ""
    strict_reliability: bool = True
    min_recommendation_confidence: float = 70.0

@lru_cache
def get_settings() -> Settings:
    return Settings()
