"""Repair pass over existing US 424B4 rows: reclassify follow-ons, fill gaps, idempotent."""
from __future__ import annotations

from app.models import IPO
from app.services import sec
from scripts.backfill_us_priced import repair_existing_us_listed, NON_IPO_FLAG, REPARSED_FLAG

IPO_TEXT = ("This is our initial public offering. The initial public offering price is $12.00 per share. "
            "Our common stock has been approved for listing on Nasdaq under the symbol BKFL.")
FOLLOW_ON_TEXT = ("We completed our initial public offering in 2019. Our common stock is listed on Nasdaq under the "
                  "symbol FOLO. On March 1, 2024, the last reported sale price of our common stock was $31.00. This "
                  "prospectus relates to the resale of shares by the selling stockholders at $30.00 per share.")


def test_repair_reclassifies_follow_ons_and_fills_missing_fields(db, monkeypatch):
    a = IPO(external_key="US:9100001", company="Real IPO Co", country="United States", status="Listed",
            filing_url="https://www.sec.gov/Archives/edgar/data/9100001/a.txt", symbol="", final_price=None)
    b = IPO(external_key="US:9100002", company="Follow On Co", country="United States", status="Listed",
            filing_url="https://www.sec.gov/Archives/edgar/data/9100002/b.txt", symbol="OUR", final_price=None)
    db.add_all([a, b])
    db.commit()
    texts = {"9100001": IPO_TEXT, "9100002": FOLLOW_ON_TEXT}
    monkeypatch.setattr(sec, "filing_head", lambda url, ua, n=0: (next(v for k, v in texts.items() if k in url), False))

    run = repair_existing_us_listed(db, limit=50, max_minutes=5)
    db.refresh(a)
    db.refresh(b)
    assert a.status == "Listed" and a.symbol == "BKFL" and a.final_price == 12.0
    assert sec.CLASSIFIED_MARKER in a.data_flags and REPARSED_FLAG in a.data_flags
    assert b.status == "Not IPO" and NON_IPO_FLAG in b.data_flags
    assert run.metadata_json == {"checked": 2, "reclassified_not_ipo": 1, "fields_filled": 2, "inconclusive": 0}

    # Idempotent: a second pass has nothing left to read.
    calls: list[str] = []
    monkeypatch.setattr(sec, "filing_head", lambda url, ua, n=0: (calls.append(url) or IPO_TEXT, False))
    run2 = repair_existing_us_listed(db, limit=50, max_minutes=5)
    assert run2.metadata_json["checked"] == 0 and calls == []


def test_repair_only_touches_us_sec_rows(db, monkeypatch):
    india = IPO(external_key="IN:repairtest", company="Repair Test Limited", country="India", status="Listed",
                filing_url="https://www.nseindia.com/x", symbol="", final_price=None)
    db.add(india)
    db.commit()
    monkeypatch.setattr(sec, "filing_head", lambda url, ua, n=0: (FOLLOW_ON_TEXT, False))
    repair_existing_us_listed(db, limit=500, max_minutes=5)
    db.refresh(india)
    assert india.status == "Listed" and not india.data_flags


def test_truncated_head_without_cover_page_is_inconclusive_not_reclassified(db, monkeypatch):
    from scripts.backfill_us_priced import INCONCLUSIVE_FLAG
    row = IPO(external_key="US:9100003", company="Deep Cover Co", country="United States", status="Listed",
              filing_url="https://www.sec.gov/Archives/edgar/data/9100003/c.txt", symbol="", final_price=None)
    db.add(row)
    db.commit()
    monkeypatch.setattr(sec, "filing_head", lambda url, ua, n=0: ("table of contents, risk factors, nothing about the offering yet", True))
    run = repair_existing_us_listed(db, limit=50, max_minutes=5)
    db.refresh(row)
    assert row.status == "Listed" and INCONCLUSIVE_FLAG in row.data_flags
    assert run.metadata_json["inconclusive"] == 1 and run.metadata_json["reclassified_not_ipo"] == 0
    # and it is not re-read on the next pass
    calls: list[str] = []
    monkeypatch.setattr(sec, "filing_head", lambda url, ua, n=0: (calls.append(url) or "", True))
    repair_existing_us_listed(db, limit=50, max_minutes=5)
    assert calls == []


def test_unknown_classification_keeps_the_row_and_is_flagged(db, monkeypatch):
    from scripts.backfill_us_priced import UNKNOWN_FLAG
    row = IPO(external_key="US:9100004", company="Ambiguous Co", country="United States", status="Listed",
              filing_url="https://www.sec.gov/Archives/edgar/data/9100004/d.txt", symbol="", final_price=None)
    db.add(row)
    db.commit()
    monkeypatch.setattr(sec, "filing_head", lambda url, ua, n=0: ("This is a best efforts offering of units. Price to public $ 2.00 per unit. Underwriting discounts and commissions", False))
    run = repair_existing_us_listed(db, limit=50, max_minutes=5)
    db.refresh(row)
    assert row.status == "Listed" and UNKNOWN_FLAG in row.data_flags and sec.CLASSIFIED_MARKER not in row.data_flags
    assert run.metadata_json["inconclusive"] == 1
