"""Native pg_dump backup for the local-production PostgreSQL cluster (no
Docker required - see deploy/backup_postgres.sh for the Docker-compose
equivalent used in the public production stack).

Writes a timestamped custom-format dump to backups/, then deletes dumps
older than the retention window. Custom format (--format=custom) is
internally compressed by pg_dump already and supports parallel/selective
pg_restore - it is NOT re-wrapped in an external gzip layer, because
pg_restore cannot read a gzip-wrapped custom-format archive directly (its
block-based reader needs the raw archive structure). Restore with:
    pg_restore -h HOST -p PORT -U USER -d DBNAME -j 4 backups/ipo_*.dump

The DB password is passed via the PGPASSWORD environment variable for the
pg_dump subprocess only - never as a command-line argument (which would be
visible in the process list) and never logged.

Usage:
    python scripts/backup_postgres.py
    python scripts/backup_postgres.py --retention-days 14
    python scripts/backup_postgres.py --database-url postgresql://user:pw@host:5432/ipo
"""
from __future__ import annotations
import argparse
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy.engine import make_url

DEFAULT_RETENTION_DAYS = 14
PG_BIN_CANDIDATES = [
    r"C:\Program Files\PostgreSQL\17\bin\pg_dump.exe",
    "pg_dump",  # fall back to PATH
]


def _find_pg_dump() -> str:
    for candidate in PG_BIN_CANDIDATES:
        if candidate == "pg_dump" or Path(candidate).exists():
            resolved = shutil.which(candidate) if candidate == "pg_dump" else candidate
            if resolved:
                return resolved
    raise SystemExit("pg_dump not found - checked PATH and the standard PostgreSQL 17 install location.")


def backup(database_url: str, backups_dir: Path, retention_days: int) -> Path:
    url = make_url(database_url)
    if not url.drivername.startswith("postgresql"):
        raise SystemExit(f"--database-url must be a postgresql:// URL, got: {url.drivername}")

    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = backups_dir / f"ipo_{stamp}.dump"
    tmp_path = out_path.with_suffix(".tmp")

    pg_dump = _find_pg_dump()
    env = dict(os.environ)
    if url.password:
        env["PGPASSWORD"] = url.password
    cmd = [
        pg_dump,
        "-h", url.host or "127.0.0.1",
        "-p", str(url.port or 5432),
        "-U", url.username or "",
        "-d", url.database or "",
        "--format=custom", "--no-owner", "--no-privileges",
        "-f", str(tmp_path),
    ]

    t0 = time.monotonic()
    result = subprocess.run(cmd, capture_output=True, env=env)
    elapsed = time.monotonic() - t0

    if result.returncode != 0:
        tmp_path.unlink(missing_ok=True)
        stderr = result.stderr.decode(errors="replace")
        raise SystemExit(f"pg_dump failed (exit {result.returncode}) after {elapsed:.1f}s:\n{stderr}")

    tmp_path.rename(out_path)
    size_mb = out_path.stat().st_size / 1_048_576
    print(f"OK: {out_path} ({size_mb:.2f} MB, {elapsed:.1f}s)")

    cutoff = time.time() - retention_days * 86400
    removed = 0
    for f in backups_dir.glob("ipo_*.dump"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1
    if removed:
        print(f"Retention: removed {removed} backup(s) older than {retention_days} days.")

    return out_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=None, help="Defaults to the app's own DATABASE_URL setting.")
    parser.add_argument("--backups-dir", default="backups", type=Path)
    parser.add_argument("--retention-days", default=DEFAULT_RETENTION_DAYS, type=int)
    args = parser.parse_args()

    if args.database_url is None:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from app.config import get_settings
        args.database_url = get_settings().database_url

    backup(args.database_url, args.backups_dir, args.retention_days)


if __name__ == "__main__":
    main()
