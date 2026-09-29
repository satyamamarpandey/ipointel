"""Phase 5: v4 cover-page readers and the final-price repair pass."""
from app.models import IPO, Provenance
from app.services import sec
from scripts import repair_us_prices as rp

COVER_TAIL = " price to public underwriting discounts and commissions proceeds, before expenses, to us"


def test_v4_price_reads_currency_prefixes_and_zero_width_tables():
    cases = {
        "This is our initial public offering. price us$18.00 per ads per ads total public offering price us$ 18.00 us$ 348,461,226 underwriting discount and commission": 18.0,
        "This is our initial public offering. the public offering price is $15.00 per common share." + COVER_TAIL: 15.0,
        "This is our initial public offering. per common share ​ total public offering price ​ ​ $ ​ 5.80 ​ $44,776,000 underwriting discounts and commissions": 5.8,
        "This is our initial public offering. underwritten offering at a public offering price of usd$4.13 per common unit." + COVER_TAIL: 4.13,
        "This is our initial public offering. the ipo price of our ordinary shares is $4.0 per share. per share total ipo price $ 4.00 $ 5,000,000 underwriting discounts": 4.0,
        "This is our initial public offering. we will offer our shares at a fixed price of $2.00 per share for the duration." + COVER_TAIL: 2.0,
        "This is our initial public offering. the offering price of the shares in this offering is us$4.00 per share." + COVER_TAIL: 4.0,
    }
    for text, want in cases.items():
        assert sec.parse_offer_price_v4(sec.flatten_filing_text(text)) == want, text[:60]


def test_v4_price_ignores_par_value_and_warrant_clauses():
    text = "common stock, par value $0.0001 per share. each warrant is exercisable at a price of $11.50 per share." + COVER_TAIL
    assert sec.parse_offer_price_v4(sec.flatten_filing_text(text)) is None


def test_v4_deep_scan_skips_dilution_assumptions():
    # Beyond the cover page only table cells / "the price is" count; the dilution
    # section's assumed midpoint must not become the offer price.
    text = "This is our initial public offering." + COVER_TAIL + " " + "x " * 3000 + "based upon a public offering price of $4.50 per ordinary share"
    assert sec.parse_offer_price_v4(sec.flatten_filing_text(text)) is None


def test_parse_priced_ipo_falls_back_to_v4():
    text = "This is our initial public offering of ordinary shares. price us$18.00 per ads total public offering price us$ 18.00 underwriting discount and commission. listed under the symbol “TDCX”"
    parsed = sec.parse_priced_ipo(text)
    assert parsed and parsed["final_price"] == 18.0 and parsed["symbol"] == "TDCX"


def test_classify_offering_type_distinguishes_documents_without_an_offer_price():
    cases = {
        "registration of the resale of shares by the registered stockholders in connection with our direct listing on nasdaq. unlike an initial public offering, the resale" + COVER_TAIL: sec.DIRECT_LISTING,
        "this prospectus relates to the resale of 11,120,000 ordinary shares by the selling shareholders. we will not receive any proceeds. no public market currently exists for our ordinary shares" + COVER_TAIL: sec.RESALE_ONLY,
        "notes due 2031. per note total public offering price 100.000 % $ 100,000,000 underwriting discounts" : sec.DEBT_OFFERING,
        "this proxy statement/prospectus relates to the business combination agreement between helix and pubco" + COVER_TAIL: sec.MERGER_PROXY,
        "in connection with the planned distribution (the “spin-off”) by meta to its stockholders. shares will be distributed in the spin-off" + COVER_TAIL: sec.SPIN_OFF,
        "the closing sales price of our shares of series a common stock as reported on nasdaq was $20.96 per share." + COVER_TAIL: "follow_on",
        "our class a ordinary shares began trading on nasdaq under the symbol “wshp” on november 14, 2025." + COVER_TAIL: "follow_on",
        "our common shares are listed on the tsx venture exchange under the symbol “kwe”." + COVER_TAIL: "follow_on",
        # A combined filing: resale cover first, but an IPO prospectus exists.
        "this prospectus relates to the resale by the selling shareholder. by separate prospectus (the ipo prospectus), we have registered shares which we are offering for sale in an initial public offering. there is no public market for our common stock" + COVER_TAIL: "ipo",
        "this is an initial public offering of our securities. we expect that the common stock will be listed on nasdaq under the symbol roc. price to public per unit $ 10.00 underwriting discounts": "ipo",
    }
    for text, want in cases.items():
        assert sec.classify_offering_type(sec.flatten_filing_text(text)) == want, text[:70]


def _ipo(db, **kw):
    base = dict(external_key="US:99", company="Acme", country="United States", status="Listed", listing_date="20240321",
                filing_url="https://www.sec.gov/Archives/edgar/data/99/0000000099-24-000001.txt")
    base.update(kw)
    ipo = IPO(**base)
    db.add(ipo)
    db.flush()
    return ipo


def test_repair_row_prices_an_ipo_and_records_provenance(db):
    ipo = _ipo(db)
    text = "This is our initial public offering of ordinary shares. price us$18.00 per ads total public offering price us$ 18.00 underwriting discount and commission. under the symbol “TDCX”"
    assert rp.repair_row(db, ipo, text, False) == "priced"
    db.commit()
    assert ipo.final_price == 18.0 and ipo.symbol == "TDCX" and ipo.status == "Listed"
    assert db.query(Provenance).filter_by(ipo_id=ipo.id, field_name="final_price", source_name=rp.PROVENANCE).count() == 1


def test_repair_row_direct_listing_keeps_row_listed_without_a_price(db):
    ipo = _ipo(db, symbol="")
    text = "registration of the resale of shares by the registered stockholders in connection with our direct listing on nasdaq. unlike an initial public offering, the resale. approved for listing under the symbol “AMPL”" + COVER_TAIL
    assert rp.repair_row(db, ipo, text, True) == "direct_listing"
    assert ipo.final_price is None and ipo.status == "Listed" and ipo.symbol == "AMPL"
    assert rp.DIRECT_LISTING_FLAG in ipo.data_flags


def test_repair_row_moves_non_equity_offerings_to_not_ipo(db):
    ipo = _ipo(db)
    text = "notes due 2031. per note total public offering price 100.000 % $ 100,000,000 underwriting discounts"
    assert rp.repair_row(db, ipo, text, False) == f"not_ipo:{sec.DEBT_OFFERING}"
    assert ipo.status == "Not IPO" and any("debt securities" in str(f) for f in ipo.data_flags)


def test_repair_row_flags_unparsed_and_is_idempotent(db):
    ipo = _ipo(db)
    text = "This is our initial public offering. the shares are offered at the fixed price of $0.25 per share." + COVER_TAIL  # below the plausible band
    assert rp.repair_row(db, ipo, text, False) == "unparsed:ipo"
    assert rp.repair_row(db, ipo, text, False) == "unparsed:ipo"
    assert ipo.data_flags.count(rp.UNPARSED_FLAG) == 1 and ipo.final_price is None


def test_repair_candidates_only_us_sec_listed_rows_without_price(db):
    _ipo(db)
    _ipo(db, external_key="US:100", final_price=10.0)
    _ipo(db, external_key="IN:acme", country="India", filing_url="https://www.nseindia.com/x")
    _ipo(db, external_key="US:101", status="Filed")
    assert [x.external_key for x in rp.candidates(db, 50)] == ["US:99"]


def test_run_uses_filing_head_and_reports_outcomes(db, monkeypatch):
    _ipo(db)
    monkeypatch.setattr(sec, "filing_head", lambda url, ua, max_bytes=0: ("This is our initial public offering. per share total ipo price $ 4.00 $ 5,000,000 underwriting discounts", False))
    out = rp.run(db, limit=10, log=lambda *a, **k: None)
    assert out == {"priced": 1}
