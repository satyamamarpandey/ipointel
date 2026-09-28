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
    assert run.metadata_json == {"checked": 2, "reclassified_not_ipo": 1, "fields_filled": 2, "inconclusive": 0, "rechecked": 0, "restored_listed": 0}

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


def test_recheck_restores_rows_wrongly_moved_to_not_ipo(db, monkeypatch):
    from scripts.backfill_us_priced import recheck_not_ipo_rows, RECHECKED_FLAG
    spac = IPO(external_key="US:9100005", company="Restored SPAC", country="United States", status="Not IPO", symbol="",
               final_price=None, filing_url="https://www.sec.gov/Archives/edgar/data/9100005/e.txt", data_flags=[NON_IPO_FLAG])
    real_follow_on = IPO(external_key="US:9100006", company="Still Follow-on", country="United States", status="Not IPO", symbol="",
                         final_price=None, filing_url="https://www.sec.gov/Archives/edgar/data/9100006/f.txt", data_flags=[NON_IPO_FLAG])
    db.add_all([spac, real_follow_on])
    db.commit()
    texts = {"9100005": IPO_TEXT, "9100006": FOLLOW_ON_TEXT}
    monkeypatch.setattr(sec, "filing_head", lambda url, ua, n=0: (next(v for k, v in texts.items() if k in url), False))
    stats = recheck_not_ipo_rows(db, limit=50, max_minutes=5)
    db.refresh(spac)
    db.refresh(real_follow_on)
    assert stats == {"rechecked": 2, "restored_listed": 1}
    assert spac.status == "Listed" and spac.symbol == "BKFL" and NON_IPO_FLAG not in spac.data_flags and RECHECKED_FLAG in spac.data_flags
    assert real_follow_on.status == "Not IPO" and RECHECKED_FLAG in real_follow_on.data_flags
    assert recheck_not_ipo_rows(db, limit=50, max_minutes=5) == {"rechecked": 0, "restored_listed": 0}


def test_repair_corrects_a_price_stored_by_the_older_parser(db, monkeypatch):
    from scripts.backfill_us_priced import PRICE_V2_FLAG, REPARSED_FLAG
    spac = IPO(external_key="US:9100007", company="Mispriced SPAC", country="United States", status="Listed", symbol="MSPCU",
               final_price=0.2, filing_url="https://www.sec.gov/Archives/edgar/data/9100007/g.txt",
               data_flags=[sec.CLASSIFIED_MARKER, REPARSED_FLAG])
    db.add(spac)
    db.commit()
    text = ("This is an initial public offering of our securities. Each unit has an offering price of $10.00. Our units have been "
            "approved for listing on Nasdaq under the symbol MSPCU. Price to Public Underwriting Discount Proceeds to us Per Unit $ 10.00 "
            "$ 0.55 $ 9.45. Our sponsor purchased private placement warrants at a price of $0.20 per warrant.")
    monkeypatch.setattr(sec, "filing_head", lambda url, ua, n=0: (text, False))
    run = repair_existing_us_listed(db, limit=50, max_minutes=5)
    db.refresh(spac)
    assert spac.final_price == 10.0 and spac.symbol == "MSPCU" and PRICE_V2_FLAG in spac.data_flags
    assert run.metadata_json["fields_filled"] == 1
    # verified rows are not read again
    calls: list[str] = []
    monkeypatch.setattr(sec, "filing_head", lambda url, ua, n=0: (calls.append(url) or text, False))
    repair_existing_us_listed(db, limit=50, max_minutes=5)
    assert calls == []


def test_repair_replaces_a_par_value_price_and_drops_its_returns(db, monkeypatch):
    from app.models import PerformanceSnapshot
    from scripts.backfill_us_priced import PRICE_V2_FLAG, PRICE_V3_FLAG, REPARSED_FLAG
    row = IPO(external_key="US:9100008", company="Par Value Corp", country="United States", status="Listed", symbol="PARV",
              final_price=0.0001, filing_url="https://www.sec.gov/Archives/edgar/data/9100008/h.txt",
              data_flags=[sec.CLASSIFIED_MARKER, REPARSED_FLAG, PRICE_V2_FLAG])
    db.add(row)
    db.flush()
    db.add(PerformanceSnapshot(ipo_id=row.id, as_of_date="2026-09-01", close_price=3.0, listing_return_pct=2999900.0, source_name="t", source_url="u"))
    db.commit()
    text = ("This is our initial public offering. The initial public offering price per share of our common stock, par value $0.0001, "
            "is $4.00 per share. Our common stock has been approved for listing on Nasdaq under the symbol PARV. "
            "Price to public $ 4.00 Underwriting discounts and commissions $ 0.28")
    monkeypatch.setattr(sec, "filing_head", lambda url, ua, n=0: (text, False))
    repair_existing_us_listed(db, limit=50, max_minutes=5)
    db.refresh(row)
    assert row.final_price == 4.0 and PRICE_V3_FLAG in row.data_flags
    assert db.query(PerformanceSnapshot).filter_by(ipo_id=row.id).count() == 0


def test_purge_implausible_performance_deletes_unit_mismatches_only(db, monkeypatch):
    from app.models import PerformanceSnapshot
    from scripts.backfill_us_priced import purge_implausible_performance
    import scripts.backfill_us_priced as bf
    ok = IPO(external_key="US:9100009", company="Fine Co", country="United States", status="Listed", symbol="FINE", final_price=10.0)
    bad = IPO(external_key="US:9100010", company="Split Co", country="United States", status="Listed", symbol="SPLT", final_price=4.0)
    db.add_all([ok, bad])
    db.flush()
    db.add_all([PerformanceSnapshot(ipo_id=ok.id, as_of_date="2026-09-01", close_price=12.0, listing_return_pct=20.0, source_name="t", source_url="u"),
                PerformanceSnapshot(ipo_id=bad.id, as_of_date="2026-09-01", close_price=1.7, listing_return_pct=1693439900.0, source_name="t", source_url="u")])
    db.commit()
    monkeypatch.setattr(bf, "refresh_market_performance", lambda db, limit: 0)
    stats = purge_implausible_performance(db)
    assert stats["snapshots_deleted"] == 1 and stats["ipos_affected"] == 1
    assert db.query(PerformanceSnapshot).filter_by(ipo_id=ok.id).count() == 1
    assert db.query(PerformanceSnapshot).filter_by(ipo_id=bad.id).count() == 0
