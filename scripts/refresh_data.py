#!/usr/bin/env python
"""One-shot data refresh entrypoint for GitHub Actions (see .github/workflows/pages.yml).

Runs the existing ingestion/scoring pipeline exactly once and exits - unlike
app/worker.py, which loops forever for the long-running server deployment.
Both call the same app.services.pipeline.refresh_all(); nothing here
reimplements ingestion or scoring logic.

refresh_all() intentionally excludes ingest_nse_history() (NSE Primary
Market Reports) - in server mode, app/worker.py's long-running loop calls it
separately, once every 96 cycles (~once/day at the default 15-minute
interval), since it backfills historical listing performance and doesn't
need the ~45-minute cadence the upcoming/current refresh runs at. On Pages
there is no long-running loop, so without this, that source would never run
at all under GitHub Actions - not "hasn't run today" but "structurally
never wired up". FULL_REFRESH=true (set by pages.yml only on its daily cron
and on workflow_dispatch) runs it here on that same slower cadence."""
from __future__ import annotations
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal, init_db  # noqa: E402
from app.services.pipeline import refresh_all, ingest_nse_history, refresh_market_performance, reconcile_lifecycle  # noqa: E402
from app.services.outcomes import sync_prediction_outcomes  # noqa: E402


def main() -> int:
    init_db()
    db = SessionLocal()
    try:
        runs = refresh_all(db)
        if os.environ.get("FULL_REFRESH", "").lower() in ("1", "true", "yes"):
            runs.append(ingest_nse_history(db, max_reports=3))
            stats = reconcile_lifecycle(db)
            print(f"lifecycle reconcile: {stats}")
            # Post-listing market data for the historical explorer and the
            # forward track record. Bounded per pass (secondary source, be
            # polite); each daily pass extends coverage a little further.
            try:
                n = refresh_market_performance(db, limit=int(os.environ.get("MARKET_REFRESH_LIMIT", "150")))
                print(f"market performance snapshots written: {n}")
            except Exception as e:  # never fatal - last-known-good snapshots stay
                print(f"market performance refresh failed: {type(e).__name__}: {e}")
            try:
                print(f"prediction outcomes: {sync_prediction_outcomes(db, limit=80)}")
            except Exception as e:
                print(f"prediction outcome sync failed: {type(e).__name__}: {e}")
        db.commit()
    finally:
        db.close()
    failed = [r for r in runs if r.status == "error"]
    for r in runs:
        print(f"{r.source}: {r.status} (seen={r.rows_seen}, changed={r.rows_changed})" + (f" - {r.error}" if r.error else ""))
    # Non-fatal by design (the workflow marks this step continue-on-error):
    # a single source failing should not stop the build from using whatever
    # data is already cached in data/ipo.db from the previous successful run.
    return 1 if failed and len(failed) == len(runs) else 0


if __name__ == "__main__":
    sys.exit(main())
