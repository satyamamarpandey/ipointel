"""Market regime at listing: strictly pre-listing index data only."""
from datetime import datetime, timedelta, timezone
from app.models import IPO, FeatureObservation
from app.services import market_regime as mr
from app.services import model_eval as me


def _bars(start: str, days: int, step: float = 1.0) -> list[dict]:
    d0 = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    out, px = [], 100.0
    for i in range(days):
        d = d0 + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        px *= 1 + (0.001 * step if i % 2 else -0.0005 * step)
        out.append({"ts": d.timestamp(), "close": px})
    return out


def test_regime_uses_only_closes_before_listing():
    bars = _bars("2024-01-01", 200)
    r = mr.regime_at(bars, "2024-05-15")
    assert r["as_of"] < "2024-05-15"
    # A crash on and after listing day must not change the value.
    shocked = [b if b["ts"] < datetime(2024, 5, 15, tzinfo=timezone.utc).timestamp() else {**b, "close": b["close"] * 0.5} for b in bars]
    assert mr.regime_at(shocked, "2024-05-15") == r
    assert r["market_vol_20d_pct"] > 0


def test_regime_requires_covering_fresh_history():
    bars = _bars("2024-01-01", 30)
    assert mr.regime_at(bars, "2024-01-10") is None       # too few closes
    assert mr.regime_at(bars, "2024-06-01") is None       # last close months stale
    assert mr.regime_at(bars, "") is None


def test_refresh_writes_observations_once_and_skips_future_listings(db):
    past = IPO(external_key="IN:A", company="A", country="India", status="Listed", listing_date="2024-05-15")
    future = IPO(external_key="IN:B", company="B", country="India", status="Upcoming", listing_date="2099-01-01")
    db.add_all([past, future])
    db.commit()
    calls = []

    def fetch(country):
        calls.append(country)
        return {"url": "https://example.test/nsei", "prices": _bars("2024-01-01", 200)}

    c = mr.refresh(db, fetch=fetch, countries=("India",))
    db.commit()
    assert c["India"]["written"] == 1
    obs = db.query(FeatureObservation).filter_by(ipo_id=past.id).all()
    assert {o.field_name for o in obs} == set(mr.FIELDS)
    assert all(o.availability_rule == mr.RULE_INDEX_CLOSE and o.available_at < "2024-05-15" and o.source_tier == 3 for o in obs)
    assert db.query(FeatureObservation).filter_by(ipo_id=future.id).count() == 0
    mr.refresh(db, fetch=fetch, countries=("India",))
    assert db.query(FeatureObservation).filter_by(ipo_id=past.id).count() == 2


def test_refresh_survives_fetch_failure(db):
    db.add(IPO(external_key="US:Z", company="Z", country="United States", status="Listed", listing_date="2024-05-15"))
    db.commit()

    def boom(country):
        raise RuntimeError("down")

    assert mr.refresh(db, fetch=boom, countries=("United States",))["United States"]["error"] == "RuntimeError"


def test_trailing_ipo_count_excludes_listing_day_and_later():
    dates = ["2024-01-01", "2024-03-01", "2024-03-31", "2024-04-01", "2024-05-01"]
    assert mr.trailing_ipo_count(dates, "2024-04-01") == 2  # 2024-01-01 is outside 90 days


def test_index_close_rule_is_point_in_time():
    assert mr.RULE_INDEX_CLOSE in me.POINT_IN_TIME_RULES
    assert set(me.REGIME_FEATURES) >= set(mr.FIELDS)
