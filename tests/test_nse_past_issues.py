"""NSE past issues (official final price / listing date) and the price-band parser."""
from app.models import IPO
from app.services import nse, performance
from app.services.pipeline import apply_nse_past_issues, repair_nse_price_bands, PAST_ISSUE_PRICE_CONFLICT_FLAG


def test_band_parser_book_built_and_fixed():
    assert nse._band("Rs.78 to Rs.82") == (78.0, 82.0)
    assert nse._band("Rs.95") == (95.0, 95.0)
    assert nse._band("-") == (None, None)
    x = nse.normalize({"companyName": "A Ltd", "symbol": "A", "issuePrice": "Rs.78 to Rs.82", "series": "EQ"})
    assert x["price_low"] == 78.0 and x["price_high"] == 82.0 and "final_price" not in x
    y = nse.normalize({"companyName": "B Ltd", "symbol": "B", "issuePrice": "Rs.95", "series": "SME"})
    assert y["final_price"] == 95.0


def test_normalize_past_issue_skips_debt_and_parses_prices():
    d = {"company": "Lumino Industries Limited", "ipoEndDate": "31-AUG-2026", "ipoStartDate": "27-AUG-2026",
         "issuePrice": "    82", "listingDate": "03-SEP-2026", "priceRange": "Rs.78 to Rs.82", "securityType": "EQ", "symbol": "LUMINO"}
    x = nse.normalize_past_issue(d)
    assert x["final_price"] == 82.0 and x["listing_date"] == "2026-09-03" and x["close_date"] == "2026-08-31"
    assert nse.normalize_past_issue({**d, "securityType": "DEBT"}) is None
    assert nse.normalize_past_issue({**d, "issuePrice": "-"})["final_price"] is None


def _item(**kw):
    base = {"symbol": "LUMINO", "company": "Lumino Industries Limited", "close_date": "2026-08-31", "open_date": "2026-08-27",
            "listing_date": "2026-09-03", "final_price": 82.0, "price_low": 78.0, "price_high": 82.0, "security_type": "EQ"}
    return {**base, **kw}


def _ipo(db, **kw):
    base = dict(external_key="IN:LUMINO", company="Lumino Industries Limited", country="India", status="Closed", symbol="LUMINO",
                close_date="2026-08-31", board="Mainboard")
    ipo = IPO(**{**base, **kw})
    db.add(ipo)
    db.commit()
    return ipo


def test_fills_price_listing_date_and_status(db):
    ipo = _ipo(db)
    s = apply_nse_past_issues(db, [_item()], today="2026-09-30")
    assert s["final_price_filled"] == 1 and s["listed"] == 1
    assert ipo.final_price == 82.0 and ipo.listing_date == "2026-09-03" and ipo.status == "Listed"


def test_symbol_reuse_needs_matching_close_date(db):
    ipo = _ipo(db, close_date="2019-05-01")
    s = apply_nse_past_issues(db, [_item()], today="2026-09-30")
    assert s["matched"] == 0 and ipo.final_price is None


def test_price_outside_band_rejected_and_conflict_never_overwrites(db):
    ipo = _ipo(db)
    assert apply_nse_past_issues(db, [_item(final_price=500.0, listing_date="")], today="2026-09-30")["outside_band"] == 1
    assert ipo.final_price is None
    ipo.final_price = 60.0
    db.commit()
    # Not yet listed: no market recheck, so the stored price must survive.
    s = apply_nse_past_issues(db, [_item(listing_date="")], today="2026-09-30")
    assert s["price_conflict"] == 1 and ipo.final_price == 60.0
    assert ipo.raw[performance.PAST_ISSUE_PRICE_KEY] == 82.0
    assert any(str(f).startswith(PAST_ISSUE_PRICE_CONFLICT_FLAG) for f in ipo.data_flags)


def test_not_ipo_rows_are_never_matched(db):
    ipo = _ipo(db, status="Not IPO", close_date="")
    assert apply_nse_past_issues(db, [_item()], today="2026-09-30")["matched"] == 0
    assert ipo.final_price is None


def test_offer_price_check_prefers_official_price_only_when_open_confirms():
    ipo = IPO(company="S D Retail", country="India", board="SME", final_price=62.0, raw={performance.PAST_ISSUE_PRICE_KEY: 131.0})
    verdict, price, ev = performance.offer_price_check(ipo, 145.0)
    assert verdict == "corrected" and price == 131.0 and ev.startswith(performance.PAST_ISSUE_EVIDENCE)
    # the open sits closer to the stored price: keep it
    assert performance.offer_price_check(ipo, 64.0)[0] == "ok"


def test_repair_price_bands_from_stored_payload(db):
    ipo = _ipo(db, price_high=78.0, raw={"issuePrice": "Rs.78 to Rs.82"})
    fixed = _ipo(db, external_key="IN:FX", symbol="FX", company="Fixed Ltd", raw={"issuePrice": "Rs.95"})
    assert repair_nse_price_bands(db) == 2
    assert (ipo.price_low, ipo.price_high, ipo.final_price) == (78.0, 82.0, None)
    assert fixed.final_price == 95.0
    assert repair_nse_price_bands(db) == 0
