from __future__ import annotations
from dataclasses import dataclass, asdict
from math import exp
from statistics import mean
from typing import Optional
from .models import IPO
from .config import get_settings

MODEL_VERSION = "v2.0-evidence-first"
FEATURE_SCHEMA_VERSION = "fs1"  # bump whenever the set/meaning of fields compute_score() reads changes

FEATURE_FIELDS = [
    "country","board","status","price_low","price_high","final_price",
    "issue_size_m","shares_offered_m","post_issue_shares_m",
    "revenue_m","revenue_prev_m","revenue_2y_ago_m","ebitda_m","net_income_m","cfo_m","debt_m","cash_m",
    "fresh_issue_pct","ofs_pct","promoter_retention_pct",
    "qib_sub","nii_sub","retail_sub","total_sub","gmp_pct","underwriter_quality","anchor_quality",
    "market_regime","sector_regime","dual_class",
    "peer_median_pe","peer_median_ps","peer_median_ev_ebitda",
]

def feature_snapshot(ipo: IPO) -> dict:
    """The exact point-in-time inputs compute_score() read for this IPO, frozen
    onto the ScoreSnapshot row so a later refresh (which mutates the live IPO
    record) can never change what this particular prediction was based on."""
    return {f: getattr(ipo, f) for f in FEATURE_FIELDS}

def clamp(v, lo=0.0, hi=100.0): return max(lo, min(hi, float(v)))
def score_linear(v: Optional[float], low: float, high: float):
    if v is None: return 50.0
    if high == low: return 50.0
    return clamp((v-low)/(high-low)*100)
def safe_div(a,b): return None if a is None or b in (None,0) else a/b
def avg(values):
    x=[v for v in values if v is not None]
    return mean(x) if x else 50.0

def pct_growth(now, prev):
    if now is None or prev in (None,0): return None
    return (now/prev-1)*100

def quality_5(v, reverse=False):
    if v is None: return 50.0
    s=clamp(v*20)
    return 100-s if reverse else s

def subscription(v):
    if v is None: return 50
    if v < 1: return max(10, 35*v)
    if v < 3: return 35 + (v-1)*8
    if v < 10: return 51 + (v-3)*2.3
    if v < 25: return 67 + (v-10)*0.8
    if v < 50: return 79 + (v-25)*0.4
    if v < 100: return 89 + (v-50)*0.14
    return 97

@dataclass
class Valuation:
    label: str
    score: float
    fair_low: float|None
    fair_high: float|None
    implied_pe: float|None
    implied_ps: float|None
    notes: list[str]

def valuation(ipo: IPO) -> Valuation:
    px = ipo.final_price or ipo.price_high
    mcap = px * ipo.post_issue_shares_m if px and ipo.post_issue_shares_m else None
    pe = safe_div(mcap, ipo.net_income_m) if ipo.net_income_m and ipo.net_income_m > 0 else None
    ps = safe_div(mcap, ipo.revenue_m) if ipo.revenue_m and ipo.revenue_m > 0 else None
    scores=[]; fair=[]; notes=[]
    if pe and ipo.peer_median_pe:
        ratio=pe/ipo.peer_median_pe
        scores.append(clamp(100 - (ratio-0.65)/1.0*100))
        fair.append(px/ratio)
        notes.append(f"P/E {pe:.1f}x vs peer median {ipo.peer_median_pe:.1f}x")
    if ps and ipo.peer_median_ps:
        ratio=ps/ipo.peer_median_ps
        scores.append(clamp(100 - (ratio-0.65)/1.0*100))
        fair.append(px/ratio)
        notes.append(f"P/S {ps:.1f}x vs peer median {ipo.peer_median_ps:.1f}x")
    if not scores:
        return Valuation("INSUFFICIENT DATA",50,None,None,pe,ps,["Peer-normalized valuation not available yet"])
    s=mean(scores)
    label = "UNDERPRICED" if s>=72 else "FAIR" if s>=45 else "OVERPRICED"
    base=mean(fair) if fair else None
    return Valuation(label,s,base*0.9 if base else None,base*1.1 if base else None,pe,ps,notes)

def confidence(ipo: IPO, conflicts: int = 0) -> float:
    fields=[
      ipo.price_high, ipo.revenue_m, ipo.revenue_prev_m, ipo.net_income_m, ipo.cfo_m,
      ipo.debt_m, ipo.cash_m, ipo.post_issue_shares_m, ipo.filing_url or None,
      ipo.fresh_issue_pct if ipo.country.lower()=="india" else ipo.lockup_days,
    ]
    complete=sum(v not in (None,"") for v in fields)/len(fields)
    primary = 1.0 if (ipo.filing_url and ("sec.gov" in ipo.filing_url or "nse" in ipo.filing_url.lower() or "sebi" in ipo.filing_url.lower())) else 0.72
    flag_pen=min(0.28, len(ipo.data_flags or [])*0.035)
    conflict_pen=min(0.25, conflicts*0.08)
    return clamp((0.68*complete+0.32*primary-flag_pen-conflict_pen)*100)

def probability_from_score(score: float, midpoint=65, scale=10):
    return clamp(100/(1+exp(-(score-midpoint)/scale)), 2, 98)

def compute_score(ipo: IPO, conflicts: int = 0) -> dict:
    settings=get_settings()
    val=valuation(ipo)
    growth=score_linear(avg([pct_growth(ipo.revenue_m,ipo.revenue_prev_m), pct_growth(ipo.revenue_prev_m,ipo.revenue_2y_ago_m)]), -10, 35)
    ebitda_margin=safe_div(ipo.ebitda_m,ipo.revenue_m)
    net_margin=safe_div(ipo.net_income_m,ipo.revenue_m)
    cfo_margin=safe_div(ipo.cfo_m,ipo.revenue_m)
    cfo_ni=safe_div(ipo.cfo_m,ipo.net_income_m) if ipo.net_income_m and ipo.net_income_m>0 else None
    net_debt=(ipo.debt_m or 0)-(ipo.cash_m or 0) if (ipo.debt_m is not None or ipo.cash_m is not None) else None
    nd_e=safe_div(net_debt,ipo.ebitda_m) if net_debt is not None and ipo.ebitda_m and ipo.ebitda_m>0 else None
    fin=avg([
      score_linear(ebitda_margin*100 if ebitda_margin is not None else None,0,25),
      score_linear(net_margin*100 if net_margin is not None else None,-5,15),
      score_linear(cfo_margin*100 if cfo_margin is not None else None,-5,18),
      score_linear(cfo_ni,0,1.2),
      100-score_linear(nd_e,0,4) if nd_e is not None else 55,
    ])
    issue=avg([score_linear(ipo.fresh_issue_pct,0,80),100-score_linear(ipo.ofs_pct,0,100)])
    if ipo.country.lower()=="india":
        demand=0.38*subscription(ipo.qib_sub)+0.17*subscription(ipo.nii_sub)+0.13*subscription(ipo.retail_sub)+0.17*subscription(ipo.total_sub)+0.15*quality_5(ipo.anchor_quality)
        governance=avg([score_linear(ipo.promoter_retention_pct,35,75), 50 if ipo.dual_class is None else (35 if ipo.dual_class else 70)])
        gmp=50 if ipo.gmp_pct is None else score_linear(ipo.gmp_pct,-10,40)
    else:
        demand=avg([quality_5(ipo.underwriter_quality), score_linear(ipo.shares_offered_m,30,2)])
        governance=50 if ipo.dual_class is None else (35 if ipo.dual_class else 70)
        gmp=50
    market=avg([quality_5(ipo.market_regime),quality_5(ipo.sector_regime)])
    conf=confidence(ipo,conflicts)
    business=50.0  # conservative until filing NLP/business-quality evidence is available
    overall=0.12*business+0.12*growth+0.16*fin+0.18*val.score+0.09*issue+0.09*governance+0.10*demand+0.07*market+0.07*conf
    listing=(0.34*demand+0.16*val.score+0.14*gmp+0.14*market+0.07*growth+0.05*fin+0.10*conf) if ipo.country.lower()=="india" else (0.27*val.score+0.18*demand+0.18*market+0.12*growth+0.10*fin+0.15*conf)
    long_term=0.17*business+0.18*growth+0.19*fin+0.19*val.score+0.10*governance+0.07*issue+0.04*market+0.06*conf
    listing_prob=probability_from_score(listing,64,9)
    long_prob=probability_from_score(long_term,67,10)
    rationale=[]; risks=[]; changes=[]
    if growth>=70: rationale.append("Revenue growth is strong versus the model's IPO baseline")
    if fin>=70: rationale.append("Profitability/cash conversion/leverage screen is constructive")
    if val.score>=72: rationale.append("Peer-normalized valuation screens attractive")
    if demand>=72: rationale.append("Demand/issue mechanics are strong")
    if val.score<42: risks.append("Peer-normalized valuation looks aggressive")
    if fin<45: risks.append("Financial quality is weak or cash conversion is unproven")
    if conf<70: risks.append("Data coverage is below the reliability threshold")
    if ipo.ofs_pct is not None and ipo.ofs_pct>65: risks.append("Secondary/OFS-heavy structure increases seller-exit risk")
    if ipo.dual_class: risks.append("Dual-class voting structure weakens public-shareholder control")
    if ipo.total_sub is not None and ipo.total_sub<1: risks.append("Issue demand is below 1x subscription")
    if val.label=="INSUFFICIENT DATA": changes.append("Add a defensible peer set and post-money share count to unlock valuation")
    if ipo.cfo_m is None: changes.append("Operating cash-flow disclosure would materially improve long-term confidence")
    if ipo.country.lower()=="india" and ipo.qib_sub is None: changes.append("QIB subscription data can materially change the listing-gain view")
    if conf<settings.min_recommendation_confidence: changes.append(f"Confidence must reach {settings.min_recommendation_confidence:.0f}% before an actionable recommendation is issued")
    if settings.strict_reliability and conf<settings.min_recommendation_confidence:
        recommendation="INSUFFICIENT RELIABLE DATA — NO RECOMMENDATION"
        horizon="WAIT FOR VERIFIED DATA"
    else:
        recommendation="INVEST — STRONG" if overall>=80 and long_term>=74 else "INVEST SELECTIVELY" if overall>=70 else "WATCH / SMALL ALLOCATION" if overall>=60 else "AVOID / WAIT"
        horizon="LISTING GAINS ONLY" if listing>=74 and long_term<65 else "BOTH — LISTING + LONG TERM" if listing>=70 and long_term>=72 else "LONG TERM — WAIT FOR PRICE DISCOVERY" if long_term>=74 and listing<65 else "LONG TERM BIAS" if long_term>=68 else "LISTING BIAS" if listing>=68 else "NO CLEAR EDGE"
    return {
      "model_version":MODEL_VERSION,"overall_score":round(overall,1),"listing_score":round(listing,1),"long_term_score":round(long_term,1),"confidence":round(conf,1),
      "listing_gain_probability":round(listing_prob,1),"long_term_outperform_probability":round(long_prob,1),"recommendation":recommendation,"horizon":horizon,
      "valuation_label":val.label,"fair_value_low":round(val.fair_low,2) if val.fair_low else None,"fair_value_high":round(val.fair_high,2) if val.fair_high else None,
      "pillars":{"Business evidence":round(business,1),"Growth":round(growth,1),"Financial quality":round(fin,1),"Valuation":round(val.score,1),"Issue structure":round(issue,1),"Governance":round(governance,1),"Demand":round(demand,1),"Market regime":round(market,1),"Data confidence":round(conf,1)},
      "rationale":rationale[:6],"risks":risks[:8],"what_changes_verdict":changes[:8],
    }
