from __future__ import annotations
"""'What would change my mind': perturbs the actual scoring inputs and bisects for the
smallest realistic change that flips the recommendation band. Every threshold reported
here is read directly off app.scoring.compute_score — never boilerplate text."""
from types import SimpleNamespace
from sqlalchemy import inspect as sa_inspect
from ..models import IPO
from ..scoring import compute_score

BANDS = ["INVEST — STRONG", "INVEST SELECTIVELY", "WATCH / SMALL ALLOCATION", "AVOID / WAIT", "INSUFFICIENT RELIABLE DATA — NO RECOMMENDATION"]

def _shadow(ipo: IPO):
    columns = sa_inspect(IPO).mapper.columns.keys()
    data = {k: getattr(ipo, k, None) for k in columns}
    return SimpleNamespace(**data)

def _rank(rec: str) -> int:
    return BANDS.index(rec) if rec in BANDS else len(BANDS)

def _recommendation_at(ipo: IPO, field: str, value) -> str:
    shadow = _shadow(ipo)
    setattr(shadow, field, value)
    return compute_score(shadow)["recommendation"]

def _bisect_better(ipo: IPO, field: str, current: float, lo: float, hi: float, base_rank: int, want_better: bool, is_int: bool = False) -> float | None:
    """Search [lo,hi] (monotonic-ish improvement direction assumed) for the boundary
    value where recommendation rank first improves (want_better=True) or worsens."""
    end_rank = _rank(_recommendation_at(ipo, field, hi if want_better else lo))
    start_rank = _rank(_recommendation_at(ipo, field, lo if want_better else hi))
    target_hits = (end_rank < base_rank) if want_better else (start_rank > base_rank)
    if not target_hits:
        return None
    a, b = (lo, hi) if want_better else (hi, lo)
    for _ in range(24):
        mid = (a + b) / 2
        r = _rank(_recommendation_at(ipo, field, round(mid) if is_int else mid))
        improved = r < base_rank if want_better else r > base_rank
        if improved:
            b = mid
        else:
            a = mid
    return round(b, 2)

def analyze(ipo: IPO) -> dict:
    base_score = compute_score(ipo)
    base_rank = _rank(base_score["recommendation"])
    upgrades, downgrades = [], []

    px = ipo.final_price or ipo.price_high
    if px:
        cheaper = _bisect_better(ipo, "final_price", px, px * 0.5, px, base_rank, want_better=True)
        if cheaper is not None:
            drop_pct = (1 - cheaper / px) * 100
            if drop_pct > 0.5:
                upgrades.append({"lever": "Valuation", "detail": f"IPO price falls by roughly {drop_pct:.0f}% (to ~{cheaper:.2f} from {px:.2f})", "field": "final_price"})
        pricier = _bisect_better(ipo, "final_price", px, px, px * 1.8, base_rank, want_better=False)
        if pricier is not None:
            rise_pct = (pricier / px - 1) * 100
            if rise_pct > 0.5:
                downgrades.append({"lever": "Valuation", "detail": f"IPO price rises by roughly {rise_pct:.0f}% (to ~{pricier:.2f})", "field": "final_price"})

    if ipo.country.lower() == "india":
        cur_qib = ipo.qib_sub or 0.5
        better_qib = _bisect_better(ipo, "qib_sub", cur_qib, cur_qib, 100, base_rank, want_better=True)
        if better_qib is not None and better_qib > cur_qib + 0.5:
            upgrades.append({"lever": "QIB demand", "detail": f"QIB subscription exceeds ~{better_qib:.1f}x (currently {cur_qib:.1f}x)", "field": "qib_sub"})
        worse_qib = _bisect_better(ipo, "qib_sub", cur_qib, 0, cur_qib, base_rank, want_better=False)
        if worse_qib is not None and worse_qib < cur_qib - 0.2:
            downgrades.append({"lever": "QIB demand", "detail": f"QIB subscription falls below ~{worse_qib:.1f}x (currently {cur_qib:.1f}x)", "field": "qib_sub"})
    else:
        cur_uw = ipo.underwriter_quality if ipo.underwriter_quality is not None else 2.5
        better_uw = _bisect_better(ipo, "underwriter_quality", cur_uw, cur_uw, 5, base_rank, want_better=True)
        if better_uw is not None and better_uw > cur_uw + 0.3:
            upgrades.append({"lever": "Underwriter quality", "detail": f"Lead underwriter quality score improves to ~{better_uw:.1f}/5 (currently {cur_uw:.1f}/5)", "field": "underwriter_quality"})

    cur_regime = ipo.market_regime if ipo.market_regime is not None else 2.5
    better_regime = _bisect_better(ipo, "market_regime", cur_regime, cur_regime, 5, base_rank, want_better=True)
    if better_regime is not None and better_regime > cur_regime + 0.3:
        upgrades.append({"lever": "Market conditions", "detail": f"Broader market regime improves to ~{better_regime:.1f}/5 (currently {cur_regime:.1f}/5)", "field": "market_regime"})
    worse_regime = _bisect_better(ipo, "market_regime", cur_regime, 0, cur_regime, base_rank, want_better=False)
    if worse_regime is not None and worse_regime < cur_regime - 0.3:
        downgrades.append({"lever": "Market conditions", "detail": f"Broader market regime deteriorates to ~{worse_regime:.1f}/5 (currently {cur_regime:.1f}/5)", "field": "market_regime"})

    if ipo.ofs_pct is not None:
        worse_ofs = _bisect_better(ipo, "ofs_pct", ipo.ofs_pct, ipo.ofs_pct, 100, base_rank, want_better=False)
        if worse_ofs is not None and worse_ofs > ipo.ofs_pct + 3:
            downgrades.append({"lever": "Issue structure", "detail": f"OFS share of the issue rises above ~{worse_ofs:.0f}% (currently {ipo.ofs_pct:.0f}%)", "field": "ofs_pct"})

    if ipo.cfo_m is not None and ipo.revenue_m:
        cur_cfo = ipo.cfo_m
        better_cfo = _bisect_better(ipo, "cfo_m", cur_cfo, cur_cfo, ipo.revenue_m * 0.4, base_rank, want_better=True)
        if better_cfo is not None and better_cfo > cur_cfo * 1.15:
            upgrades.append({"lever": "Cash conversion", "detail": f"Verified operating cash flow rises to ~{better_cfo:.1f}m (currently {cur_cfo:.1f}m)", "field": "cfo_m"})

    return {
        "current_recommendation": base_score["recommendation"], "current_overall_score": base_score["overall_score"],
        "current_confidence": base_score["confidence"],
        "upgrade_conditions": upgrades[:5], "downgrade_conditions": downgrades[:5],
        "note": "Thresholds are computed by re-running the live scoring model with one input perturbed at a time, holding all else constant; real-world moves rarely happen in isolation." if (upgrades or downgrades) else "No single-lever perturbation within realistic bounds changes the recommendation band — the current verdict is not close to a threshold.",
    }
