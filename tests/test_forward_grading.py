"""Phase 9/10: every forward prediction lands in exactly one explicit grading
category, the grader selects by category (not by "recently updated Listed
rows"), failed attempts are recorded, and identical re-ingests do not create
duplicate snapshots."""
from datetime import date, datetime, timezone
import pytest
from sqlalchemy import select
from app.models import IPO, ScoreSnapshot, PredictionOutcome
from app.services import forward_grading as fg
from app.services import outcomes as outcomes_svc
from app.services.pipeline import upsert_ipo

TODAY = date(2026, 9, 29)


def _ipo(**kw):
    base = dict(company="Co", country="United States", status="Listed", symbol="COX", final_price=10.0, listing_date="2026-09-01")
    base.update(kw)
    return IPO(**base)


def _snap(ipo_id, created=None, forward=True):
    return ScoreSnapshot(ipo_id=ipo_id, overall_score=55, listing_score=55, long_term_score=55, confidence=40,
                         listing_gain_probability=30, long_term_outperform_probability=30, recommendation="WATCH",
                         horizon="NONE", valuation_label="INSUFFICIENT DATA", event_stage="ipo_discovered", is_forward=forward,
                         created_at=created or datetime(2026, 8, 20, tzinfo=timezone.utc))


# ---------- classify_prediction: one test per category, pure in-memory ----------

def test_invalid_forward_record_for_not_ipo_and_withdrawn():
    assert fg.classify_prediction(_ipo(status="Not IPO"), None, TODAY)[0] == fg.INVALID_FORWARD_RECORD
    assert fg.classify_prediction(_ipo(status="Withdrawn"), None, TODAY)[0] == fg.INVALID_FORWARD_RECORD
    # even with a stray outcome row attached, an invalid record stays invalid
    o = PredictionOutcome(listing_close_return_pct=5.0, grading_status=fg.GRADED)
    assert fg.classify_prediction(_ipo(status="Not IPO"), o, TODAY)[0] == fg.INVALID_FORWARD_RECORD


def test_graded_when_outcome_has_a_return():
    o = PredictionOutcome(listing_close_return_pct=12.5, grading_status=fg.GRADED)
    assert fg.classify_prediction(_ipo(), o, TODAY) == (fg.GRADED, "realized return attached")


def test_not_yet_eligible_before_listing_and_on_listing_day():
    for st in ("Filed", "Upcoming", "Open", "Closed", "Priced"):
        cat, reason = fg.classify_prediction(_ipo(status=st), None, TODAY)
        assert cat == fg.NOT_YET_ELIGIBLE and st in reason
    cat, _ = fg.classify_prediction(_ipo(listing_date=TODAY.isoformat()), None, TODAY)
    assert cat == fg.NOT_YET_ELIGIBLE


def test_blocked_identity_without_symbol_or_listing_date():
    assert fg.classify_prediction(_ipo(symbol=""), None, TODAY)[0] == fg.BLOCKED_IDENTITY
    assert fg.classify_prediction(_ipo(listing_date=""), None, TODAY)[0] == fg.BLOCKED_IDENTITY


def test_blocked_offer_price_without_final_price():
    assert fg.classify_prediction(_ipo(final_price=None), None, TODAY)[0] == fg.BLOCKED_OFFER_PRICE
    assert fg.classify_prediction(_ipo(final_price=0), None, TODAY)[0] == fg.BLOCKED_OFFER_PRICE


def test_blocked_market_data_uses_recorded_attempt():
    o = PredictionOutcome(grading_status=fg.BLOCKED_MARKET_DATA, grading_note="no price series for symbol COX")
    assert fg.classify_prediction(_ipo(), o, TODAY) == (fg.BLOCKED_MARKET_DATA, "no price series for symbol COX")


def test_pending_when_everything_present_and_no_attempt():
    assert fg.classify_prediction(_ipo(), None, TODAY)[0] == fg.PENDING
    # an empty outcome row (no status, no returns) is still pending
    assert fg.classify_prediction(_ipo(), PredictionOutcome(), TODAY)[0] == fg.PENDING


def test_every_outcome_is_one_of_the_seven_categories():
    for ipo in (_ipo(), _ipo(status="Open"), _ipo(symbol=""), _ipo(final_price=None), _ipo(status="Not IPO")):
        assert fg.classify_prediction(ipo, None, TODAY)[0] in fg.CATEGORIES


# ---------- ledger + grader against the DB, network mocked ----------

def _seed(db):
    listed = _ipo(external_key="g-1", company="GradeCo")
    blocked = _ipo(external_key="g-2", company="NoSym", symbol="")
    open_ = _ipo(external_key="g-3", company="StillOpen", status="Open")
    notipo = _ipo(external_key="g-4", company="Follow-on", status="Not IPO")
    nodata = _ipo(external_key="g-5", company="Delisted", symbol="GONE")
    hist = _ipo(external_key="g-6", company="Backfilled")
    db.add_all([listed, blocked, open_, nodata, notipo, hist]); db.commit()
    for i in (listed, blocked, open_, notipo, nodata):
        db.refresh(i)
    db.refresh(hist)
    snaps = [_snap(listed.id), _snap(listed.id, created=datetime(2026, 8, 25, tzinfo=timezone.utc)),
             _snap(blocked.id), _snap(open_.id), _snap(notipo.id), _snap(nodata.id), _snap(hist.id, forward=False)]
    db.add_all(snaps); db.commit()
    return listed, nodata


def _fake_history(symbol, country, **kw):
    base_ts = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
    if symbol == "GONE":
        return {"prices": [], "url": "https://query1.finance.yahoo.com/gone", "splits": []}
    bars = [{"ts": base_ts, "open": 11.0, "close": 12.0}, {"ts": base_ts + 7 * 86400, "open": 12.0, "close": 13.0},
            {"ts": base_ts + 27 * 86400, "open": 13.0, "close": 14.0}]
    return {"prices": bars, "url": "https://query1.finance.yahoo.com/fake", "splits": []}


def test_ledger_counts_every_forward_snapshot_once(db):
    _seed(db)
    ledger = fg.forward_ledger(db, TODAY)
    assert len(ledger) == 6  # the is_forward=False row is not a forward prediction
    counts = fg.ledger_counts(ledger)["by_category"]
    assert counts == {fg.GRADED: 0, fg.PENDING: 3, fg.BLOCKED_IDENTITY: 1, fg.BLOCKED_OFFER_PRICE: 0,
                      fg.BLOCKED_MARKET_DATA: 0, fg.NOT_YET_ELIGIBLE: 1, fg.INVALID_FORWARD_RECORD: 1}
    assert sum(counts.values()) == len(ledger)


def test_grader_grades_every_pending_snapshot_or_records_why_not(db, monkeypatch):
    listed, nodata = _seed(db)
    monkeypatch.setattr(outcomes_svc.market, "fetch_yahoo_history", _fake_history)
    monkeypatch.setattr(outcomes_svc.market, "fetch_benchmark_history", lambda country: None)
    before = {s.id for s in db.scalars(select(ScoreSnapshot)).all()}
    result = outcomes_svc.sync_prediction_outcomes(db, today=TODAY)
    assert result["checked"] == 3 and result["graded"] == 2 and result["blocked_market_data"] == 1
    assert result["skipped_by_category"][fg.PENDING] == 0  # nothing pending was left behind
    # both forward snapshots of the listed IPO carry the same realized outcome
    outs = db.scalars(select(PredictionOutcome).where(PredictionOutcome.ipo_id == listed.id)).all()
    assert len(outs) == 2 and all(o.grading_status == fg.GRADED for o in outs)
    assert outs[0].listing_close_return_pct == pytest.approx(20.0) and outs[0].return_7d_pct == pytest.approx(30.0)
    assert outs[0].listing_open_return_pct == pytest.approx(10.0)
    # the delisted symbol is recorded as blocked, with the reason, not skipped
    o = db.scalar(select(PredictionOutcome).where(PredictionOutcome.ipo_id == nodata.id))
    assert o.grading_status == fg.BLOCKED_MARKET_DATA and "GONE" in o.grading_note and o.graded_at is not None
    ledger = fg.public_ledger(db, TODAY)
    assert ledger["by_category"][fg.GRADED] == 2 and ledger["by_category"][fg.BLOCKED_MARKET_DATA] == 1
    assert ledger["by_category"][fg.PENDING] == 0 and ledger["gradable"] == 4 and ledger["graded"] == 2
    # snapshots untouched
    assert before == {s.id for s in db.scalars(select(ScoreSnapshot)).all()} and len(before) == 7


def test_blocked_attempt_is_retried_only_after_the_retry_window(db, monkeypatch):
    listed, nodata = _seed(db)
    monkeypatch.setattr(outcomes_svc.market, "fetch_yahoo_history", _fake_history)
    monkeypatch.setattr(outcomes_svc.market, "fetch_benchmark_history", lambda country: None)
    outcomes_svc.sync_prediction_outcomes(db, today=TODAY)
    second = outcomes_svc.sync_prediction_outcomes(db, today=TODAY)
    assert second["checked"] == 0 and second["skipped_by_category"][fg.BLOCKED_MARKET_DATA] == 1
    # after the window the blocked row is retried; symbol now has data
    monkeypatch.setattr(outcomes_svc.market, "fetch_yahoo_history", lambda s, c, **kw: _fake_history("OK", c))
    o = db.scalar(select(PredictionOutcome).where(PredictionOutcome.ipo_id == nodata.id))
    o.graded_at = datetime(2026, 9, 1, tzinfo=timezone.utc); db.commit()
    third = outcomes_svc.sync_prediction_outcomes(db, today=TODAY)
    assert third["checked"] == 1 and third["graded"] == 1
    db.refresh(o)
    assert o.grading_status == fg.GRADED and o.grading_note == ""


def test_grader_respects_secondary_market_data_switch(db, monkeypatch):
    _seed(db)
    from app import config
    settings = config.get_settings()
    monkeypatch.setattr(settings, "allow_secondary_market_data", False)
    result = outcomes_svc.sync_prediction_outcomes(db, today=TODAY)
    assert result["checked"] == 0 and "disabled" in result


def test_track_record_endpoint_exposes_categories(authed_client, db, monkeypatch):
    _seed(db)
    monkeypatch.setattr(outcomes_svc.market, "fetch_yahoo_history", _fake_history)
    monkeypatch.setattr(outcomes_svc.market, "fetch_benchmark_history", lambda country: None)
    outcomes_svc.sync_prediction_outcomes(db, today=TODAY)
    r = authed_client.get("/api/track-record")
    assert r.status_code == 200
    body = r.json()
    assert body["total_forward_predictions"] == 6 and body["graded_with_outcome"] == 2 and body["gradable"] == 4
    assert set(body["ledger"]["by_category"]) == set(fg.CATEGORIES)
    assert body["categories"] == list(fg.CATEGORIES)
    for p in body["predictions"]:
        assert p["outcome_status"] in fg.CATEGORIES and p["grading"]["category"] == p["outcome_status"] and p["grading"]["reason"]
        if p["outcome_status"] == fg.GRADED:
            assert p["outcome"]["listing_close_return_pct"] == pytest.approx(20.0)
        else:
            assert p["outcome"] is None
    assert "—" not in body["note"]


# ---------- Phase 10: no duplicate snapshots from identical re-ingests ----------

def test_identical_reingest_creates_no_duplicate_snapshot(db):
    """Legacy 'material_change' duplicates in production were written by the
    pipeline before 2026-09-28; the current _event_stage only snapshots a
    structural event or an actual score/recommendation move. Those legacy
    rows are kept (immutability) and deduplicated at read time."""
    row = dict(company="DupCo", country="India", symbol="DUPCO", status="Open", price_low=95, price_high=105, total_sub=1.2341,
               filing_url="https://nseindia.com/x")
    upsert_ipo(db, dict(row), "NSE", "https://nseindia.com/x", 1); db.commit()
    upsert_ipo(db, dict(row), "NSE", "https://nseindia.com/x", 1); db.commit()
    snaps = db.scalars(select(ScoreSnapshot)).all()
    assert len(snaps) == 1 and snaps[0].event_stage == "ipo_discovered"
    # a subscription tick that does not move the score is not a prediction event
    upsert_ipo(db, {**row, "total_sub": 1.2343}, "NSE", "https://nseindia.com/x", 1); db.commit()
    assert len(db.scalars(select(ScoreSnapshot)).all()) == 1
