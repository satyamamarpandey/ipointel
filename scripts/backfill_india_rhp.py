#!/usr/bin/env python
"""Backfill point-in-time India pre-IPO financials from NSE's RHP archive (A-002).

For each India row with an NSE symbol: download RHP_<SYMBOL>.zip, read the
RHP text, extract restated revenue and profit (app/services/rhp_financials.py)
and store FeatureObservation rows (rule "prospectus_filing", available_at =
issue open date or the day before listing).

Resumable: every attempted row gets a data flag ("rhp_financials: ...") and is
skipped afterwards; network errors are not flagged so they retry.

    python scripts/backfill_india_rhp.py --limit 50 --max-minutes 9
    python scripts/backfill_india_rhp.py --cache-dir <dir>   # local: keep page text
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import IPO, FeatureObservation, Provenance  # noqa: E402
from app.services import rhp_financials as rf  # noqa: E402

FLAG_PREFIX = "rhp_financials:"
FLAG_PARSED = FLAG_PREFIX + " parsed"
STATUSES = ("Listed", "Open", "Closed", "Upcoming")


def _flagged(ipo: IPO) -> bool:
    return any(str(f).startswith(FLAG_PREFIX) for f in (ipo.data_flags or []))


def _add_flag(ipo: IPO, flag: str) -> None:
    flags = [str(f) for f in (ipo.data_flags or []) if not str(f).startswith(FLAG_PREFIX)]
    ipo.data_flags = [*flags, flag]


def candidates(db: Session, limit: int) -> list[IPO]:
    rows = db.scalars(select(IPO).where(IPO.country == "India", IPO.status.in_(STATUSES), IPO.symbol != "")
                      .order_by(IPO.listing_date.desc(), IPO.id.desc())).all()
    return [r for r in rows if not _flagged(r)][:limit]


def default_pages(symbol: str) -> list[str]:
    return rf.pages_from_zip(rf.fetch_rhp(symbol))


def cached_pages(cache_dir: Path):
    cache_dir.mkdir(parents=True, exist_ok=True)

    def pages(symbol: str) -> list[str]:
        p = cache_dir / f"{symbol.upper()}.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        out = default_pages(symbol)
        p.write_text(json.dumps(out), encoding="utf-8")
        return out
    return pages


def process(db: Session, ipo: IPO, get_pages=default_pages) -> str:
    """Outcome label. Commits nothing."""
    available = rf.availability_date(ipo)
    try:
        pages = get_pages(ipo.symbol)
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code == 404:
            if ipo.status != "Listed":
                return "not_yet_published"  # an issue still in progress may publish later: retried
            _add_flag(ipo, FLAG_PREFIX + " no RHP archive")
            return "no_archive"
        raise
    except (ValueError, OSError, KeyError) as e:  # broken zip or PDF: recorded, not retried daily
        _add_flag(ipo, f"{FLAG_PREFIX} unreadable archive ({type(e).__name__})")
        return "unreadable"
    obs, why = rf.extract(pages, available)
    if obs:
        rf.apply_observations(db, ipo, obs, rf.RHP_URL.format(symbol=ipo.symbol.upper()))
        _add_flag(ipo, f"{FLAG_PARSED} ({why})")
        return "parsed"
    _add_flag(ipo, f"{FLAG_PREFIX} not extracted: {why}")
    return "not_extracted"


def run(db: Session, *, limit: int, max_minutes: float, log=print, get_pages=default_pages) -> dict:
    deadline = time.monotonic() + max_minutes * 60
    counts: dict[str, int] = {}
    for ipo in candidates(db, limit):
        if time.monotonic() > deadline:
            counts["stopped_at_deadline"] = 1
            break
        try:
            label = process(db, ipo, get_pages)
            ipo.updated_at = datetime.now(timezone.utc)
            db.commit()
        except httpx.HTTPError as e:
            db.rollback()
            label = f"error:{type(e).__name__}"
        counts[label] = counts.get(label, 0) + 1
        log(f"  {ipo.id} {ipo.symbol}: {label}")
    log(f"india rhp financials: {counts}")
    return counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--max-minutes", type=float, default=9)
    ap.add_argument("--cache-dir", default="", help="local only: keep extracted page text")
    ap.add_argument("--reparse", action="store_true", help="clear earlier RHP results first (use with --cache-dir)")
    a = ap.parse_args()
    init_db()
    db = SessionLocal()
    try:
        if a.reparse:
            for ipo in db.scalars(select(IPO).where(IPO.country == "India")).all():
                if _flagged(ipo):
                    ipo.data_flags = [f for f in (ipo.data_flags or []) if not str(f).startswith(FLAG_PREFIX)]
            db.query(FeatureObservation).filter(FeatureObservation.source_name == rf.SOURCE_NAME).delete()
            # Undo display columns an earlier run filled; the new run only fills empty ones.
            for prov in db.scalars(select(Provenance).where(Provenance.source_name == rf.PROVENANCE_SOURCE)).all():
                ipo = db.get(IPO, prov.ipo_id)
                if ipo is not None and str(getattr(ipo, prov.field_name, None)) == prov.observed_value:
                    setattr(ipo, prov.field_name, None)
                db.delete(prov)
            db.commit()
        get_pages = cached_pages(Path(a.cache_dir)) if a.cache_dir else default_pages
        run(db, limit=a.limit, max_minutes=a.max_minutes, get_pages=get_pages)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
