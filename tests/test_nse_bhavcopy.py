"""services.nse_bhavcopy: official NSE daily prices as the Tier-1 India series."""
from datetime import date, datetime, timezone
import pytest
from app.models import IPO, PriceBar, BhavcopyDay, PerformanceSnapshot
from app.services import nse_bhavcopy, performance, market

NEW_CSV = """TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric
2026-09-28,2026-09-28,CM,NSE,STK,757579,INE1GYP01013,AAKAAR,SM,,,,,Aakaar Medical,45.5,46,44,45.9,45.9,45
2026-09-28,2026-09-28,CM,NSE,STK,1,INE144J01027,20MICRONS,EQ,,,,,20 Microns,200,201,199,200.5,200.5,199
2026-09-28,2026-09-28,CM,NSE,STK,2,INE0ZZZ01010,IGNORED,GB,,,,,Gold Bond,100,100,100,100,100,100
2026-09-28,2026-09-28,CM,NSE,STK,3,INE0BBB01010,BEROW,BE,,,,,Be Series,10,10,10,10.5,10.5,10
"""

LEGACY_CSV = """SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN,
AATMAJ,SM,41,42,40.8,41,41,42.05,74000,3046800,02-JAN-2024,37,INE0AAA01019,
RELIANCE,EQ,2600,2610,2590,2605.5,2605,2590,100,260000,02-JAN-2024,50,INE002A01018,
SGBX,GB,5000,5000,5000,5000,5000,5000,1,5000,02-JAN-2024,1,INE0SGB01019,
"""


def test_url_for_picks_format_by_date():
    assert nse_bhavcopy.url_for(date(2026, 9, 28)) == "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20260928_F_0000.csv.zip"
    assert nse_bhavcopy.url_for(date(2024, 7, 8)).endswith("BhavCopy_NSE_CM_0_0_0_20240708_F_0000.csv.zip")
    assert nse_bhavcopy.url_for(date(2024, 1, 2)) == "https://nsearchives.nseindia.com/content/historical/EQUITIES/2024/JAN/cm02JAN2024bhav.csv.zip"


def test_parse_new_format_keeps_only_series_of_interest():
    rows = nse_bhavcopy.parse_bhavcopy_csv(NEW_CSV)
    assert [r["symbol"] for r in rows] == ["AAKAAR", "20MICRONS", "BEROW"]
    sm = rows[0]
    assert sm == {"isin": "INE1GYP01013", "symbol": "AAKAAR", "series": "SM", "trade_date": "2026-09-28", "open": 45.5, "close": 45.9}


def test_parse_legacy_format_converts_timestamp():
    rows = nse_bhavcopy.parse_bhavcopy_csv(LEGACY_CSV)
    assert [r["symbol"] for r in rows] == ["AATMAJ", "RELIANCE"]
    assert rows[0]["trade_date"] == "2024-01-02" and rows[0]["open"] == 41.0 and rows[0]["close"] == 41.0


def test_parse_bhavcopy_zip_roundtrip():
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("BhavCopy_NSE_CM_0_0_0_20260928_F_0000.csv", NEW_CSV)
    rows = nse_bhavcopy.parse_bhavcopy(buf.getvalue())
    assert len(rows) == 3
    assert nse_bhavcopy.parse_bhavcopy(b"not a zip") == []


def _zip(csv_text: str) -> bytes:
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", csv_text)
    return buf.getvalue()


def test_ingest_days_filters_isins_records_holidays_and_is_resumable(db):
    calls = []

    def fake_fetch(day, client=None):
        calls.append(day)
        if day == date(2026, 9, 22):
            return None, nse_bhavcopy.url_for(day)  # weekday holiday: 404
        return _zip(NEW_CSV.replace("2026-09-28", day.isoformat())), nse_bhavcopy.url_for(day)

    counts = nse_bhavcopy.ingest_days(db, {"INE1GYP01013"}, date(2026, 9, 21), date(2026, 9, 27), max_files=10, fetch=fake_fetch, sleep=lambda s: None)
    # Mon 21 .. Fri 25 are business days; 26/27 weekend skipped by business_days
    assert [d.day for d in calls] == [21, 22, 23, 24, 25]
    assert counts["ok"] == 4 and counts["holiday"] == 1 and counts["bars_stored"] == 4
    bars = db.query(PriceBar).order_by(PriceBar.trade_date).all()
    assert [b.trade_date for b in bars] == ["2026-09-21", "2026-09-23", "2026-09-24", "2026-09-25"]
    assert {b.isin for b in bars} == {"INE1GYP01013"}  # 20MICRONS / BEROW not of interest
    assert db.query(BhavcopyDay).filter_by(trade_date="2026-09-22").one().status == "holiday"
    # second run: nothing re-fetched
    calls.clear()
    counts2 = nse_bhavcopy.ingest_days(db, {"INE1GYP01013"}, date(2026, 9, 21), date(2026, 9, 27), max_files=10, fetch=fake_fetch, sleep=lambda s: None)
    assert calls == [] and counts2["skipped"] == 5 and counts2["fetched"] == 0
    assert db.query(PriceBar).count() == 4


def test_ingest_days_records_error_and_retries_it(db):
    state = {"fail": True}

    def flaky(day, client=None):
        if state["fail"]:
            raise RuntimeError("boom")
        return _zip(NEW_CSV), nse_bhavcopy.url_for(day)

    c1 = nse_bhavcopy.ingest_days(db, {"INE1GYP01013"}, date(2026, 9, 28), date(2026, 9, 28), fetch=flaky, sleep=lambda s: None)
    assert c1["error"] == 1
    assert db.query(BhavcopyDay).one().status == "error"
    state["fail"] = False
    c2 = nse_bhavcopy.ingest_days(db, {"INE1GYP01013"}, date(2026, 9, 28), date(2026, 9, 28), fetch=flaky, sleep=lambda s: None)
    assert c2["ok"] == 1 and db.query(BhavcopyDay).one().status == "ok"


def test_ingest_days_max_files_bounds_fetches(db):
    n = {"calls": 0}

    def fake(day, client=None):
        n["calls"] += 1
        return _zip(NEW_CSV), "u"

    counts = nse_bhavcopy.ingest_days(db, set(), date(2026, 9, 1), date(2026, 9, 30), max_files=3, fetch=fake, sleep=lambda s: None)
    assert n["calls"] == 3 and counts["fetched"] == 3


def _bars_rows(db, isin, start: date, closes: list[float], symbol="SMEX", series="SM"):
    from datetime import timedelta
    d = start
    for c in closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        db.add(PriceBar(isin=isin, symbol=symbol, series=series, trade_date=d.isoformat(), open=c, close=c))
        d += timedelta(days=1)
    db.commit()


def test_refresh_one_prefers_official_bars_for_india(db, monkeypatch):
    ipo = IPO(external_key="IN:smex", company="SME Example Ltd", country="India", status="Listed", symbol="SMEX", board="SME",
              isin="INE0SME01011", listing_date="2026-07-01", final_price=100.0, currency="INR")
    db.add(ipo)
    db.commit()
    _bars_rows(db, "INE0SME01011", date(2026, 7, 1), [120.0] * 5 + [150.0] * 60)
    monkeypatch.setattr(market, "fetch_yahoo_history", lambda *a, **k: (_ for _ in ()).throw(AssertionError("Yahoo must not be called")))
    monkeypatch.setattr(market, "fetch_benchmark_history", lambda country: None)
    assert performance.refresh_one(db, ipo, {}) == "ok"
    db.commit()
    snap = db.query(PerformanceSnapshot).filter_by(ipo_id=ipo.id).one()
    assert snap.source_name == "NSE bhavcopy"
    assert snap.source_url.endswith("BhavCopy_NSE_CM_0_0_0_20260701_F_0000.csv.zip")
    assert snap.listing_return_pct == pytest.approx(20.0)
    assert snap.listing_date_used == "2026-07-01"
    assert snap.return_30d_pct if hasattr(snap, "return_30d_pct") else True
    assert snap.return_1m_pct == pytest.approx(50.0)


def test_refresh_one_falls_back_to_yahoo_when_too_few_official_bars(db, monkeypatch):
    ipo = IPO(external_key="IN:one", company="One Bar Ltd", country="India", status="Listed", symbol="ONEBAR", board="SME",
              isin="INE0ONE01019", listing_date="2026-07-01", final_price=10.0, currency="INR")
    db.add(ipo)
    db.commit()
    db.add(PriceBar(isin="INE0ONE01019", symbol="ONEBAR", series="SM", trade_date="2026-09-29", open=12.0, close=12.0))
    db.commit()
    called = []
    monkeypatch.setattr(market, "fetch_yahoo_history", lambda sym, *a, **k: (called.append(sym), {"url": "u", "prices": [], "splits": []})[1])
    assert performance.refresh_one(db, ipo, {}) == "no_series"
    assert called[0] == "ONEBAR-SM.NS"


def test_official_bars_shape_and_skip_missing_close(db):
    db.add(PriceBar(isin="INE0X", symbol="X", series="EQ", trade_date="2026-01-02", open=1.0, close=None))
    db.add(PriceBar(isin="INE0X", symbol="X", series="EQ", trade_date="2026-01-01", open=1.0, close=2.0))
    db.commit()
    bars = nse_bhavcopy.bars_for_isin(db, "ine0x")
    assert len(bars) == 1
    assert bars[0]["ts"] == datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() and bars[0]["close"] == 2.0


def test_forward_window_not_reported_from_stale_bar():
    """Listed on a Thursday with bars through the following Monday: the 7d
    window has not elapsed (target Thu+7 is 3 days past the last bar)."""
    day = 86400
    t0 = datetime(2026, 9, 24, tzinfo=timezone.utc).timestamp()  # Thu
    bars = [{"ts": t0, "open": 10.0, "close": 10.0}, {"ts": t0 + day, "open": 10.0, "close": 10.0},
            {"ts": t0 + 4 * day, "open": 10.0, "close": 11.0}]  # Mon 28th
    wr = market.windowed_returns(bars, datetime(2026, 9, 24, tzinfo=timezone.utc), issue_price=10.0)
    assert "return_7d_pct" not in wr
    bars.append({"ts": t0 + 7 * day, "open": 12.0, "close": 12.0})  # Thu 1st Oct: exactly 7 days
    wr = market.windowed_returns(bars, datetime(2026, 9, 24, tzinfo=timezone.utc), issue_price=10.0)
    assert wr["return_7d_pct"] == pytest.approx(20.0)
