from __future__ import annotations
"""Nearest-neighbour historical IPO matching. Standardizes a small structured
feature vector (issue size, growth, margin, valuation multiple, demand) across all
IPOs in the same country and ranks by Euclidean distance in z-score space. Returns
the matches plus their realized returns where known, and is explicit when a match
is weak."""
from sqlalchemy import select
from sqlalchemy.orm import Session
from statistics import mean, pstdev
from ..models import IPO, PerformanceSnapshot

FEATURES = ["issue_size_m", "growth_pct", "margin_pct", "ps_multiple", "total_sub"]

def _features(ipo: IPO) -> dict:
    growth = None
    if ipo.revenue_m and ipo.revenue_prev_m:
        growth = (ipo.revenue_m / ipo.revenue_prev_m - 1) * 100
    margin = (ipo.ebitda_m / ipo.revenue_m * 100) if ipo.ebitda_m is not None and ipo.revenue_m else None
    px = ipo.final_price or ipo.price_high
    mcap = px * ipo.post_issue_shares_m if px and ipo.post_issue_shares_m else None
    ps = (mcap / ipo.revenue_m) if mcap and ipo.revenue_m else None
    return {"issue_size_m": ipo.issue_size_m, "growth_pct": growth, "margin_pct": margin, "ps_multiple": ps, "total_sub": ipo.total_sub}

def _latest_perf(db: Session, ipo_id: int) -> PerformanceSnapshot | None:
    return db.scalar(select(PerformanceSnapshot).where(PerformanceSnapshot.ipo_id == ipo_id).order_by(PerformanceSnapshot.created_at.desc()).limit(1))

def find_similar(db: Session, target: IPO, k: int = 8, candidates: list[IPO] | None = None) -> dict:
    """`candidates`, when supplied, must be every Listed IPO in target.country
    (any country/status filtering is the caller's responsibility - see
    scripts/build_pages.py, which fetches this once per country instead of
    once per target IPO to avoid an O(n^2) rescan when generating hundreds
    of detail artifacts in one run). Identical result either way; this is a
    caching optimization only, never a change to the matching algorithm."""
    if candidates is None:
        candidates = db.scalars(select(IPO).where(IPO.country == target.country, IPO.status.in_(["Listed"]))).all()
    candidates = [c for c in candidates if c.id != target.id]
    tf = _features(target)
    rows = []
    for c in candidates:
        cf = _features(c)
        rows.append((c, cf))
    stats = {}
    for f in FEATURES:
        vals = [cf[f] for _, cf in rows if cf[f] is not None]
        if len(vals) >= 5:
            stats[f] = (mean(vals), pstdev(vals) or 1.0)
    usable_features = [f for f in FEATURES if f in stats and tf[f] is not None]
    if len(usable_features) < 2:
        return {"available": False, "reason": "Not enough comparable structured fields (issue size, growth, margin, valuation, demand) populated yet to compute a defensible similarity score.", "matches": []}

    scored = []
    for c, cf in rows:
        dims = [f for f in usable_features if cf[f] is not None]
        if len(dims) < 2:
            continue
        dist2 = 0.0
        for f in dims:
            m, s = stats[f]
            dist2 += ((cf[f] - m) / s - (tf[f] - m) / s) ** 2
        dist = (dist2 / len(dims)) ** 0.5
        scored.append((dist, len(dims), c, cf, dims))
    scored.sort(key=lambda x: (x[0], -x[1]))
    top = scored[:k]

    matches = []
    for dist, ndims, c, cf, dims in top:
        perf = _latest_perf(db, c.id)
        why = []
        for f in dims:
            label = {"issue_size_m": "issue size", "growth_pct": "revenue growth", "margin_pct": "EBITDA margin", "ps_multiple": "P/S multiple", "total_sub": "subscription demand"}[f]
            why.append(f"comparable {label}")
        matches.append({
            "ipo_id": c.id, "company": c.company, "symbol": c.symbol, "listing_date": c.listing_date,
            "distance": round(dist, 3), "matched_on": why, "dims_used": ndims,
            "listing_return_pct": round(perf.listing_return_pct, 1) if perf and perf.listing_return_pct is not None else None,
        })

    returns = [m["listing_return_pct"] for m in matches if m["listing_return_pct"] is not None]
    agg = None
    if returns:
        pos = sum(1 for r in returns if r > 0)
        agg = {"n_with_return": len(returns), "mean_listing_return_pct": round(mean(returns), 1),
               "median_listing_return_pct": round(sorted(returns)[len(returns)//2], 1), "success_rate_pct": round(pos/len(returns)*100, 1)}
    quality = "STRONG" if usable_features and matches and matches[0]["dims_used"] >= 4 else "MODERATE" if len(usable_features) >= 3 else "WEAK"
    return {"available": True, "match_quality": quality, "features_used": usable_features, "matches": matches, "aggregate": agg}
