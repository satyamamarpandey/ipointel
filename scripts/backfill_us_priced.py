#!/usr/bin/env python
"""Backfill historical US priced IPOs (SEC form 424B4) into the rolling window.

The live pipeline (ingest_sec_priced) only looks a few days back, so the US
"Listed" history starts the day production ingestion started. This script
walks EDGAR's daily master index day by day, oldest first, over the part of
the five-year window that has never been scanned, and records every 424B4
that is an IPO prospectus (sec.is_ipo_prospectus) as a Listed IPO through the
same upsert_ipo() path the live ingestion uses.

Design constraints (SEC fair-access policy, GitHub Actions job limits):

- Bounded per invocation: --days business days and --max-minutes wall clock,
  whichever ends first. Progress is committed per day and the last completed
  day is stored on the IngestionRun row (metadata_json["through"]), so the
  next invocation resumes where this one stopped. Re-running never re-scans.
- Head-only downloads: only the first ~400 KB of each submission is read
  (sec.filing_text_head) - the cover page carries the price, symbol and the
  IPO statement. Filings whose CIK is already tracked are not downloaded.
- Never destructive: rows are created through upsert_ipo(), which enforces
  the status no-regression rules; existing rows are never merged or deleted.

Usage:
    python scripts/backfill_us_priced.py --days 260 --max-minutes 45
    python scripts/backfill_us_priced.py --start 2021-09-28 --end 2021-12-31
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import IPO, IngestionRun  # noqa: E402
from app.services import sec  # noqa: E402
from app.services.pipeline import upsert_ipo, add_provenance  # noqa: E402

SOURCE = "SEC 424B4 backfill"
REPAIR_SOURCE = "SEC 424B4 repair"
NON_IPO_FLAG = f"{sec.NON_IPO_FLAG_PREFIX}: 424B4 prospectus without an IPO cover statement (follow-on or resale offering)"
REPARSED_FLAG = "424b4_reparsed: cover page re-read"
WINDOW_YEARS = 5
HEAD_BYTES = 1_500_000
INCONCLUSIVE_FLAG = f"424b4_reparsed: inconclusive (cover page not within the first {HEAD_BYTES // 1000} KB)"
UNKNOWN_FLAG = "424b4_reparsed: inconclusive (prospectus states neither an IPO nor a prior listing)"
RECHECKED_FLAG = "424b4_reparsed: not-ipo verdict re-checked against the cover page"
PRICE_V2_FLAG = "424b4_reparsed: price and symbol verified against the cover page (v2)"


def window_start(today: date, years: int = WINDOW_YEARS) -> date:
    try:
        return today.replace(year=today.year - years)
    except ValueError:  # 29 Feb
        return today.replace(year=today.year - years, day=28)


def previous_runs(db: Session) -> list[IngestionRun]:
    return db.scalars(select(IngestionRun).where(IngestionRun.source == SOURCE).order_by(IngestionRun.id.desc())).all()


def last_completed_day(db: Session) -> date | None:
    """Resume point: the newest 'through' date any previous backfill recorded."""
    days = [r.metadata_json.get("through") for r in previous_runs(db) if r.metadata_json and r.metadata_json.get("through")]
    return max(date.fromisoformat(d) for d in days) if days else None


def recorded_target_end(db: Session) -> date | None:
    """The end day fixed by the first backfill run. Recomputing it later from
    the database would see the backfill's own (older) rows and stop early."""
    for r in previous_runs(db):
        if r.metadata_json and r.metadata_json.get("target_end"):
            return date.fromisoformat(r.metadata_json["target_end"])
    return None


def earliest_live_listing(db: Session) -> date | None:
    """Day the live ingestion's coverage begins (oldest US Listed row written
    by 'SEC 424B4'); the backfill never needs to go past it."""
    rows = db.scalars(select(IPO.listing_date).where(IPO.country == "United States", IPO.status == "Listed")).all()
    parsed = []
    for raw in rows:
        raw = (raw or "").strip()
        for fmt in ("%Y%m%d", "%Y-%m-%d"):
            try:
                parsed.append(datetime.strptime(raw[:10] if fmt != "%Y%m%d" else raw[:8], fmt).date())
                break
            except ValueError:
                continue
    return min(parsed) if parsed else None


def known_ciks(db: Session) -> set[str]:
    keys = db.scalars(select(IPO.external_key).where(IPO.country == "United States")).all()
    return {k.split(":", 1)[1] for k in keys if k and k.startswith("US:")}


def business_days(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def backfill(db: Session, *, days: int, max_minutes: float, start: date | None = None, end: date | None = None,
             today: date | None = None, log=print) -> IngestionRun:
    s = get_settings()
    today = today or datetime.now(timezone.utc).date()
    resume = last_completed_day(db)
    first = start or ((resume + timedelta(days=1)) if resume else window_start(today))
    last = end or recorded_target_end(db) or (earliest_live_listing(db) or today) - timedelta(days=1)
    run = IngestionRun(source=SOURCE, status="running", metadata_json={"from": first.isoformat()})
    db.add(run)
    db.commit()

    deadline = time.monotonic() + max_minutes * 60
    ciks = known_ciks(db)
    scanned = added = processed = inconclusive = 0
    warnings: list[str] = []
    unpublished: list[str] = []
    through: date | None = None
    stopped = "completed"

    for day in business_days(first, last):
        if processed >= days:
            stopped = "day budget reached"
            break
        if time.monotonic() > deadline:
            stopped = "time budget reached"
            break
        try:
            idx, _ = sec.master_index_if_published(day, s.sec_user_agent)
        except sec.DailyIndexUnavailable:
            unpublished.append(day.isoformat())
            idx = {}
        except Exception as e:  # real fetch failure: stop here so the day is retried next time
            warnings.append(f"{day}: {type(e).__name__}")
            stopped = f"fetch failure on {day}"
            break
        for meta in idx.get("424B4", []):
            scanned += 1
            cik = meta["cik"].strip()
            if cik in ciks:
                continue  # already tracked (live ingestion or an earlier day of this backfill)
            try:
                text, truncated = sec.filing_head(meta["filing_url"], s.sec_user_agent, HEAD_BYTES)
                parsed = sec.parse_priced_ipo(text)
            except Exception as e:
                warnings.append(f"{meta.get('company', '?')}: {type(e).__name__}")
                continue
            if not parsed:
                if truncated or sec.classify_prospectus(sec.flatten_filing_text(text)) == "unknown":
                    inconclusive += 1  # neither confirmed nor rejected: never stored as an IPO
                continue
            row = {**meta, **parsed, "country": "United States", "exchange": "NASDAQ/NYSE/Other", "board": "Mainboard",
                   "sector": "Unknown", "status": "Listed", "currency": "USD", "listing_date": meta.get("filing_date", "")}
            if upsert_ipo(db, row, "SEC 424B4", meta["filing_url"], 1):
                added += 1
            stored = db.scalar(select(IPO).where(IPO.external_key == f"US:{cik.lower()}"))
            if stored is not None:
                stored.data_flags = [f for f in (stored.data_flags or []) if f not in (sec.CLASSIFIED_MARKER, REPARSED_FLAG, PRICE_V2_FLAG)] + [sec.CLASSIFIED_MARKER, REPARSED_FLAG, PRICE_V2_FLAG]
            ciks.add(cik)
        through = day
        processed += 1
        db.commit()
        if processed % 20 == 0:
            log(f"  ...{day}: {scanned} filings scanned, {added} IPOs added")

    run = db.get(IngestionRun, run.id)
    run.status = "ok" if not warnings else "partial"
    run.error = " | ".join(warnings)[:4000]
    run.rows_seen = scanned
    run.rows_changed = added
    run.finished_at = datetime.now(timezone.utc)
    run.metadata_json = {"from": first.isoformat(), "through": (through or (first - timedelta(days=1))).isoformat(),
                         "target_end": last.isoformat(), "business_days": processed, "stopped": stopped,
                         "inconclusive_truncated": inconclusive,
                         "index_not_published": unpublished[:200]}
    db.commit()
    return run


def repair_candidates(db: Session, limit: int) -> list[IPO]:
    """US 'Listed' rows written from a 424B4 that were never classified with
    the strict IPO-cover test, or that still lack a symbol / final price and
    have not been re-read yet."""
    rows = db.scalars(select(IPO).where(IPO.country == "United States", IPO.status == "Listed",
                                        IPO.filing_url.like("https://www.sec.gov/%")).order_by(IPO.id.desc())).all()
    out: list[IPO] = []
    for ipo in rows:
        flags = ipo.data_flags or []
        if PRICE_V2_FLAG in flags or INCONCLUSIVE_FLAG in flags or UNKNOWN_FLAG in flags:
            continue  # already verified with the cover-page extractor, or deliberately left alone
        out.append(ipo)
        if len(out) >= limit:
            break
    return out


def recheck_candidates(db: Session, limit: int) -> list[IPO]:
    """US rows this tool moved to 'Not IPO' that have not been re-judged with
    the cover-page-only classifier."""
    rows = db.scalars(select(IPO).where(IPO.country == "United States", IPO.status == "Not IPO",
                                        IPO.filing_url.like("https://www.sec.gov/%")).order_by(IPO.id.desc())).all()
    out = [r for r in rows if NON_IPO_FLAG in (r.data_flags or []) and RECHECKED_FLAG not in (r.data_flags or [])]
    return out[:limit]


def recheck_not_ipo_rows(db: Session, *, limit: int, max_minutes: float, log=print) -> dict:
    """Re-read the cover page of rows previously reclassified as Not IPO. A row
    whose cover states an IPO is restored to Listed (with provenance) and gets
    its ticker/price filled; every re-checked row is flagged so this runs once."""
    s = get_settings()
    deadline = time.monotonic() + max_minutes * 60
    checked = restored = 0
    for ipo in recheck_candidates(db, limit):
        if time.monotonic() > deadline:
            break
        try:
            text, truncated = sec.filing_head(ipo.filing_url, s.sec_user_agent, HEAD_BYTES)
        except Exception:
            continue
        flat = sec.flatten_filing_text(text)
        checked += 1
        flags = [f for f in (ipo.data_flags or []) if f != RECHECKED_FLAG] + [RECHECKED_FLAG]
        if sec.classify_prospectus(flat) == "ipo":
            ipo.status = "Listed"
            flags = [f for f in flags if f != NON_IPO_FLAG]
            add_provenance(db, ipo, "status", "Listed", "SEC 424B4", ipo.filing_url, 1)
            parsed = sec.parse_priced_ipo(flat) or {}
            if parsed.get("symbol"):
                ipo.symbol = parsed["symbol"]
            if parsed.get("final_price") is not None:
                ipo.final_price = parsed["final_price"]
            flags = [f for f in flags if f != PRICE_V2_FLAG] + [PRICE_V2_FLAG]
            restored += 1
        ipo.data_flags = flags
        ipo.updated_at = datetime.now(timezone.utc)
        db.commit()
    if checked:
        log(f"  recheck: {checked} Not IPO rows re-read, {restored} restored to Listed")
    return {"rechecked": checked, "restored_listed": restored}


def repair_existing_us_listed(db: Session, *, limit: int, max_minutes: float, log=print) -> IngestionRun:
    """Re-read the cover page of existing US Listed rows: rows whose 424B4 is
    a follow-on/resale prospectus become 'Not IPO' (with provenance); genuine
    IPOs get their missing ticker symbol and final price filled in. Only
    empty fields are ever written; nothing is deleted or merged."""
    s = get_settings()
    run = IngestionRun(source=REPAIR_SOURCE, status="running")
    db.add(run)
    db.commit()
    deadline = time.monotonic() + max_minutes * 60
    checked = reclassified = filled = inconclusive = 0
    warnings: list[str] = []
    for ipo in repair_candidates(db, limit):
        if time.monotonic() > deadline:
            break
        try:
            text, truncated = sec.filing_head(ipo.filing_url, s.sec_user_agent, HEAD_BYTES)
            flat = sec.flatten_filing_text(text)
        except Exception as e:
            warnings.append(f"{ipo.company}: {type(e).__name__}")
            continue
        checked += 1
        flags = [f for f in (ipo.data_flags or []) if f not in (sec.CLASSIFIED_MARKER, REPARSED_FLAG, PRICE_V2_FLAG)]
        if truncated and "initial public offering" not in flat.lower():
            # The cover page was not reached; the row keeps its status. Never
            # reclassify on evidence we did not actually read.
            ipo.data_flags = flags + [INCONCLUSIVE_FLAG]
            inconclusive += 1
            db.commit()
            continue
        verdict = sec.classify_prospectus(flat)
        if verdict == "unknown":
            ipo.data_flags = flags + [UNKNOWN_FLAG]
            inconclusive += 1
            db.commit()
            continue
        flags += [sec.CLASSIFIED_MARKER, REPARSED_FLAG, PRICE_V2_FLAG]
        if verdict == "follow_on":
            ipo.status = "Not IPO"
            if NON_IPO_FLAG not in flags:
                flags.append(NON_IPO_FLAG)
            flags.append(RECHECKED_FLAG)  # judged with the cover-page classifier already
            add_provenance(db, ipo, "status", "Not IPO", "SEC 424B4", ipo.filing_url, 1)
            reclassified += 1
        else:
            # The cover page is the primary source for both fields: fill blanks
            # and correct values an earlier, looser parser stored.
            parsed = sec.parse_priced_ipo(flat) or {}
            if parsed.get("symbol") and ipo.symbol != parsed["symbol"]:
                ipo.symbol = parsed["symbol"]
                add_provenance(db, ipo, "symbol", ipo.symbol, "SEC 424B4", ipo.filing_url, 1)
                filled += 1
            if parsed.get("final_price") is not None and ipo.final_price != parsed["final_price"]:
                ipo.final_price = parsed["final_price"]
                add_provenance(db, ipo, "final_price", ipo.final_price, "SEC 424B4", ipo.filing_url, 1)
                filled += 1
        ipo.data_flags = flags
        ipo.updated_at = datetime.now(timezone.utc)
        db.commit()
        if checked % 50 == 0:
            log(f"  ...repair: {checked} checked, {reclassified} reclassified, {filled} fields filled")
    run = db.get(IngestionRun, run.id)
    run.status = "ok" if not warnings else "partial"
    run.error = " | ".join(warnings)[:4000]
    run.rows_seen = checked
    run.rows_changed = reclassified + filled
    run.finished_at = datetime.now(timezone.utc)
    meta = {"checked": checked, "reclassified_not_ipo": reclassified, "fields_filled": filled, "inconclusive": inconclusive}
    meta.update(recheck_not_ipo_rows(db, limit=limit, max_minutes=max(0.0, (deadline - time.monotonic()) / 60), log=log))
    run.metadata_json = meta
    db.commit()
    return run


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--days", type=int, default=int(os.environ.get("BACKFILL_US_DAYS", "260")), help="business days to scan this run")
    ap.add_argument("--max-minutes", type=float, default=45.0)
    ap.add_argument("--start", type=date.fromisoformat, default=None, help="override resume point (YYYY-MM-DD)")
    ap.add_argument("--end", type=date.fromisoformat, default=None, help="override end day (YYYY-MM-DD)")
    ap.add_argument("--repair-existing", action="store_true", help="re-read existing US Listed 424B4 rows first (classification, symbol, price)")
    ap.add_argument("--repair-limit", type=int, default=400)
    args = ap.parse_args()
    if args.days <= 0 and not args.repair_existing:
        print("backfill: nothing to do (days <= 0)")
        return 0
    init_db()
    db = SessionLocal()
    try:
        if args.repair_existing:
            rep = repair_existing_us_listed(db, limit=args.repair_limit, max_minutes=min(args.max_minutes, 15.0))
            print(f"{REPAIR_SOURCE}: {rep.status} {rep.metadata_json}" + (f" - {rep.error}" if rep.error else ""))
        if args.days <= 0:
            return 0
        run = backfill(db, days=args.days, max_minutes=args.max_minutes, start=args.start, end=args.end)
        m = run.metadata_json
        print(f"{SOURCE}: {run.status} scanned={run.rows_seen} added={run.rows_changed} "
              f"{m['from']}..{m['through']} (target {m['target_end']}, {m['business_days']} business days, {m['stopped']})"
              + (f" - {run.error}" if run.error else ""))
        return 0 if run.status != "error" else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
