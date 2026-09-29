from __future__ import annotations
"""Attaches realized returns to genuine forward predictions AFTER the IPO
lists, without ever touching the original ScoreSnapshot rows (enforced at
the ORM level - see models._reject_score_snapshot_mutation).

Every forward snapshot of a listed IPO was written before the outcome was
known, so all of them share one realized outcome: each gets its own
PredictionOutcome row (unique per snapshot) carrying the same returns.

Selection is driven by forward_grading.forward_ledger, never by "the most
recently updated Listed rows" - that ordering surfaced backfilled historical
rows with no forward snapshot and starved the eligible ones, which is why
the track record showed zero graded predictions.

Nothing is dropped silently: every PENDING snapshot considered in a pass
ends up GRADED or BLOCKED_MARKET_DATA (with a note saying why), and a
BLOCKED_MARKET_DATA row is retried after RETRY_BLOCKED_AFTER_DAYS."""
from datetime import date, datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import IPO, ScoreSnapshot, PredictionOutcome
from ..config import get_settings
from . import market, performance
from .forward_grading import (forward_ledger, GRADED, PENDING, BLOCKED_MARKET_DATA, CATEGORIES)

RETRY_BLOCKED_AFTER_DAYS = 14
_RETURN_FIELDS = ("return_7d_pct", "return_30d_pct", "return_6m_pct", "return_12m_pct", "return_24m_pct")


def now(): return datetime.now(timezone.utc)


def _outcome_for(db: Session, snapshot_id: int, ipo_id: int) -> PredictionOutcome:
    outcome = db.scalar(select(PredictionOutcome).where(PredictionOutcome.score_snapshot_id == snapshot_id))
    if not outcome:
        outcome = PredictionOutcome(score_snapshot_id=snapshot_id, ipo_id=ipo_id)
        db.add(outcome)
    return outcome


def _block(outcome: PredictionOutcome, note: str, when: datetime) -> None:
    outcome.grading_status = BLOCKED_MARKET_DATA
    outcome.grading_note = note
    outcome.graded_at = when
    outcome.listing_open_return_pct = None
    outcome.listing_close_return_pct = None
    for f in _RETURN_FIELDS:
        setattr(outcome, f, None)
    outcome.benchmark_relative_return_pct = None


def _grade(outcome: PredictionOutcome, ipo: IPO, wr: dict, bench_rel: float | None, source_url: str, when: datetime) -> None:
    open_px, close_px = wr.get("listing_open"), wr.get("listing_close")
    price = ipo.final_price
    outcome.listing_open_return_pct = round((open_px / price - 1) * 100, 2) if open_px and price else None
    outcome.listing_close_return_pct = round(wr["listing_return_pct"], 2) if wr.get("listing_return_pct") is not None else (round((close_px / price - 1) * 100, 2) if close_px and price else None)
    for f in _RETURN_FIELDS:
        v = wr.get(f)
        setattr(outcome, f, round(v, 2) if v is not None else None)
    outcome.benchmark_relative_return_pct = round(bench_rel, 2) if bench_rel is not None else None
    outcome.source_name = source_url or "Yahoo Finance fallback"
    outcome.grading_status = GRADED
    outcome.grading_note = ""
    outcome.graded_at = when


def _benchmark_relative_12m(bench_bars: list[dict], listing_ts: float, ret_12m: float | None) -> float | None:
    if not bench_bars or ret_12m is None:
        return None
    b_start = market.bar_on_or_after(bench_bars, listing_ts)
    b_end = market.bar_nearest_before_or_on(bench_bars, listing_ts + 365 * 86400)
    if b_start and b_end and b_start.get("close") and b_end["ts"] > b_start["ts"]:
        return ret_12m - (b_end["close"] / b_start["close"] - 1) * 100
    return None


def _retry_due(db: Session, snapshot_id: int, today: date) -> bool:
    outcome = db.scalar(select(PredictionOutcome).where(PredictionOutcome.score_snapshot_id == snapshot_id))
    if outcome is None or outcome.graded_at is None:
        return True
    return (today - outcome.graded_at.date()).days >= RETRY_BLOCKED_AFTER_DAYS


def sync_prediction_outcomes(db: Session, limit: int = 60, today: date | None = None) -> dict:
    settings = get_settings()
    today = today or now().date()
    result = {"checked": 0, "graded": 0, "blocked_market_data": 0, "skipped_by_category": {c: 0 for c in CATEGORIES}}
    if not settings.allow_secondary_market_data:
        result["disabled"] = "ALLOW_SECONDARY_MARKET_DATA is false"
        return result
    ledger = forward_ledger(db, today)
    todo: dict[int, list[int]] = {}  # ipo_id -> snapshot ids
    for row in ledger:
        cat = row["category"]
        if cat == PENDING or (cat == BLOCKED_MARKET_DATA and _retry_due(db, row["snapshot_id"], today)):
            todo.setdefault(row["ipo_id"], []).append(row["snapshot_id"])
        else:
            result["skipped_by_category"][cat] += 1
    when = now()
    bench_cache: dict[str, list[dict]] = {}
    for ipo_id in list(todo)[:limit]:
        ipo = db.get(IPO, ipo_id)
        snapshot_ids = todo[ipo_id]
        if ipo is None:
            continue
        result["checked"] += len(snapshot_ids)
        listing_dt = market.parse_date(ipo.listing_date)
        # Same ticker candidates as the performance backfill (SPAC units are
        # "XXXX-UN" on Yahoo, NSE SME issues "SYMBOL-SM.NS"), so the grader and
        # the history explorer can never disagree about whether a series exists.
        try:
            h, _sym_used = performance.fetch_history(ipo)
            h = h or {"prices": [], "url": "", "splits": []}
        except Exception as e:
            h = {"prices": [], "url": "", "splits": [], "error": f"{type(e).__name__}"}
        bars = h.get("prices") or []
        note = ""
        wr: dict = {}
        if not bars:
            note = f"no price series for symbol {ipo.symbol}" + (f" ({h['error']})" if h.get("error") else "")
        elif listing_dt is None:
            note = "listing date unparseable"
        else:
            wr = market.windowed_returns(bars, listing_dt, issue_price=ipo.final_price, splits=h.get("splits"))
            if not wr:
                note = f"no listing session bar within tolerance of {listing_dt.date().isoformat()} for {ipo.symbol}"
            elif wr.get("listing_return_pct") is None and wr.get("listing_close") is None:
                note = f"listing session bar for {ipo.symbol} has no close"
        if note:
            for sid in snapshot_ids:
                _block(_outcome_for(db, sid, ipo.id), note, when)
            result["blocked_market_data"] += len(snapshot_ids)
            continue
        key = ipo.country.lower()
        if key not in bench_cache:
            try:
                bench_cache[key] = (market.fetch_benchmark_history(ipo.country) or {}).get("prices") or []
            except Exception:
                bench_cache[key] = []
        bench_rel = _benchmark_relative_12m(bench_cache[key], listing_dt.timestamp(), wr.get("return_12m_pct"))
        for sid in snapshot_ids:
            _grade(_outcome_for(db, sid, ipo.id), ipo, wr, bench_rel, h.get("url", ""), when)
        result["graded"] += len(snapshot_ids)
    db.commit()
    result["updated"] = result["graded"]  # backward-compatible alias
    return result


def outcomes_for_ipo(db: Session, ipo_id: int) -> list[PredictionOutcome]:
    return db.scalars(select(PredictionOutcome).join(ScoreSnapshot, ScoreSnapshot.id == PredictionOutcome.score_snapshot_id)
                      .where(PredictionOutcome.ipo_id == ipo_id).order_by(ScoreSnapshot.created_at.desc())).all()


def retry_horizon(today: date | None = None) -> date:
    """Date before which a BLOCKED_MARKET_DATA attempt is considered stale."""
    return (today or now().date()) - timedelta(days=RETRY_BLOCKED_AFTER_DAYS)
