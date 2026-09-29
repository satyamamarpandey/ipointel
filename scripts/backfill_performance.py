#!/usr/bin/env python
"""Bulk post-listing performance backfill with explicit attempt state.

    python scripts/backfill_performance.py --country India --limit 300 --max-minutes 20
    python scripts/backfill_performance.py --country all --limit 1000 --db sqlite:///./data/work.db

Every Listed row with a symbol ends in one market_data_status (ok, no_series,
no_listing_bar, implausible), so a later run picks up where this one stopped
and never re-fetches a known-empty ticker before --retry-after-days."""
from __future__ import annotations
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--country", default="all", help='India | "United States" | all')
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--max-minutes", type=float, default=30)
    ap.add_argument("--retry-after-days", type=int, default=30)
    ap.add_argument("--include-stale", action="store_true", help="also refresh rows whose latest snapshot is older than 7 days")
    ap.add_argument("--db", default="", help="DATABASE_URL override, e.g. sqlite:///./data/work_d.db")
    args = ap.parse_args()
    if args.db:
        os.environ["DATABASE_URL"] = args.db
    from app.db import SessionLocal, init_db  # noqa: E402  (after DATABASE_URL is set)
    from app.services import performance  # noqa: E402

    init_db()
    db = SessionLocal()
    try:
        countries = [None] if args.country == "all" else [args.country]
        for country in countries:
            counts = performance.refresh_many(db, country=country, limit=args.limit, retry_after_days=args.retry_after_days,
                                              only_missing=not args.include_stale, max_seconds=args.max_minutes * 60)
            print(f"{country or 'all'}: {counts}")
        for country, cov in performance.coverage(db).items():
            print(f"coverage {country}: {cov['with_snapshot']}/{cov['listed']} listed ({cov['pct']}%), symbols {cov['with_symbol']}, status {cov['status_counts']}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
