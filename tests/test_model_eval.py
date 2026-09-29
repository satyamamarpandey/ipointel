"""Leakage-safe model research module (app/services/model_eval.py)."""
from datetime import date, timedelta
import random
from app.services import model_eval as me


def _rows(n=400, signal=True, seed=1):
    rnd = random.Random(seed)
    rows = []
    start = date(2019, 1, 1)
    for i in range(n):
        x = rnd.gauss(0, 1)
        z = 1.5 * x if signal else 0.0
        p = 1 / (1 + pow(2.718281828, -z))
        y = 1 if rnd.random() < p else 0
        rows.append(me.Row(ipo_id=i, country="X", listing_date=start + timedelta(days=i * 6),
                           features={"log_issue_size": x, "is_sme": float(i % 2), "log_revenue": None if i % 3 == 0 else x * 0.5},
                           y_listing=y, y_12m=y, y_12m_relative=y, heuristic_listing_prob=50.0, heuristic_long_prob=50.0))
    return rows


def test_walk_forward_recovers_signal_and_never_trains_on_future():
    rows = _rows(signal=True)
    wf = me.walk_forward(rows, ["log_issue_size", "is_sme", "log_revenue"], "listing")
    scored = [f for f in wf.folds if f.get("auc") is not None]
    assert scored, wf.folds
    for f in wf.folds:
        # training rows are strictly earlier years than the test year
        assert f["n_train"] == sum(1 for r in rows if r.listing_date.year < f["year"])
    m = me.metrics(wf.model_pairs)
    assert m["auc"] > 0.75
    base = me.metrics(wf.base_rate_pairs)
    assert m["brier"] < base["brier"]
    gate = me.release_gate(m, base, wf.folds)
    assert gate["passed"] is True


def test_walk_forward_no_signal_is_not_released():
    rows = _rows(signal=False, seed=7)
    wf = me.walk_forward(rows, ["log_issue_size", "is_sme", "log_revenue"], "listing")
    m = me.metrics(wf.model_pairs)
    base = me.metrics(wf.base_rate_pairs)
    assert 0.35 < m["auc"] < 0.65
    assert me.release_gate(m, base, wf.folds)["passed"] is False


def test_first_years_are_skipped_until_min_train():
    rows = _rows(n=100)
    wf = me.walk_forward(rows, ["log_issue_size"], "listing", min_train=60)
    assert wf.folds[0].get("skipped")


def test_missing_indicator_and_constant_feature_handling():
    train = [{"a": 1.0, "b": 5.0, "c": None}, {"a": 2.0, "b": 5.0, "c": 1.0}, {"a": 3.0, "b": 5.0, "c": 2.0},
             {"a": 4.0, "b": 5.0, "c": None}, {"a": 5.0, "b": 5.0, "c": 3.0}, {"a": 6.0, "b": 5.0, "c": 4.0},
             {"a": 7.0, "b": 5.0, "c": 6.0}]
    xtr, xte, design = me._standardize(train, [{"a": None, "b": 5.0, "c": None}], ["a", "b", "c"])
    assert "b" not in design  # constant in training carries no information
    assert "c__missing" in design and "a__missing" in design
    assert len(xtr[0]) == 1 + len(design)


def test_pr_auc_and_ece_bounds():
    pairs = [(90.0, 1), (80.0, 1), (20.0, 0), (10.0, 0)]
    assert me._pr_auc(pairs) == 1.0
    assert 0 <= me._ece(pairs) <= 1
    assert me._pr_auc([(50.0, 1), (50.0, 1)]) is None


def test_features_for_only_uses_observations_available_before_listing():
    class Obs:
        def __init__(self, value, period_end, available_at):
            self.value, self.period_end, self.available_at = value, period_end, available_at

    class Ipo:
        id = 1; issue_size_m = 100.0; final_price = 10.0; fresh_issue_pct = None; ofs_pct = None
        board = "Mainboard"; company = "Acme Acquisition Corp"; shares_offered_m = 5.0

    fin = {(1, "revenue_m"): [Obs(50.0, "2023-12-31", "2024-03-01"), Obs(80.0, "2024-12-31", "2025-03-01")]}
    f = me.features_for(Ipo(), fin, "2024-06-01")
    assert abs(f["log_revenue"] - me.math.log(50.0)) < 1e-9  # the 2024 figure was not yet available
    assert f["is_spac"] == 1.0
    assert f["revenue_growth_pct"] is None


def test_passing_research_gate_never_relabels_the_heuristic():
    from app.services import walkforward
    research = {"targets": {"listing": {"release_gate": {"passed": True}, "out_of_sample": {"auc": 0.7, "n": 500}}}}
    s = walkforward._semantics(research, "listing")
    assert s["label"] == "SCORE" and s["calibrated"] is False and s["research_gate_passed"] is True
    assert "not deployed" in s["reason"]
