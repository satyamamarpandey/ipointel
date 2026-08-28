from __future__ import annotations
"""Deterministic consistency engine. Two kinds of checks:
1. Cross-source: the same field reported with materially different values by two
   different sources (uses Provenance rows already persisted by pipeline.upsert_ipo).
2. Cross-field: internally inconsistent structured numbers within a single IPO record.
Neither kind accuses anyone of wrongdoing — every output is phrased as
"potential disclosure inconsistency — review required" per product policy.
Narrative cross-checking (DRHP prose vs RHP prose, "risk factor" text vs financial
statements) is NOT implemented: this app does not retain full filing text long-term,
and inventing that capability would violate the no-fabrication rule. That is a real
gap, not a hidden one — see README/limitations."""
from ..models import IPO, Provenance

NUMERIC_TOLERANCE = 0.04  # 4% relative difference before two sources are considered to disagree

def _numeric(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def cross_source(provenance: list[Provenance]) -> list[dict]:
    by_field: dict[str, list[Provenance]] = {}
    for p in provenance:
        by_field.setdefault(p.field_name, []).append(p)
    out = []
    for field, rows in by_field.items():
        numeric_rows = [(p, _numeric(p.observed_value)) for p in rows]
        numeric_rows = [(p, v) for p, v in numeric_rows if v is not None]
        if len(numeric_rows) < 2:
            continue
        for i in range(len(numeric_rows)):
            for j in range(i + 1, len(numeric_rows)):
                pa, va = numeric_rows[i]
                pb, vb = numeric_rows[j]
                if pa.source_name == pb.source_name:
                    continue
                base = max(abs(va), abs(vb), 1e-9)
                if abs(va - vb) / base > NUMERIC_TOLERANCE:
                    out.append({
                        "code": "cross_source_disagreement",
                        "field": field,
                        "summary": f"Potential disclosure inconsistency — review required: '{field}' differs across sources.",
                        "evidence_a": {"source": pa.source_name, "value": pa.observed_value, "url": pa.source_url, "observed_at": pa.observed_at.isoformat() if pa.observed_at else None},
                        "evidence_b": {"source": pb.source_name, "value": pb.observed_value, "url": pb.source_url, "observed_at": pb.observed_at.isoformat() if pb.observed_at else None},
                    })
    return out

def cross_field(ipo: IPO) -> list[dict]:
    out = []
    def add(code, summary, a_label, a_val, b_label, b_val):
        out.append({
            "code": code, "field": None,
            "summary": f"Potential disclosure inconsistency — review required: {summary}",
            "evidence_a": {"source": a_label, "value": a_val, "url": ipo.filing_url, "observed_at": ipo.updated_at.isoformat() if ipo.updated_at else None},
            "evidence_b": {"source": b_label, "value": b_val, "url": ipo.filing_url, "observed_at": ipo.updated_at.isoformat() if ipo.updated_at else None},
        })

    if ipo.net_income_m is not None and ipo.cfo_m is not None and ipo.net_income_m > 0 and ipo.cfo_m < 0:
        add("profit_vs_cash", "reported net income is positive while operating cash flow is negative.",
            "net_income_m", ipo.net_income_m, "cfo_m", ipo.cfo_m)

    if ipo.price_low is not None and ipo.final_price is not None and ipo.price_high is not None:
        lo, hi = min(ipo.price_low, ipo.price_high), max(ipo.price_low, ipo.price_high)
        band = hi - lo
        if band > 0 and (ipo.final_price < lo - 0.02 * band or ipo.final_price > hi + 0.02 * band):
            add("price_outside_band", "final price falls outside the disclosed price band.",
                "price_band", f"{lo}-{hi}", "final_price", ipo.final_price)

    subs = [ipo.qib_sub, ipo.nii_sub, ipo.retail_sub]
    known = [s for s in subs if s is not None]
    if ipo.total_sub is not None and len(known) >= 2:
        lo_known, hi_known = min(known), max(known)
        if ipo.total_sub < lo_known - 0.5 or ipo.total_sub > hi_known + 0.5:
            add("subscription_mismatch", "total subscription figure is inconsistent with the disclosed category-wise subscription figures.",
                "category subscriptions (QIB/NII/retail)", known, "total_sub", ipo.total_sub)

    if ipo.fresh_issue_pct is not None and ipo.ofs_pct is not None:
        total = ipo.fresh_issue_pct + ipo.ofs_pct
        if total < 90 or total > 110:
            add("structure_mismatch", "fresh issue % and OFS % do not sum to approximately 100%.",
                "fresh_issue_pct", ipo.fresh_issue_pct, "ofs_pct", ipo.ofs_pct)

    return out

def evaluate(ipo: IPO, provenance: list[Provenance]) -> list[dict]:
    return cross_field(ipo) + cross_source(provenance)
