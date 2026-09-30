"""scripts/backfill_prospectus_financials.py outcome labels and resumability."""
from app.models import IPO, FeatureObservation
from scripts import backfill_prospectus_financials as bp
from tests.test_prospectus_financials import HEADER, INTERIM_AND_ANNUAL, _doc


def _ipo(db, key, name):
    ipo = IPO(external_key=key, company=name, country="United States", status="Listed", listing_date="2024-03-21",
              filing_url=f"https://www.sec.gov/Archives/edgar/data/1/{key}.txt")
    db.add(ipo)
    db.flush()
    return ipo


def test_outcomes_and_flags(db):
    parsed = _ipo(db, "US-1", "Acme Robotics Inc")
    empty = _ipo(db, "US-2", "Beta Inc")
    spac_name = _ipo(db, "US-3", "Gamma Acquisition Corp")
    spac_sic = _ipo(db, "US-4", "Capital Partners V Ltd")
    docs = {parsed.filing_url: _doc(INTERIM_AND_ANNUAL), empty.filing_url: _doc("<p>no tables</p>"),
            spac_sic.filing_url: _doc("", HEADER.replace("SERVICES-PREPACKAGED SOFTWARE [7372]", "BLANK CHECKS [6770]"))}
    fetched = []

    def fetch(url, ua, max_bytes):
        fetched.append(url)
        return docs[url], False

    assert bp.process(db, parsed, fetch=fetch) == "parsed"
    assert bp.process(db, empty, fetch=fetch) == "empty"
    assert bp.process(db, spac_name, fetch=fetch) == "spac"
    assert bp.process(db, spac_sic, fetch=fetch) == "spac"
    db.commit()
    assert spac_name.filing_url not in fetched  # skipped without a download
    assert db.query(FeatureObservation).filter_by(ipo_id=parsed.id).count() >= 3
    assert db.query(FeatureObservation).filter_by(ipo_id=spac_sic.id).count() == 0
    assert bp.candidates(db, 10) == []  # every row flagged: nothing retried
