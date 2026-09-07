"""No silent SQLite fallback (or other unsafe default) in production.
Direct unit tests of validate_production_settings() - no app/DB fixture
needed, this is pure settings-object validation."""
import pytest
from app.config import Settings, ProductionConfigError, validate_production_settings

def _settings(**overrides):
    # Every field this validator reads is pinned explicitly: Settings still
    # loads the developer's local .env, so leaving e.g. enable_email unset
    # would make these tests pass or fail based on whoever's machine ran them.
    base = dict(
        app_env="production",
        database_url="postgresql+psycopg://user:pw@host:5432/ipo",
        public_base_url="https://ipo.example.com",
        admin_token="a-real-secret-token",
        enable_email=False,
        email_provider="resend",
        email_from="IPO Intelligence <updates@ipo.example.com>",
        resend_from="IPO Intelligence <updates@ipo.example.com>",
        resend_api_key="",
        freeresend_base_url="",
        freeresend_api_key="",
        smtp_host="smtp.example.com",
    )
    base.update(overrides)
    return Settings(**base)

def test_non_production_env_is_never_validated():
    validate_production_settings(_settings(app_env="development", database_url="sqlite:///./data/ipo.db", admin_token="change-me-in-production"))

def test_production_with_full_valid_config_passes():
    validate_production_settings(_settings())

def test_production_refuses_sqlite_database_url():
    with pytest.raises(ProductionConfigError, match="sqlite"):
        validate_production_settings(_settings(database_url="sqlite:///./data/ipo.db"))

def test_production_allows_localhost_base_url_for_local_production_verification():
    # http://localhost is the documented value for the "local production
    # verification" tier (Caddy fronting the stack on one local URL) -
    # only a genuinely empty PUBLIC_BASE_URL should fail.
    validate_production_settings(_settings(public_base_url="http://localhost"))

def test_production_refuses_missing_base_url():
    with pytest.raises(ProductionConfigError, match="PUBLIC_BASE_URL"):
        validate_production_settings(_settings(public_base_url=""))

def test_production_refuses_default_admin_token():
    with pytest.raises(ProductionConfigError, match="ADMIN_TOKEN"):
        validate_production_settings(_settings(admin_token="change-me-in-production"))

def test_production_refuses_empty_admin_token():
    with pytest.raises(ProductionConfigError, match="ADMIN_TOKEN"):
        validate_production_settings(_settings(admin_token=""))

def test_production_reports_multiple_errors_at_once():
    with pytest.raises(ProductionConfigError) as exc:
        validate_production_settings(_settings(database_url="sqlite:///./data/ipo.db", admin_token=""))
    msg = str(exc.value)
    assert "sqlite" in msg and "ADMIN_TOKEN" in msg

# ---- ENABLE_EMAIL=true must reach a real transport ------------------------
# Every case below otherwise ends at get_provider()'s DisabledEmailProvider
# (or a dev catcher that isn't running): mail is queued, then silently never
# delivered. Same failure class as the SQLite fallback above.

def test_email_disabled_never_validates_provider_settings():
    validate_production_settings(_settings(enable_email=False, email_provider="mailpit", smtp_host="127.0.0.1"))

def test_production_refuses_mailpit_dev_catcher():
    with pytest.raises(ProductionConfigError, match="mailpit"):
        validate_production_settings(_settings(enable_email=True, email_provider="mailpit", email_from="IPO <a@b.com>"))

def test_production_refuses_smtp_pointed_at_loopback():
    with pytest.raises(ProductionConfigError, match="SMTP_HOST"):
        validate_production_settings(_settings(enable_email=True, email_provider="smtp", smtp_host="127.0.0.1", email_from="IPO <a@b.com>"))

def test_production_accepts_smtp_with_a_real_relay_host():
    validate_production_settings(_settings(enable_email=True, email_provider="smtp", smtp_host="smtp.mailgun.org", smtp_port=587, email_from="IPO <a@b.com>"))

def test_production_refuses_resend_without_api_key():
    with pytest.raises(ProductionConfigError, match="RESEND_API_KEY"):
        validate_production_settings(_settings(enable_email=True, email_provider="resend", resend_api_key="", email_from="IPO <a@b.com>"))

def test_production_accepts_resend_with_api_key():
    validate_production_settings(_settings(enable_email=True, email_provider="resend", resend_api_key="re_live_key", email_from="IPO <a@b.com>"))

def test_production_refuses_freeresend_without_credentials():
    with pytest.raises(ProductionConfigError, match="FREERESEND"):
        validate_production_settings(_settings(enable_email=True, email_provider="freeresend", freeresend_base_url="https://mail.example.com", freeresend_api_key="", email_from="IPO <a@b.com>"))

def test_production_refuses_unknown_email_provider():
    with pytest.raises(ProductionConfigError, match="EMAIL_PROVIDER"):
        validate_production_settings(_settings(enable_email=True, email_provider="sendgrid", email_from="IPO <a@b.com>"))

def test_production_refuses_email_with_no_sender_address():
    with pytest.raises(ProductionConfigError, match="sender address"):
        validate_production_settings(_settings(enable_email=True, email_provider="resend", resend_api_key="re_live_key", email_from="", resend_from=""))


# ---- Swagger UI is not published by accident in production ----------------

def test_api_docs_are_on_by_default_outside_production():
    assert Settings(app_env="development").enable_api_docs is True


def test_api_docs_default_closed_under_production(monkeypatch):
    """Forgetting to set ENABLE_API_DOCS must not be what publishes a full
    route inventory (admin endpoints included) on a public host."""
    monkeypatch.delenv("ENABLE_API_DOCS", raising=False)
    assert _settings().enable_api_docs is False


def test_operator_can_still_opt_back_into_docs_in_production(monkeypatch):
    monkeypatch.setenv("ENABLE_API_DOCS", "true")
    assert _settings(enable_api_docs=True).enable_api_docs is True
