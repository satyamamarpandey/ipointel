"""Live India issues that have listed: promotion from NSE masters and
bhavcopy re-read for ISINs learned after their listing day was ingested."""
from datetime import date
from app.models import IPO, PriceBar, BhavcopyDay
from app.services import nse_bhavcopy
from app.services.nse_master import MasterIndex, MasterRow
from app.services.pipeline import resolve_india_symbols
from tests.test_nse_bhavcopy import NEW_CSV, _zip

URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"


def _row(symbol="AAKAAR", name="Aakaar Medical Technologies Limited", listed="26-SEP-2026"):
    return MasterRow(symbol=symbol, name=name, isin="INE1GYP01013", series="SM", board="SME",
                     date_of_listing=listed, source_name="NSE SME equity master", source_url=URL)


def test_listing_for_live_symbol_rules():
    idx = MasterIndex([_row()])
    m, listed, _ = idx.listing_for_live_symbol("AAKAAR", "Aakaar Medical Technologies Ltd", "2026-09-23")
    assert m is not None and listed == "2026-09-26"
    assert idx.listing_for_live_symbol("AAKAAR", "Some Other Company Limited", "2026-09-23")[0] is None
    assert idx.listing_for_live_symbol("AAKAAR", "Aakaar Medical Technologies Ltd", "2026-06-01")[0] is None  # old listing of a reused symbol
    assert idx.listing_for_live_symbol("AAKAAR", "Aakaar Medical Technologies Ltd", "2026-09-29")[0] is None  # listed before close
    assert idx.listing_for_live_symbol("NOPE", "Aakaar Medical Technologies Ltd", "2026-09-23")[0] is None


def test_closed_issue_promoted_to_listed_with_provenance(db):
    ipo = IPO(external_key="IN:AAKAAR", company="Aakaar Medical Technologies Limited", country="India", status="Closed",
              symbol="AAKAAR", close_date="2026-09-23", board="SME")
    db.add(ipo)
    db.commit()
    stats = resolve_india_symbols(db, masters=[_row()])
    db.commit()
    assert stats["LISTED_FROM_MASTER"] == 1
    assert ipo.status == "Listed" and ipo.listing_date == "2026-09-26" and ipo.isin == "INE1GYP01013"


def test_new_listing_rereads_days_already_ingested(db):
    db.add(IPO(external_key="IN:A2", company="Aakaar", country="India", status="Listed", symbol="AAKAAR",
               isin="INE1GYP01013", listing_date="2026-09-28", board="SME"))
    db.add(BhavcopyDay(trade_date="2026-09-28", status="ok", rows_stored=0))  # ingested before the ISIN was known
    db.commit()

    def fake(day, client=None):
        return _zip(NEW_CSV.replace("2026-09-28", day.isoformat())), nse_bhavcopy.url_for(day)

    # a plain ingest skips the already-recorded day
    assert nse_bhavcopy.ingest_days(db, {"INE1GYP01013"}, date(2026, 9, 28), date(2026, 9, 28), fetch=fake, sleep=lambda s: None)["skipped"] == 1
    c = nse_bhavcopy.backfill_new_listings(db, today=date(2026, 9, 29), fetch=fake, sleep=lambda s: None)
    assert c["isins"] == 1 and c["bars_stored"] == 2
    assert db.query(PriceBar).filter_by(isin="INE1GYP01013", trade_date="2026-09-28").count() == 1
    assert nse_bhavcopy.backfill_new_listings(db, today=date(2026, 9, 29), fetch=fake, sleep=lambda s: None) == {"isins": 0}


def test_listed_row_without_isin_gets_isin_from_master(db):
    ipo = IPO(external_key="IN:AAK2", company="Aakaar Medical Technologies Limited", country="India", status="Listed",
              symbol="AAKAAR", close_date="2026-09-23", listing_date="2026-09-26", board="SME")
    db.add(ipo)
    db.commit()
    stats = resolve_india_symbols(db, masters=[_row()])
    assert stats["LISTED_FROM_MASTER"] == 1
    assert ipo.isin == "INE1GYP01013" and ipo.listing_date == "2026-09-26" and ipo.status == "Listed"
