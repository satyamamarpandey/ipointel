from __future__ import annotations
"""Bulk / backfill path for post-listing market performance, with an explicit
attempt state per IPO row.

Every Listed row with a symbol ends each pass in exactly one of these
`ipos.market_data_status` values, so a symbol Yahoo has no history for (a
delisted SPAC, a reassigned ticker, an SME issue that only trades on BSE)
is recorded and retried on a schedule instead of re-fetched forever:

    ok              a PerformanceSnapshot with at least one real return was written
    no_series       the price source returned no bars for any candidate symbol
    no_listing_bar  bars exist but none falls on/near the stated listing date
    implausible     bars exist, the listing return is a unit mismatch and was
                    suppressed; forward windows (vs listing close) still stored

Missing means null. Nothing here ever writes a zero it did not observe, and a
snapshot whose every return would be null is not written at all.

Yahoo Finance is a Tier-3 secondary source and is labelled as such on every
snapshot's source_name. pipeline.refresh_market_performance is the older
bounded path used by the scheduled refresh; this module is the one the
backfill script drives and the one pipeline can be switched to."""
import time
from datetime import datetime, timezone, timedelta, date
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from ..models import IPO, PerformanceSnapshot
from . import market, nse_bhavcopy

SOURCE_NAME = "Yahoo Finance fallback"
NSE_SOURCE_NAME = "NSE bhavcopy"
MIN_OFFICIAL_BARS = 2
STATUS_OK, STATUS_NO_SERIES, STATUS_NO_LISTING_BAR, STATUS_IMPLAUSIBLE = "ok", "no_series", "no_listing_bar", "implausible"
STATUSES = (STATUS_OK, STATUS_NO_SERIES, STATUS_NO_LISTING_BAR, STATUS_IMPLAUSIBLE)
POLITE_SLEEP_SECONDS = 0.25
RETURN_FIELDS = ("listing_return_pct", "listing_open_return_pct", "return_7d_pct", "return_1m_pct", "return_90d_pct",
                 "return_6m_pct", "return_12m_pct", "return_24m_pct")


def now() -> datetime:
    return datetime.now(timezone.utc)


def candidate_symbols(ipo: IPO) -> list[str]:
    """Yahoo tickers to try, in order.

    India: Yahoo lists NSE Emerge (SME) issues as "<SYMBOL>-SM.NS" and
    mainboard issues as "<SYMBOL>.NS" (verified: SHREEDHAR -> SHREEDHAR-SM.NS,
    FASCINATE -> FASCINATE-SM.NS). The board decides which is tried first; the
    other form and the BSE listing follow, because a symbol can migrate from
    Emerge to the mainboard.
    US: the symbol as stored; a SPAC unit "XXXXU" is listed by Yahoo as
    "XXXX-UN" (verified: HYACU -> HYAC-UN), so that form is the fallback."""
    sym = (ipo.symbol or "").strip()
    if not sym:
        return []
    if (ipo.country or "").lower() == "india":
        if sym.endswith((".NS", ".BO")):
            return [sym]
        nse_main, nse_sme, bse = f"{sym}.NS", f"{sym}-SM.NS", f"{sym}.BO"
        return [nse_sme, nse_main, bse] if (ipo.board or "") == "SME" else [nse_main, nse_sme, bse]
    out = [sym]
    if len(sym) >= 5 and sym.endswith("U") and sym.isalpha():
        out.append(f"{sym[:-1]}-UN")
    return out


def official_history(db: Session, ipo: IPO) -> dict | None:
    """India only: daily bars from NSE's bhavcopy archive (Tier 1) when at
    least MIN_OFFICIAL_BARS exist for the row's ISIN. Bhavcopy prices are
    unadjusted, so `splits` is None: a face-value split after listing shows
    up as a large negative return, and the implausible guard in
    market.windowed_returns applies to the listing return. The URL points at
    the file of the first stored bar (the listing session when coverage is
    complete)."""
    if (ipo.country or "").lower() != "india" or not ipo.isin:
        return None
    bars = nse_bhavcopy.bars_for_isin(db, ipo.isin)
    if len(bars) < MIN_OFFICIAL_BARS:
        return None
    first_day = datetime.strptime(bars[0]["trade_date"], "%Y-%m-%d").date()
    return {"url": nse_bhavcopy.url_for(first_day), "prices": bars, "splits": None, "source_name": NSE_SOURCE_NAME}


def fetch_history(ipo: IPO) -> tuple[dict | None, str]:
    """(history, symbol_used). History is the first candidate that returns
    bars; None when every candidate came back empty."""
    for sym in candidate_symbols(ipo):
        try:
            h = market.fetch_yahoo_history(sym, ipo.country, raw_symbol=True)
        except Exception:
            continue
        if h and h.get("prices"):
            return h, sym
    return None, ""


def benchmark_return(bench_cache: dict[str, dict], country: str, listing_ts: float, window_days: int) -> float | None:
    key = (country or "").lower()
    if key not in bench_cache:
        try:
            bench_cache[key] = market.fetch_benchmark_history(country) or {}
        except Exception:
            bench_cache[key] = {}
    bars = bench_cache[key].get("prices") or []
    if not bars:
        return None
    start = market.bar_on_or_after(bars, listing_ts)
    end = market.bar_nearest_before_or_on(bars, listing_ts + window_days * 86400)
    if not start or not end or not start.get("close") or end["ts"] <= start["ts"]:
        return None
    return (end["close"] / start["close"] - 1) * 100


def _mark(ipo: IPO, status: str, when: datetime) -> str:
    ipo.market_data_status = status
    ipo.market_data_checked_at = when
    return status


def refresh_one(db: Session, ipo: IPO, bench_cache: dict[str, dict], today: date | None = None) -> str:
    """Fetch the price series for one Listed row, write a PerformanceSnapshot
    when there is anything real to write, and record the attempt outcome on
    the row. Returns the status. Does not commit."""
    when = now()
    listing_dt = market.parse_date(ipo.listing_date)
    h = official_history(db, ipo)
    if h is None:
        h, _sym_used = fetch_history(ipo)
    if not h:
        return _mark(ipo, STATUS_NO_SERIES, when)
    source_name = h.get("source_name") or SOURCE_NAME
    bars = h["prices"]
    wr = market.windowed_returns(bars, listing_dt, issue_price=ipo.final_price, splits=h.get("splits")) if listing_dt else {}
    if not wr:
        return _mark(ipo, STATUS_NO_LISTING_BAR, when)
    values = {f: wr.get(f) for f in RETURN_FIELDS}
    if all(v is None for v in values.values()):
        # Listed so recently that no window has elapsed and no listing return
        # could be formed (no offer price): nothing observable yet.
        return _mark(ipo, STATUS_NO_LISTING_BAR, when)
    listing_used = market.parse_date(wr.get("listing_date_used") or "")
    listing_ts = listing_used.timestamp() if listing_used else listing_dt.timestamp()
    bench_12m = benchmark_return(bench_cache, ipo.country, listing_ts, 365)
    rel_12m = (values["return_12m_pct"] - bench_12m) if values["return_12m_pct"] is not None and bench_12m is not None else None
    latest = bars[-1]
    snap = PerformanceSnapshot(
        ipo_id=ipo.id,
        as_of_date=datetime.fromtimestamp(latest["ts"], tz=timezone.utc).date().isoformat(),
        close_price=latest["close"],
        listing_return_pct=values["listing_return_pct"],
        listing_open_return_pct=values["listing_open_return_pct"],
        return_7d_pct=values["return_7d_pct"],
        return_1m_pct=values["return_1m_pct"],
        return_90d_pct=values["return_90d_pct"],
        return_6m_pct=values["return_6m_pct"],
        return_12m_pct=values["return_12m_pct"],
        return_24m_pct=values["return_24m_pct"],
        benchmark_return_pct=bench_12m,
        benchmark_relative_12m_pct=rel_12m,
        listing_date_used=wr.get("listing_date_used") or "",
        source_name=source_name + (" (suppressed listing return: " + wr["listing_return_note"] + ")" if wr.get("listing_return_note") else ""),
        source_url=h.get("url", ""),
    )
    db.add(snap)
    status = STATUS_IMPLAUSIBLE if wr.get("listing_return_note") else STATUS_OK
    return _mark(ipo, status, when)


def _latest_snapshot_dates(db: Session, ipo_ids: list[int]) -> dict[int, str]:
    if not ipo_ids:
        return {}
    rows = db.execute(select(PerformanceSnapshot.ipo_id, func.max(PerformanceSnapshot.as_of_date))
                      .where(PerformanceSnapshot.ipo_id.in_(ipo_ids)).group_by(PerformanceSnapshot.ipo_id)).all()
    return {r[0]: r[1] for r in rows}


def select_candidates(db: Session, country: str | None, limit: int, retry_after_days: int, only_missing: bool,
                      today: date | None = None) -> list[IPO]:
    """Listed rows with a symbol, in priority order:
    1. never attempted (market_data_status == "")
    2. (unless only_missing) status ok whose latest snapshot is older than 7 days
    3. no_series / no_listing_bar / implausible checked more than retry_after_days ago"""
    today = today or now().date()
    stmt = select(IPO).where(IPO.status == "Listed", IPO.symbol != "")
    if country:
        stmt = stmt.where(IPO.country == country)
    rows = db.scalars(stmt.order_by(IPO.updated_at.desc())).all()
    never = [r for r in rows if not (r.market_data_status or "")]
    stale_ok: list[IPO] = []
    if not only_missing:
        ok_rows = [r for r in rows if r.market_data_status == STATUS_OK]
        latest = _latest_snapshot_dates(db, [r.id for r in ok_rows])
        cutoff = (today - timedelta(days=7)).isoformat()
        stale_ok = [r for r in ok_rows if (latest.get(r.id) or "") < cutoff]
    retry_cutoff = now() - timedelta(days=retry_after_days)
    retry = [r for r in rows if r.market_data_status in (STATUS_NO_SERIES, STATUS_NO_LISTING_BAR, STATUS_IMPLAUSIBLE)
             and (r.market_data_checked_at is None or _aware(r.market_data_checked_at) < retry_cutoff)]
    return (never + stale_ok + retry)[:limit]


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def refresh_many(db: Session, country: str | None = None, limit: int = 200, retry_after_days: int = 30,
                 only_missing: bool = True, max_seconds: float | None = None, sleep=time.sleep) -> dict:
    """Bounded pass over select_candidates(). Commits after every row so an
    interrupted run keeps its progress. Returns counts per status."""
    counts = {s: 0 for s in STATUSES}
    counts["attempted"] = 0
    started = time.monotonic()
    bench_cache: dict[str, dict] = {}
    for ipo in select_candidates(db, country, limit, retry_after_days, only_missing):
        if max_seconds is not None and time.monotonic() - started > max_seconds:
            counts["stopped_on_time"] = True
            break
        try:
            status = refresh_one(db, ipo, bench_cache)
            db.commit()
        except Exception as e:  # one bad row never aborts the pass
            db.rollback()
            counts.setdefault("errors", 0)
            counts["errors"] += 1
            counts.setdefault("last_error", f"{type(e).__name__}: {e}"[:200])
            continue
        counts[status] += 1
        counts["attempted"] += 1
        sleep(POLITE_SLEEP_SECONDS)
    return counts


def coverage(db: Session) -> dict[str, dict]:
    """Listed rows with at least one PerformanceSnapshot, per country."""
    out = {}
    snapped = set(db.scalars(select(PerformanceSnapshot.ipo_id).distinct()).all())
    for country in ("India", "United States"):
        listed = db.scalars(select(IPO).where(IPO.country == country, IPO.status == "Listed")).all()
        with_snap = sum(1 for r in listed if r.id in snapped)
        statuses: dict[str, int] = {}
        for r in listed:
            statuses[r.market_data_status or "never"] = statuses.get(r.market_data_status or "never", 0) + 1
        out[country] = {"listed": len(listed), "with_snapshot": with_snap, "with_symbol": sum(1 for r in listed if r.symbol),
                        "pct": round(with_snap / len(listed) * 100, 1) if listed else None, "status_counts": statuses}
    return out
