"""Operational behaviour: pool configuration, health split, clean shutdown."""
import signal
import threading
import time

from app.config import Settings


# ---- connection pool ----------------------------------------------------

def test_pool_settings_are_not_passed_to_sqlite():
    """SQLite's pool implementation rejects pool_size/max_overflow outright -
    passing them would break every dev and CI run."""
    import app.db as d
    assert d._is_sqlite is (d.settings.database_url.startswith("sqlite"))
    if d._is_sqlite:
        assert d._pool_kwargs == {}


def test_postgres_pool_is_bounded_and_recycles():
    """A pool that waits forever piles requests up behind an exhausted pool,
    and connections that never recycle go stale behind idle timeouts."""
    s = Settings(app_env="development", database_url="postgresql+psycopg://u:p@h:5432/db")
    assert s.db_pool_size > 0
    assert s.db_pool_timeout > 0, "an unbounded pool wait hangs the request thread"
    assert 0 < s.db_pool_recycle_seconds <= 3600, "must recycle well inside common idle timeouts"


# ---- health endpoints ---------------------------------------------------

def test_liveness_does_not_depend_on_the_database(client):
    """It must answer even when the database is unreachable, or a blip
    restarts healthy containers."""
    r = client.get("/health/live")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_readiness_reports_database_state(client):
    r = client.get("/health/ready")
    assert r.status_code == 200 and r.json()["status"] == "ready"


def test_existing_health_endpoint_is_unchanged(client):
    """The compose healthcheck points at /health - it must keep working."""
    assert client.get("/health").status_code == 200


# ---- graceful shutdown --------------------------------------------------

def test_shutdown_flag_interrupts_the_sleep_promptly():
    """SIGTERM during the worker's idle window must not wait out the full
    interval and take a hard kill 10s later."""
    from app.worker import _Shutdown
    stop = _Shutdown()
    t0 = time.monotonic()
    threading.Timer(0.2, lambda: setattr(stop, "requested", True)).start()
    completed = stop.sleep(30)
    elapsed = time.monotonic() - t0
    assert completed is False, "sleep should report that it was interrupted"
    assert elapsed < 5, f"took {elapsed:.1f}s to notice shutdown"


def test_sleep_runs_to_completion_when_nothing_interrupts():
    from app.worker import _Shutdown
    stop = _Shutdown()
    assert stop.sleep(0.05) is True


def test_signal_handler_sets_the_flag_rather_than_dying_mid_step():
    """The first signal asks for a clean stop; it must not raise, or the
    worker dies wherever it happens to be - possibly between a provider
    accepting an email and the row being marked SENT."""
    from app.worker import _Shutdown
    stop = _Shutdown()
    stop._handle(signal.SIGTERM, None)
    assert stop.requested is True


# ---- optional error reporting -------------------------------------------

def test_error_reporting_is_inert_without_a_dsn():
    """The default deployment must send nothing to any third party."""
    from app.main import _init_error_reporting
    assert _init_error_reporting(Settings(app_env="development", sentry_dsn="")) is False


def test_a_broken_dsn_does_not_stop_the_app_from_starting():
    """Error reporting failing to initialise is not a reason to refuse to
    serve traffic."""
    from app.main import _init_error_reporting
    assert _init_error_reporting(Settings(app_env="development", sentry_dsn="not-a-valid-dsn")) is False


def test_error_reporting_initialises_with_a_well_formed_dsn():
    from app.main import _init_error_reporting
    ok = _init_error_reporting(Settings(app_env="development",
                                        sentry_dsn="https://examplePublicKey@o0.ingest.sentry.io/0"))
    assert ok is True


# ---- .env.example is the one file the operator edits ---------------------

def test_env_example_is_loadable_as_a_real_env_file(tmp_path):
    """`cp .env.example .env` is step one of every setup and of CI's compose
    validation. An empty value for a typed field (a bool, say) is not a
    parseable value and stops the app from starting - so this file has to be
    valid input, not just documentation."""
    import pathlib, subprocess, sys
    root = pathlib.Path(__file__).resolve().parents[1]
    (tmp_path / ".env").write_text((root / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
    r = subprocess.run(
        [sys.executable, "-c", f"import sys; sys.path.insert(0, r'{root}'); from app.config import Settings; Settings()"],
        cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, f".env.example does not load:\n{r.stderr[-800:]}"


def test_env_example_documents_every_setting():
    """A setting nobody can discover is a setting nobody configures."""
    import pathlib, re
    from app.config import Settings
    root = pathlib.Path(__file__).resolve().parents[1]
    text = (root / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^#?([A-Z0-9_]+)=", text, re.M))
    missing = [n.upper() for n in Settings.model_fields if n.upper() not in documented]
    assert not missing, f"undocumented settings: {missing}"
