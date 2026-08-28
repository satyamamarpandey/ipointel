"""Phase 14: immutable forward prediction snapshots.

ScoreSnapshot is the prospective-prediction record. These tests prove three
things a track record depends on: (1) a row can never be mutated after
creation, (2) event_stage/is_forward/feature_snapshot are populated
correctly and deterministically by the real ingestion path (not just when
constructed directly in a test), and (3) attaching a realized outcome later
never touches the original snapshot."""
import pytest
from sqlalchemy import select
from app.models import IPO, ScoreSnapshot, PredictionOutcome, ImmutableRecordError
from app.services.pipeline import upsert_ipo
from app.services import outcomes as outcomes_svc

def _row(**kw):
    base = dict(company="TestCo", country="India", symbol="TESTCO", exchange="NSE/BSE",
                revenue_m=500, revenue_prev_m=400, ebitda_m=80, net_income_m=40, cfo_m=60,
                debt_m=50, cash_m=30, fresh_issue_pct=70, ofs_pct=30, filing_url="https://nseindia.com/x")
    base.update(kw)
    return base

# ---------- immutability ----------

def test_score_snapshot_cannot_be_mutated(db):
    ipo = IPO(external_key="imm-1", company="ImmCo", country="India")
    db.add(ipo); db.commit(); db.refresh(ipo)
    sc = ScoreSnapshot(ipo_id=ipo.id, overall_score=70, listing_score=70, long_term_score=70, confidence=80,
                        listing_gain_probability=70, long_term_outperform_probability=70, recommendation="WATCH",
                        horizon="BOTH", valuation_label="FAIR", event_stage="ipo_discovered")
    db.add(sc); db.commit()
    sc.overall_score = 99
    with pytest.raises(ImmutableRecordError):
        db.commit()
    db.rollback()

def test_score_snapshot_deletion_is_not_blocked_only_mutation(db):
    # The guard targets accidental overwrite, not the ORM's normal cascade-delete
    # behavior (e.g. if an IPO record itself were ever purged in a test DB reset).
    ipo = IPO(external_key="imm-2", company="ImmCo2", country="India")
    db.add(ipo); db.commit(); db.refresh(ipo)
    sc = ScoreSnapshot(ipo_id=ipo.id, overall_score=70, listing_score=70, long_term_score=70, confidence=80,
                        listing_gain_probability=70, long_term_outperform_probability=70, recommendation="WATCH",
                        horizon="BOTH", valuation_label="FAIR")
    db.add(sc); db.commit()
    db.delete(sc); db.commit()  # must not raise
    assert db.get(ScoreSnapshot, sc.id) is None

# ---------- event stage / is_forward via the real ingestion path ----------

def test_new_live_ipo_automatically_gets_a_forward_snapshot(db):
    # upsert_ipo derives external_key from the row itself via pipeline.external_key()
    row = _row(status="Filed")
    upsert_ipo(db, row, "NSE", "https://nseindia.com/x", 1)
    db.commit()
    ipo = db.scalar(select(IPO).where(IPO.company == "TestCo"))
    snaps = db.scalars(select(ScoreSnapshot).where(ScoreSnapshot.ipo_id == ipo.id).order_by(ScoreSnapshot.created_at.asc())).all()
    assert len(snaps) == 1
    assert snaps[0].event_stage == "ipo_discovered"
    assert snaps[0].is_forward is True
    assert snaps[0].feature_schema_version == "fs1"
    assert snaps[0].feature_snapshot.get("revenue_m") == 500
    assert snaps[0].model_version

def test_backfilled_already_listed_ipo_is_not_a_forward_prediction(db):
    row = _row(company="BackfillCo", status="Listed", final_price=100, listing_date="2024-01-01")
    upsert_ipo(db, row, "SEC 424B4", "https://sec.gov/x", 1)
    db.commit()
    ipo = db.scalar(select(IPO).where(IPO.company == "BackfillCo"))
    sc = db.scalar(select(ScoreSnapshot).where(ScoreSnapshot.ipo_id == ipo.id))
    assert sc.is_forward is False
    assert sc.event_stage == "ipo_discovered"  # still the first-ever snapshot for this IPO

def test_price_band_then_subscription_produce_two_distinct_immutable_snapshots(db):
    row = _row(company="StageCo", status="Filed")
    upsert_ipo(db, row, "NSE", "https://nseindia.com/x", 1)
    db.commit()
    ipo = db.scalar(select(IPO).where(IPO.company == "StageCo"))
    first = db.scalar(select(ScoreSnapshot).where(ScoreSnapshot.ipo_id == ipo.id))
    first_id, first_overall = first.id, first.overall_score

    upsert_ipo(db, _row(company="StageCo", status="Filed", price_low=95, price_high=105), "NSE", "https://nseindia.com/x", 1)
    db.commit()
    upsert_ipo(db, _row(company="StageCo", status="Filed", price_low=95, price_high=105, qib_sub=12), "NSE", "https://nseindia.com/x", 1)
    db.commit()

    snaps = db.scalars(select(ScoreSnapshot).where(ScoreSnapshot.ipo_id == ipo.id).order_by(ScoreSnapshot.created_at.asc())).all()
    stages = [s.event_stage for s in snaps]
    assert "price_band_set" in stages
    assert "subscription_update" in stages
    # the original snapshot is still there, unchanged, at its original id
    original = db.get(ScoreSnapshot, first_id)
    assert original.overall_score == first_overall

def test_final_pre_listing_stage_before_status_flips_to_listed(db):
    row = _row(company="FinalCo", status="Open")
    upsert_ipo(db, row, "NSE", "https://nseindia.com/x", 1)
    db.commit()
    upsert_ipo(db, _row(company="FinalCo", status="Open", final_price=110), "NSE", "https://nseindia.com/x", 1)
    db.commit()
    ipo = db.scalar(select(IPO).where(IPO.company == "FinalCo"))
    last = db.scalar(select(ScoreSnapshot).where(ScoreSnapshot.ipo_id == ipo.id).order_by(ScoreSnapshot.created_at.desc()).limit(1))
    assert last.event_stage == "final_pre_listing"
    assert last.is_forward is True

# ---------- track record API ----------

def test_track_record_endpoint_only_returns_forward_predictions(client, db):
    fwd_ipo = IPO(external_key="tr-fwd", company="FwdCo", country="India", status="Filed")
    back_ipo = IPO(external_key="tr-back", company="BackCo", country="United States", status="Listed")
    db.add_all([fwd_ipo, back_ipo]); db.commit(); db.refresh(fwd_ipo); db.refresh(back_ipo)
    common = dict(overall_score=70, listing_score=70, long_term_score=70, confidence=80,
                  listing_gain_probability=70, long_term_outperform_probability=70,
                  recommendation="WATCH", horizon="BOTH", valuation_label="FAIR", event_stage="ipo_discovered")
    db.add(ScoreSnapshot(ipo_id=fwd_ipo.id, is_forward=True, **common))
    db.add(ScoreSnapshot(ipo_id=back_ipo.id, is_forward=False, **common))
    db.commit()
    r = client.get("/api/track-record")
    assert r.status_code == 200
    body = r.json()
    companies = [p["company"] for p in body["predictions"]]
    assert "FwdCo" in companies
    assert "BackCo" not in companies
    assert body["total_forward_predictions"] >= 1

# ---------- outcome attachment never touches the snapshot ----------

def test_sync_outcomes_attaches_returns_without_mutating_snapshot(db, monkeypatch):
    ipo = IPO(external_key="out-1", company="OutCo", country="United States", status="Listed",
              symbol="OUTC", final_price=20.0, listing_date="2024-01-02")
    db.add(ipo); db.commit(); db.refresh(ipo)
    sc = ScoreSnapshot(ipo_id=ipo.id, overall_score=65, listing_score=65, long_term_score=65, confidence=80,
                        listing_gain_probability=65, long_term_outperform_probability=65, recommendation="WATCH",
                        horizon="BOTH", valuation_label="FAIR", event_stage="ipo_discovered", is_forward=True)
    db.add(sc); db.commit(); db.refresh(sc)
    snapshot_fields_before = {c.name: getattr(sc, c.name) for c in ScoreSnapshot.__table__.columns}

    def fake_history(symbol, country, **kw):
        base_ts = 1704153600  # 2024-01-02
        bars = [{"ts": base_ts, "open": 22.0, "close": 24.0}, {"ts": base_ts + 7*86400, "close": 26.0},
                {"ts": base_ts + 30*86400, "close": 28.0}]
        return {"prices": bars, "url": "https://query1.finance.yahoo.com/fake"}
    monkeypatch.setattr(outcomes_svc.market, "fetch_yahoo_history", fake_history)
    monkeypatch.setattr(outcomes_svc.market, "fetch_benchmark_history", lambda country: None)

    result = outcomes_svc.sync_prediction_outcomes(db)
    assert result["updated"] == 1

    outcome = db.scalar(select(PredictionOutcome).where(PredictionOutcome.score_snapshot_id == sc.id))
    assert outcome is not None
    assert outcome.listing_open_return_pct == pytest.approx((22.0/20.0-1)*100, abs=0.01)
    assert outcome.listing_close_return_pct == pytest.approx((24.0/20.0-1)*100, abs=0.01)

    db.refresh(sc)
    snapshot_fields_after = {c.name: getattr(sc, c.name) for c in ScoreSnapshot.__table__.columns}
    assert snapshot_fields_before == snapshot_fields_after
