from __future__ import annotations
"""Leakage-safe model research: dataset construction, trivial baselines, a
walk-forward logistic model and calibration metrics, per market.

Rules enforced here (Phases 12-16 of the production plan):
- A row's features may only use values whose availability date is on or
  before the row's listing date (FeatureObservation.available_at) or that are
  structural properties of the issue itself (board, price, size, structure).
- Evaluation is walk-forward by listing year: every out-of-sample prediction
  for year Y comes from a model fitted on rows listed strictly before Y.
  Nothing is shuffled.
- Baselines are computed on exactly the same test rows as the model.
- The release gate is per market and per target and is decided by numbers,
  never by assertion (see release_gate()).

No numpy/sklearn dependency: dimensions are small (about a dozen features,
a few thousand rows), so Newton's method in pure Python is fast enough.
"""
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from statistics import mean
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import IPO, FeatureObservation
from .market import parse_date
from .walkforward import _earliest_scores_by_ipo, _latest_perf_by_ipo, _auc, _brier, _log_loss

MIN_TRAIN = 60
MIN_TEST_TOTAL = 100
GATE_AUC = 0.58
GATE_FOLD_STABILITY = 0.6  # share of folds with AUC > 0.5
_SPAC = re.compile(r"\bacquisition\b|\bblank check\b|\bspac\b", re.I)

STRUCTURAL_FEATURES = ["log_issue_size", "log_offer_price", "fresh_issue_pct", "ofs_pct", "is_sme", "is_spac", "log_shares_offered"]
FINANCIAL_FEATURES = ["log_revenue", "revenue_growth_pct", "net_margin_pct", "cfo_margin_pct"]
ALL_FEATURES = STRUCTURAL_FEATURES + FINANCIAL_FEATURES

AVAILABILITY_RULES = {
    "structural": "properties of the issue printed in the prospectus/report before listing (size, price, structure, board)",
    "is_spac": "derived from the issuer name at ingestion",
    "financial": "FeatureObservation rows with available_at <= listing_date only",
    "market_regime": "benchmark index return over the 60 calendar days ending the day before listing (not yet in the dataset)",
}


@dataclass(frozen=True)
class Row:
    ipo_id: int
    country: str
    listing_date: date
    features: dict
    y_listing: int | None
    y_12m: int | None
    y_12m_relative: int | None  # 12m return minus the benchmark index over the same window > 0
    heuristic_listing_prob: float | None
    heuristic_long_prob: float | None


def _log(v: float | None) -> float | None:
    return math.log(v) if v is not None and v > 0 else None


def _financials_available(db: Session, ipo_ids: list[int]) -> dict[tuple[int, str], list[FeatureObservation]]:
    out: dict[tuple[int, str], list[FeatureObservation]] = {}
    if not ipo_ids:
        return out
    for o in db.scalars(select(FeatureObservation).where(FeatureObservation.ipo_id.in_(ipo_ids))).all():
        out.setdefault((o.ipo_id, o.field_name), []).append(o)
    return out


def _latest_available(obs: list[FeatureObservation] | None, as_of: str) -> float | None:
    """Latest period_end observation whose available_at <= as_of."""
    if not obs:
        return None
    ok = [o for o in obs if o.available_at and o.available_at <= as_of and o.value is not None]
    if not ok:
        return None
    return max(ok, key=lambda o: (o.period_end, o.available_at)).value


def features_for(ipo: IPO, fin: dict[tuple[int, str], list[FeatureObservation]], as_of: str) -> dict:
    rev = _latest_available(fin.get((ipo.id, "revenue_m")), as_of)
    rev_prev = _latest_available(fin.get((ipo.id, "revenue_prev_m")), as_of)
    ni = _latest_available(fin.get((ipo.id, "net_income_m")), as_of)
    cfo = _latest_available(fin.get((ipo.id, "cfo_m")), as_of)
    return {
        "log_issue_size": _log(ipo.issue_size_m),
        "log_offer_price": _log(ipo.final_price),
        "fresh_issue_pct": ipo.fresh_issue_pct,
        "ofs_pct": ipo.ofs_pct,
        "is_sme": 1.0 if (ipo.board or "") == "SME" else 0.0,
        "is_spac": 1.0 if _SPAC.search(ipo.company or "") else 0.0,
        "log_shares_offered": _log(ipo.shares_offered_m),
        "log_revenue": _log(rev),
        "revenue_growth_pct": (rev / rev_prev - 1) * 100 if rev is not None and rev_prev else None,
        "net_margin_pct": ni / rev * 100 if ni is not None and rev else None,
        "cfo_margin_pct": cfo / rev * 100 if cfo is not None and rev else None,
    }


def _relative_target(pf) -> int | None:
    """Benchmark-relative 12m outcome. Prefers the stored relative column;
    falls back to return_12m minus the stored 12m index return when both
    exist. A SPAC unit parked at trust value earns a small absolute return
    while trailing the index, which is why this is the honest long-term target."""
    rel = getattr(pf, "benchmark_relative_12m_pct", None)
    if rel is None and pf.return_12m_pct is not None and pf.benchmark_return_pct is not None:
        rel = pf.return_12m_pct - pf.benchmark_return_pct
    return None if rel is None else int(rel > 0)


def build_rows(db: Session, country: str) -> list[Row]:
    ipos = db.scalars(select(IPO).where(IPO.country == country, IPO.status == "Listed")).all()
    ids = [i.id for i in ipos]
    perfs = _latest_perf_by_ipo(db, ids)
    scores = _earliest_scores_by_ipo(db, ids)
    fin = _financials_available(db, ids)
    rows = []
    for ipo in ipos:
        ld = parse_date(ipo.listing_date)
        pf = perfs.get(ipo.id)
        if ld is None or pf is None:
            continue
        as_of = ld.date().isoformat()
        sc = scores.get(ipo.id)
        rows.append(Row(
            ipo_id=ipo.id, country=country, listing_date=ld.date(), features=features_for(ipo, fin, as_of),
            y_listing=None if pf.listing_return_pct is None else int(pf.listing_return_pct > 0),
            y_12m=None if pf.return_12m_pct is None else int(pf.return_12m_pct > 0),
            y_12m_relative=_relative_target(pf),
            heuristic_listing_prob=sc.listing_gain_probability if sc else None,
            heuristic_long_prob=sc.long_term_outperform_probability if sc else None,
        ))
    rows.sort(key=lambda r: r.listing_date)
    return rows


# ------------------------------------------------------------ numerics ----
def _standardize(train: list[dict], test: list[dict], names: list[str]) -> tuple[list[list[float]], list[list[float]], list[str]]:
    cols = []
    stats = {}
    for n in names:
        vals = [r[n] for r in train if r.get(n) is not None]
        if len(vals) < 5 or len(set(vals)) < 2:
            continue  # constant or absent in training: carries no information
        m = mean(vals)
        sd = (sum((v - m) ** 2 for v in vals) / len(vals)) ** 0.5 or 1.0
        stats[n] = (m, sd)
        cols.append(n)
    has_missing = [n for n in cols if any(r.get(n) is None for r in train + test)]
    design = cols + [f"{n}__missing" for n in has_missing]

    def enc(r: dict) -> list[float]:
        x = [1.0]
        for n in cols:
            v = r.get(n)
            m, sd = stats[n]
            x.append(0.0 if v is None else (v - m) / sd)
        for n in has_missing:
            x.append(1.0 if r.get(n) is None else 0.0)
        return x
    return [enc(r) for r in train], [enc(r) for r in test], design


def _solve(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[p] = m[p], m[c]
        if abs(m[c][c]) < 1e-12:
            m[c][c] = 1e-12
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                if f:
                    m[r] = [x - f * y for x, y in zip(m[r], m[c], strict=True)]
    return [m[i][n] / m[i][i] for i in range(n)]


def fit_logistic(x: list[list[float]], y: list[int], l2: float = 1.0, iters: int = 25) -> list[float]:
    """Ridge-penalised logistic regression by Newton's method. The intercept
    (column 0) is not penalised."""
    d = len(x[0])
    w = [0.0] * d
    for _ in range(iters):
        g = [0.0] * d
        h = [[0.0] * d for _ in range(d)]
        for xi, yi in zip(x, y, strict=True):
            z = sum(a * b for a, b in zip(w, xi, strict=True))
            p = 1 / (1 + math.exp(-max(-30, min(30, z))))
            r = p - yi
            s = p * (1 - p)
            for j in range(d):
                g[j] += r * xi[j]
                for k in range(d):
                    h[j][k] += s * xi[j] * xi[k]
        for j in range(1, d):
            g[j] += l2 * w[j]
            h[j][j] += l2
        step = _solve(h, g)
        w = [wj - sj for wj, sj in zip(w, step, strict=True)]
        if max(abs(s) for s in step) < 1e-6:
            break
    return w


def predict(w: list[float], x: list[list[float]]) -> list[float]:
    return [100 / (1 + math.exp(-max(-30, min(30, sum(a * b for a, b in zip(w, xi, strict=True)))))) for xi in x]


# ------------------------------------------------------------- metrics ----
def _pr_auc(pairs: list[tuple[float, int]]) -> float | None:
    pos = sum(y for _, y in pairs)
    if pos == 0 or pos == len(pairs):
        return None
    ranked = sorted(pairs, key=lambda t: -t[0])
    tp = 0
    area = 0.0
    prev_recall = 0.0
    for i, (_, y) in enumerate(ranked, start=1):
        if y == 1:
            tp += 1
            recall = tp / pos
            area += (recall - prev_recall) * (tp / i)
            prev_recall = recall
    return area


def _ece(pairs: list[tuple[float, int]], bins: int = 10) -> float | None:
    if not pairs:
        return None
    total = 0.0
    for i in range(bins):
        lo, hi = i * 100 / bins, (i + 1) * 100 / bins
        chunk = [(p, y) for p, y in pairs if lo <= p < hi or (i == bins - 1 and p == 100)]
        if chunk:
            total += len(chunk) / len(pairs) * abs(mean(p for p, _ in chunk) / 100 - mean(y for _, y in chunk))
    return total


def _calibration_table(pairs: list[tuple[float, int]], bins: int = 5) -> list[dict]:
    out = []
    for i in range(bins):
        lo, hi = i * 100 / bins, (i + 1) * 100 / bins
        chunk = [(p, y) for p, y in pairs if lo <= p < hi or (i == bins - 1 and p == 100)]
        if chunk:
            out.append({"predicted_range": f"{lo:.0f}-{hi:.0f}%", "n": len(chunk),
                        "avg_predicted_pct": round(mean(p for p, _ in chunk), 1),
                        "actual_positive_pct": round(mean(y for _, y in chunk) * 100, 1)})
    return out


def metrics(pairs: list[tuple[float, int]]) -> dict:
    if not pairs:
        return {"n": 0}
    auc = _auc(pairs)
    return {"n": len(pairs), "positive_rate_pct": round(mean(y for _, y in pairs) * 100, 1),
            "auc": round(auc, 3) if auc is not None else None,
            "pr_auc": round(_pr_auc(pairs), 3) if _pr_auc(pairs) is not None else None,
            "brier": round(_brier(pairs), 4), "log_loss": round(_log_loss(pairs), 4),
            "ece": round(_ece(pairs), 4), "calibration": _calibration_table(pairs)}


# --------------------------------------------------------- walk-forward ----
@dataclass
class WalkForwardResult:
    target: str
    feature_names: list[str]
    folds: list[dict] = field(default_factory=list)
    model_pairs: list[tuple[float, int]] = field(default_factory=list)
    base_rate_pairs: list[tuple[float, int]] = field(default_factory=list)
    constant_pairs: list[tuple[float, int]] = field(default_factory=list)
    heuristic_pairs: list[tuple[float, int]] = field(default_factory=list)


TARGET_ATTRS = {"listing": "y_listing", "12m": "y_12m", "12m_relative": "y_12m_relative"}


def walk_forward(rows: list[Row], feature_names: list[str], target: str, min_train: int = MIN_TRAIN) -> WalkForwardResult:
    y_attr = TARGET_ATTRS[target]
    h_attr = "heuristic_listing_prob" if target == "listing" else "heuristic_long_prob"
    usable = [r for r in rows if getattr(r, y_attr) is not None]
    res = WalkForwardResult(target=target, feature_names=feature_names)
    years = sorted({r.listing_date.year for r in usable})
    for yr in years:
        train = [r for r in usable if r.listing_date.year < yr]
        test = [r for r in usable if r.listing_date.year == yr]
        ys = [getattr(r, y_attr) for r in train]
        if len(train) < min_train or len(set(ys)) < 2 or not test:
            res.folds.append({"year": yr, "n_train": len(train), "n_test": len(test), "skipped": "insufficient or single-class training data"})
            continue
        xtr, xte, design = _standardize([r.features for r in train], [r.features for r in test], feature_names)
        w = fit_logistic(xtr, ys)
        preds = predict(w, xte)
        base = mean(ys) * 100
        fold_pairs = [(p, getattr(r, y_attr)) for p, r in zip(preds, test, strict=True)]
        res.model_pairs.extend(fold_pairs)
        res.base_rate_pairs.extend((base, getattr(r, y_attr)) for r in test)
        res.constant_pairs.extend((50.0, getattr(r, y_attr)) for r in test)
        res.heuristic_pairs.extend((getattr(r, h_attr), getattr(r, y_attr)) for r in test if getattr(r, h_attr) is not None)
        fa = _auc(fold_pairs)
        res.folds.append({"year": yr, "n_train": len(train), "n_test": len(test), "train_positive_pct": round(base, 1),
                          "auc": round(fa, 3) if fa is not None else None, "brier": round(_brier(fold_pairs), 4),
                          "design_columns": len(design)})
    return res


def release_gate(model: dict, base: dict, folds: list[dict]) -> dict:
    scored = [f for f in folds if f.get("auc") is not None]
    stable = (sum(1 for f in scored if f["auc"] > 0.5) / len(scored)) if scored else 0.0
    checks = {
        "sample_size_ok": model.get("n", 0) >= MIN_TEST_TOTAL,
        "auc_ok": (model.get("auc") or 0) >= GATE_AUC,
        "beats_base_rate_brier": model.get("brier") is not None and base.get("brier") is not None and model["brier"] < base["brier"],
        "fold_stability_ok": stable >= GATE_FOLD_STABILITY,
    }
    return {"passed": all(checks.values()), "checks": checks, "fold_stability_share": round(stable, 2),
            "thresholds": {"min_n": MIN_TEST_TOTAL, "min_auc": GATE_AUC, "min_fold_stability": GATE_FOLD_STABILITY}}


def evaluate_market(rows: list[Row], country: str) -> dict:
    out = {"country": country, "rows": len(rows), "feature_coverage_pct": {}, "targets": {}}
    for n in ALL_FEATURES:
        k = sum(1 for r in rows if r.features.get(n) is not None)
        out["feature_coverage_pct"][n] = round(k / len(rows) * 100, 1) if rows else None
    for target in ("listing", "12m", "12m_relative"):
        wf = walk_forward(rows, ALL_FEATURES, target)
        model, base = metrics(wf.model_pairs), metrics(wf.base_rate_pairs)
        out["targets"][target] = {
            "usable_rows": sum(1 for r in rows if getattr(r, TARGET_ATTRS[target]) is not None),
            "out_of_sample": model, "baselines": {"base_rate": base, "constant_50": metrics(wf.constant_pairs), "heuristic_v2": metrics(wf.heuristic_pairs)},
            "folds": wf.folds, "release_gate": release_gate(model, base, wf.folds),
        }
    # SPAC units hold near trust value, so in the US "positive 12m return"
    # largely means "is a SPAC". Operating companies are evaluated on their own
    # so a structural signal is never mistaken for predictive skill.
    operating = [r for r in rows if not r.features.get("is_spac")]
    if 0 < len(operating) < len(rows):
        seg = {"rows": len(operating), "targets": {}}
        for target in ("listing", "12m", "12m_relative"):
            wf = walk_forward(operating, ALL_FEATURES, target)
            model, base = metrics(wf.model_pairs), metrics(wf.base_rate_pairs)
            seg["targets"][target] = {"out_of_sample": model, "baselines": {"base_rate": base},
                                      "folds": wf.folds, "release_gate": release_gate(model, base, wf.folds)}
        out["segments"] = {"operating_companies": seg}
    return out


def evaluate(db: Session) -> dict:
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "availability_rules": AVAILABILITY_RULES,
              "method": "walk-forward by listing year, ridge logistic (Newton), baselines on identical test rows", "markets": {}}
    for country in ("India", "United States"):
        report["markets"][country] = evaluate_market(build_rows(db, country), country)
    return report
