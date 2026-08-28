from app.models import IPO, Provenance
from app.services import redflags, contradictions, dcf, sensitivity

def weak_ipo():
    return IPO(external_key="rf1", company="WeakCo", country="India", currency="INR",
               price_low=100, price_high=90, final_price=95, post_issue_shares_m=50,
               revenue_m=800, revenue_prev_m=850, ebitda_m=-20, net_income_m=-30, cfo_m=-40,
               debt_m=500, cash_m=20, ofs_pct=85, fresh_issue_pct=15, dual_class=True,
               total_sub=0.6, qib_sub=0.4, filing_url="https://www.nseindia.com/x")

def strong_ipo():
    return IPO(external_key="rf2", company="StrongCo", country="India", currency="INR",
               price_low=95, price_high=100, final_price=100, post_issue_shares_m=100,
               revenue_m=2500, revenue_prev_m=1900, revenue_2y_ago_m=1500, ebitda_m=500,
               net_income_m=260, cfo_m=310, debt_m=150, cash_m=250, fresh_issue_pct=80,
               ofs_pct=20, promoter_retention_pct=70, qib_sub=65, nii_sub=30, retail_sub=12,
               total_sub=35, anchor_quality=4.5, market_regime=4, sector_regime=4,
               peer_median_pe=55, peer_median_ps=5, filing_url="https://www.nseindia.com/y")

def test_redflags_detects_negative_cfo_and_high_ofs():
    flags = redflags.evaluate(weak_ipo())
    codes = {f["code"] for f in flags}
    assert "neg_cfo" in codes
    assert "high_ofs" in codes
    summary = redflags.summarize(flags)
    assert summary["worst"] in ("CRITICAL", "HIGH")

def test_redflags_clean_ipo_has_few_flags():
    flags = redflags.evaluate(strong_ipo())
    codes = {f["code"] for f in flags}
    assert "neg_cfo" not in codes
    assert "high_ofs" not in codes

def test_contradiction_profit_vs_cash():
    ipo = weak_ipo()
    ipo.net_income_m = 30
    ipo.cfo_m = -40
    out = contradictions.cross_field(ipo)
    assert any(c["code"] == "profit_vs_cash" for c in out)

def test_contradiction_price_outside_band():
    ipo = weak_ipo()
    ipo.price_low, ipo.price_high, ipo.final_price = 90, 100, 150
    out = contradictions.cross_field(ipo)
    assert any(c["code"] == "price_outside_band" for c in out)

def test_contradiction_cross_source_disagreement():
    ipo = strong_ipo()
    ipo.id = 1
    provs = [
        Provenance(ipo_id=1, field_name="revenue_m", source_name="SEC EDGAR", source_url="https://sec.gov/a", observed_value="2500"),
        Provenance(ipo_id=1, field_name="revenue_m", source_name="Licensed enrichment feed", source_url="https://enrich/b", observed_value="1800"),
    ]
    out = contradictions.cross_source(provs)
    assert len(out) == 1
    assert out[0]["field"] == "revenue_m"

def test_scenario_dcf_requires_minimum_data():
    sparse = IPO(external_key="d1", company="Sparse", country="United States", price_high=20)
    out = dcf.scenario_dcf(sparse)
    assert out["available"] is False and "reason" in out

def test_scenario_dcf_runs_with_full_data():
    out = dcf.scenario_dcf(strong_ipo())
    assert out["available"] is True
    assert set(out["scenarios"]) == {"bear", "base", "bull"}
    assert out["scenarios"]["base"]["fair_value_per_share"] is not None

def test_reverse_dcf_produces_expectations_gap():
    ipo = strong_ipo()
    ipo.final_price = 400  # inflate price to force a demanding implied growth rate
    out = dcf.reverse_dcf(ipo)
    assert out["available"] is True
    assert out["expectations_gap"] in ("LOW", "MODERATE", "HIGH", "EXTREME")
    assert out["implied_revenue_cagr_pct"] is not None

def test_sensitivity_finds_thresholds_for_borderline_ipo():
    ipo = strong_ipo()
    ipo.final_price = 140  # push valuation to be more marginal so there's room to move
    out = sensitivity.analyze(ipo)
    assert "current_recommendation" in out
    assert isinstance(out["upgrade_conditions"], list)
    assert isinstance(out["downgrade_conditions"], list)
