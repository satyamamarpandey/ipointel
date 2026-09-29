"""services.performance: explicit market-data attempt state per Listed row."""
from datetime import datetime, timezone, timedelta
import pytest
from app.models import IPO, PerformanceSnapshot
from app.services import performance, market

DAY = 86400


def _ipo(db, **kw):
    base = dict(external_key=f"US:{kw.get('symbol','X')}", company="Test Co", country="United States", status="Listed",
                symbol="TST", listing_date="2024-03-01", final_price=10.0)
    base.update(kw)
    ipo = IPO(**base)
    db.add(ipo)
    db.commit()
    return ipo


def _bars(start_iso: str, closes: list[float]):
    t0 = datetime.fromisoformat(start_iso).replace(tzinfo=timezone.utc).timestamp()
    return [{"ts": t0 + i * DAY, "open": c, "close": c} for i, c in enumerate(closes)]


@pytest.fixture
def no_bench(monkeypatch):
    monkeypatch.setattr(market, "fetch_benchmark_history", lambda country: None)


def test_ok_writes_snapshot_with_extended_windows(db, no_bench, monkeypatch):
    ipo = _ipo(db)
    bars = _bars("2024-03-01", [12.0] * 8 + [14.0] * 100)
    monkeypatch.setattr(market, "fetch_yahoo_history", lambda sym, country, raw_symbol=False, **k: {"url": f"u/{sym}", "prices": bars, "splits": []})
    status = performance.refresh_one(db, ipo, {})
    db.commit()
    assert status == "ok"
    assert ipo.market_data_status == "ok" and ipo.market_data_checked_at is not None
    snap = db.query(PerformanceSnapshot).filter_by(ipo_id=ipo.id).one()
    assert snap.listing_return_pct == pytest.approx(20.0)
    assert snap.listing_open_return_pct == pytest.approx(20.0)
    assert snap.return_7d_pct == pytest.approx(20.0)
    assert snap.return_1m_pct == pytest.approx(40.0)
    assert snap.return_90d_pct == pytest.approx(40.0)
    assert snap.return_12m_pct is None  # window not elapsed: null, never zero
    assert snap.listing_date_used == "2024-03-01"
    assert "Yahoo" in snap.source_name


def test_no_series_marks_row_and_writes_nothing(db, no_bench, monkeypatch):
    ipo = _ipo(db, symbol="GONE")
    monkeypatch.setattr(market, "fetch_yahoo_history", lambda *a, **k: {"url": "u", "prices": [], "splits": []})
    assert performance.refresh_one(db, ipo, {}) == "no_series"
    db.commit()
    assert ipo.market_data_status == "no_series"
    assert db.query(PerformanceSnapshot).count() == 0


def test_no_listing_bar_when_history_starts_late(db, no_bench, monkeypatch):
    ipo = _ipo(db, symbol="LATE", listing_date="2024-03-01")
    bars = _bars("2024-06-01", [5.0] * 40)  # first bar 3 months after the stated listing
    monkeypatch.setattr(market, "fetch_yahoo_history", lambda *a, **k: {"url": "u", "prices": bars, "splits": []})
    assert performance.refresh_one(db, ipo, {}) == "no_listing_bar"
    assert db.query(PerformanceSnapshot).count() == 0


def test_implausible_listing_return_is_suppressed_not_published(db, no_bench, monkeypatch):
    ipo = _ipo(db, symbol="UNIT", final_price=0.01)  # 0.01 vs 12.0 close: unit mismatch
    bars = _bars("2024-03-01", [12.0] * 40)
    monkeypatch.setattr(market, "fetch_yahoo_history", lambda *a, **k: {"url": "u", "prices": bars, "splits": []})
    assert performance.refresh_one(db, ipo, {}) == "implausible"
    db.commit()
    snap = db.query(PerformanceSnapshot).filter_by(ipo_id=ipo.id).one()
    assert snap.listing_return_pct is None
    assert snap.return_7d_pct == pytest.approx(0.0)  # vs listing close, a real observation
    assert "suppressed" in snap.source_name


def test_india_falls_back_from_ns_to_bo(db, no_bench, monkeypatch):
    ipo = _ipo(db, external_key="IN:smeco", country="India", symbol="SMECO", currency="INR")
    calls = []

    def fake(sym, country, raw_symbol=False, **k):
        calls.append(sym)
        if sym.endswith(".NS"):
            return {"url": "u/ns", "prices": [], "splits": []}
        return {"url": f"u/{sym}", "prices": _bars("2024-03-01", [11.0] * 40), "splits": []}

    monkeypatch.setattr(market, "fetch_yahoo_history", fake)
    assert performance.refresh_one(db, ipo, {}) == "ok"
    assert calls == ["SMECO.NS", "SMECO-SM.NS", "SMECO.BO"]
    db.commit()
    assert db.query(PerformanceSnapshot).one().source_url == "u/SMECO.BO"


def test_candidate_symbols():
    assert performance.candidate_symbols(IPO(country="India", symbol="ABC", board="Mainboard")) == ["ABC.NS", "ABC-SM.NS", "ABC.BO"]
    assert performance.candidate_symbols(IPO(country="India", symbol="ABC", board="SME")) == ["ABC-SM.NS", "ABC.NS", "ABC.BO"]
    assert performance.candidate_symbols(IPO(country="India", symbol="ABC.BO")) == ["ABC.BO"]
    assert performance.candidate_symbols(IPO(country="United States", symbol="HYACU")) == ["HYACU", "HYAC-UN"]
    assert performance.candidate_symbols(IPO(country="United States", symbol="RDDT")) == ["RDDT"]
    assert performance.candidate_symbols(IPO(country="United States", symbol="")) == []


def test_benchmark_relative_12m(db, monkeypatch):
    ipo = _ipo(db, symbol="REL")
    bars = _bars("2024-03-01", [10.0] + [15.0] * 400)  # +50% over 12m vs offer price 10
    monkeypatch.setattr(market, "fetch_yahoo_history", lambda *a, **k: {"url": "u", "prices": bars, "splits": []})
    monkeypatch.setattr(market, "fetch_benchmark_history", lambda country: {"prices": _bars("2024-03-01", [100.0] + [110.0] * 400)})
    assert performance.refresh_one(db, ipo, {}) == "ok"
    db.commit()
    snap = db.query(PerformanceSnapshot).one()
    assert snap.return_12m_pct == pytest.approx(50.0)
    assert snap.benchmark_return_pct == pytest.approx(10.0)
    assert snap.benchmark_relative_12m_pct == pytest.approx(40.0)
    assert snap.return_24m_pct is None


def test_refresh_many_orders_never_attempted_first_and_respects_retry(db, no_bench, monkeypatch):
    _ipo(db, symbol="NEW1")
    _ipo(db, symbol="OLDF", market_data_status="no_series",
         market_data_checked_at=datetime.now(timezone.utc) - timedelta(days=90))
    recent_fail = _ipo(db, symbol="RECF", market_data_status="no_series",
                       market_data_checked_at=datetime.now(timezone.utc) - timedelta(days=2))
    already_ok = _ipo(db, symbol="OKAY", market_data_status="ok")
    db.add(PerformanceSnapshot(ipo_id=already_ok.id, as_of_date="2024-01-01", listing_return_pct=1.0))
    db.commit()
    seen = []

    def fake(sym, country, raw_symbol=False, **k):
        seen.append(sym)
        return {"url": "u", "prices": _bars("2024-03-01", [11.0] * 40), "splits": []}

    monkeypatch.setattr(market, "fetch_yahoo_history", fake)
    order = [r.symbol for r in performance.select_candidates(db, None, 10, 30, only_missing=True)]
    assert order == ["NEW1", "OLDF"]  # never-attempted first, then retry-eligible failure; ok rows skipped when only_missing
    counts = performance.refresh_many(db, limit=10, retry_after_days=30, only_missing=True, sleep=lambda s: None)
    assert counts["attempted"] == 2 and counts["ok"] == 2
    assert seen == ["NEW1", "OLDF"]
    db.refresh(recent_fail)
    assert recent_fail.market_data_status == "no_series"
    stale = [r.symbol for r in performance.select_candidates(db, None, 10, 30, only_missing=False)]
    assert "OKAY" in stale  # snapshot older than 7 days: coverage refresh when stale rows are included


def test_refresh_many_limit_is_honoured(db, no_bench, monkeypatch):
    for i in range(5):
        _ipo(db, symbol=f"S{i}")
    monkeypatch.setattr(market, "fetch_yahoo_history", lambda *a, **k: {"url": "u", "prices": _bars("2024-03-01", [11.0] * 40), "splits": []})
    counts = performance.refresh_many(db, limit=3, sleep=lambda s: None)
    assert counts["attempted"] == 3
    assert db.query(PerformanceSnapshot).count() == 3


def test_windowed_returns_has_90d_window():
    bars = _bars("2024-03-01", [10.0] * 200)
    wr = market.windowed_returns(bars, datetime(2024, 3, 1, tzinfo=timezone.utc), issue_price=8.0)
    assert wr["return_90d_pct"] == pytest.approx(25.0)
