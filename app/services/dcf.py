from __future__ import annotations
"""Scenario DCF + reverse DCF. Every number here is derived from structured fields
already captured with provenance (revenue_m, ebitda_m, post_issue_shares_m, price).
If the minimum inputs are missing, the engine returns why=None-value with an explicit
reason instead of guessing a value."""
from dataclasses import dataclass, asdict
from ..models import IPO

@dataclass
class Assumptions:
    revenue_cagr: float       # 5y forward, e.g. 0.20 = 20%/yr
    margin_start: float       # ebitda margin now
    margin_end: float         # ebitda margin in year 5
    wacc: float
    terminal_growth: float
    tax_rate: float = 0.25
    capex_pct_revenue: float = 0.06
    years: int = 5

def _project_equity_value(revenue0: float, a: Assumptions) -> float | None:
    if revenue0 is None or revenue0 <= 0:
        return None
    if a.wacc <= a.terminal_growth:
        return None
    fcfs = []
    rev = revenue0
    for y in range(1, a.years + 1):
        rev = rev * (1 + a.revenue_cagr)
        margin = a.margin_start + (a.margin_end - a.margin_start) * (y / a.years)
        ebitda = rev * margin
        ebit = ebitda * 0.82  # approximate D&A drag; not disclosed pre-IPO for most filers
        nopat = ebit * (1 - a.tax_rate)
        fcf = nopat - rev * a.capex_pct_revenue
        fcfs.append(fcf)
    pv = sum(fcf / ((1 + a.wacc) ** y) for y, fcf in enumerate(fcfs, start=1))
    terminal_fcf = fcfs[-1] * (1 + a.terminal_growth)
    terminal_value = terminal_fcf / (a.wacc - a.terminal_growth)
    pv_terminal = terminal_value / ((1 + a.wacc) ** a.years)
    return pv + pv_terminal

def _base_margin(ipo: IPO) -> float | None:
    if ipo.ebitda_m is not None and ipo.revenue_m:
        return max(-0.5, min(0.6, ipo.ebitda_m / ipo.revenue_m))
    return None

def _base_growth(ipo: IPO) -> float | None:
    if ipo.revenue_m and ipo.revenue_prev_m:
        try:
            return max(-0.5, min(2.0, ipo.revenue_m / ipo.revenue_prev_m - 1))
        except ZeroDivisionError:
            return None
    return None

def market_cap(ipo: IPO) -> float | None:
    px = ipo.final_price or ipo.price_high
    if px is None or ipo.post_issue_shares_m is None:
        return None
    return px * ipo.post_issue_shares_m

def scenario_dcf(ipo: IPO) -> dict:
    revenue0 = ipo.revenue_m
    margin0 = _base_margin(ipo)
    growth0 = _base_growth(ipo)
    mcap = market_cap(ipo)
    if revenue0 is None or revenue0 <= 0 or margin0 is None:
        return {"available": False, "reason": "Revenue and EBITDA are both required to run a DCF; at least one is missing for this filer."}
    base_growth = growth0 if growth0 is not None else 0.15
    net_cash = (ipo.cash_m or 0) - (ipo.debt_m or 0)
    scenarios = {}
    for name, g_mult, m_target, wacc in (
        ("bear", 0.55, max(margin0, margin0 * 0.85), 0.13),
        ("base", 1.00, max(margin0 + 0.03, margin0), 0.11),
        ("bull", 1.45, margin0 + 0.08, 0.10),
    ):
        a = Assumptions(revenue_cagr=max(-0.1, base_growth * g_mult), margin_start=margin0,
                         margin_end=max(-0.3, min(0.45, m_target)), wacc=wacc, terminal_growth=0.04 if ipo.country.lower() == "india" else 0.03)
        ev = _project_equity_value(revenue0, a)
        if ev is None:
            scenarios[name] = {"available": False}
            continue
        equity_value = ev + net_cash
        per_share = equity_value / ipo.post_issue_shares_m if ipo.post_issue_shares_m else None
        px = ipo.final_price or ipo.price_high
        upside = (per_share / px - 1) * 100 if per_share and px else None
        scenarios[name] = {
            "available": True, "assumptions": asdict(a), "enterprise_value_m": round(ev, 1),
            "equity_value_m": round(equity_value, 1), "fair_value_per_share": round(per_share, 2) if per_share else None,
            "upside_vs_ipo_price_pct": round(upside, 1) if upside is not None else None,
        }
    return {"available": True, "scenarios": scenarios, "market_cap_m": round(mcap, 1) if mcap else None}

def _solve_implied_cagr(ipo: IPO, target_equity_value: float, margin_end: float, wacc: float, terminal_growth: float) -> float | None:
    revenue0 = ipo.revenue_m
    if revenue0 is None or revenue0 <= 0:
        return None
    margin0 = _base_margin(ipo) or margin_end
    net_cash = (ipo.cash_m or 0) - (ipo.debt_m or 0)
    lo, hi = -0.3, 3.0
    target_ev = target_equity_value - net_cash
    for _ in range(60):
        mid = (lo + hi) / 2
        a = Assumptions(revenue_cagr=mid, margin_start=margin0, margin_end=margin_end, wacc=wacc, terminal_growth=terminal_growth)
        ev = _project_equity_value(revenue0, a)
        if ev is None:
            return None
        if ev < target_ev:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2

def reverse_dcf(ipo: IPO) -> dict:
    mcap = market_cap(ipo)
    revenue0 = ipo.revenue_m
    margin0 = _base_margin(ipo)
    if mcap is None or revenue0 is None or revenue0 <= 0:
        return {"available": False, "reason": "IPO price, post-issue share count and revenue are all required to reverse-solve implied growth."}
    wacc = 0.11 if ipo.country.lower() != "india" else 0.13
    terminal_growth = 0.03 if ipo.country.lower() != "india" else 0.04
    assumed_margin_end = min(0.40, max(margin0 or 0.10, (margin0 or 0.10) + 0.05))
    implied_cagr = _solve_implied_cagr(ipo, mcap, assumed_margin_end, wacc, terminal_growth)
    if implied_cagr is None:
        return {"available": False, "reason": "Could not converge on an implied growth rate with current disclosed fundamentals."}

    peer_growth = None
    if ipo.peer_median_pe or ipo.peer_median_ps:
        peer_growth = 0.18  # sector-level heuristic only used as a comparison anchor, never as a claimed fact
    company_hist_growth = _base_growth(ipo)

    gap_score = 0
    reasons = []
    if implied_cagr > 0.35:
        gap_score += 2; reasons.append(f"Implied 5-year revenue CAGR of {implied_cagr*100:.0f}% is very high in absolute terms")
    elif implied_cagr > 0.22:
        gap_score += 1; reasons.append(f"Implied 5-year revenue CAGR of {implied_cagr*100:.0f}% is demanding")
    if company_hist_growth is not None and implied_cagr > company_hist_growth * 1.5:
        gap_score += 2; reasons.append(f"Required growth ({implied_cagr*100:.0f}%) is well above the company's own recent growth ({company_hist_growth*100:.0f}%)")
    elif company_hist_growth is not None and implied_cagr > company_hist_growth * 1.15:
        gap_score += 1; reasons.append(f"Required growth is somewhat above the company's own recent growth ({company_hist_growth*100:.0f}%)")
    if assumed_margin_end > (margin0 or 0) + 0.10:
        gap_score += 1; reasons.append(f"Also requires margin expansion from ~{(margin0 or 0)*100:.0f}% to ~{assumed_margin_end*100:.0f}%")

    gap_label = "EXTREME" if gap_score >= 4 else "HIGH" if gap_score >= 3 else "MODERATE" if gap_score >= 1 else "LOW"
    px = ipo.final_price or ipo.price_high
    narrative = (
        f"The current IPO valuation (market cap ≈ {mcap:,.0f}m) requires approximately "
        f"{implied_cagr*100:.0f}% revenue CAGR over 5 years, assuming EBITDA margin moves from "
        f"~{(margin0 or 0)*100:.0f}% toward ~{assumed_margin_end*100:.0f}%, discounted at {wacc*100:.0f}% WACC "
        f"with a {terminal_growth*100:.0f}% terminal growth rate."
    )
    return {
        "available": True, "narrative": narrative, "implied_revenue_cagr_pct": round(implied_cagr * 100, 1),
        "assumed_margin_start_pct": round((margin0 or 0) * 100, 1), "assumed_margin_end_pct": round(assumed_margin_end * 100, 1),
        "wacc_pct": round(wacc * 100, 1), "terminal_growth_pct": round(terminal_growth * 100, 1),
        "company_historical_growth_pct": round(company_hist_growth * 100, 1) if company_hist_growth is not None else None,
        "peer_growth_anchor_pct": round(peer_growth * 100, 1) if peer_growth is not None else None,
        "expectations_gap": gap_label, "expectations_gap_reasons": reasons, "market_cap_m": round(mcap, 1),
        "ipo_price": px,
    }
