"""Phase 2: India symbol resolution from NSE's official masters. Exact
matches only; every non-resolution carries a stated reason; rows that
already have a symbol are never overwritten."""
from __future__ import annotations
from sqlalchemy import select

from app.models import IPO, Provenance
from app.services import nse_master
from app.services.nse_master import (parse_master_csv, MasterIndex, RESOLVED_ISIN, RESOLVED_NAME_ISIN_PREFIX,
                                     CONFLICTING, AMBIGUOUS, UNRESOLVED, SOURCE_MAINBOARD, SOURCE_SME,
                                     EQUITY_MASTER_URL, SME_MASTER_URL)
from app.services.pipeline import resolve_india_symbols

EQUITY_CSV = (
    "SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\n"
    "SIGACHI,Sigachi Industries Limited,EQ,15-NOV-2021,1,1,INE0D0K01022,1\n"
    "SENCO,Senco Gold Limited,EQ,14-JUL-2023,10,1,INE602W01027,10\n"
    "DUPA,Duplicate Name Limited,EQ,01-JAN-2020,10,1,INE111111111,10\n"
    "DUPB,Duplicate Name Limited,EQ,01-JAN-2021,10,1,INE222222222,10\n"
)
SME_CSV = (
    "SYMBOL,NAME_OF_COMPANY,SERIES,DATE_OF_LISTING,PAID_UP_VALUE,ISIN_NUMBER,FACE_VALUE,\n"
    "COOLCAPS,Cool Caps Industries Limited,SM,15-Mar-22,10,INE0HS001028,10,\n"
    "AXIOMGAS,Axiom Gas Engineering Limited,ST,25-Sep-26,5,INE16J201028,5,\n"
)


def masters():
    return parse_master_csv(EQUITY_CSV, "Mainboard", SOURCE_MAINBOARD, EQUITY_MASTER_URL) + \
        parse_master_csv(SME_CSV, "SME", SOURCE_SME, SME_MASTER_URL)


def test_parse_master_csv_handles_both_header_spellings():
    rows = masters()
    by = {r.symbol: r for r in rows}
    assert by["SIGACHI"].isin == "INE0D0K01022" and by["SIGACHI"].board == "Mainboard" and by["SIGACHI"].name == "Sigachi Industries Limited"
    assert by["COOLCAPS"].isin == "INE0HS001028" and by["COOLCAPS"].board == "SME" and by["COOLCAPS"].date_of_listing == "15-Mar-22"
    assert by["AXIOMGAS"].source_url == SME_MASTER_URL


def test_index_resolution_outcomes_are_a_closed_vocabulary():
    idx = MasterIndex(masters())
    assert idx.resolve("INE0D0K01022", "anything")[0] == RESOLVED_ISIN
    # ISIN changed after a split: same 9-char issuer prefix, exact name match
    cls, m, _ = idx.resolve("INE0D0K01014", "Sigachi Industries Ltd.")
    assert cls == RESOLVED_NAME_ISIN_PREFIX and m.symbol == "SIGACHI"
    # exact name but a different issuer prefix: never resolved
    assert idx.resolve("INE999999999", "Senco Gold Limited")[0] == CONFLICTING
    # name maps to two symbols
    assert idx.resolve("INE333333333", "Duplicate Name Ltd")[0] == AMBIGUOUS
    # name match without an ISIN to confirm it stays unresolved
    assert idx.resolve("", "Senco Gold Limited")[0] == UNRESOLVED
    assert idx.resolve("INE000000000", "Unknown Co")[0] == UNRESOLVED
    assert idx.check_symbol("INE602W01027", "SENCO") == (True, "")
    assert idx.check_symbol("INE602W01027", "WRONG")[0] is False
    assert idx.check_symbol("INE000000000", "X") == (None, "")


def _india(db, **kw):
    base = dict(country="India", status="Listed", board="Mainboard", currency="INR", listing_date="2023-07-14")
    base.update(kw)
    ipo = IPO(**base)
    db.add(ipo)
    db.flush()
    return ipo


def test_resolve_india_symbols_sets_symbol_with_provenance_and_flags_the_rest(db):
    a = _india(db, external_key="IN:senco gold limited", company="Senco Gold Limited", isin="INE602W01027")
    b = _india(db, external_key="IN:cool caps", company="Cool Caps Industries Limited", isin="INE0HS001010", board="Mainboard")
    c = _india(db, external_key="IN:senco dup", company="Senco Gold Limited", isin="INE999999999")
    d = _india(db, external_key="IN:nobody", company="Nobody Knows Limited", isin="INE000000000")
    e = _india(db, external_key="IN:dupname", company="Duplicate Name Limited", isin="INE333333333")
    already = _india(db, external_key="IN:sigachi", company="Sigachi Industries Limited", isin="INE0D0K01022", symbol="SIGACHI")
    wrong = _india(db, external_key="IN:wrong", company="Sigachi Industries Limited", isin="INE0D0K01022", symbol="OTHER")
    us = IPO(external_key="US:1", company="Acme Inc", country="United States", status="Listed", isin="INE602W01027")
    db.add(us)
    db.commit()

    stats = resolve_india_symbols(db, masters=masters())
    assert stats[RESOLVED_ISIN] == 1 and stats[RESOLVED_NAME_ISIN_PREFIX] == 1
    assert stats[CONFLICTING] == 1 and stats[UNRESOLVED] == 1 and stats[AMBIGUOUS] == 1
    assert stats["existing_agree"] == 1 and stats["existing_conflict"] == 1

    for x in (a, b, c, d, e, already, wrong, us):
        db.refresh(x)
    assert a.symbol == "SENCO"
    prov = db.scalars(select(Provenance).where(Provenance.ipo_id == a.id, Provenance.field_name == "symbol")).all()
    assert prov and prov[0].source_name == SOURCE_MAINBOARD and prov[0].source_url == EQUITY_MASTER_URL and prov[0].source_tier == 1
    # split case: new ISIN stored, old one preserved, board corrected from the SME master
    assert b.symbol == "COOLCAPS" and b.isin == "INE0HS001028" and b.raw["isin_at_ipo"] == "INE0HS001010" and b.board == "SME"
    assert c.symbol == "" and any(f.startswith("symbol_resolution: CONFLICTING") for f in c.data_flags)
    assert d.symbol == "" and any(f.startswith("symbol_resolution: UNRESOLVED") for f in d.data_flags)
    assert e.symbol == "" and any(f.startswith("symbol_resolution: AMBIGUOUS") for f in e.data_flags)
    assert already.symbol == "SIGACHI" and not any("symbol_resolution" in f for f in already.data_flags)
    assert wrong.symbol == "OTHER" and any(f.startswith("symbol_resolution: CONFLICTING: stored OTHER") for f in wrong.data_flags)
    assert us.symbol == ""  # US rows are never touched

    # idempotent: second pass resolves nothing new and does not duplicate flags
    stats2 = resolve_india_symbols(db, masters=masters())
    assert stats2[RESOLVED_ISIN] == 0 and stats2[UNRESOLVED] == 1
    db.refresh(d)
    assert sum(1 for f in d.data_flags if f.startswith("symbol_resolution")) == 1


def test_fetch_masters_validates_hosts_and_parses(monkeypatch):
    calls = []

    class R:
        def __init__(self, text):
            self.text = text
        def raise_for_status(self):
            pass

    class C:
        def __init__(self, *a, **k):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def get(self, url):
            calls.append(url)
            return R(EQUITY_CSV if "content/equities" in url else SME_CSV)

    monkeypatch.setattr(nse_master.httpx, "Client", C)
    validated = []
    monkeypatch.setattr(nse_master, "validate_outbound_url", lambda url, allowed_hosts=None: validated.append((url, tuple(sorted(allowed_hosts)))))
    rows = nse_master.fetch_masters()
    assert {r.symbol for r in rows} == {"SIGACHI", "SENCO", "DUPA", "DUPB", "COOLCAPS", "AXIOMGAS"}
    assert calls == [EQUITY_MASTER_URL, SME_MASTER_URL]
    assert all(hosts == ("nsearchives.nseindia.com",) for _, hosts in validated)


def test_debt_isin_on_equity_row_resolves_to_issuer_equity_listing():
    from app.services.nse_master import MasterIndex, MasterRow
    rows = [MasterRow(symbol="TATACAP", name="Tata Capital Limited", isin="INE976I01018", series="EQ", board="Mainboard",
                      date_of_listing="13-OCT-2025", source_name="NSE equity master", source_url="https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv")]
    idx = MasterIndex(rows)
    m, why = idx.equity_for_non_equity_isin("INE976I07CV5", "Tata Capital Limited")
    assert m is not None and m.symbol == "TATACAP" and "instrument type 07" in why
    # an equity ISIN, a different issuer, or an ambiguous name never resolves this way
    assert idx.equity_for_non_equity_isin("INE976I01018", "Tata Capital Limited")[0] is None
    assert idx.equity_for_non_equity_isin("INE999X07AB1", "Tata Capital Limited")[0] is None
    assert idx.equity_for_non_equity_isin("INE976I07CV5", "Some Other Company")[0] is None
