#!/usr/bin/env python
"""Backfill point-in-time US pre-IPO financials from the prospectus (A-001).

For each US row with an EDGAR submission URL, reads the first document of
the submission (the prospectus), parses the summary financial data table
(falling back to the audited statements) and stores FeatureObservation rows
with availability_rule "prospectus_filing" and available_at = the filing
date. See app/services/prospectus_financials.py.

Resumable: every attempted row gets a data flag ("prospectus_financials: ...")
and is skipped afterwards; network errors are not flagged so they retry.
SPACs are flagged without a download (no operating history to parse).

    python scripts/backfill_prospectus_financials.py --limit 200 --max-minutes 9
"""
from __future__ import annotations
import argparse
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import IPO, FeatureObservation, Provenance  # noqa: E402
from app.services import sec, prospectus_financials as pf  # noqa: E402

FLAG_PREFIX = "prospectus_financials:"
FLAG_PARSED = FLAG_PREFIX + " parsed"
FLAG_EMPTY = FLAG_PREFIX + " no annual USD table"
FLAG_SPAC = FLAG_PREFIX + " spac (skipped)"
FLAG_TRUNCATED = FLAG_PREFIX + " prospectus larger than cap"
STATUSES = ("Listed", "Filed", "Upcoming", "Open", "Closed", "Priced")
MAX_DOCUMENT_BYTES = 12_000_000
_SPAC = re.compile(r"\bacquisition\b|\bblank check\b|\bspac\b", re.I)


def _flagged(ipo: IPO) -> bool:
    return any(str(f).startswith(FLAG_PREFIX) for f in (ipo.data_flags or []))


def _add_flag(ipo: IPO, flag: str) -> None:
    flags = [str(f) for f in (ipo.data_flags or []) if not str(f).startswith(FLAG_PREFIX)]
    ipo.data_flags = [*flags, flag]


def candidates(db: Session, limit: int) -> list[IPO]:
    rows = db.scalars(select(IPO).where(IPO.country == "United States", IPO.status.in_(STATUSES),
                                        IPO.filing_url.like("https://www.sec.gov/%"))
                      .order_by(IPO.listing_date.desc(), IPO.id.desc())).all()
    return [r for r in rows if not _flagged(r)][:limit]


def process(db: Session, ipo: IPO, fetch=sec.filing_first_document, user_agent: str = "") -> str:
    """Returns the outcome label. Commits nothing; the caller commits."""
    if _SPAC.search(ipo.company or ""):
        _add_flag(ipo, FLAG_SPAC)
        return "spac"
    text, truncated = fetch(ipo.filing_url, user_agent, max_bytes=MAX_DOCUMENT_BYTES)
    if pf.is_blank_check(text):
        _add_flag(ipo, FLAG_SPAC)
        return "spac"
    obs = pf.extract(text)
    if obs:
        pf.apply_observations(db, ipo, obs, ipo.filing_url)
        _add_flag(ipo, FLAG_PARSED)
        return "parsed"
    _add_flag(ipo, FLAG_TRUNCATED if truncated else FLAG_EMPTY)
    return "truncated" if truncated else "empty"


def cached_fetch(cache_dir: Path):
    """Local development only: keep each downloaded prospectus on disk so a
    parser change can be re-applied without downloading again."""
    cache_dir.mkdir(parents=True, exist_ok=True)

    def fetch(url: str, user_agent: str, max_bytes: int) -> tuple[str, bool]:
        p = cache_dir / (re.sub(r"[^A-Za-z0-9.-]", "_", url.rsplit("/", 2)[-2] + "_" + url.rsplit("/", 1)[-1]))
        if p.exists():
            return p.read_text(encoding="utf-8"), False
        text, truncated = sec.filing_first_document(url, user_agent, max_bytes=max_bytes)
        if not truncated:
            p.write_text(text, encoding="utf-8")
        return text, truncated
    return fetch


def run(db: Session, *, limit: int, max_minutes: float, log=print, fetch=sec.filing_first_document) -> dict:
    ua = get_settings().sec_user_agent
    deadline = time.monotonic() + max_minutes * 60
    counts: dict[str, int] = {}
    for ipo in candidates(db, limit):
        if time.monotonic() > deadline:
            counts["stopped_at_deadline"] = 1
            break
        try:
            label = process(db, ipo, fetch=fetch, user_agent=ua)
            ipo.updated_at = datetime.now(timezone.utc)
            db.commit()
        except (httpx.HTTPError, ValueError) as e:
            db.rollback()
            label = f"error:{type(e).__name__}"
        counts[label] = counts.get(label, 0) + 1
        log(f"  {ipo.id} {ipo.company[:40]}: {label}")
    log(f"prospectus financials: {counts}")
    return counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--max-minutes", type=float, default=9)
    ap.add_argument("--cache-dir", default="", help="local only: reuse downloaded prospectuses")
    ap.add_argument("--reparse", action="store_true", help="clear earlier prospectus results first (use with --cache-dir)")
    a = ap.parse_args()
    init_db()
    db = SessionLocal()
    try:
        if a.reparse:
            for ipo in db.scalars(select(IPO).where(IPO.country == "United States")).all():
                if _flagged(ipo):
                    ipo.data_flags = [f for f in (ipo.data_flags or []) if not str(f).startswith(FLAG_PREFIX)]
            db.query(FeatureObservation).filter(FeatureObservation.source_name == pf.SOURCE_NAME).delete()
            # Undo display columns an earlier parser run filled, so the new run
            # can fill them again (it only fills empty columns).
            for prov in db.scalars(select(Provenance).where(Provenance.source_name == pf.PROVENANCE_SOURCE)).all():
                ipo = db.get(IPO, prov.ipo_id)
                col = prov.field_name
                if ipo is not None and hasattr(ipo, col) and str(getattr(ipo, col)) == prov.observed_value:
                    setattr(ipo, col, None)
                db.delete(prov)
            db.commit()
        fetch = cached_fetch(Path(a.cache_dir)) if a.cache_dir else sec.filing_first_document
        run(db, limit=a.limit, max_minutes=a.max_minutes, fetch=fetch)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
