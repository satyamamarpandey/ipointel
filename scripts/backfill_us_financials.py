#!/usr/bin/env python
"""Backfill pre-IPO financial features for US rows from SEC XBRL companyfacts.

Resumable and bounded: the cursor (last IPO id processed) is kept in the
metadata of an IngestionRun(source="SEC XBRL financials"); a 404 from
companyfacts is recorded as the data flag "xbrl: no companyfacts" so the row
is not retried on every run. See app/services/xbrl_financials.py for the
availability rules.

    python scripts/backfill_us_financials.py --limit 200 --max-minutes 20
"""
from __future__ import annotations
import argparse
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
from app.models import IPO, IngestionRun  # noqa: E402
from app.services import sec  # noqa: E402
from app.services.xbrl_financials import extract_pre_ipo_financials, apply_observations  # noqa: E402

SOURCE = "SEC XBRL financials"
NO_FACTS_FLAG = "xbrl: no companyfacts"
DONE_FLAG = "xbrl: companyfacts read"
STATUSES = ("Listed", "Filed", "Upcoming", "Open", "Closed", "Priced")
PAUSE_SECONDS = 0.15


def _cik(ipo: IPO) -> str:
    key = ipo.external_key or ""
    cik = key[3:] if key.startswith("US:") else ""
    return cik if cik.isdigit() else ""


def _has_flag(ipo: IPO, prefix: str) -> bool:
    return any(str(f).startswith(prefix) for f in (ipo.data_flags or []))


def _add_flag(ipo: IPO, flag: str) -> None:
    flags = [str(f) for f in (ipo.data_flags or [])]
    if flag not in flags:
        ipo.data_flags = [*flags, flag]


def last_cursor(db: Session) -> int:
    run = db.scalar(select(IngestionRun).where(IngestionRun.source == SOURCE, IngestionRun.status == "ok").order_by(IngestionRun.started_at.desc()).limit(1))
    if run and isinstance(run.metadata_json, dict) and not run.metadata_json.get("exhausted"):
        return int(run.metadata_json.get("cursor") or 0)
    return 0


def candidates(db: Session, after_id: int, limit: int) -> list[IPO]:
    rows = db.scalars(select(IPO).where(IPO.country == "United States", IPO.status.in_(STATUSES), IPO.revenue_m.is_(None), IPO.id > after_id).order_by(IPO.id).limit(limit * 3)).all()
    out = [r for r in rows if _cik(r) and not _has_flag(r, NO_FACTS_FLAG) and not _has_flag(r, DONE_FLAG)]
    return out[:limit]


def process(db: Session, ipo: IPO, user_agent: str) -> dict:
    cik = _cik(ipo)
    url = f"{sec.DATA}/api/xbrl/companyfacts/CIK{cik.zfill(10)}.json"
    try:
        facts = sec.fetch_companyfacts(cik, user_agent)
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code == 404:
            _add_flag(ipo, NO_FACTS_FLAG)
            return {"status": "no_companyfacts"}
        raise
    listing = ipo.listing_date or ipo.filing_date or datetime.now(timezone.utc).date().isoformat()
    obs = extract_pre_ipo_financials(facts, listing)
    result = apply_observations(db, ipo, obs, url) if obs else {"observations": 0, "columns_filled": []}
    _add_flag(ipo, DONE_FLAG)
    return {"status": "ok", **result}


def run(db: Session, *, limit: int, max_minutes: float, log=print) -> IngestionRun:
    s = get_settings()
    run = IngestionRun(source=SOURCE, status="running")
    db.add(run)
    db.commit()
    started = time.monotonic()
    cursor = last_cursor(db)
    stats = {"attempted": 0, "ok": 0, "no_companyfacts": 0, "errors": 0, "observations": 0, "columns": {}}
    rows = candidates(db, cursor, limit)
    exhausted = len(rows) < limit
    for ipo in rows:
        if (time.monotonic() - started) / 60 > max_minutes:
            exhausted = False
            break
        stats["attempted"] += 1
        try:
            r = process(db, ipo, s.sec_user_agent)
            stats[r["status"]] += 1
            stats["observations"] += r.get("observations", 0)
            for c in r.get("columns_filled", []):
                stats["columns"][c] = stats["columns"].get(c, 0) + 1
            db.commit()
        except Exception as e:  # one bad row never stops the pass
            db.rollback()
            stats["errors"] += 1
            log(f"  {ipo.company}: {type(e).__name__}: {e}")
        cursor = ipo.id
        time.sleep(PAUSE_SECONDS)
    run = db.get(IngestionRun, run.id)
    run.status = "ok"
    run.rows_seen = stats["attempted"]
    run.rows_changed = stats["ok"]
    run.finished_at = datetime.now(timezone.utc)
    run.metadata_json = {"cursor": 0 if exhausted else cursor, "exhausted": exhausted, **stats}
    db.commit()
    log(f"SEC XBRL financials: {stats} cursor={cursor} exhausted={exhausted}")
    return run


def coverage(db: Session, log=print) -> None:
    for status in ("Listed", "Filed"):
        rows = db.scalars(select(IPO).where(IPO.country == "United States", IPO.status == status)).all()
        n = len(rows) or 1
        cov = {c: sum(1 for r in rows if getattr(r, c) is not None) for c in ("revenue_m", "revenue_prev_m", "net_income_m", "cfo_m", "ebitda_m", "cash_m", "debt_m", "post_issue_shares_m")}
        log(f"US {status} ({len(rows)}): " + ", ".join(f"{k} {v} ({v / n * 100:.0f}%)" for k, v in cov.items()))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=150)
    ap.add_argument("--max-minutes", type=float, default=20)
    args = ap.parse_args()
    init_db()
    db = SessionLocal()
    try:
        run(db, limit=args.limit, max_minutes=args.max_minutes)
        coverage(db)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
