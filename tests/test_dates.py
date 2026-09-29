"""Phase 11: canonical YYYY-MM-DD storage for every date column."""
from __future__ import annotations
import pytest
from sqlalchemy import select

from app.models import IPO
from app.services.identity import normalize_date
from app.services.market import parse_date
from app.services.pipeline import repair_dates, upsert_ipo


@pytest.mark.parametrize("raw,iso", [
    ("21-Oct-2022", "2022-10-21"),
    ("20251022", "2025-10-22"),
    ("2026-07-01 00:00:00", "2026-07-01"),
    ("07-Jul-26", "2026-07-07"),
    ("2024-02-23", "2024-02-23"),
    ("15/03/2022", "2022-03-15"),
    ("", ""),
    (None, ""),
    ("not a date", ""),
])
def test_normalize_date(raw, iso):
    assert normalize_date(raw) == iso
    if iso:
        assert parse_date(iso).date().isoformat() == iso  # the canonical form round-trips through the shared parser


def test_upsert_stores_canonical_dates_and_keeps_raw(db):
    row = {"company": "Date Co Limited", "symbol": "DATECO", "country": "India", "status": "Listed",
           "listing_date": "21-Oct-2022", "open_date": "13-Oct-2022", "close_date": "17/10/2022", "raw": {"listing_date": "21-Oct-2022"}}
    upsert_ipo(db, row, "NSE Primary Market Report", "https://www.nseindia.com/x.xlsx", 1)
    db.commit()
    ipo = db.scalar(select(IPO).where(IPO.symbol == "DATECO"))
    assert (ipo.listing_date, ipo.open_date, ipo.close_date) == ("2022-10-21", "2022-10-13", "2022-10-17")
    assert ipo.raw["listing_date"] == "21-Oct-2022"


def test_repair_dates_is_idempotent_and_flags_unparseable(db):
    good = IPO(external_key="IN:a", company="A", country="India", status="Listed", listing_date="30-Nov-2022", open_date="2022-11-22 00:00:00")
    us = IPO(external_key="US:1", company="B", country="United States", status="Listed", listing_date="20251022", filing_date="2025-10-01")
    bad = IPO(external_key="IN:c", company="C", country="India", status="Listed", listing_date="TBA")
    db.add_all([good, us, bad])
    db.commit()
    assert repair_dates(db) == 3
    db.commit()
    for x in (good, us, bad):
        db.refresh(x)
    assert good.listing_date == "2022-11-30" and good.open_date == "2022-11-22"
    assert us.listing_date == "2025-10-22" and us.filing_date == "2025-10-01"
    assert bad.listing_date == "TBA"  # never invented
    assert bad.data_flags == ["date_unparseable: listing_date=TBA"]
    assert repair_dates(db) == 0
    db.refresh(bad)
    assert bad.data_flags == ["date_unparseable: listing_date=TBA"]  # flag not duplicated


def test_canonical_dates_sort_lexically(db):
    for i, d in enumerate(["21-Oct-2022", "20230105", "2021-03-09 00:00:00"]):
        db.add(IPO(external_key=f"IN:{i}", company=f"C{i}", country="India", status="Listed", listing_date=d))
    db.commit()
    repair_dates(db)
    db.commit()
    dates = db.scalars(select(IPO.listing_date).order_by(IPO.listing_date)).all()
    assert dates == ["2021-03-09", "2022-10-21", "2023-01-05"]
