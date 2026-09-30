"""scripts/backfill_india_rhp.py outcome labels, flags and resumability."""
import httpx

from app.models import IPO, FeatureObservation
from scripts import backfill_india_rhp as br
from tests.test_rhp_financials import FISCAL_MILLION


def _ipo(db, symbol, **kw):
    ipo = IPO(external_key=f"IN:{symbol}", company=f"{symbol} Ltd", country="India", status="Listed", symbol=symbol,
              open_date="2026-08-27", listing_date="2026-09-03", **kw)
    db.add(ipo)
    db.commit()
    return ipo


def _not_found(symbol):
    req = httpx.Request("GET", f"https://example.test/{symbol}")
    raise httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, request=req))


def test_outcomes_flags_and_no_retry(db):
    parsed, empty, missing, broken = (_ipo(db, s) for s in ("GOOD", "EMPTY", "GONE", "BROKEN"))
    pages = {"GOOD": [FISCAL_MILLION], "EMPTY": ["no tables here"]}

    def get_pages(symbol):
        if symbol == "GONE":
            _not_found(symbol)
        if symbol == "BROKEN":
            raise ValueError("bad zip")
        return pages[symbol]

    counts = br.run(db, limit=10, max_minutes=5, log=lambda *_: None, get_pages=get_pages)
    assert counts == {"parsed": 1, "not_extracted": 1, "no_archive": 1, "unreadable": 1}
    assert parsed.revenue_m == 4448.78
    assert db.query(FeatureObservation).filter_by(ipo_id=parsed.id).count() == 4
    assert db.query(FeatureObservation).filter_by(ipo_id=empty.id).count() == 0
    assert any(str(f).startswith(br.FLAG_PARSED) for f in parsed.data_flags)
    assert any("no RHP archive" in str(f) for f in missing.data_flags)
    assert any("unreadable" in str(f) for f in broken.data_flags)
    assert br.candidates(db, 10) == []  # every row flagged: nothing retried daily


def test_network_errors_are_retried_next_run(db):
    ipo = _ipo(db, "FLAKY")

    def get_pages(symbol):
        raise httpx.ConnectTimeout("timeout")

    counts = br.run(db, limit=10, max_minutes=5, log=lambda *_: None, get_pages=get_pages)
    assert counts == {"error:ConnectTimeout": 1}
    assert br.candidates(db, 10) == [ipo]


def test_only_india_rows_with_a_symbol_are_candidates(db):
    _ipo(db, "OK")
    db.add(IPO(external_key="IN:NOSYM", company="No Symbol Ltd", country="India", status="Listed", symbol=""))
    db.add(IPO(external_key="US:X", company="X Inc", country="United States", status="Listed", symbol="X"))
    db.add(IPO(external_key="IN:WD", company="Withdrawn Ltd", country="India", status="Withdrawn", symbol="WD"))
    db.flush()
    assert [i.symbol for i in br.candidates(db, 10)] == ["OK"]


def test_upcoming_issue_without_an_archive_is_retried(db):
    ipo = _ipo(db, "SOON")
    ipo.status = "Upcoming"
    db.commit()
    counts = br.run(db, limit=10, max_minutes=5, log=lambda *_: None, get_pages=_not_found)
    assert counts == {"not_yet_published": 1}
    assert br.candidates(db, 10) == [ipo]
