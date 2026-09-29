#!/usr/bin/env python
"""Ingest NSE daily bhavcopy files (official end-of-day prices) for every ISIN
the product tracks in India. Resumable: days already recorded in
bhavcopy_days are skipped, so repeated runs only fetch what is missing.

    python scripts/backfill_nse_bhavcopy.py --start 2026-07-01 --end 2026-09-29 --max-files 70
    python scripts/backfill_nse_bhavcopy.py --start 2021-09-01 --max-files 400 --db sqlite:///./data/work_d.db

Then rerun scripts/backfill_performance.py --country India so India rows get
their returns from these bars (Tier 1) instead of Yahoo."""
from __future__ import annotations
import argparse
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=_date, default=_date("2021-09-01"))
    ap.add_argument("--end", type=_date, default=datetime.now(timezone.utc).date())
    ap.add_argument("--max-files", type=int, default=100)
    ap.add_argument("--db", default="", help="DATABASE_URL override")
    args = ap.parse_args()
    if args.db:
        os.environ["DATABASE_URL"] = args.db
    from sqlalchemy import select, func  # noqa: E402
    from app.db import SessionLocal, init_db  # noqa: E402
    from app.models import IPO, PriceBar  # noqa: E402
    from app.services import nse_bhavcopy  # noqa: E402

    init_db()
    db = SessionLocal()
    try:
        isins = {i.upper() for i in db.scalars(select(IPO.isin).where(IPO.country == "India", IPO.isin != "")).all()}
        print(f"ISINs of interest: {len(isins)}")
        counts = nse_bhavcopy.ingest_days(db, isins, args.start, args.end, max_files=args.max_files)
        print(f"ingest: {counts}")
        pruned = nse_bhavcopy.prune_bars(db, nse_bhavcopy.listing_dates_by_isin(db))
        print(f"pruned {pruned} bars no return window reads")
        total = db.scalar(select(func.count()).select_from(PriceBar))
        covered = db.scalar(select(func.count(func.distinct(PriceBar.isin))))
        print(f"price_bars: {total} rows across {covered} ISINs")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
