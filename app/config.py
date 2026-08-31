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
    clerk_publishable_key: str = ""
    clerk_secret_key: str = ""
    clerk_webhook_secret: str = ""
    google_sheets_enabled: bool = False
    google_sheets_spreadsheet_id: str = ""
    google_sheets_service_account_json: str = ""  # raw JSON string (server-side env only, never committed, never sent to frontend)

@lru_cache
def get_settings() -> Settings:
    return Settings()

class ProductionConfigError(RuntimeError):
    """Raised at startup when APP_ENV=production but a required production
    setting is missing or still holds a dev-only default. This must fail
    fast - the alternative (silently running production traffic against
    SQLite, or with the default admin token) is worse than refusing to
    start."""

_PRODUCTION_ENV_VALUES = {"production", "prod"}

def validate_production_settings(s: Settings) -> None:
    if s.app_env not in _PRODUCTION_ENV_VALUES:
        return
    errors = []
    if s.database_url.startswith("sqlite"):
        errors.append(
            "DATABASE_URL is a sqlite:// URL. Production must use PostgreSQL - "
            "see scripts/migrate_sqlite_to_postgres.py for the one-shot data migration."
        )
    if not s.public_base_url:
        errors.append("PUBLIC_BASE_URL is unset - set it (http://localhost is valid for local-production verification).")
    if not s.admin_token or s.admin_token == "change-me-in-production":
        errors.append("ADMIN_TOKEN is unset or still the placeholder default - set a real secret.")
    if errors:
        raise ProductionConfigError(
            "Refusing to start with APP_ENV=" + s.app_env + " - fix the following and restart:\n- "
            + "\n- ".join(errors)
        )
