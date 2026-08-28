from __future__ import annotations
"""Model performance evaluation, split by country and by listing vs long-term model.
Uses the EARLIEST ScoreSnapshot recorded for each IPO (the score as first computed,
before any post-listing information could have influenced it) against realized
PerformanceSnapshot returns. This is a fixed-weight heuristic model (app.scoring) —
there is no training step, so 'walk-forward' here means out-of-sample-by-construction
evaluation bucketed by listing year, not train/test weight refitting. Weights in
app/scoring.py are never adjusted based on these results.

MIN_SAMPLE gates every statistic: nothing is reported for a bucket below it."""
from sqlalchemy import select
from sqlalchemy.orm import Session
from statistics import mean, median
from ..models import IPO, ScoreSnapshot, PerformanceSnapshot
from ..services.market import parse_date

MIN_SAMPLE = 20
BANDS = [(90, 101), (80, 90), (70, 80), (60, 70), (50, 60), (0, 50)]
BAND_LABELS = ["90-100", "80-89", "70-79", "60-69", "50-59", "<50"]

def _earliest_score(db: Session, ipo_id: int) -> ScoreSnapshot | None:
    return db.scalar(select(ScoreSnapshot).where(ScoreSnapshot.ipo_id == ipo_id).order_by(ScoreSnapshot.created_at.asc()).limit(1))

def _latest_perf(db: Session, ipo_id: int) -> PerformanceSnapshot | None:
    return db.scalar(select(PerformanceSnapshot).where(PerformanceSnapshot.ipo_id == ipo_id).order_by(PerformanceSnapshot.created_at.desc()).limit(1))

def _auc(pairs: list[tuple[float, int]]) -> float | None:
    """Mann-Whitney U based AUC — no sklearn dependency."""
    pos = [p for p, y in pairs if y == 1]
    neg = [p for p, y in pairs if y == 0]
    if not pos or not neg:
        return None
    wins = 0.0
    for p in pos:
        for n in neg:
            if p > n:
                wins += 1
            elif p == n:
                wins += 0.5
    return wins / (len(pos) * len(neg))

def _brier(pairs: list[tuple[float, int]]) -> float | None:
    if not pairs:
        return None
    return sum(((p / 100) - y) ** 2 for p, y in pairs) / len(pairs)

def _log_loss(pairs: list[tuple[float, int]]) -> float | None:
    if not pairs:
        return None
    eps = 1e-6
    total = 0.0
    for p, y in pairs:
        pr = min(1 - eps, max(eps, p / 100))
        total += -(y * __import__("math").log(pr) + (1 - y) * __import__("math").log(1 - pr))
    return total / len(pairs)

def _spearman(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 5:
        return None
    def rank(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        ranks = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg_rank = (i + j) / 2 + 1
            for k in range(i, j + 1):
                ranks[order[k]] = avg_rank
            i = j + 1
        return ranks
    rx, ry = rank(xs), rank(ys)
    mx, my = mean(rx), mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else None

def _calibration(pairs: list[tuple[float, int]], buckets=5) -> list[dict]:
    if not pairs:
        return []
    out = []
    edges = [i * 100 / buckets for i in range(buckets + 1)]
    for i in range(buckets):
        lo, hi = edges[i], edges[i + 1]
        chunk = [(p, y) for p, y in pairs if (p >= lo and (p < hi or i == buckets - 1))]
        if not chunk:
            continue
        out.append({"predicted_range": f"{lo:.0f}-{hi:.0f}%", "n": len(chunk),
                     "avg_predicted_pct": round(mean(p for p, _ in chunk), 1),
                     "actual_positive_pct": round(mean(y for _, y in chunk) * 100, 1)})
    return out

def _band_breakdown(rows: list[dict], score_key: str, return_key: str) -> list[dict]:
    out = []
    for (lo, hi), label in zip(BANDS, BAND_LABELS):
        chunk = [r for r in rows if lo <= r[score_key] < hi and r[return_key] is not None]
        if not chunk:
            out.append({"band": label, "n": 0})
            continue
        rets = [r[return_key] for r in chunk]
        out.append({
            "band": label, "n": len(chunk),
            "positive_pct": round(sum(1 for x in rets if x > 0) / len(rets) * 100, 1),
            "mean_return_pct": round(mean(rets), 1), "median_return_pct": round(median(rets), 1),
            "gt10pct_pct": round(sum(1 for x in rets if x > 10) / len(rets) * 100, 1),
            "gt20pct_pct": round(sum(1 for x in rets if x > 20) / len(rets) * 100, 1),
            "loss_pct": round(sum(1 for x in rets if x < 0) / len(rets) * 100, 1),
        })
    return out

def _model_block(rows: list[dict], score_key: str, prob_key: str, return_key: str) -> dict:
    usable = [r for r in rows if r[return_key] is not None]
    n = len(usable)
    block = {"sample_size": n, "min_sample_required": MIN_SAMPLE, "band_breakdown": _band_breakdown(rows, score_key, return_key)}
    if n < MIN_SAMPLE:
        block["status"] = f"insufficient sample (n={n}, need {MIN_SAMPLE}+) — no AUC/Brier/calibration displayed"
        return block
    pairs = [(r[prob_key], 1 if r[return_key] > 0 else 0) for r in usable]
    block.update({
        "status": "evaluated",
        "auc": round(_auc(pairs), 3) if _auc(pairs) is not None else None,
        "brier_score": round(_brier(pairs), 4),
        "log_loss": round(_log_loss(pairs), 4),
        "spearman_score_vs_return": round(_spearman([r[score_key] for r in usable], [r[return_key] for r in usable]), 3) if _spearman([r[score_key] for r in usable], [r[return_key] for r in usable]) is not None else None,
        "calibration": _calibration(pairs),
        "positive_rate_actual_pct": round(mean(y for _, y in pairs) * 100, 1),
        "avg_predicted_probability_pct": round(mean(p for p, _ in pairs), 1),
    })
    return block

def _by_year(rows: list[dict], return_key: str) -> list[dict]:
    years: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("listing_year") and r.get(return_key) is not None:
            years.setdefault(r["listing_year"], []).append(r)
    out = []
    for y in sorted(years):
        chunk = years[y]
        rets = [r[return_key] for r in chunk]
        out.append({"year": y, "n": len(chunk), "mean_return_pct": round(mean(rets), 1) if rets else None})
    return out

def _collect(db: Session, country: str) -> list[dict]:
    ipos = db.scalars(select(IPO).where(IPO.country == country, IPO.status == "Listed")).all()
    rows = []
    for ipo in ipos:
        sc = _earliest_score(db, ipo.id)
        pf = _latest_perf(db, ipo.id)
        if not sc:
            continue
        ld = parse_date(ipo.listing_date)
        rows.append({
            "ipo_id": ipo.id, "overall": sc.overall_score, "listing": sc.listing_score, "long_term": sc.long_term_score,
            "listing_prob": sc.listing_gain_probability, "long_term_prob": sc.long_term_outperform_probability,
            "listing_return": pf.listing_return_pct if pf else None,
            "return_12m": pf.return_12m_pct if pf else None,
            "listing_year": str(ld.year) if ld else None,
        })
    return rows

def evaluate(db: Session) -> dict:
    out = {}
    for country in ("India", "United States"):
        rows = _collect(db, country)
        listing_block = _model_block(rows, "listing", "listing_prob", "listing_return")
        listing_block["by_year"] = _by_year(rows, "listing_return")
        long_block = _model_block(rows, "long_term", "long_term_prob", "return_12m")
        long_block["by_year"] = _by_year(rows, "return_12m")
        out[country] = {"total_listed_with_score": len(rows), "listing_model": listing_block, "long_term_model": long_block}
    return out
