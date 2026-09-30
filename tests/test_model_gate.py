"""Point-in-time dataset rules (A-001) and the tiered release gate (A-010)."""
import random
from app.services import model_eval as me
from app.services.walkforward import _auc, _semantics


class Obs:
    def __init__(self, value, period_end, available_at, rule):
        self.value, self.period_end, self.available_at, self.availability_rule = value, period_end, available_at, rule


class Ipo:
    id = 1; issue_size_m = 100.0; final_price = 10.0; fresh_issue_pct = None; ofs_pct = None
    board = "Mainboard"; company = "Acme Robotics Inc"; shares_offered_m = 5.0


def test_post_ipo_comparatives_never_enter_the_production_dataset():
    # Comparative describes FY2023 but was published in the first 10-K after listing.
    fin = {(1, "revenue_m"): [Obs(50.0, "2023-12-31", "2024-11-01", "xbrl_post_ipo_comparative")]}
    prod = me.features_for(Ipo(), fin, "2024-06-01", me.DATASET_PRODUCTION)
    research = me.features_for(Ipo(), fin, "2024-06-01", me.DATASET_RESEARCH)
    assert prod["log_revenue"] is None
    assert research["log_revenue"] is not None


def test_prospectus_values_enter_only_after_their_filing_date():
    fin = {(1, "revenue_m"): [Obs(40.0, "2023-12-31", "2024-05-20", "prospectus_filing")]}
    assert me.features_for(Ipo(), fin, "2024-06-01")["log_revenue"] is not None
    assert me.features_for(Ipo(), fin, "2024-05-19")["log_revenue"] is None


def test_unknown_availability_rules_fail_closed():
    fin = {(1, "revenue_m"): [Obs(40.0, "2023-12-31", "2024-01-01", "some_new_rule")]}
    assert me.features_for(Ipo(), fin, "2024-06-01")["log_revenue"] is None
    assert me.features_for(Ipo(), fin, "2024-06-01", me.DATASET_RESEARCH)["log_revenue"] is None


def test_rank_auc_matches_pairwise_auc():
    rnd = random.Random(3)
    pairs = [(round(rnd.random() * 10), rnd.randint(0, 1)) for _ in range(150)]
    assert abs(me._rank_auc(pairs) - _auc(pairs)) < 1e-12


def _good():
    model = {"n": 400, "auc": 0.66, "brier": 0.20, "ece": 0.03}
    base = {"n": 400, "brier": 0.24}
    folds = [{"auc": 0.6}, {"auc": 0.62}, {"auc": 0.7}, {"auc": 0.65}]
    return model, base, folds


def test_research_dataset_can_never_pass_production():
    m, b, f = _good()
    g = me.release_gate(m, b, f, leakage_free=False, forward_graded=100)
    assert g["screening_passed"] is True
    assert g["passed"] is False and g["checks"]["no_known_leakage"] is False
    assert g["probability_allowed"] is False


def test_probability_needs_fifty_graded_forward_outcomes_and_calibration():
    m, b, f = _good()
    assert me.release_gate(m, b, f, forward_graded=49)["probability_allowed"] is False
    assert me.release_gate(m, b, f, forward_graded=50)["probability_allowed"] is True
    m_bad = {**m, "ece": 0.09}
    g = me.release_gate(m_bad, b, f, forward_graded=500)
    assert g["passed"] is True and g["probability_allowed"] is False


def test_thin_signal_passes_screening_but_not_production():
    # Roughly the US operating-company listing result from RC-001.
    model = {"n": 450, "auc": 0.595, "brier": 0.2410, "ece": 0.04}
    base = {"n": 450, "brier": 0.2488}
    folds = [{"auc": 0.6}, {"auc": 0.55}, {"auc": 0.48}, {"auc": 0.62}]
    g = me.release_gate(model, base, folds, auc_ci=(0.54, 0.65))
    assert g["screening_passed"] is True
    assert g["checks"]["discrimination_ok"] is True  # interval above 0.5
    assert g["checks"]["brier_gain_ok"] is True       # 3.1% gain
    assert g["checks"]["fold_stability_ok"] is True   # 75%
    g2 = me.release_gate({**model, "brier": 0.2460}, base, folds, auc_ci=(0.49, 0.65))
    assert g2["passed"] is False


def test_segment_instability_blocks_production():
    m, b, f = _good()
    assert me.release_gate(m, b, f, segment_stable=False)["passed"] is False


def test_semantics_stay_score_without_probability_tier():
    research = {"targets": {"listing": {"release_gate": {"passed": True, "probability_allowed": False}, "out_of_sample": {"auc": 0.7, "n": 500}}}}
    s = _semantics(research, "listing")
    assert s["label"] == "SCORE" and s["calibrated"] is False


def test_spac_composition_gap_fails_segment_stability():
    # RC-002: US 12m absolute full-market AUC 0.826 vs operating companies 0.686.
    assert me.segment_stable(0.826, 0.686) is False
    assert me.segment_stable(0.63, 0.60) is True
    assert me.segment_stable(0.63, 0.49) is False


def test_too_few_scored_folds_fail_production():
    m, b, _ = _good()
    assert me.release_gate(m, b, [{"auc": 0.7}, {"auc": 0.7}])["checks"]["fold_stability_ok"] is False
