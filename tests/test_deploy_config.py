"""Deployment-manifest assertions.

These are plain file assertions rather than runtime tests because the things
they protect are only observable in a deployed container - by the time a
missing uvicorn flag or a leaked .env layer shows up in production, it has
already shipped. Each one encodes a bug that was actually present.

Deliberately parsed without PyYAML: it is not in requirements.txt and is not
pulled in transitively, so importing it would pass locally and ImportError in
CI. The checks below only need to read a named service's block.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def _service_block(compose_filename: str, service: str) -> str:
    """Return the raw YAML text of one top-level service, without needing a
    YAML parser: everything from `  <service>:` up to the next 2-space key."""
    text = (ROOT / compose_filename).read_text(encoding="utf-8")
    m = re.search(rf"^  {re.escape(service)}:\n(.*?)(?=^  \S|^\S|\Z)", text, re.M | re.S)
    assert m, f"{compose_filename}: no `{service}` service found"
    return m.group(1)

# ---- migrations ----------------------------------------------------------

def test_production_stack_runs_alembic_before_serving():
    """app.db.init_db() is a no-op under APP_ENV=production, so the deployed
    schema comes from this service and nowhere else."""
    migrate = _service_block("docker-compose.production.yml", "migrate")
    assert '"alembic","upgrade","head"' in migrate.replace(" ", "")

def test_web_and_worker_wait_for_migrations_to_finish():
    for service in ("web", "worker"):
        block = _service_block("docker-compose.production.yml", service)
        assert "migrate: {condition: service_completed_successfully}" in block, (
            f"{service} must not serve traffic against a schema that is behind the migration history"
        )

def test_migrate_service_does_not_restart():
    """A one-shot job that restarts would re-run migrations in a loop and
    never satisfy service_completed_successfully."""
    assert 'restart: "no"' in _service_block("docker-compose.production.yml", "migrate")

# ---- proxy / rate limiting ----------------------------------------------

def test_uvicorn_trusts_the_proxy_that_actually_fronts_it():
    """--proxy-headers alone is inert behind a container proxy: uvicorn only
    honours X-Forwarded-For from peers in --forwarded-allow-ips (default
    127.0.0.1), and Caddy's container address is not that. Without this the
    app sees Caddy's IP for every request and all three rate limiters
    (sign-in, signup, events) share one global bucket.

    Asserted against the CMD line itself, not the file: the explanatory
    comment above it also names the flag, so a whole-file substring check
    still passed with the flag deleted from CMD."""
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    cmd = next((ln for ln in dockerfile.splitlines() if ln.startswith("CMD")), "")
    assert "uvicorn" in cmd, "no uvicorn CMD line in Dockerfile"
    assert "--proxy-headers" in cmd
    assert "--forwarded-allow-ips" in cmd

def test_production_web_publishes_no_ports_so_forwarded_headers_cannot_be_spoofed():
    """The precondition for --forwarded-allow-ips=*: in production Caddy must
    be the only route to the app, so no outside client can set its own
    X-Forwarded-For. If `web` ever publishes a port here, that flag has to be
    narrowed at the same time. (docker-compose.yml deliberately does publish
    8000 - that stack is localhost-only development.)"""
    assert "ports:" not in _service_block("docker-compose.production.yml", "web"), (
        "production web is directly reachable - see the Dockerfile CMD comment"
    )

# ---- image hygiene -------------------------------------------------------

def test_dockerignore_excludes_real_secrets_and_local_data():
    """`COPY . .` would otherwise bake .env.production (ADMIN_TOKEN, the
    Postgres password) and local databases/backups into a readable layer."""
    patterns = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    for required in (".env", ".env.*", "data/*.db", "data/*.db.*", "backups"):
        assert required in patterns, f".dockerignore is missing {required!r}"
