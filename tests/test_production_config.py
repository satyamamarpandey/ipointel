"""No silent SQLite fallback (or other unsafe default) in production.
Direct unit tests of validate_production_settings() - no app/DB fixture
needed, this is pure settings-object validation."""
import pytest
from app.config import Settings, ProductionConfigError, validate_production_settings

def _settings(**overrides):
    base = dict(
        app_env="production",
        database_url="postgresql+psycopg://user:pw@host:5432/ipo",
        public_base_url="https://ipo.example.com",
        admin_token="a-real-secret-token",
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
