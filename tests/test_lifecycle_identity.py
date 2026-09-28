"""Lifecycle classification, issuer identity and label hygiene.

These guard the production data rules introduced with the India/US coverage
hardening: non-IPO issues from NSE's monthly report are never IPOs, a listed
row never regresses to Upcoming, duplicate issuer spellings resolve to one
record prospectively, subscription ticks with an unchanged score do not
create duplicate prediction snapshots, and no user-visible string carries an
em dash (U+2014)."""
from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.models import IPO, ScoreSnapshot
from app.scoring import compute_score, confidence_reasons
from app.services import sec
from app.services.identity import (canonical_name, classify_issue_type, board_for_issue_type,
                                   status_can_transition, sanitize_label, sanitize_tree, EM_DASH)
from app.services.pipeline import upsert_ipo, reconcile_lifecycle, NameIndex, _event_stage
from app.services import sensitivity
from scripts.build_pages import upcoming_exclusion_reason, dedupe_by_issuer, pipeline_status, US_STALE_FILING_DAYS


# ------------------------------------------------------------- identity ----

@pytest.mark.parametrize("a,b", [
    ("Adani Green Energy Ltd.", "ADANI GREEN ENERGY LIMITED"),
    ("A B Cotspin India Limited", "A B Cotspin India Ltd"),
    ("360 ONE WAM LIMITED", "360 One WAM Ltd."),
    ("Rays of Belief Limited- For Profit Social Enterprise (FPSE)", "Rays of Belief Limited - For Profit Social Enterprise"),
    ("Tata  Technologies\tLimited", "Tata Technologies Limited"),
    ("Aarey Drugs & Pharmaceuticals Ltd", "Aarey Drugs and Pharmaceuticals Limited"),
])
def test_canonical_name_collapses_punctuation_and_suffix_variants(a, b):
    assert canonical_name(a) == canonical_name(b)


def test_canonical_name_keeps_distinct_issuers_distinct():
    assert canonical_name("Purple Style Labs Limited") != canonical_name("Purple United Sales Limited")
    assert canonical_name("Deepa Jewellers Limited") != canonical_name("Deepak Builders And Engineers India Limited")


def test_canonical_name_is_total():
    assert canonical_name(None) == ""
    assert canonical_name("Limited") == "limited"  # nothing but a suffix falls back to raw tokens


# ---------------------------------------------------------- issue types ----

@pytest.mark.parametrize("raw,expected", [
    ("IPO", "IPO"), ("NSE SME IPO", "SME IPO"), (" NSE SME IPO", "SME IPO"), ("SME-IPO", "SME IPO"), ("SME - IPO", "SME IPO"),
    ("NSE SME", "SME IPO"), ("InvIT IPO", "InvIT IPO"),
    ("Preferential", None), ("Preferential Allotment", None), ("QIP", None), ("Rights", None), ("Rights Issue", None),
    ("Converion Of Warrants", None), ("Equity Shares", None), ("None", None), (None, None), ("", None),
])
def test_classify_issue_type(raw, expected):
    assert classify_issue_type(raw) == expected


def test_board_follows_issue_type_and_exchange():
    assert board_for_issue_type("SME IPO") == "SME"
    assert board_for_issue_type("IPO", "NSE SME") == "SME"
    assert board_for_issue_type("IPO", "NSE/BSE") == "Mainboard"


# ------------------------------------------------------------- statuses ----

def test_status_never_regresses_from_listed_or_withdrawn():
    assert status_can_transition("Upcoming", "Open")
    assert status_can_transition("Open", "Closed")
    assert status_can_transition("Closed", "Listed")
    assert not status_can_transition("Listed", "Open")
    assert not status_can_transition("Listed", "Upcoming")
    assert not status_can_transition("Withdrawn", "Filed")
    assert status_can_transition("Filed", "Withdrawn")


def test_upsert_refuses_to_downgrade_a_listed_row(db):
    row = dict(company="Regress Co", country="India", symbol="REGR", status="Listed", listing_date="2026-01-05", final_price=100.0)
    upsert_ipo(db, row, "NSE Primary Market Report", "https://nsearchives.nseindia.com/r.xlsx", 1)
    db.commit()
    upsert_ipo(db, {**row, "status": "Upcoming"}, "NSE", "https://www.nseindia.com/x", 1)
    db.commit()
    ipo = db.scalar(select(IPO).where(IPO.symbol == "REGR"))
    assert ipo.status == "Listed"


# ------------------------------------------- report row resolves to live row ----

def test_report_row_without_symbol_updates_the_live_feed_row_not_a_duplicate(db):
    live = dict(company="Kanohar Electricals Limited", country="India", symbol="KANOHAR", status="Open",
                open_date="08-Sep-2026", close_date="10-Sep-2026", price_low=100.0, price_high=105.0)
    upsert_ipo(db, live, "NSE", "https://www.nseindia.com/x", 1)
    db.commit()
    index = NameIndex(db)
    report = dict(company="KANOHAR ELECTRICALS LTD.", country="India", symbol="", isin="INE0TEST0001", status="Listed",
                  listing_date="2026-09-15 00:00:00", final_price=105.0, board="SME")
    upsert_ipo(db, report, "NSE Primary Market Report", "https://nsearchives.nseindia.com/r.xlsx", 1, index)
    db.commit()
    rows = db.scalars(select(IPO).where(IPO.country == "India")).all()
    assert len(rows) == 1
    ipo = rows[0]
    assert ipo.external_key == "IN:kanohar"
    assert ipo.status == "Listed" and ipo.listing_date == "2026-09-15 00:00:00" and ipo.isin == "INE0TEST0001"
    assert ipo.company == "Kanohar Electricals Limited"  # live-feed spelling kept


# ---------------------------------------------------------- reconcile ----

def test_reconcile_marks_non_ipo_report_rows_and_transitions_closed(db):
    pref = IPO(external_key="in:pref co", company="Pref Co", country="India", status="Listed", listing_date="2025-01-10",
               raw={"issue_type": "Preferential Allotment", "exchange": "NSE"})
    sme = IPO(external_key="in:sme co", company="SME Co", country="India", status="Listed", board="EQ", listing_date="2025-02-10",
              raw={"issue_type": " NSE SME IPO", "exchange": "NSE SME", "industry": "Textiles", "isin_number": "INE0SME00001"})
    old_open = IPO(external_key="in:oldopen", company="Old Open Ltd", country="India", symbol="OLDOPEN", status="Open",
                   open_date="01-Jan-2026", close_date="03-Jan-2026")
    listed_twin = IPO(external_key="in:twin co", company="TWIN CO LTD.", country="India", status="Listed", listing_date="2026-03-01", final_price=50.0,
                      raw={"issue_type": "IPO", "exchange": "NSE"})
    live_twin = IPO(external_key="in:twin", company="Twin Co Limited", country="India", symbol="TWIN", status="Closed",
                    open_date="20-Feb-2026", close_date="24-Feb-2026")
    db.add_all([pref, sme, old_open, listed_twin, live_twin]); db.commit()
    stats = reconcile_lifecycle(db)
    assert stats["not_ipo"] == 1 and stats["closed"] == 1 and stats["listed_from_match"] == 1
    db.expire_all()
    assert db.get(IPO, pref.id).status == "Not IPO"
    s = db.get(IPO, sme.id)
    assert s.board == "SME" and s.sector == "Textiles" and s.isin == "INE0SME00001" and s.status == "Listed"
    assert db.get(IPO, old_open.id).status == "Closed"
    t = db.get(IPO, live_twin.id)
    assert t.status == "Listed" and t.listing_date == "2026-03-01" and t.final_price == 50.0
    # idempotent
    assert reconcile_lifecycle(db) == {"not_ipo": 0, "listed_from_match": 0, "closed": 0, "board_fixed": 0}


# ------------------------------------------------- duplicate snapshots ----

def _ipo(**kw):
    base = dict(external_key="in:snap", company="Snap Co", country="India", status="Open", filing_url="",
                data_flags=[], qib_sub=1.2, total_sub=1.5)
    base.update(kw)
    return IPO(**base)


def test_subscription_tick_with_unchanged_score_creates_no_snapshot():
    ipo = _ipo()
    score = compute_score(ipo)
    last = ScoreSnapshot(ipo_id=1, **score)
    # identical score, only a subscription field "changed" (e.g. 1.2341 -> 1.2343)
    assert _event_stage(False, {"total_sub"}, {"filing_url": "", "final_price": None}, ipo, last, score) is None


def test_subscription_move_that_changes_the_score_still_snapshots():
    ipo = _ipo()
    score = compute_score(ipo)
    last = ScoreSnapshot(ipo_id=1, **{**score, "listing_score": score["listing_score"] - 5})
    assert _event_stage(False, {"qib_sub"}, {"filing_url": "", "final_price": None}, ipo, last, score) == "subscription_update"


def test_structural_events_always_snapshot_even_with_unchanged_score():
    ipo = _ipo()
    score = compute_score(ipo)
    last = ScoreSnapshot(ipo_id=1, **score)
    assert _event_stage(False, {"price_high"}, {"filing_url": "", "final_price": None}, ipo, last, score) == "price_band_set"
    assert _event_stage(False, {"filing_url"}, {"filing_url": "", "final_price": None}, ipo, last, score) == "filing_ingested"


def test_legacy_em_dash_recommendation_is_not_a_recommendation_change():
    ipo = _ipo()
    score = compute_score(ipo)
    assert score["recommendation"] == "INSUFFICIENT RELIABLE DATA: NO RECOMMENDATION"
    legacy = ScoreSnapshot(ipo_id=1, **{**score, "recommendation": "INSUFFICIENT RELIABLE DATA " + EM_DASH + " NO RECOMMENDATION"})
    assert _event_stage(False, set(), {"filing_url": "", "final_price": None}, ipo, legacy, score) is None


# ------------------------------------------------------------ em dashes ----

def test_sanitize_label_maps_legacy_wordings_and_generic_dashes():
    assert sanitize_label("INVEST " + EM_DASH + " STRONG") == "INVEST: STRONG"
    assert sanitize_label("BOTH " + EM_DASH + " LISTING + LONG TERM") == "BOTH: LISTING + LONG TERM"
    assert sanitize_label("a " + EM_DASH + " b") == "a, b"
    assert sanitize_label("x" + EM_DASH + "y") == "x-y"
    assert sanitize_label(12) == 12 and sanitize_label(None) is None


def test_sanitize_tree_reaches_every_string():
    tree = {"k" + EM_DASH: ["v " + EM_DASH + " w", {"n": 1, "s": EM_DASH}], "t": ("a", EM_DASH)}
    out = sanitize_tree(tree)
    assert EM_DASH not in json.dumps(out, ensure_ascii=False)


def test_scoring_and_sensitivity_vocabulary_has_no_em_dash():
    ipo = SimpleNamespace(**{c: None for c in IPO.__table__.columns.keys()})
    ipo.country = "India"; ipo.filing_url = "https://www.sebi.gov.in/x"; ipo.data_flags = []
    for rich in (False, True):
        if rich:
            ipo.price_high = 100; ipo.post_issue_shares_m = 10; ipo.revenue_m = 500; ipo.revenue_prev_m = 400; ipo.revenue_2y_ago_m = 300
            ipo.ebitda_m = 100; ipo.net_income_m = 60; ipo.cfo_m = 70; ipo.debt_m = 10; ipo.cash_m = 50; ipo.fresh_issue_pct = 80; ipo.ofs_pct = 20
            ipo.peer_median_pe = 30; ipo.peer_median_ps = 5; ipo.qib_sub = 40; ipo.nii_sub = 30; ipo.retail_sub = 10; ipo.total_sub = 30
            ipo.market_regime = 4; ipo.sector_regime = 4; ipo.promoter_retention_pct = 60
        out = compute_score(ipo)
        assert EM_DASH not in json.dumps(out)
    assert all(EM_DASH not in b for b in sensitivity.BANDS)


def test_confidence_reasons_explain_the_gate():
    ipo = SimpleNamespace(**{c: None for c in IPO.__table__.columns.keys()})
    ipo.country = "United States"; ipo.filing_url = ""; ipo.data_flags = ["ipo_classification: checked"]
    reasons = confidence_reasons(ipo, conflicts=2)
    joined = " | ".join(reasons)
    assert "No primary filing" in joined and "Price band" in joined and "conflict" in joined
    assert "ipo_classification" not in joined  # bookkeeping marker is not a data problem
    ipo.filing_url = "https://sec.gov.evil.com/x"
    assert any("secondary source" in r for r in confidence_reasons(ipo))


# ------------------------------------------------------ SEC lifecycle ----

def test_master_index_groups_priced_and_withdrawn_filings():
    text = ("CIK|Company Name|Form Type|Date Filed|Filename\n"
            "123|Example Inc|424B4|2026-08-20|edgar/data/123/a.txt\n"
            "456|Gone Corp|RW|2026-08-20|edgar/data/456/b.txt\n"
            "789|Other Co|10-K|2026-08-20|edgar/data/789/c.txt\n")
    idx = sec.parse_master_index_forms(text)
    assert [r["cik"] for r in idx["424B4"]] == ["123"] and [r["cik"] for r in idx["RW"]] == ["456"]
    assert sec.parse_master_index(text)[0]["cik"] == "123"  # legacy accessor unchanged


def test_ipo_registration_classifiers():
    assert sec.is_ipo_registration_text("This prospectus relates to our initial public offering of common stock.")
    assert not sec.is_ipo_registration_text("The selling stockholders may offer and sell shares from time to time.")
    facts_public = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [{"val": 1, "form": "10-K", "filed": "2025-03-01"}]}}}}}
    facts_new = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [{"val": 1, "form": "S-1", "filed": "2026-03-01"}]}}}}}
    assert sec.already_reporting(facts_public) and not sec.already_reporting(facts_new)


def test_sec_get_backs_off_and_gives_up(monkeypatch):
    calls = []
    class FakeResp:
        def __init__(self, code): self.status_code = code; self.request = None
        def raise_for_status(self):
            if self.status_code >= 400:
                import httpx
                raise httpx.HTTPStatusError("boom", request=None, response=None)
    class FakeClient:
        def get(self, url): calls.append(url); return FakeResp(403)
    sleeps = []
    import httpx
    with pytest.raises(httpx.HTTPStatusError):
        sec._get(FakeClient(), "https://www.sec.gov/x", sleep=sleeps.append)
    assert len(calls) == sec._MAX_ATTEMPTS
    assert [s for s in sleeps if s >= 1.0] == [1.0, 2.0]  # exponential backoff between attempts


def test_withdrawal_marks_filed_row_withdrawn(db):
    upsert_ipo(db, dict(company="Gone Corp", country="United States", cik="456", status="Filed", filing_url="https://www.sec.gov/Archives/edgar/data/456/s1.htm"),
               "SEC EDGAR", "https://www.sec.gov/Archives/edgar/data/456/s1.htm", 1)
    db.commit()
    upsert_ipo(db, dict(company="Gone Corp", country="United States", cik="456", status="Withdrawn", filing_url="https://www.sec.gov/Archives/edgar/data/456/rw.txt"),
               "SEC RW", "https://www.sec.gov/Archives/edgar/data/456/rw.txt", 1)
    db.commit()
    ipo = db.scalar(select(IPO).where(IPO.external_key == "US:456"))
    assert ipo.status == "Withdrawn"
    snaps = db.scalars(select(ScoreSnapshot).where(ScoreSnapshot.ipo_id == ipo.id).order_by(ScoreSnapshot.created_at)).all()
    assert snaps[-1].event_stage == "status_changed" and snaps[-1].is_forward is False


# ------------------------------------------------- publish-time rules ----

def test_upcoming_exclusion_reasons():
    now = datetime(2026, 9, 28, tzinfo=timezone.utc)
    stale = SimpleNamespace(country="United States", status="Filed", filing_date=(now - timedelta(days=US_STALE_FILING_DAYS + 1)).date().isoformat(), data_flags=[], close_date="")
    fresh = SimpleNamespace(country="United States", status="Filed", filing_date=(now - timedelta(days=30)).date().isoformat(), data_flags=[], close_date="")
    non_ipo = SimpleNamespace(country="United States", status="Filed", filing_date=fresh.filing_date, data_flags=["non_ipo_registration: registrant already files periodic reports"], close_date="")
    closed_old = SimpleNamespace(country="India", status="Closed", filing_date="", data_flags=[], close_date="01-Jan-2026")
    closed_recent = SimpleNamespace(country="India", status="Closed", filing_date="", data_flags=[], close_date="20-Sep-2026")
    assert "stale registration" in upcoming_exclusion_reason(stale, now)
    assert upcoming_exclusion_reason(fresh, now) is None
    assert upcoming_exclusion_reason(non_ipo, now).startswith("not an IPO registration")
    assert "no confirmed listing" in upcoming_exclusion_reason(closed_old, now)
    assert upcoming_exclusion_reason(closed_recent, now) is None


def test_dedupe_keeps_the_most_complete_record_and_names_it():
    a = SimpleNamespace(id=1, country="India", company="Dup Co Ltd", status="Listed", listing_date="2025-01-01", final_price=10.0, price_high=None, isin="", issue_size_m=None, fresh_issue_pct=None, sector="Unknown", symbol="", external_key="in:dup co ltd")
    b = SimpleNamespace(id=2, country="India", company="DUP CO LIMITED", status="Listed", listing_date="2025-01-01", final_price=10.0, price_high=10.0, isin="INE1", issue_size_m=100.0, fresh_issue_pct=60.0, sector="Textiles", symbol="DUP", external_key="in:dup")
    c = SimpleNamespace(id=3, country="United States", company="Dup Co", status="Listed", listing_date="2025-01-01", final_price=None, price_high=None, isin="", issue_size_m=None, fresh_issue_pct=None, sector="Unknown", symbol="", external_key="us:3")
    kept, dropped = dedupe_by_issuer([a, b, c])
    assert {k.id for k in kept} == {2, 3}
    assert dropped[0]["id"] == 1 and "kept id 2" in dropped[0]["reason"]


def test_pipeline_status_is_the_worst_required_source():
    rows = [{"source": "SEC EDGAR", "public_status": "LIVE"}, {"source": "NSE", "public_status": "LIVE"},
            {"source": "SEC Priced IPOs", "public_status": "PARTIAL"}, {"source": "NSE Primary Market Reports", "public_status": "LIVE"},
            {"source": "Licensed enrichment feed", "public_status": "FAILED"}]
    assert pipeline_status(rows) == "PARTIAL"  # optional feed failure does not count
    rows[1]["public_status"] = "FAILED"
    assert pipeline_status(rows) == "FAILED"


def test_clean_company_name_strips_trailing_edgar_cik_artifact():
    from app.services.pipeline import clean_company_name
    assert clean_company_name("ARES STRATEGIC MINING INC. (0001804792) (Filer)", "United States") == "ARES STRATEGIC MINING INC."
    assert clean_company_name("1 - ACME, INC. (0001234567) (Filer)", "United States") == "ACME, INC."
    assert clean_company_name("Some India Co (1234567) (Filer)", "India") == "Some India Co (1234567) (Filer)"  # SEC-only rule


# ------------------------------------------------ SEC priced-IPO robustness --

def test_priced_ipo_price_regex_does_not_swallow_sentence_punctuation():
    txt = ("This prospectus relates to our initial public offering. The initial public offering price is $1.00. "
           "Our shares will trade under the symbol RUIH.")
    parsed = sec.parse_priced_ipo(txt)
    assert parsed["final_price"] == 1.0 and parsed["symbol"] == "RUIH"
    assert sec.parse_priced_ipo("initial public offering price of $1,250.50 per share")["final_price"] == 1250.5
    assert sec.parse_price_range("offering price between $4 and $6 per share") == (4.0, 6.0)


def _http_status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/master.20260928.idx")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(code, request=req))


def test_missing_daily_index_is_not_a_fetch_failure(monkeypatch):
    from datetime import date
    monkeypatch.setattr(sec, "master_index_for_date", lambda day, ua: (_ for _ in ()).throw(_http_status_error(403)))
    with pytest.raises(sec.DailyIndexUnavailable):
        sec.master_index_if_published(date(2026, 9, 28), "ua")
    monkeypatch.setattr(sec, "master_index_for_date", lambda day, ua: (_ for _ in ()).throw(_http_status_error(503)))
    with pytest.raises(httpx.HTTPStatusError):
        sec.master_index_if_published(date(2026, 9, 28), "ua")


def test_ingest_sec_priced_stays_ok_when_index_files_are_not_published(db, monkeypatch):
    from app.services import pipeline
    monkeypatch.setattr(sec, "master_index_if_published", lambda day, ua: (_ for _ in ()).throw(sec.DailyIndexUnavailable(str(day))))
    run = pipeline.ingest_sec_priced(db, lookback_days=3)
    assert run.status == "ok" and run.error == ""
    assert run.metadata_json["index_not_published"]  # the skipped days are recorded, not hidden
