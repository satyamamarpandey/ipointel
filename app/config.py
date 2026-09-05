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
_KNOWN_EMAIL_PROVIDERS = {"mailpit", "smtp", "freeresend", "resend"}
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}

def _email_errors(s: Settings) -> list[str]:
    """ENABLE_EMAIL=true in production must reach a real mail transport.
    Every branch below otherwise ends at get_provider()'s
    DisabledEmailProvider (or a dev mail catcher that isn't running), which
    means signup confirmations and score alerts are accepted, queued and
    then never delivered - a silent failure, and the whole reason this
    validator exists."""
    if not s.enable_email:
        return []
    errors = []
    if s.email_provider not in _KNOWN_EMAIL_PROVIDERS:
        errors.append(
            f"EMAIL_PROVIDER={s.email_provider!r} is not one of {sorted(_KNOWN_EMAIL_PROVIDERS)} - "
            "get_provider() would fall through to DisabledEmailProvider and drop every message."
        )
    elif s.email_provider == "mailpit":
        errors.append(
            "EMAIL_PROVIDER=mailpit is the local development mail catcher - it is not a "
            "deliverable transport. Use resend, freeresend or smtp in production (or set ENABLE_EMAIL=false)."
        )
    elif s.email_provider == "smtp" and s.smtp_host in _LOOPBACK_HOSTS:
        errors.append(
            f"EMAIL_PROVIDER=smtp with SMTP_HOST={s.smtp_host} points at this container's own "
            "loopback address - set the real relay host (or set ENABLE_EMAIL=false)."
        )
    elif s.email_provider == "resend" and not s.resend_api_key:
        errors.append("EMAIL_PROVIDER=resend but RESEND_API_KEY is unset - every send would be silently dropped.")
    elif s.email_provider == "freeresend" and not (s.freeresend_base_url and s.freeresend_api_key):
        errors.append("EMAIL_PROVIDER=freeresend but FREERESEND_BASE_URL/FREERESEND_API_KEY are unset - every send would be silently dropped.")
    if not (s.email_from or s.resend_from):
        errors.append("ENABLE_EMAIL=true but neither EMAIL_FROM nor RESEND_FROM is set - there is no sender address.")
    return errors

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
    errors.extend(_email_errors(s))
    if errors:
        raise ProductionConfigError(
            "Refusing to start with APP_ENV=" + s.app_env + " - fix the following and restart:\n- "
            + "\n- ".join(errors)
        )
