"""Unit tests for the destructive-test-target guard (tests/db_safety.py).
These run against the string logic only - no real database connection - so
they execute identically under either dialect and are never skipped."""
import pytest
from tests.db_safety import assert_safe_test_database_url, UnsafeTestDatabaseError, OVERRIDE_ENV_VAR

def test_allows_default_sqlite_test_db():
    assert_safe_test_database_url("sqlite:///./data/test.db")

def test_allows_sqlite_memory_db():
    assert_safe_test_database_url("sqlite:///:memory:")

def test_refuses_sqlite_non_test_filename():
    with pytest.raises(UnsafeTestDatabaseError):
        assert_safe_test_database_url("sqlite:///./data/ipo.db")

def test_allows_postgres_test_database():
    assert_safe_test_database_url("postgresql+psycopg://user:pw@127.0.0.1:55433/ipo_test")

def test_refuses_postgres_real_app_database_name():
    with pytest.raises(UnsafeTestDatabaseError):
        assert_safe_test_database_url("postgresql+psycopg://user:pw@127.0.0.1:5435/ipo")

def test_refuses_postgres_database_without_test_in_name():
    with pytest.raises(UnsafeTestDatabaseError):
        assert_safe_test_database_url("postgresql+psycopg://user:pw@127.0.0.1:5435/some_other_db")

def test_refuses_blocked_name_even_if_only_db_word_matches():
    for blocked in ("postgres", "production", "prod"):
        with pytest.raises(UnsafeTestDatabaseError):
            assert_safe_test_database_url(f"postgresql+psycopg://user:pw@127.0.0.1:5435/{blocked}")

def test_refuses_url_with_no_database_name():
    with pytest.raises(UnsafeTestDatabaseError):
        assert_safe_test_database_url("postgresql+psycopg://user:pw@127.0.0.1:5435/")

def test_override_env_var_bypasses_the_guard(monkeypatch):
    monkeypatch.setenv(OVERRIDE_ENV_VAR, "1")
    assert_safe_test_database_url("postgresql+psycopg://user:pw@127.0.0.1:5435/ipo")

def test_override_absent_by_default(monkeypatch):
    monkeypatch.delenv(OVERRIDE_ENV_VAR, raising=False)
    with pytest.raises(UnsafeTestDatabaseError):
        assert_safe_test_database_url("postgresql+psycopg://user:pw@127.0.0.1:5435/ipo")
