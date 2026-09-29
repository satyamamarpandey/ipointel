"""Phases 18-22: valuation degrades with named missing inputs, contradictions
carry a reason, similar-IPO matching penalises missingness and does not pad,
and every score movement is attributed."""
from datetime import datetime, timezone, timedelta
from app.models import IPO, ScoreSnapshot, PerformanceSnapshot
from app.services import dcf, contradictions, similarity, changes


def _ipo(**kw):
    base = dict(external_key=kw.pop("external_key", "US:t"), company="Test Co", country="United States", status="Filed")
    base.update(kw)
    return IPO(**base)


def test_dcf_reports_specific_missing_fields():
    r = dcf.scenario_dcf(_ipo(revenue_m=None, ebitda_m=None))
    assert r["available"] is False and r["label"] == "INSUFFICIENT DATA"
    assert r["missing_fields"] == ["revenue_m", "ebitda_m"]
    r = dcf.reverse_dcf(_ipo(revenue_m=100.0, post_issue_shares_m=None, final_price=10.0))
    assert r["available"] is False and r["missing_fields"] == ["post_issue_shares_m"]


def test_contradictions_carry_a_reason():
    ipo = _ipo(net_income_m=10.0, cfo_m=-5.0, price_low=10.0, price_high=12.0, final_price=15.0)
    items = contradictions.cross_field(ipo)
    codes = {i["code"] for i in items}
    assert {"profit_vs_cash", "price_outside_band"} <= codes
    assert all(i["reason"] for i in items)
    assert all(i["evidence_a"]["source"] and i["evidence_b"]["source"] for i in items)


def test_similarity_penalises_missing_dimensions_and_drops_non_comparables(db):
    target = _ipo(external_key="US:target", status="Filed", issue_size_m=100.0, revenue_m=200.0, revenue_prev_m=100.0,
                  ebitda_m=40.0, total_sub=None)
    close_full = _ipo(external_key="US:c1", status="Listed", issue_size_m=105.0, revenue_m=210.0, revenue_prev_m=100.0, ebitda_m=42.0)
    close_sparse = _ipo(external_key="US:c2", status="Listed", issue_size_m=100.0, revenue_m=None, ebitda_m=None)
    far = _ipo(external_key="US:c3", status="Listed", issue_size_m=5000.0, revenue_m=10.0, revenue_prev_m=100.0, ebitda_m=-50.0)
    fillers = [_ipo(external_key=f"US:f{i}", status="Listed", issue_size_m=100.0 + i, revenue_m=200.0 + i, revenue_prev_m=100.0, ebitda_m=40.0) for i in range(6)]
    db.add_all([target, close_full, close_sparse, far] + fillers)
    db.commit()
    r = similarity.find_similar(db, target, k=8)
    assert r["available"] is True
    ids = [m["ipo_id"] for m in r["matches"]]
    assert far.id not in ids  # beyond the comparable distance: dropped, not padded
    assert close_sparse.id not in ids  # only one dimension in common: not comparable
    assert all(m["match_strength"] in ("close", "moderate", "weak") for m in r["matches"])
    assert r["matches"][0]["dims_missing"] == 0


def test_changes_attribute_every_score_move(db):
    ipo = _ipo(external_key="US:chg", status="Filed")
    db.add(ipo)
    db.commit()
    t0 = datetime.now(timezone.utc) - timedelta(days=2)
    common = dict(listing_score=50.0, long_term_score=50.0, confidence=30.0, listing_gain_probability=20.0,
                  long_term_outperform_probability=20.0, recommendation="AVOID / WAIT", horizon="x", valuation_label="INSUFFICIENT DATA")
    db.add(ScoreSnapshot(ipo_id=ipo.id, overall_score=50.0, event_stage="ipo_discovered", model_version="v2.0",
                         feature_snapshot={"revenue_m": None}, pillars={"Growth": 50.0}, created_at=t0, **common))
    db.add(ScoreSnapshot(ipo_id=ipo.id, overall_score=55.0, event_stage="filing_amendment", model_version="v2.0",
                         feature_snapshot={"revenue_m": 100.0}, pillars={"Growth": 60.0}, created_at=t0 + timedelta(days=1), **common))
    db.add(ScoreSnapshot(ipo_id=ipo.id, overall_score=55.0, event_stage="material_score_change", model_version="v2.1",
                         feature_snapshot={"revenue_m": 100.0}, pillars={"Growth": 60.0}, created_at=t0 + timedelta(days=2), **common))
    db.commit()
    tl = changes.timeline(db, ipo.id)
    assert tl[0]["attribution"] == ["new event: ipo_discovered"]
    assert any(a.startswith("feature: revenue_m") for a in tl[1]["attribution"])
    assert "event: filing_amendment" in tl[1]["attribution"]
    assert any(a.startswith("model version: v2.0 -> v2.1") for a in tl[2]["attribution"])
    assert all(t["attribution"] for t in tl)


def test_similarity_unavailable_when_target_too_sparse(db):
    target = _ipo(external_key="US:sparse", issue_size_m=None, revenue_m=None)
    db.add(target)
    db.add(PerformanceSnapshot(ipo_id=1, as_of_date="2026-01-01"))
    db.commit()
    r = similarity.find_similar(db, target)
    assert r["available"] is False and r["matches"] == []
