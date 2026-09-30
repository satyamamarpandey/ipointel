"""US prospectus summary-table parser (A-001 production source)."""
from app.services import prospectus_financials as pf
from app.services import model_eval as me

HEADER = (
    "<SEC-HEADER>\nCONFORMED SUBMISSION TYPE:\t424B4\nFILED AS OF DATE:\t\t20240321\n"
    "STANDARD INDUSTRIAL CLASSIFICATION:\tSERVICES-PREPACKAGED SOFTWARE [7372]\n</SEC-HEADER>\n<DOCUMENT>\n"
)
TOC = "<table><tr><td>Summary Consolidated Financial Data</td><td>12</td></tr></table>"


def _doc(body: str, header: str = HEADER) -> str:
    return header + "<html><body>" + TOC + body + "</body></html></DOCUMENT>\n<DOCUMENT>exhibit</DOCUMENT>"


INTERIM_AND_ANNUAL = """
<p><b>SUMMARY CONSOLIDATED FINANCIAL DATA</b></p>
<p>The following tables present summary data (in thousands).</p>
<table>
<tr><td></td><td colspan="4">Six Months Ended<br>June 30,</td><td colspan="4">Year Ended<br>December 31,</td></tr>
<tr><td></td><td colspan="2">2023</td><td colspan="2">2022</td><td colspan="2">2022</td><td colspan="2">2021</td></tr>
<tr><td>Revenue</td><td>$</td><td>60,000</td><td>$</td><td>50,000</td><td>$</td><td>110,000</td><td>$</td><td>90,000</td></tr>
<tr><td>Net loss</td><td>$</td><td>(5,000</td><td>$</td><td>(4,000</td><td>$</td><td>(12,500</td><td>)</td><td>(9,000</td></tr>
<tr><td>Net cash provided by (used in) operating activities</td><td></td><td>1,000</td><td></td><td>900</td><td></td><td>(2,000</td><td>)</td><td>1,500</td></tr>
</table>
<p>RISK FACTORS</p>
"""


def test_annual_columns_only_scaled_and_signed():
    out = pf.extract(_doc(INTERIM_AND_ANNUAL))
    assert out["revenue_m"]["value"] == 110.0 and out["revenue_m"]["period_end"] == "2022-12-31"
    assert out["revenue_prev_m"]["value"] == 90.0
    assert out["net_income_m"]["value"] == -12.5
    assert out["cfo_m"]["value"] == -2.0
    for o in out.values():
        assert o["available_at"] == "2024-03-21" and o["availability_rule"] == pf.RULE_PROSPECTUS
        assert o["section"] == pf.SECTION_SUMMARY


ROWSPAN_MIXED_CURRENCY = """
<p>Summary Financial Information</p>
<p>For the periods ended June 30, 2024 and 2023 and the years ended December 31, 2023 and 2022</p>
<table>
<tr><td rowspan="2"></td><td colspan="4">June 30,</td><td colspan="4">December 31,</td></tr>
<tr><td colspan="2">2024</td><td colspan="2">2023</td><td colspan="2">2023</td><td colspan="2">2022</td></tr>
<tr><td></td><td>USD</td><td>AED</td><td>USD</td><td>AED</td><td>USD</td><td>AED</td><td>USD</td><td>AED</td></tr>
<tr><td>Revenue</td><td>1,000</td><td>3,670</td><td>900</td><td>3,300</td><td>2,500</td><td>9,175</td><td>2,000</td><td>7,340</td></tr>
</table>
"""


def test_rowspan_grid_currency_and_caption_periods():
    out = pf.extract(_doc(ROWSPAN_MIXED_CURRENCY))
    assert out["revenue_m"]["value"] == 0.0025  # USD column, annual (caption year-end date), not interim, not AED
    assert out["revenue_prev_m"]["value"] == 0.002


PRO_FORMA_AND_CAPTION_TOTAL = """
<p>SUMMARY HISTORICAL AND PRO FORMA FINANCIAL DATA</p>
<table>
<tr><td></td><td>Pro Forma</td><td colspan="2">Historical</td></tr>
<tr><td></td><td>Year Ended December 31, 2021</td><td colspan="2">Years Ended December 31,</td></tr>
<tr><td></td><td></td><td>2021</td><td>2020</td></tr>
<tr><td>(in millions)</td></tr>
<tr><td>Revenues</td></tr>
<tr><td>Product sales</td><td>3,737</td><td>3,737</td><td>3,381</td></tr>
<tr><td>Other revenues</td><td>28</td><td>28</td><td>31</td></tr>
<tr><td></td><td>3,765</td><td>3,765</td><td>3,412</td></tr>
<tr><td>Net income</td><td>190</td><td>193</td><td>150</td></tr>
<tr><td>Net cash provided by (used in):</td></tr>
<tr><td>Operating activities</td><td></td><td>873</td><td>522</td></tr>
</table>
"""


def test_pro_forma_excluded_caption_total_and_cfo_caption():
    out = pf.extract(_doc(PRO_FORMA_AND_CAPTION_TOTAL))
    assert out["revenue_m"]["value"] == 3765.0
    assert out["net_income_m"]["value"] == 193.0  # historical, not the 190 pro forma
    assert out["cfo_m"]["value"] == 873.0


STATEMENTS_ONLY = """
<p>Consolidated Statements of Operations</p>
<p>For the years ended December 31, 2022 and 2021</p>
<table>
<tr><td></td><td>2022</td><td>2021</td></tr>
<tr><td>Revenue (Note 2)</td><td>2,684,735</td><td>2,721,000</td></tr>
<tr><td>Net loss</td><td>(4,780,061)</td><td>(3,000,000)</td></tr>
</table>
"""


def test_statement_fallback_when_summary_is_absent():
    out = pf.extract(_doc(STATEMENTS_ONLY))
    assert abs(out["revenue_m"]["value"] - 2.684735) < 1e-9
    assert out["net_income_m"]["section"] == pf.SECTION_STATEMENTS


def test_non_usd_table_without_usd_column_yields_nothing():
    body = """<p>Summary Consolidated Financial Data</p>
<table><tr><td></td><td colspan="2">Year Ended December 31,</td></tr>
<tr><td></td><td>2022 RMB</td><td>2021 RMB</td></tr>
<tr><td>Revenues</td><td>720,000</td><td>415,000</td></tr></table>"""
    assert pf.extract(_doc(body)) == {}


def test_missing_filing_date_or_empty_document_yields_nothing():
    assert pf.extract("<html>no header</html>") == {}
    assert pf.extract(_doc("<p>nothing here</p>")) == {}


def test_blank_check_detected_from_sic():
    spac = HEADER.replace("SERVICES-PREPACKAGED SOFTWARE [7372]", "BLANK CHECKS [6770]")
    assert pf.is_blank_check(_doc("", spac)) is True
    assert pf.is_blank_check(_doc("")) is False


def test_only_first_document_is_read():
    doc = HEADER + "<p>x</p></DOCUMENT><DOCUMENT>" + INTERIM_AND_ANNUAL + "</DOCUMENT>"
    assert pf.extract(doc) == {}


def test_prospectus_rule_is_admitted_by_the_production_dataset():
    assert pf.RULE_PROSPECTUS in me.POINT_IN_TIME_RULES


def test_apply_observations_upserts_and_keeps_existing_columns(db):
    from app.models import IPO, FeatureObservation
    ipo = IPO(external_key="US:77", company="Acme", country="United States", status="Listed", listing_date="2024-03-21", revenue_m=1.0)
    db.add(ipo)
    db.flush()
    obs = pf.extract(_doc(INTERIM_AND_ANNUAL))
    r = pf.apply_observations(db, ipo, obs, "https://www.sec.gov/Archives/x.txt")
    pf.apply_observations(db, ipo, obs, "https://www.sec.gov/Archives/x.txt")
    db.commit()
    assert "revenue_m" not in r["columns_filled"] and ipo.revenue_m == 1.0
    assert ipo.net_income_m == -12.5
    assert db.query(FeatureObservation).filter_by(ipo_id=ipo.id).count() == len(obs)
