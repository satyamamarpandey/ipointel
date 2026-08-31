"""Guards against destructive test setup (drop_all/create_all/truncate) ever
running against a real database. Checked at the database-URL/database-name
level, not via APP_ENV, because APP_ENV is trivially misconfigured or absent
while the URL is the one thing every destructive call actually touches.

A SQLite target must have "test" in its filename (the repo's own convention,
data/test.db). A Postgres/MySQL/etc target must have "test" in its database
name AND must not be one of a small blocklist of names that look like a real
deployment (the live app database is named "ipo" - refusing that name by
default is the whole point of this module). Both checks can be bypassed only
by setting DESTRUCTIVE_TEST_DB_OVERRIDE=1 explicitly - an extraordinary,
deliberate opt-out, never a default."""
from __future__ import annotations
import os
from sqlalchemy.engine import make_url

OVERRIDE_ENV_VAR = "DESTRUCTIVE_TEST_DB_OVERRIDE"

# Names that must never be wiped even if they happen to contain "test"
# somewhere else in the URL (they don't here, but this blocklist is checked
# first and independently of the "test" substring check below).
_BLOCKED_DB_NAMES = {"ipo", "postgres", "production", "prod"}


class UnsafeTestDatabaseError(RuntimeError):
    """Raised when a destructive test operation would target a database that
    is not clearly and exclusively a disposable test database."""


def assert_safe_test_database_url(url: str) -> None:
    if os.environ.get(OVERRIDE_ENV_VAR):
        return
    u = make_url(url)
    if u.drivername.startswith("sqlite"):
        db_path = u.database or ""
        filename = db_path.replace("\\", "/").rsplit("/", 1)[-1]
        if filename == ":memory:":
            return
        if "test" not in filename.lower():
            raise UnsafeTestDatabaseError(
                f"Refusing destructive test setup against sqlite file {db_path!r} - "
                f"filename must contain 'test' (e.g. 'test.db'). "
                f"Set {OVERRIDE_ENV_VAR}=1 to override."
            )
        return
    dbname = (u.database or "").lower()
    if not dbname:
        raise UnsafeTestDatabaseError(
            "Refusing destructive test setup - the target URL has no database name."
        )
    if dbname in _BLOCKED_DB_NAMES:
        raise UnsafeTestDatabaseError(
            f"Refusing destructive test setup against database {dbname!r} - this name "
            f"is blocked because it looks like a real deployment database, regardless "
            f"of whether it also contains 'test'. Set {OVERRIDE_ENV_VAR}=1 to override."
        )
    if "test" not in dbname:
        raise UnsafeTestDatabaseError(
            f"Refusing destructive test setup against database {dbname!r} - the name "
            f"must contain 'test' (e.g. 'ipo_test'). Set {OVERRIDE_ENV_VAR}=1 to override."
        )
