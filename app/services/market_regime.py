from __future__ import annotations
"""Market regime at listing, per IPO, strictly from data before the listing day.

Features (stored as FeatureObservation, rule "market_index_close"):
  market_return_60d_pct  benchmark index return over the 60 calendar days
                         ending at the last close BEFORE the listing date
  market_vol_20d_pct     annualised volatility of the last 20 daily index
                         returns before the listing date
available_at is the date of the last close used, so it is always earlier
than the listing date. Benchmarks: NIFTY 50 (^NSEI) and S&P 500 (^GSPC)
from Yahoo Finance, Tier 3, labelled as such (A-004: no official free index
history is stored). A third regime feature, the trailing 90-day IPO count,
is derived in model_eval from listing dates already in the database.
"""
import math
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import IPO, FeatureObservation
from . import market

RULE_INDEX_CLOSE = "market_index_close"
FIELDS = ("market_return_60d_pct", "market_vol_20d_pct")
LOOKBACK_DAYS = 60
VOL_WINDOW = 20
MAX_STALENESS_DAYS = 7  # last close must be within a week of listing
SOURCE_NAMES = {"India": "Yahoo Finance ^NSEI (NIFTY 50), Tier 3", "United States": "Yahoo Finance ^GSPC (S&P 500), Tier 3"}
STATUSES = ("Listed", "Upcoming", "Open", "Closed", "Priced")


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()


def regime_at(bars: list[dict], listing_date: str) -> dict | None:
    """{field: value, "as_of": last close date} from closes strictly before
    listing_date, or None when the series does not cover the window."""
    ld = market.parse_date(listing_date)
    if ld is None:
        return None
    cutoff = ld.timestamp()
    prior = [b for b in bars if b.get("close") and b["ts"] < cutoff]
    if len(prior) < VOL_WINDOW + 1:
        return None
    last = prior[-1]
    if cutoff - last["ts"] > MAX_STALENESS_DAYS * 86400:
        return None
    start_ts = last["ts"] - LOOKBACK_DAYS * 86400
    base = next((b for b in prior if b["ts"] >= start_ts), None)
    if base is None or base["ts"] - start_ts > MAX_STALENESS_DAYS * 86400 or base is last:
        return None
    window = prior[-(VOL_WINDOW + 1):]
    rets = [math.log(b["close"] / a["close"]) for a, b in zip(window, window[1:], strict=False)]
    mean = sum(rets) / len(rets)
    vol = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(252) * 100
    return {"market_return_60d_pct": (last["close"] / base["close"] - 1) * 100, "market_vol_20d_pct": vol,
            "as_of": _day(last["ts"])}


def _existing(db: Session, ids: list[int]) -> set[int]:
    if not ids:
        return set()
    rows = db.scalars(select(FeatureObservation.ipo_id).where(
        FeatureObservation.ipo_id.in_(ids), FeatureObservation.field_name == FIELDS[0],
        FeatureObservation.availability_rule == RULE_INDEX_CLOSE)).all()
    return set(rows)


def refresh(db: Session, fetch=market.fetch_benchmark_history, countries=("India", "United States")) -> dict:
    """Write regime observations for rows that lack them. One index request
    per market. Returns counts per market. Does not commit."""
    out: dict[str, dict] = {}
    for country in countries:
        ipos = db.scalars(select(IPO).where(IPO.country == country, IPO.status.in_(STATUSES), IPO.listing_date != "")).all()
        done = _existing(db, [i.id for i in ipos])
        todo = [i for i in ipos if i.id not in done]
        counts = {"candidates": len(todo), "written": 0, "no_window": 0}
        if not todo:
            out[country] = counts
            continue
        try:
            hist = fetch(country) or {}
        except Exception as e:  # network: record and move on, next run retries
            counts["error"] = type(e).__name__
            out[country] = counts
            continue
        bars = sorted((b for b in hist.get("prices") or [] if b.get("close")), key=lambda b: b["ts"])
        url = hist.get("url", "")
        today = datetime.now(timezone.utc).date().isoformat()
        for ipo in todo:
            if (ipo.listing_date or "")[:10] > today:
                continue  # future listing: the window is not complete yet
            r = regime_at(bars, ipo.listing_date)
            if r is None:
                counts["no_window"] += 1
                continue
            for f in FIELDS:
                db.add(FeatureObservation(
                    ipo_id=ipo.id, field_name=f, value=round(r[f], 4), unit="pct", source_name=SOURCE_NAMES[country],
                    source_url=url, source_tier=3, source_form="index close", period_start=_day(market.parse_date(r["as_of"]).timestamp() - LOOKBACK_DAYS * 86400),
                    period_end=r["as_of"], available_at=r["as_of"], availability_rule=RULE_INDEX_CLOSE, confidence=0.9,
                    raw={"listing_date": ipo.listing_date, "lookback_days": LOOKBACK_DAYS, "vol_window": VOL_WINDOW}))
            counts["written"] += 1
        out[country] = counts
    return out


def trailing_ipo_count(listing_dates: list[str], as_of: str, days: int = 90) -> int:
    """IPOs in the same market that listed in the `days` before as_of
    (strictly earlier dates). listing_dates must be YYYY-MM-DD strings."""
    lo = (datetime.fromisoformat(as_of) - timedelta(days=days)).date().isoformat()
    return sum(1 for d in listing_dates if lo <= d < as_of)
