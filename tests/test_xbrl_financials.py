"""Pre-IPO XBRL comparatives: selection rules and availability timestamps."""
from app.models import IPO, FeatureObservation, Provenance
from app.services.xbrl_financials import (
    extract_pre_ipo_financials, apply_observations, RULE_COMPARATIVE, RULE_FIRST_REPORT, SOURCE_NAME,
)

LISTING = "2024-03-21"


def _fact(val, start, end, filed, form="10-K", accn="acc-1"):
    d = {"val": val, "end": end, "filed": filed, "form": form, "accn": accn, "fy": int(end[:4]), "fp": "FY"}
    if start:
        d["start"] = start
    return d


def _facts(us_gaap: dict, dei: dict | None = None) -> dict:
    facts = {"facts": {"us-gaap": {k: {"units": {"USD": v}} for k, v in us_gaap.items()}}}
    if dei:
        facts["facts"]["dei"] = {k: {"units": {"shares": v}} for k, v in dei.items()}
    return facts


def test_annual_facts_selected_and_ordered_into_current_prev_2y():
    facts = _facts({
        "Revenues": [
            _fact(300e6, "2023-01-01", "2023-12-31", "2025-02-20"),   # FY2023: latest year before listing
            _fact(200e6, "2022-01-01", "2022-12-31", "2025-02-20"),   # FY2022
            _fact(100e6, "2021-01-01", "2021-12-31", "2025-02-20"),   # FY2021
            _fact(90e6, "2023-10-01", "2023-12-31", "2024-05-10", form="10-Q"),  # quarterly: ignored
        ],
    })
    out = extract_pre_ipo_financials(facts, LISTING)
    assert out["revenue_m"]["value"] == 300.0
    assert out["revenue_prev_m"]["value"] == 200.0
    assert out["revenue_2y_ago_m"]["value"] == 100.0
    assert out["revenue_m"]["period_end"] == "2023-12-31"
    assert out["revenue_m"]["available_at"] == LISTING
    assert out["revenue_m"]["availability_rule"] == RULE_COMPARATIVE
    assert out["revenue_m"]["confidence"] < 1.0


def test_earliest_filed_fact_wins_over_restatement():
    facts = _facts({"NetIncomeLoss": [
        _fact(-50e6, "2023-01-01", "2023-12-31", "2025-02-20", accn="restated"),
        _fact(-40e6, "2023-01-01", "2023-12-31", "2024-05-10", form="10-Q", accn="first"),
    ]})
    # Both are annual-duration facts; the one filed first is the prospectus figure.
    out = extract_pre_ipo_financials(facts, LISTING)
    assert out["net_income_m"]["value"] == -40.0
    assert out["net_income_m"]["accn"] == "first"


def test_periods_ending_after_listing_are_excluded():
    facts = _facts({"Revenues": [
        _fact(500e6, "2024-01-01", "2024-12-31", "2025-02-20"),  # ends after listing: future information
        _fact(300e6, "2023-01-01", "2023-12-31", "2025-02-20"),
    ]})
    out = extract_pre_ipo_financials(facts, LISTING)
    assert out["revenue_m"]["value"] == 300.0
    assert "revenue_prev_m" not in out


def test_facts_filed_years_later_are_excluded():
    facts = _facts({"Revenues": [_fact(300e6, "2023-01-01", "2023-12-31", "2027-06-01")]})
    assert extract_pre_ipo_financials(facts, LISTING) == {}


def test_debt_sums_current_and_noncurrent_when_no_total_exists():
    facts = _facts({
        "Revenues": [_fact(300e6, "2023-01-01", "2023-12-31", "2025-02-20")],
        "LongTermDebtCurrent": [_fact(10e6, None, "2023-12-31", "2025-02-20")],
        "LongTermDebtNoncurrent": [_fact(90e6, None, "2023-12-31", "2025-02-20")],
        "CashAndCashEquivalentsAtCarryingValue": [_fact(25e6, None, "2023-12-31", "2025-02-20")],
    })
    out = extract_pre_ipo_financials(facts, LISTING)
    assert out["debt_m"]["value"] == 100.0
    assert out["cash_m"]["value"] == 25.0
    assert out["debt_m"]["period_end"] == "2023-12-31"


def test_ebitda_only_when_operating_income_and_da_share_a_fiscal_year():
    facts = _facts({
        "OperatingIncomeLoss": [_fact(40e6, "2023-01-01", "2023-12-31", "2025-02-20")],
        "DepreciationDepletionAndAmortization": [_fact(10e6, "2022-01-01", "2022-12-31", "2025-02-20")],
    })
    assert "ebitda_m" not in extract_pre_ipo_financials(facts, LISTING)
    facts["facts"]["us-gaap"]["DepreciationDepletionAndAmortization"]["units"]["USD"].append(_fact(12e6, "2023-01-01", "2023-12-31", "2025-02-20"))
    assert extract_pre_ipo_financials(facts, LISTING)["ebitda_m"]["value"] == 52.0


def test_shares_outstanding_dated_by_first_periodic_report():
    facts = _facts({}, dei={"EntityCommonStockSharesOutstanding": [
        _fact(60e6, None, "2024-06-30", "2024-08-09", form="10-Q"),
        _fact(55e6, None, "2024-03-31", "2024-05-10", form="10-Q"),
    ]})
    out = extract_pre_ipo_financials(facts, LISTING)
    assert out["post_issue_shares_m"]["value"] == 55.0
    assert out["post_issue_shares_m"]["available_at"] == "2024-05-10"
    assert out["post_issue_shares_m"]["availability_rule"] == RULE_FIRST_REPORT


def test_unparseable_listing_date_or_empty_facts_return_empty():
    assert extract_pre_ipo_financials({}, LISTING) == {}
    assert extract_pre_ipo_financials(_facts({}), "") == {}
    assert extract_pre_ipo_financials(_facts({"Revenues": [_fact(1e6, "2023-01-01", "2023-12-31", "2025-02-20")]}), "20240321")["revenue_m"]["value"] == 1.0


def test_apply_observations_fills_only_missing_columns_and_records_provenance(db):
    ipo = IPO(external_key="US:1", company="Acme", country="United States", status="Listed", listing_date="20240321", revenue_m=999.0)
    db.add(ipo)
    db.flush()
    facts = _facts({
        "Revenues": [_fact(300e6, "2023-01-01", "2023-12-31", "2025-02-20")],
        "NetIncomeLoss": [_fact(-40e6, "2023-01-01", "2023-12-31", "2025-02-20")],
    })
    obs = extract_pre_ipo_financials(facts, ipo.listing_date)
    r = apply_observations(db, ipo, obs, "https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json")
    db.commit()
    assert r["observations"] == 2
    assert r["columns_filled"] == ["net_income_m"]  # revenue_m already had a value: never overwritten
    assert ipo.revenue_m == 999.0 and ipo.net_income_m == -40.0
    rows = db.query(FeatureObservation).filter_by(ipo_id=ipo.id).all()
    assert {x.field_name for x in rows} == {"revenue_m", "net_income_m"}
    assert all(x.available_at == "2024-03-21" and x.source_name == SOURCE_NAME and x.source_tier == 1 for x in rows)
    prov = db.query(Provenance).filter_by(ipo_id=ipo.id, field_name="net_income_m").one()
    assert "post-IPO comparative" in prov.source_name
    # Re-applying is idempotent (unique key upsert).
    r2 = apply_observations(db, ipo, obs, "https://data.sec.gov/x")
    db.commit()
    assert r2["observations"] == 2 and r2["columns_filled"] == []
    assert db.query(FeatureObservation).filter_by(ipo_id=ipo.id).count() == 2
