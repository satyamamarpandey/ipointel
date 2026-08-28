from __future__ import annotations
"""Attaches realized returns to a forward prediction AFTER the IPO lists,
without ever touching the original ScoreSnapshot row (enforced at the ORM
level - see models._reject_score_snapshot_mutation). One PredictionOutcome
row per IPO, linked to that IPO's LAST is_forward=True snapshot: the model's
final call before the outcome was known. The outcome row itself is upserted
in place as more return windows become observable (a 24m return literally
cannot exist until 24 months have passed)."""
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import IPO, ScoreSnapshot, PredictionOutcome
from ..config import get_settings
from . import market

def now(): return datetime.now(timezone.utc)

def _last_forward_snapshot(db: Session, ipo_id: int) -> ScoreSnapshot | None:
    return db.scalar(
        select(ScoreSnapshot)
        .where(ScoreSnapshot.ipo_id == ipo_id, ScoreSnapshot.is_forward == True)  # noqa: E712
        .order_by(ScoreSnapshot.created_at.desc()).limit(1)
    )

def sync_prediction_outcomes(db: Session, limit: int = 60) -> dict:
    settings = get_settings()
    result = {"checked": 0, "updated": 0, "no_forward_snapshot": 0, "no_price_data": 0}
    if not settings.allow_secondary_market_data:
        return result
    ipos = db.scalars(select(IPO).where(IPO.status == "Listed", IPO.symbol != "").order_by(IPO.updated_at.desc()).limit(limit)).all()
    bench_cache: dict[str, dict] = {}
    for ipo in ipos:
        snap = _last_forward_snapshot(db, ipo.id)
        if not snap:
            result["no_forward_snapshot"] += 1
            continue
        result["checked"] += 1
        try:
            h = market.fetch_yahoo_history(ipo.symbol, ipo.country)
            bars = h["prices"]
            if not bars:
                result["no_price_data"] += 1
                continue
            listing_dt = market.parse_date(ipo.listing_date)
            if not listing_dt:
                result["no_price_data"] += 1
                continue
            wr = market.windowed_returns(bars, listing_dt)
            if not wr:
                result["no_price_data"] += 1
                continue
            key = ipo.country.lower()
            if key not in bench_cache:
                try: bench_cache[key] = market.fetch_benchmark_history(ipo.country) or {}
                except Exception: bench_cache[key] = {}
            bench_bars = bench_cache[key].get("prices") or []
            bench_rel = None
            if bench_bars and wr.get("return_12m_pct") is not None:
                b_start = market.bar_on_or_after(bench_bars, listing_dt.timestamp())
                b_end = market.bar_nearest_before_or_on(bench_bars, listing_dt.timestamp() + 365 * 86400)
                if b_start and b_end and b_start.get("close") and b_end["ts"] > b_start["ts"]:
                    bench_index_return = (b_end["close"] / b_start["close"] - 1) * 100
                    bench_rel = wr["return_12m_pct"] - bench_index_return
            open_px, close_px = wr.get("listing_open"), wr.get("listing_close")
            outcome = db.scalar(select(PredictionOutcome).where(PredictionOutcome.score_snapshot_id == snap.id))
            if not outcome:
                outcome = PredictionOutcome(score_snapshot_id=snap.id, ipo_id=ipo.id)
                db.add(outcome)
            outcome.listing_open_return_pct = round((open_px / ipo.final_price - 1) * 100, 2) if open_px and ipo.final_price else outcome.listing_open_return_pct
            outcome.listing_close_return_pct = round((close_px / ipo.final_price - 1) * 100, 2) if close_px and ipo.final_price else outcome.listing_close_return_pct
            for field, key_ in (("return_7d_pct", "return_7d_pct"), ("return_30d_pct", "return_30d_pct"), ("return_6m_pct", "return_6m_pct"), ("return_12m_pct", "return_12m_pct"), ("return_24m_pct", "return_24m_pct")):
                v = wr.get(key_)
                if v is not None:
                    setattr(outcome, field, round(v, 2))
            if bench_rel is not None:
                outcome.benchmark_relative_return_pct = round(bench_rel, 2)
            outcome.source_name = h.get("url", "Yahoo Finance fallback")
            result["updated"] += 1
        except Exception:
            continue
    db.commit()
    return result
