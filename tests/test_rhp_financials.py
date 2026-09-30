"""India RHP restated financials: header layouts, units, stubs, peer tables,
point-in-time availability and the display-column fill."""
import re

from app.models import IPO, FeatureObservation, Provenance
from app.services import rhp_financials as rf
from app.services.model_eval import POINT_IN_TIME_RULES

AVAILABLE = "2026-08-27"

FISCAL_MILLION = """Summary of Restated Financial Information
(in Rs. million)
Particulars Fiscal 2026 Fiscal 2025 Fiscal 2024
Revenue from operations 4,448.78 3,785.26 2,748.10
Profit After Tax 710.67 625.55 409.19
"""

# Numeric dates, a note-number column and a "year / period" caption.
NUMERIC_DATES_LAKHS = """STATEMENT OF STANDALONE PROFIT AND LOSS AS RESTATED
(Rs. in Lakhs)
Particulars Note No For the year / period ended
31.03.2026 31.03.2025 31.03.2024
Income
Revenue From Operations II.1 10,075.82 8,983.83 7,453.80
Other Income II.2 1.90 1.00 0.03
Profit/(Loss) For The Period 945.35 774.94 574.08
"""

# Prose dates above the table must not be read as column labels.
PROSE_THEN_TABLE = """The KPIs were approved by our Audit Committee dated May 11, 2026 and certified
by the auditors by their certificate dated May 26, 2026 who hold a valid certificate issued by the
Peer Review Board of the ICAI, and have been included in Material Contracts and Documents for
Inspection of this Red Herring Prospectus, as described in the section on Basis for Offer Price.
( Rs. In Lakhs except Percentages)
Sr. No. Metrix As of and for the Fiscal
2026 2025 2024
1 Revenue From Operation ( Rs. in Lakhs) 11,708.69 6024.73 2888.02
7 Profit/(loss) after tax for the year ( Rs. in Lakhs) 1,509.70 581.11 47.78
"""

STUB_FIRST = """Restated Summary Statements (Rs. in million)
Particulars Six months ended September 30, 2025 March 31, 2025 March 31, 2024 March 31, 2023
Revenue from operations 900.00 1,700.00 1,500.00 1,200.00
Restated profit for the period/year 90.00 170.00 150.00 120.00
"""

PEER_CAPTION = """For Balaji Telefilms Ltd
(Consolidated) (INR in Lakhs except for percentages and ratios)
Particulars For the year ended March 31,
2026 2025 2024
Revenue from Operations (Rs. in Lakhs) 21083.45 45,308.92 62,512.59
Profit After Tax (Rs. in Lakhs) 607.38 828.26 822.27
"""

PEER_SIDE_BY_SIDE = """Comparison of our key performance indicators with our listed industry peers
(in Rs. million, except otherwise stated)
Particulars Bondada Engineering Limited Likhitha Infrastructure Limited
Fiscal 2026 Fiscal 2025 Fiscal 2024 Fiscal 2026 Fiscal 2025 Fiscal 2024
Revenue from operations 28,428.05 15,709.57 8,007.22 4,567.34 5,200.86 4,216.81
Profit After Tax 2,110.79 1,131.71 463.08 385.49 694.29 652.27
"""


def _doc(*pages):
    """The pages plus a later page repeating their figures, as the MD&A of a
    real RHP repeats the issuer's own numbers."""
    figures = " ".join(re.findall(r"\d[\d,]*\.\d+", " ".join(pages)))
    return [*pages, "Management discussion figures: " + figures]


def _values(obs):
    return {k: (round(v["value"], 2), v["period_end"]) for k, v in obs.items()}


def test_fiscal_labels_in_million():
    obs, why = rf.extract(_doc("cover page", FISCAL_MILLION), AVAILABLE)
    assert why == "page 2"
    assert _values(obs) == {"revenue_m": (4448.78, "2026-03-31"), "revenue_prev_m": (3785.26, "2025-03-31"),
                            "revenue_2y_ago_m": (2748.1, "2024-03-31"), "net_income_m": (710.67, "2026-03-31")}


def test_numeric_dates_note_numbers_and_lakhs():
    obs, _ = rf.extract(_doc(NUMERIC_DATES_LAKHS), AVAILABLE)
    # Lakhs are converted to millions; the note reference "II.1" is not an amount.
    assert _values(obs)["revenue_m"] == (1007.58, "2026-03-31")
    assert _values(obs)["revenue_2y_ago_m"] == (745.38, "2024-03-31")
    assert _values(obs)["net_income_m"] == (94.53, "2026-03-31")


def test_prose_dates_above_the_header_are_ignored():
    obs, _ = rf.extract(_doc(PROSE_THEN_TABLE), AVAILABLE)
    assert _values(obs)["revenue_m"] == (1170.87, "2026-03-31")
    assert _values(obs)["revenue_prev_m"] == (602.47, "2025-03-31")


def test_stub_period_is_never_used_as_a_fiscal_year():
    obs, _ = rf.extract(_doc(STUB_FIRST), AVAILABLE)
    assert _values(obs)["revenue_m"] == (1700.0, "2025-03-31")
    assert _values(obs)["net_income_m"] == (170.0, "2025-03-31")


def test_peer_tables_are_rejected_and_the_next_page_is_used():
    obs, why = rf.extract(_doc(PEER_CAPTION, PEER_SIDE_BY_SIDE), AVAILABLE)
    assert obs == {} and "peer comparison" in why
    obs, why = rf.extract(_doc(PEER_CAPTION, PEER_SIDE_BY_SIDE, FISCAL_MILLION), AVAILABLE)
    assert why == "page 3" and _values(obs)["revenue_m"] == (4448.78, "2026-03-31")


def test_figures_found_on_no_other_page_are_rejected():
    obs, why = rf.extract([FISCAL_MILLION, "unrelated text"], AVAILABLE)
    assert obs == {} and "not found elsewhere" in why


def test_part_year_column_between_fiscal_years_is_rejected():
    split = """(Rs. in lakhs)
Particulars November 30, 2025 March 31, 2025 March 31, 2024 December 20, 2023 March 31, 2023
Revenue from Operations 3,687.50 3,563.67 599.66 1,700.70 1,674.68
PAT 348.71 267.41 14.80 86.91 41.11
"""
    obs, why = rf.extract(_doc(split), AVAILABLE)
    assert obs == {} and "part-year" in why


def test_observations_are_point_in_time():
    obs, _ = rf.extract(_doc(FISCAL_MILLION), AVAILABLE)
    assert {o["available_at"] for o in obs.values()} == {AVAILABLE}
    assert {o["availability_rule"] for o in obs.values()} <= POINT_IN_TIME_RULES
    assert rf.extract(_doc(FISCAL_MILLION), "") == ({}, "no date to establish when the prospectus was public")


def test_availability_date_prefers_open_then_close_then_day_before_listing():
    ipo = IPO(open_date="2026-08-27", close_date="2026-08-29", listing_date="2026-09-03")
    assert rf.availability_date(ipo) == "2026-08-27"
    ipo.open_date = ""
    assert rf.availability_date(ipo) == "2026-08-29"
    ipo.close_date = ""
    assert rf.availability_date(ipo) == "2026-09-02"
    ipo.listing_date = ""
    assert rf.availability_date(ipo) == ""


def test_missing_unit_or_header_yields_nothing():
    no_unit = FISCAL_MILLION.replace("(in Rs. million)\n", "")
    assert rf.extract(_doc(no_unit), AVAILABLE)[0] == {}
    no_header = FISCAL_MILLION.replace("Particulars Fiscal 2026 Fiscal 2025 Fiscal 2024", "Particulars")
    assert rf.extract(_doc(no_header), AVAILABLE)[0] == {}


def test_pick_pdf_prefers_the_rhp_over_the_general_information_document():
    sizes = {"GID_X.pdf": 16_000_000, "RHP_X.pdf": 12_000_000, "readme.txt": 1}
    assert rf.pick_pdf(list(sizes), sizes.get) == "RHP_X.pdf"
    assert rf.pick_pdf(["readme.txt"], sizes.get) is None


def test_apply_fills_only_empty_display_columns(db):
    ipo = IPO(external_key="IN:ACME", company="Acme Ltd", country="India", status="Listed", symbol="ACME",
              net_income_m=700.0)
    db.add(ipo)
    db.flush()
    obs, _ = rf.extract(_doc(FISCAL_MILLION), AVAILABLE)
    out = rf.apply_observations(db, ipo, obs, rf.RHP_URL.format(symbol="ACME"))
    db.commit()
    assert out["observations"] == 4
    assert sorted(out["columns_filled"]) == ["revenue_2y_ago_m", "revenue_m", "revenue_prev_m"]
    assert ipo.revenue_m == 4448.78 and ipo.net_income_m == 700.0  # another source's value is kept
    assert db.query(FeatureObservation).filter_by(ipo_id=ipo.id, source_name=rf.SOURCE_NAME).count() == 4
    assert db.query(Provenance).filter_by(ipo_id=ipo.id, source_name=rf.PROVENANCE_SOURCE).count() == 3
    # Re-applying updates in place rather than duplicating.
    rf.apply_observations(db, ipo, obs, rf.RHP_URL.format(symbol="ACME"))
    db.commit()
    assert db.query(FeatureObservation).filter_by(ipo_id=ipo.id, source_name=rf.SOURCE_NAME).count() == 4
