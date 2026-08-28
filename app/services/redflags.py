from __future__ import annotations
"""Deterministic red-flag engine. No LLM in the loop — every flag is a rule over
structured fields already captured with provenance. If a category from the product
spec cannot be derived from the current data model (e.g. auditor opinions, customer
concentration — no such structured field exists yet), it is simply not emitted rather
than guessed."""
from ..models import IPO

INFO, WATCH, HIGH, CRITICAL = "INFO", "WATCH", "HIGH", "CRITICAL"

def _pct(a, b):
    if a is None or b in (None, 0):
        return None
    return a / b * 100

def evaluate(ipo: IPO) -> list[dict]:
    flags: list[dict] = []
    def flag(code, severity, title, detail, metric_field, metric_value, rule):
        flags.append({
            "code": code, "severity": severity, "title": title, "detail": detail,
            "metric_field": metric_field, "metric_value": metric_value,
            "rule": rule, "observed_at": ipo.updated_at.isoformat() if ipo.updated_at else None,
        })

    # --- Financial ---
    if ipo.cfo_m is not None and ipo.cfo_m < 0:
        flag("neg_cfo", HIGH, "Negative operating cash flow",
             f"Latest reported operating cash flow is {ipo.cfo_m:.1f}m — the business is not self-funding from operations.",
             "cfo_m", ipo.cfo_m, "cfo_m < 0")
    if ipo.net_income_m is not None and ipo.cfo_m is not None:
        if ipo.net_income_m > 0 and ipo.cfo_m < ipo.net_income_m * 0.5:
            flag("pat_cfo_gap", WATCH, "Cash conversion materially below reported profit",
                 f"Net income {ipo.net_income_m:.1f}m vs operating cash flow {ipo.cfo_m:.1f}m — accruals are not converting to cash at a normal rate.",
                 "cfo_m", ipo.cfo_m, "cfo_m < 0.5 * net_income_m (net_income_m > 0)")
    if ipo.net_income_m is not None and ipo.net_income_m < 0:
        flag("losses", WATCH, "Company is loss-making at IPO",
             f"Reported net income is {ipo.net_income_m:.1f}m.",
             "net_income_m", ipo.net_income_m, "net_income_m < 0")
    if ipo.revenue_m is not None and ipo.revenue_prev_m is not None and ipo.revenue_prev_m:
        growth = (ipo.revenue_m / ipo.revenue_prev_m - 1) * 100
        if growth < 0:
            flag("revenue_decline", HIGH, "Revenue declining into the IPO",
                 f"Revenue moved from {ipo.revenue_prev_m:.1f}m to {ipo.revenue_m:.1f}m ({growth:.1f}%).",
                 "revenue_m", ipo.revenue_m, "latest revenue < prior period revenue")
    net_debt = None
    if ipo.debt_m is not None or ipo.cash_m is not None:
        net_debt = (ipo.debt_m or 0) - (ipo.cash_m or 0)
    if net_debt is not None and ipo.ebitda_m and ipo.ebitda_m > 0 and net_debt / ipo.ebitda_m > 3.5:
        flag("high_leverage", WATCH, "Elevated net leverage",
             f"Net debt/EBITDA is approximately {net_debt/ipo.ebitda_m:.1f}x.",
             "debt_m", net_debt, "net_debt / ebitda_m > 3.5")
    if ipo.ebitda_m is not None and ipo.ebitda_m < 0:
        flag("neg_ebitda", WATCH, "Negative EBITDA",
             f"Reported EBITDA is {ipo.ebitda_m:.1f}m.", "ebitda_m", ipo.ebitda_m, "ebitda_m < 0")

    # --- IPO structure ---
    if ipo.ofs_pct is not None and ipo.ofs_pct >= 70:
        flag("high_ofs", HIGH, "Offer dominated by selling shareholders",
             f"{ipo.ofs_pct:.0f}% of the issue is an offer-for-sale — existing holders are monetizing, minimal new capital reaches the company.",
             "ofs_pct", ipo.ofs_pct, "ofs_pct >= 70")
    elif ipo.ofs_pct is not None and ipo.ofs_pct >= 45:
        flag("moderate_ofs", WATCH, "Meaningful secondary component",
             f"{ipo.ofs_pct:.0f}% of the issue is an offer-for-sale.",
             "ofs_pct", ipo.ofs_pct, "45 <= ofs_pct < 70")
    if ipo.fresh_issue_pct is not None and ipo.fresh_issue_pct < 15 and (ipo.ofs_pct or 0) > 0:
        flag("low_primary_capital", WATCH, "Little fresh capital raised",
             f"Only {ipo.fresh_issue_pct:.0f}% of the issue is fresh issuance; growth funding from this IPO is limited.",
             "fresh_issue_pct", ipo.fresh_issue_pct, "fresh_issue_pct < 15")
    if ipo.market_overhang_pct is not None and ipo.market_overhang_pct >= 40:
        flag("overhang", WATCH, "Large post-listing supply overhang",
             f"Approximately {ipo.market_overhang_pct:.0f}% of shares are estimated to be lockup-free or pre-IPO-investor-held within the near term.",
             "market_overhang_pct", ipo.market_overhang_pct, "market_overhang_pct >= 40")
    if ipo.lockup_days is not None and ipo.lockup_days < 90:
        flag("short_lockup", INFO, "Short lock-up period",
             f"Disclosed lock-up is {ipo.lockup_days} days, shorter than the common 180-day norm.",
             "lockup_days", ipo.lockup_days, "lockup_days < 90")

    # --- Governance / structure ---
    if ipo.dual_class:
        flag("dual_class", WATCH, "Dual-class share structure",
             "Filing indicates unequal voting rights between share classes, weakening public-shareholder control.",
             "dual_class", True, "dual_class == True")
    if ipo.promoter_retention_pct is not None and ipo.promoter_retention_pct < 40 and ipo.country.lower() == "india":
        flag("low_promoter_retention", WATCH, "Low post-issue promoter holding",
             f"Promoter/founder retained stake is approximately {ipo.promoter_retention_pct:.0f}% after listing.",
             "promoter_retention_pct", ipo.promoter_retention_pct, "promoter_retention_pct < 40 (India)")

    # --- Demand ---
    if ipo.total_sub is not None and ipo.total_sub < 1:
        flag("undersubscribed", HIGH, "Issue undersubscribed",
             f"Total subscription is {ipo.total_sub:.2f}x — demand did not cover the offered shares at close.",
             "total_sub", ipo.total_sub, "total_sub < 1")
    if ipo.qib_sub is not None and ipo.qib_sub < 1 and ipo.country.lower() == "india":
        flag("weak_qib", WATCH, "Weak institutional (QIB) demand",
             f"QIB subscription is {ipo.qib_sub:.2f}x.", "qib_sub", ipo.qib_sub, "qib_sub < 1 (India)")

    # --- Valuation-adjacent structural flags ---
    if ipo.price_low is not None and ipo.price_high is not None and ipo.price_low > ipo.price_high:
        flag("inverted_band", CRITICAL, "Price band data inverted",
             f"Recorded low ({ipo.price_low}) exceeds recorded high ({ipo.price_high}) — likely a data-capture error, needs correction.",
             "price_low", ipo.price_low, "price_low > price_high")

    severity_rank = {CRITICAL: 0, HIGH: 1, WATCH: 2, INFO: 3}
    flags.sort(key=lambda f: severity_rank[f["severity"]])
    return flags

def summarize(flags: list[dict]) -> dict:
    counts = {INFO: 0, WATCH: 0, HIGH: 0, CRITICAL: 0}
    for f in flags:
        counts[f["severity"]] += 1
    worst = next((lvl for lvl in (CRITICAL, HIGH, WATCH, INFO) if counts[lvl]), None)
    return {"counts": counts, "worst": worst, "total": len(flags)}
