from __future__ import annotations
"""NSE daily bhavcopy (official end-of-day prices, Tier 1) for the Indian
securities the product tracks. Yahoo Finance has no history for NSE Emerge
(SME) issues, so listing and forward returns for those rows can only come
from here.

Two file formats exist on nsearchives.nseindia.com:
  from 2024-07-08   content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip
                    (TradDt, ISIN, TckrSymb, SctySrs, OpnPric, ClsPric ...)
  before that       content/historical/EQUITIES/YYYY/MON/cmDDMONYYYYbhav.csv.zip
                    (SYMBOL, SERIES, OPEN, CLOSE, TIMESTAMP as DD-MON-YYYY, ISIN)

A 404 on a weekday is a market holiday, not an error. Only rows in series
EQ/BE (mainboard) and SM/ST (SME) are kept, and only for ISINs of interest.
Bars are stored as traded (unadjusted for later splits)."""
import csv
import io
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import PriceBar, BhavcopyDay
from .net_safety import validate_outbound_url

HOST = "nsearchives.nseindia.com"
ALLOWED_HOSTS = {HOST}
NEW_FORMAT_FROM = date(2024, 7, 8)
SERIES_OF_INTEREST = ("EQ", "BE", "SM", "ST")
SOURCE_NAME = "NSE bhavcopy"
_UA = {"User-Agent": "Mozilla/5.0 IPOIntelligence/2.0"}
POLITE_SLEEP_SECONDS = 0.2
_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def url_for(day: date) -> str:
    if day >= NEW_FORMAT_FROM:
        return f"https://{HOST}/content/cm/BhavCopy_NSE_CM_0_0_0_{day.strftime('%Y%m%d')}_F_0000.csv.zip"
    mon = _MONTHS[day.month - 1]
    return f"https://{HOST}/content/historical/EQUITIES/{day.year}/{mon}/cm{day.day:02d}{mon}{day.year}bhav.csv.zip"


def _num(v) -> float | None:
    if v in (None, "", "-"):
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def _iso_from_legacy(ts: str) -> str:
    """'02-JAN-2024' -> '2024-01-02'."""
    try:
        return datetime.strptime(ts.strip(), "%d-%b-%Y").date().isoformat()
    except ValueError:
        return ""


def parse_bhavcopy(content: bytes) -> list[dict]:
    """Rows of a bhavcopy zip (either format) for the series of interest:
    {isin, symbol, series, trade_date (ISO), open, close}. Returns [] for an
    unreadable payload rather than raising."""
    try:
        z = zipfile.ZipFile(io.BytesIO(content))
        name = next((n for n in z.namelist() if n.lower().endswith(".csv")), None)
        if not name:
            return []
        text = z.read(name).decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError):
        return []
    return parse_bhavcopy_csv(text)


def parse_bhavcopy_csv(text: str) -> list[dict]:
    out: list[dict] = []
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        if "TckrSymb" in row:  # new format
            series, isin, symbol = row.get("SctySrs", ""), row.get("ISIN", ""), row.get("TckrSymb", "")
            trade_date = row.get("TradDt", "")
            open_, close = _num(row.get("OpnPric")), _num(row.get("ClsPric"))
        elif "SYMBOL" in row:  # legacy format
            series, isin, symbol = row.get("SERIES", ""), row.get("ISIN", ""), row.get("SYMBOL", "")
            trade_date = _iso_from_legacy(row.get("TIMESTAMP", ""))
            open_, close = _num(row.get("OPEN")), _num(row.get("CLOSE"))
        else:
            continue
        if series not in SERIES_OF_INTEREST or not isin or not trade_date:
            continue
        out.append({"isin": isin.upper(), "symbol": symbol.upper(), "series": series, "trade_date": trade_date, "open": open_, "close": close})
    return out


class BhavcopyUnavailable(Exception):
    """NSE published no file for that date (weekend or market holiday)."""


def fetch_day(day: date, client: httpx.Client | None = None) -> tuple[bytes | None, str]:
    """(zip bytes, url), or (None, url) when NSE has no file for that day."""
    url = url_for(day)
    validate_outbound_url(url, allowed_hosts=ALLOWED_HOSTS)
    own = client is None
    c = client or httpx.Client(headers=_UA, timeout=60, follow_redirects=True)
    try:
        r = c.get(url)
        if r.status_code == 404:
            return None, url
        r.raise_for_status()
        return r.content, url
    finally:
        if own:
            c.close()


def business_days(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def ingested_days(db: Session) -> dict[str, str]:
    return {r.trade_date: r.status for r in db.scalars(select(BhavcopyDay)).all()}


def store_rows(db: Session, rows: list[dict], isins_of_interest: set[str]) -> int:
    """Insert bars for ISINs of interest, skipping (isin, trade_date) pairs
    already stored. Returns the number inserted."""
    wanted = [r for r in rows if r["isin"] in isins_of_interest]
    if not wanted:
        return 0
    day = wanted[0]["trade_date"]
    existing = set(db.scalars(select(PriceBar.isin).where(PriceBar.trade_date == day)).all())
    n = 0
    for r in wanted:
        if r["isin"] in existing:
            continue
        existing.add(r["isin"])
        db.add(PriceBar(isin=r["isin"], symbol=r["symbol"], series=r["series"], trade_date=r["trade_date"],
                        open=r["open"], close=r["close"], source_name=SOURCE_NAME))
        n += 1
    return n


def ingest_days(db: Session, isins_of_interest: set[str], start: date, end: date, max_files: int = 100,
                fetch=fetch_day, sleep=time.sleep, retry_errors: bool = True) -> dict:
    """Fetch every business day in [start, end] not already recorded in
    bhavcopy_days (an 'error' day is retried when retry_errors), storing bars
    for isins_of_interest only. Commits per day. Bounded by max_files actual
    fetches. Returns counts."""
    counts = {"fetched": 0, "ok": 0, "holiday": 0, "error": 0, "skipped": 0, "bars_stored": 0}
    done = ingested_days(db)
    with httpx.Client(headers=_UA, timeout=60, follow_redirects=True) as client:
        for day in business_days(start, end):
            key = day.isoformat()
            prior = done.get(key)
            if prior in ("ok", "holiday") or (prior == "error" and not retry_errors):
                counts["skipped"] += 1
                continue
            if counts["fetched"] >= max_files:
                break
            counts["fetched"] += 1
            ledger = db.scalar(select(BhavcopyDay).where(BhavcopyDay.trade_date == key)) or BhavcopyDay(trade_date=key)
            try:
                content, url = fetch(day, client)
                ledger.source_url = url
                if content is None:
                    ledger.status, ledger.error, ledger.rows_stored = "holiday", "", 0
                    counts["holiday"] += 1
                else:
                    rows = parse_bhavcopy(content)
                    n = store_rows(db, rows, isins_of_interest)
                    ledger.status, ledger.error, ledger.rows_stored = "ok", "", n
                    counts["ok"] += 1
                    counts["bars_stored"] += n
                ledger.fetched_at = datetime.now(timezone.utc)
                db.add(ledger)
                db.commit()
            except Exception as e:
                db.rollback()
                ledger = db.scalar(select(BhavcopyDay).where(BhavcopyDay.trade_date == key)) or BhavcopyDay(trade_date=key)
                ledger.status, ledger.error = "error", f"{type(e).__name__}: {e}"[:400]
                ledger.fetched_at = datetime.now(timezone.utc)
                db.add(ledger)
                db.commit()
                counts["error"] += 1
            sleep(POLITE_SLEEP_SECONDS)
    return counts


def bars_for_isin(db: Session, isin: str) -> list[dict]:
    """Daily bars in market.windowed_returns shape ({ts, open, close}), ts at
    00:00 UTC of the trade date, ordered by date. Rows without a close are
    dropped (never invented)."""
    if not isin:
        return []
    rows = db.scalars(select(PriceBar).where(PriceBar.isin == isin.upper()).order_by(PriceBar.trade_date)).all()
    out = []
    for r in rows:
        if r.close is None:
            continue
        ts = datetime.strptime(r.trade_date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        out.append({"ts": ts, "open": r.open, "close": r.close, "trade_date": r.trade_date})
    return out
