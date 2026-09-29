from __future__ import annotations
"""Classification of every genuine forward prediction (ScoreSnapshot with
is_forward=True) into exactly one explicit grading category. Nothing is
silently dropped: the same function drives the measurement report, the
public track record and the outcome grader, so the three can never disagree
about why a prediction has (or has not) been graded.

Categories (closed vocabulary):
  GRADED                 a PredictionOutcome row with at least one realized return
  PENDING                listed, eligible, everything needed is present, grader has not attached a return yet
  BLOCKED_IDENTITY       listed but no exchange symbol on record
  BLOCKED_OFFER_PRICE    listed with a symbol but no final offer price
  BLOCKED_MARKET_DATA    listed, symbol and price present, last grading attempt found no usable price series
  NOT_YET_ELIGIBLE       the IPO has not listed yet, or listed too recently for a closing bar to exist
  INVALID_FORWARD_RECORD flagged forward but the row can never have an outcome (Not IPO, Withdrawn)
"""
from datetime import date, datetime, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import IPO, ScoreSnapshot, PredictionOutcome
from .market import parse_date

GRADED = "GRADED"
PENDING = "PENDING"
BLOCKED_IDENTITY = "BLOCKED_IDENTITY"
BLOCKED_OFFER_PRICE = "BLOCKED_OFFER_PRICE"
BLOCKED_MARKET_DATA = "BLOCKED_MARKET_DATA"
NOT_YET_ELIGIBLE = "NOT_YET_ELIGIBLE"
INVALID_FORWARD_RECORD = "INVALID_FORWARD_RECORD"
CATEGORIES = (GRADED, PENDING, BLOCKED_IDENTITY, BLOCKED_OFFER_PRICE, BLOCKED_MARKET_DATA, NOT_YET_ELIGIBLE, INVALID_FORWARD_RECORD)

# A listing session must have closed before any return exists.
MIN_DAYS_SINCE_LISTING = 1


def outcome_has_return(outcome: PredictionOutcome | None) -> bool:
    if outcome is None:
        return False
    return any(getattr(outcome, f) is not None for f in (
        "listing_open_return_pct", "listing_close_return_pct", "return_7d_pct", "return_30d_pct",
        "return_6m_pct", "return_12m_pct", "return_24m_pct"))


def classify_prediction(ipo: IPO, outcome: PredictionOutcome | None, today: date | None = None) -> tuple[str, str]:
    """(category, reason) for one forward prediction of `ipo`. `outcome` is
    the PredictionOutcome attached to that snapshot, if any. Pure: no I/O."""
    today = today or datetime.now(timezone.utc).date()
    status = ipo.status or ""
    if status in ("Not IPO", "Withdrawn"):
        return INVALID_FORWARD_RECORD, f"issue is {status}: no listing outcome can exist"
    if outcome_has_return(outcome):
        return GRADED, "realized return attached"
    if status != "Listed":
        return NOT_YET_ELIGIBLE, f"status {status}: not listed yet"
    listing_dt = parse_date(ipo.listing_date)
    if listing_dt is None:
        return BLOCKED_IDENTITY, "listed but listing date is missing or unparseable"
    if (today - listing_dt.date()).days < MIN_DAYS_SINCE_LISTING:
        return NOT_YET_ELIGIBLE, "listed today: first closing bar not available yet"
    if not ipo.symbol:
        return BLOCKED_IDENTITY, "listed but no exchange symbol resolved"
    if not ipo.final_price or ipo.final_price <= 0:
        return BLOCKED_OFFER_PRICE, "listed but final offer price unknown"
    if outcome is not None and (outcome.grading_status or "") == BLOCKED_MARKET_DATA:
        return BLOCKED_MARKET_DATA, outcome.grading_note or "no usable price series on last attempt"
    return PENDING, "eligible: awaiting grader"


def forward_ledger(db: Session, today: date | None = None) -> list[dict]:
    """One row per forward snapshot with its category. Used by the report and
    the public track record."""
    snaps = db.scalars(select(ScoreSnapshot).where(ScoreSnapshot.is_forward == True).order_by(ScoreSnapshot.created_at.desc())).all()  # noqa: E712
    ipo_ids = {s.ipo_id for s in snaps}
    ipos = {i.id: i for i in db.scalars(select(IPO).where(IPO.id.in_(ipo_ids))).all()} if ipo_ids else {}
    outcomes = {o.score_snapshot_id: o for o in db.scalars(select(PredictionOutcome).where(PredictionOutcome.score_snapshot_id.in_([s.id for s in snaps]))).all()} if snaps else {}
    out = []
    for s in snaps:
        ipo = ipos.get(s.ipo_id)
        if ipo is None:
            out.append({"snapshot_id": s.id, "ipo_id": s.ipo_id, "category": INVALID_FORWARD_RECORD, "reason": "ipo row missing"})
            continue
        cat, reason = classify_prediction(ipo, outcomes.get(s.id), today)
        out.append({"snapshot_id": s.id, "ipo_id": ipo.id, "country": ipo.country, "status": ipo.status,
                    "event_stage": s.event_stage, "created_at": s.created_at.isoformat() if s.created_at else None,
                    "category": cat, "reason": reason})
    return out


def ledger_counts(ledger: list[dict]) -> dict:
    counts = {c: 0 for c in CATEGORIES}
    by_country: dict[str, dict] = {}
    for row in ledger:
        counts[row["category"]] += 1
        bc = by_country.setdefault(row.get("country") or "?", {c: 0 for c in CATEGORIES})
        bc[row["category"]] += 1
    reasons: dict[str, int] = {}
    for row in ledger:
        if row["category"] != GRADED:
            k = f"{row['category']}: {row['reason']}"
            reasons[k] = reasons.get(k, 0) + 1
    return {"total": len(ledger), "by_category": counts, "by_country": by_country,
            "blocked_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1]))}
