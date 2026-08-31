"""One-shot SQLite -> PostgreSQL data migration for the real dev DB.

Copies every row of every current model table, in FK-safe order, preserving
primary keys exactly (so ScoreSnapshot/PredictionOutcome/Provenance FK links
survive), then resets each Postgres sequence to max(id)+1. Naive datetimes
read back from SQLite (see the _aware() pattern used elsewhere in this repo -
SQLite never persists tzinfo) are normalized to UTC before insert, since every
DateTime column in this schema is timezone=True.

Read-only against SQLite: never writes to data/ipo.db. Safe to re-run against
an empty Postgres target; refuses to run if the target table already has rows,
to avoid silent duplication.

Usage:
    SQLITE_URL=sqlite:///./data/ipo.db DATABASE_URL=postgresql+psycopg://... \
        python scripts/migrate_sqlite_to_postgres.py
"""
from __future__ import annotations
import os
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine, select, text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.db import Base
from app import models  # noqa: F401  (populates Base.metadata)

SQLITE_URL = os.environ.get("SQLITE_URL", "sqlite:///./data/ipo.db")
PG_URL = os.environ["DATABASE_URL"]
if not PG_URL.startswith("postgresql"):
    raise SystemExit(f"DATABASE_URL must point at Postgres for this script, got: {PG_URL}")


def _aware(v):
    if isinstance(v, datetime) and v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v


def main():
    src = create_engine(SQLITE_URL, connect_args={"check_same_thread": False})
    dst = create_engine(PG_URL)

    report = []
    with src.connect() as sconn, dst.begin() as dconn:
        for table in Base.metadata.sorted_tables:
            existing = dconn.execute(text(f'SELECT count(*) FROM "{table.name}"')).scalar()
            if existing:
                raise SystemExit(f"Refusing to migrate into non-empty table '{table.name}' ({existing} rows already present)")

            rows = sconn.execute(select(table)).mappings().all()
            if rows:
                payload = [{k: _aware(v) for k, v in dict(r).items()} for r in rows]
                dconn.execute(table.insert(), payload)

            pk_cols = [c for c in table.columns if c.primary_key and c.autoincrement is not False]
            if pk_cols and rows:
                pk = pk_cols[0].name
                dconn.execute(text(
                    f'SELECT setval(pg_get_serial_sequence(\'"{table.name}"\', \'{pk}\'), '
                    f'(SELECT COALESCE(MAX({pk}), 1) FROM "{table.name}"), true)'
                ))

            src_count = len(rows)
            dst_count = dconn.execute(text(f'SELECT count(*) FROM "{table.name}"')).scalar()
            status = "OK" if src_count == dst_count else "MISMATCH"
            report.append((table.name, src_count, dst_count, status))
            print(f"{table.name:24s} sqlite={src_count:6d}  postgres={dst_count:6d}  {status}")

    mismatches = [r for r in report if r[3] != "OK"]
    if mismatches:
        raise SystemExit(f"Row count mismatch in {len(mismatches)} table(s) - see above")
    print("\nAll table row counts match. Migration complete.")


if __name__ == "__main__":
    main()
