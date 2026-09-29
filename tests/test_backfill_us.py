"""Historical US 424B4 backfill: bounded, resumable, non-duplicating."""
from __future__ import annotations
from datetime import date

from sqlalchemy import select

from app.models import IPO, IngestionRun
from app.services import sec
from scripts.backfill_us_priced import backfill, SOURCE, window_start

IPO_TEXT = ("This is our initial public offering. The initial public offering price is $12.00 per share. "
            "Our common stock has been approved for listing on Nasdaq under the symbol BKFL.")
FOLLOW_ON_TEXT = ("We completed our initial public offering in 2019. Our common stock is listed on Nasdaq under the "
                  "symbol FOLO. On March 1, 2024, the last reported sale price of our common stock was $31.00. This "
                  "prospectus relates to the resale of shares by the selling stockholders at $30.00 per share.")


def _index(day, entries):
    return ({"424B4": [{"cik": cik, "company": name, "filing_date": day.strftime("%Y%m%d"), "form": "424B4",
                        "filing_url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{cik}-{day:%Y%m%d}.txt"} for cik, name in entries],
             "RW": []}, "https://www.sec.gov/idx")


def test_follow_on_prospectuses_are_not_ipos():
    assert sec.is_ipo_prospectus(IPO_TEXT)
    assert not sec.is_ipo_prospectus(FOLLOW_ON_TEXT)
    assert sec.parse_priced_ipo(FOLLOW_ON_TEXT) is None
    assert sec.is_ipo_prospectus("Prior to this offering, there has been no public market for our Class A common stock. initial public offering")


def test_backfill_is_bounded_resumable_and_skips_known_ciks(db, monkeypatch):
    fetched = []
    days_served = {
        date(2024, 3, 4): [("9000001", "Backfill Co"), ("9000002", "Follow On Inc")],
        date(2024, 3, 5): [("9000003", "Second Day Corp")],
        date(2024, 3, 6): [("9000001", "Backfill Co")],  # same issuer again: must not be fetched twice
    }
    monkeypatch.setattr(sec, "master_index_if_published", lambda day, ua: _index(day, days_served.get(day, [])))

    def fake_head(url, ua, max_bytes=0):
        fetched.append(url)
        return (FOLLOW_ON_TEXT if "9000002" in url else IPO_TEXT), False
    monkeypatch.setattr(sec, "filing_head", fake_head)

    run1 = backfill(db, days=2, max_minutes=5, start=date(2024, 3, 4), end=date(2024, 3, 6))
    assert run1.status == "ok" and run1.metadata_json["through"] == "2024-03-05"
    assert run1.metadata_json["stopped"] == "day budget reached"
    assert run1.rows_changed == 2 and len(fetched) == 3

    ipo = db.scalar(select(IPO).where(IPO.external_key == "US:9000001"))
    assert ipo is not None and ipo.status == "Listed" and ipo.final_price == 12.0 and ipo.symbol == "BKFL"
    assert ipo.listing_date == "2024-03-04"
    assert db.scalar(select(IPO).where(IPO.external_key == "US:9000002")) is None  # follow-on never stored

    # Second invocation resumes after 'through' and does not re-download the known issuer.
    run2 = backfill(db, days=5, max_minutes=5, end=date(2024, 3, 6))
    assert run2.metadata_json["from"] == "2024-03-06" and run2.metadata_json["through"] == "2024-03-06"
    assert run2.rows_changed == 0 and len(fetched) == 3
    assert db.scalar(select(IngestionRun).where(IngestionRun.source == SOURCE).order_by(IngestionRun.id.desc())).metadata_json["stopped"] == "completed"
    # Backfill never duplicates an issuer.
    assert len(db.scalars(select(IPO).where(IPO.external_key == "US:9000001")).all()) == 1


def test_unpublished_index_days_are_recorded_not_failures(db, monkeypatch):
    monkeypatch.setattr(sec, "master_index_if_published", lambda day, ua: (_ for _ in ()).throw(sec.DailyIndexUnavailable(str(day))))
    run = backfill(db, days=3, max_minutes=5, start=date(2024, 7, 4), end=date(2024, 7, 5))
    assert run.status == "ok" and run.error == ""
    assert run.metadata_json["index_not_published"] == ["2024-07-04", "2024-07-05"]


def test_window_start_handles_leap_day():
    assert window_start(date(2028, 2, 29)) == date(2023, 2, 28)
    assert window_start(date(2026, 9, 28)) == date(2021, 9, 28)


def test_classify_prospectus_three_way():
    assert sec.classify_prospectus(IPO_TEXT.lower()) == "ipo"
    assert sec.classify_prospectus(FOLLOW_ON_TEXT) == "follow_on"
    # Cover without the word "initial" but with an initial-listing application and the IPO dilution statement.
    caring = ("This is a firm commitment public offering of 1,000,000 shares of our Common Stock. We have applied to list "
              "our Common Stock on the Nasdaq Capital Market under the symbol CABR. Price to public per share $ 4.00. "
              "your ownership interest will be diluted to the extent that the initial public offering price per share of our "
              "Common Stock exceeds the tangible book value per share")
    assert sec.classify_prospectus(caring) == "ipo"
    # Neither statement: the caller must treat it as unknown, not as an IPO and not as a follow-on.
    assert sec.classify_prospectus("This is a best efforts offering of units. Price to public $ 2.00 per unit.") == "unknown"
    assert sec.parse_priced_ipo("This is a best efforts offering of units. initial public offering. Price to public $ 2.00 per unit.") is None


def test_follow_on_marker_beats_a_warrant_no_market_sentence():
    # A listed issuer selling common stock plus warrants: the "no public market" sentence is about the
    # warrants and the prospectus quotes the last sale price of the stock, so it is a follow-on.
    blue_star = ("Our common stock is listed on the Nasdaq Capital Market under the symbol BSFC. The last reported sale price "
                 "of our common stock on the Nasdaq Capital Market on September 6, 2023, was $0.4655 per share. There is no "
                 "established public trading market for the common stock purchase warrants and pre-funded warrants. "
                 "initial public offering price")
    assert sec.classify_prospectus(blue_star) == "follow_on"
    assert sec.parse_priced_ipo(blue_star) is None
    assert sec.classify_prospectus("This is our initial public offering. There is no public market for our common stock warrants.") == "ipo"


def test_follow_on_markers_only_count_on_the_cover_page():
    spac = ("This is an initial public offering of our securities. Each unit has an offering price of $10.00. Our units have been "
            "approved for listing on Nasdaq under the symbol SPACU. Price to public $10.00 Underwriting discounts and commissions $0.55. "
            + "risk factors " * 400 +
            "Our sponsor is an affiliate of BigCo, whose common stock is listed on the New York Stock Exchange under the symbol BIG; "
            "the last reported sale price of BigCo common stock was $45.10.")
    assert sec.classify_prospectus(spac) == "ipo"
    assert sec.parse_priced_ipo(spac)["symbol"] == "SPACU"


def test_backfill_resume_keeps_the_original_target_end(db, monkeypatch):
    monkeypatch.setattr(sec, "master_index_if_published", lambda day, ua: ({"424B4": [], "RW": []}, "u"))
    first = backfill(db, days=2, max_minutes=5, start=date(2022, 1, 3), end=date(2022, 1, 31))
    assert first.metadata_json["target_end"] == "2022-01-31" and first.metadata_json["through"] == "2022-01-04"
    # A resumed run without --end must not shrink the target because of rows written meanwhile.
    db.add(IPO(external_key="US:early", company="Early Backfilled Corp", country="United States", status="Listed", listing_date="20220103"))
    db.commit()
    second = backfill(db, days=2, max_minutes=5)
    assert second.metadata_json["from"] == "2022-01-05" and second.metadata_json["target_end"] == "2022-01-31"
    assert second.metadata_json["through"] == "2022-01-06"


def test_backfilled_rows_are_marked_classified_so_repair_skips_them(db, monkeypatch):
    from scripts.backfill_us_priced import repair_candidates, REPARSED_FLAG
    monkeypatch.setattr(sec, "master_index_if_published", lambda day, ua: _index(day, [("9000010", "Marked Co")]))
    monkeypatch.setattr(sec, "filing_head", lambda url, ua, n=0: (IPO_TEXT, False))
    backfill(db, days=1, max_minutes=5, start=date(2024, 5, 6), end=date(2024, 5, 6))
    row = db.scalar(select(IPO).where(IPO.external_key == "US:9000010"))
    assert sec.CLASSIFIED_MARKER in row.data_flags and REPARSED_FLAG in row.data_flags
    assert row not in repair_candidates(db, 1000)


def test_recorded_target_end_is_the_furthest_one(db):
    from scripts.backfill_us_priced import recorded_target_end
    db.add_all([IngestionRun(source=SOURCE, status="ok", metadata_json={"through": "2022-09-26", "target_end": "2025-10-23"}),
                IngestionRun(source=SOURCE, status="ok", metadata_json={"through": "2022-09-26", "target_end": "2021-09-27"})])
    db.commit()
    assert recorded_target_end(db) == date(2025, 10, 23)
