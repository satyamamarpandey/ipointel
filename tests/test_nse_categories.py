"""Phase 7: per-category subscription from NSE's official per-issue endpoint,
captured forward-only as timestamped, event-staged FeatureObservations."""
from __future__ import annotations
from datetime import date

from sqlalchemy import select

from app.models import IPO, FeatureObservation
from app.services import nse
from app.services.pipeline import record_subscription_observations, subscription_stage, ingest_nse, NSE_LIVE_SOURCE

DETAIL = {"companyName": "SHAHINVEST", "bidDetails": [
    {"category": "Qualified Institutional Buyers(QIBs)", "noOfSharesOffered": "1079840", "noOfTime": "0.388", "srNo": "1"},
    {"category": "Foreign Institutional Investors(FIIs)", "noOfSharesOffered": "", "noOfTime": "", "srNo": "1(a)"},
    {"category": "Non Institutional Investors", "noOfSharesOffered": "809880", "noOfTime": "1.75", "srNo": "2"},
    {"category": "Retail Individual Investors(RIIs)", "noOfSharesOffered": "1889720", "noOfTime": "3.2", "srNo": "3"},
    {"category": "Total", "noOfSharesOffered": "3779440", "noOfTime": "1.9", "srNo": ""},
]}


def test_parse_bid_details_reads_top_level_categories_only():
    assert nse.parse_bid_details(DETAIL) == {"qib_sub": 0.388, "nii_sub": 1.75, "retail_sub": 3.2}
    assert nse.parse_bid_details({"bidDetails": []}) == {}
    assert nse.parse_bid_details(None) == {}


def test_fetch_current_attaches_categories_and_survives_detail_failure(monkeypatch):
    class R:
        def __init__(self, payload, status=200):
            self._p, self.status_code = payload, status
        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")
        def json(self):
            return self._p

    class C:
        def __init__(self, *a, **k):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def get(self, url):
            if url.endswith("/api/ipo-current-issue"):
                return R([{"symbol": "SHAHINVEST", "companyName": "Shah Investments", "series": "EQ", "noOfTime": "1.9", "issueStartDate": "24-Sep-2026", "issueEndDate": "28-Sep-2026"},
                          {"symbol": "BROKEN", "companyName": "Broken Detail", "series": "EQ", "noOfTime": "0.5"}])
            if "all-upcoming-issues" in url:
                return R([{"symbol": "NEXT", "companyName": "Next Issue", "series": "EQ"}])
            if "ipo-detail?symbol=SHAHINVEST" in url:
                return R(DETAIL)
            if "ipo-detail?symbol=BROKEN" in url:
                return R({}, 503)
            return R({})

    monkeypatch.setattr(nse.httpx, "Client", C)
    rows, warnings = nse.fetch_current()
    by = {r["symbol"]: r for r in rows}
    assert by["SHAHINVEST"]["qib_sub"] == 0.388 and by["SHAHINVEST"]["retail_sub"] == 3.2 and by["SHAHINVEST"]["total_sub"] == 1.9
    assert by["SHAHINVEST"]["subscription_source_url"] == nse.detail_url("SHAHINVEST", "EQ")
    assert by["BROKEN"]["total_sub"] == 0.5 and by["BROKEN"].get("qib_sub") is None and "subscription_source_url" not in by["BROKEN"]
    assert "NEXT" in by and by["NEXT"].get("qib_sub") is None  # upcoming issues have no bids yet
    assert any("NSE detail BROKEN" in w for w in warnings)


def test_subscription_stage_counts_business_days():
    assert subscription_stage("2026-09-24", date(2026, 9, 24)) == "subscription_day_1"  # Thursday, day 1
    assert subscription_stage("2026-09-24", date(2026, 9, 25)) == "subscription_day_2"
    assert subscription_stage("2026-09-24", date(2026, 9, 28)) == "subscription_day_3"  # weekend skipped
    assert subscription_stage("2026-09-24", date(2026, 10, 30)) == "subscription_day_5"  # capped
    assert subscription_stage("", date(2026, 9, 28)) == "subscription_day_unknown"


def test_record_subscription_observations_is_point_in_time_and_upserts_per_day(db):
    ipo = IPO(external_key="IN:shahinvest", company="Shah Investments", symbol="SHAHINVEST", country="India", status="Open", open_date="2026-09-24")
    db.add(ipo)
    db.commit()
    row = {"qib_sub": 0.388, "nii_sub": 1.75, "retail_sub": 3.2, "total_sub": 1.9, "open_date": "2026-09-24", "subscription_source_url": nse.detail_url("SHAHINVEST")}
    assert record_subscription_observations(db, ipo, row, today=date(2026, 9, 24)) == 4
    db.commit()
    obs = db.scalars(select(FeatureObservation).where(FeatureObservation.ipo_id == ipo.id)).all()
    assert len(obs) == 4 and {o.event_stage for o in obs} == {"subscription_day_1"}
    assert all(o.available_at == "2026-09-24" and o.period_end == "2026-09-24" and o.availability_rule == "nse_live_feed" and o.source_tier == 1 and o.unit == "x" for o in obs)
    # same day, higher figure: updated in place, no new row
    assert record_subscription_observations(db, ipo, {**row, "total_sub": 2.4}, today=date(2026, 9, 24)) == 1
    db.commit()
    assert db.scalar(select(FeatureObservation.value).where(FeatureObservation.ipo_id == ipo.id, FeatureObservation.field_name == "total_sub")) == 2.4
    assert len(db.scalars(select(FeatureObservation).where(FeatureObservation.ipo_id == ipo.id)).all()) == 4
    # next business day: a new set of rows carrying Day 2, Day 1 rows untouched
    assert record_subscription_observations(db, ipo, {**row, "total_sub": 5.0}, today=date(2026, 9, 25)) == 4
    db.commit()
    days = sorted({(o.period_end, o.event_stage) for o in db.scalars(select(FeatureObservation).where(FeatureObservation.ipo_id == ipo.id)).all()})
    assert days == [("2026-09-24", "subscription_day_1"), ("2026-09-25", "subscription_day_2")]


def test_ingest_nse_writes_observations_for_open_issues(db, monkeypatch):
    monkeypatch.setattr(nse, "fetch_current", lambda: ([
        {"company": "Shah Investments", "symbol": "SHAHINVEST", "isin": "", "country": "India", "exchange": "NSE/BSE", "board": "Mainboard",
         "status": "Open", "sector": "Unknown", "currency": "INR", "open_date": "24-Sep-2026", "close_date": "28-Sep-2026",
         "total_sub": 1.9, "qib_sub": 0.388, "nii_sub": 1.75, "retail_sub": 3.2, "subscription_source_url": nse.detail_url("SHAHINVEST"), "raw": {}},
        {"company": "Next Issue", "symbol": "NEXT", "isin": "", "country": "India", "exchange": "NSE/BSE", "board": "Mainboard",
         "status": "Upcoming", "sector": "Unknown", "currency": "INR", "raw": {}},
    ], []))
    run = ingest_nse(db)
    assert run.status == "ok" and run.metadata_json["subscription_observations"] == 4
    ipo = db.scalar(select(IPO).where(IPO.symbol == "SHAHINVEST"))
    assert ipo.open_date == "2026-09-24" and ipo.qib_sub == 0.388
    obs = db.scalars(select(FeatureObservation).where(FeatureObservation.ipo_id == ipo.id, FeatureObservation.source_name == NSE_LIVE_SOURCE)).all()
    assert {o.field_name for o in obs} == {"qib_sub", "nii_sub", "retail_sub", "total_sub"}
    assert db.scalar(select(FeatureObservation).join(IPO, IPO.id == FeatureObservation.ipo_id).where(IPO.symbol == "NEXT")) is None
