from __future__ import annotations
"""Pre-IPO financial features for US issuers from SEC XBRL companyfacts.

Registration statements (S-1, S-1/A, F-1) and the 424B4 prospectus carry no
XBRL, so the audited figures a prospectus prints are only machine-readable
once the issuer files its first periodic report (10-K/10-Q/20-F), whose
comparative columns restate the same pre-IPO fiscal years.

This module reads those comparatives for fiscal periods that ENDED before
the listing date and records each value as a FeatureObservation with:
  period_end       the fiscal period the value describes
  available_at     the listing date, rule "xbrl_post_ipo_comparative": the
                   same audited number was printed in the prospectus, so it
                   was public by the listing day (Q-001 option A; restatement
                   risk is why confidence is 0.85, not 1.0)
Only the EARLIEST-filed fact per (concept, period_end) is used (the first
periodic report; later reports may be restated), and only facts filed within
18 months of listing, so a much later restatement can never leak in.

Shares outstanding come from the dei cover fact of the earliest post-IPO
filing and are dated by that filing (rule "first_periodic_report").
"""
from datetime import date, datetime, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import IPO, FeatureObservation

SOURCE_NAME = "SEC XBRL companyfacts"
PROVENANCE_SOURCE = "SEC XBRL companyfacts (post-IPO comparative)"
RULE_COMPARATIVE = "xbrl_post_ipo_comparative"
RULE_FIRST_REPORT = "first_periodic_report"
CONF_COMPARATIVE = 0.85
CONF_FIRST_REPORT = 0.9
ANNUAL_MIN_DAYS, ANNUAL_MAX_DAYS = 350, 380
MAX_MONTHS_AFTER_LISTING = 18
TAXONOMIES = ("us-gaap", "ifrs-full")

# us-gaap names first, then the ifrs-full equivalents used by 20-F filers.
# Only USD-denominated facts are read: no currency conversion is attempted.
DURATION_CONCEPTS = {
    "revenue": ("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet", "Revenue"),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "cfo": ("NetCashProvidedByUsedInOperatingActivities", "CashFlowsFromUsedInOperatingActivities"),
    "operating_income": ("OperatingIncomeLoss", "ProfitLossFromOperatingActivities"),
    "d_and_a": ("DepreciationDepletionAndAmortization", "DepreciationAndAmortization", "DepreciationAndAmortisationExpense"),
}
INSTANT_CONCEPTS = {
    "cash": ("CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "CashAndCashEquivalents"),
    "debt": ("LongTermDebt",),
    "debt_current": ("LongTermDebtCurrent",),
    "debt_noncurrent": ("LongTermDebtNoncurrent",),
    "equity": ("StockholdersEquity",),
}
# IPO column written for each observation key (only when currently None).
IPO_COLUMNS = {
    "revenue_m": "revenue_m", "revenue_prev_m": "revenue_prev_m", "revenue_2y_ago_m": "revenue_2y_ago_m",
    "net_income_m": "net_income_m", "cfo_m": "cfo_m", "ebitda_m": "ebitda_m", "cash_m": "cash_m", "debt_m": "debt_m",
    "post_issue_shares_m": "post_issue_shares_m",
}


def _iso(d: str | None) -> date | None:
    if not d:
        return None
    s = str(d).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s[:10] if fmt == "%Y-%m-%d" else s[:8], fmt).date()
        except ValueError:
            continue
    return None


def _months_between(a: date, b: date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def _facts(facts: dict, concepts: tuple[str, ...], units: tuple[str, ...], taxonomies=TAXONOMIES) -> list[dict]:
    out = []
    for tax in taxonomies:
        for concept in concepts:
            node = facts.get("facts", {}).get(tax, {}).get(concept, {})
            for unit, vals in node.get("units", {}).items():
                if unit not in units:
                    continue
                for x in vals:
                    if x.get("val") is None or not x.get("end") or not x.get("filed"):
                        continue
                    out.append({**x, "_concept": concept, "_unit": unit})
        if out:
            break  # first taxonomy with data wins (us-gaap before ifrs-full)
    return out


def _admissible(fact: dict, listing: date) -> bool:
    end = _iso(fact.get("end"))
    filed = _iso(fact.get("filed"))
    if end is None or filed is None or end > listing:
        return False
    return 0 <= _months_between(listing, filed) <= MAX_MONTHS_AFTER_LISTING


def _earliest_filed_by_end(facts: list[dict]) -> dict[str, dict]:
    """{period_end: fact} keeping the earliest-filed fact per period end."""
    best: dict[str, dict] = {}
    for f in facts:
        end = f["end"]
        cur = best.get(end)
        if cur is None or (f["filed"], f.get("accn", "")) < (cur["filed"], cur.get("accn", "")):
            best[end] = f
    return best


def _annual(facts: list[dict]) -> list[dict]:
    out = []
    for f in facts:
        s, e = _iso(f.get("start")), _iso(f.get("end"))
        if s is None or e is None:
            continue
        if ANNUAL_MIN_DAYS <= (e - s).days <= ANNUAL_MAX_DAYS:
            out.append(f)
    return out


def _obs(fact: dict, listing: date, *, rule: str, confidence: float, available_at: date | None = None) -> dict:
    return {
        "value": float(fact["val"]) / 1_000_000,
        "period_start": fact.get("start", "") or "",
        "period_end": fact["end"],
        "source_form": fact.get("form", "") or "",
        "filed": fact["filed"],
        "accn": fact.get("accn", "") or "",
        "available_at": (available_at or listing).isoformat(),
        "availability_rule": rule,
        "confidence": confidence,
        "concept": fact["_concept"],
        "unit": "USD_m",
    }


def extract_pre_ipo_financials(facts: dict, listing_date: str) -> dict:
    """Observations keyed by IPO feature name, or {} when nothing qualifies.
    Pure: no I/O. See module docstring for the selection rules."""
    listing = _iso(listing_date)
    if listing is None or not isinstance(facts, dict):
        return {}
    out: dict[str, dict] = {}
    usd = ("USD",)
    annual: dict[str, dict[str, dict]] = {}
    for key, concepts in DURATION_CONCEPTS.items():
        rows = [f for f in _annual(_facts(facts, concepts, usd)) if _admissible(f, listing)]
        annual[key] = _earliest_filed_by_end(rows)
    rev_ends = sorted(annual["revenue"], reverse=True)
    for name, idx in (("revenue_m", 0), ("revenue_prev_m", 1), ("revenue_2y_ago_m", 2)):
        if len(rev_ends) > idx:
            out[name] = _obs(annual["revenue"][rev_ends[idx]], listing, rule=RULE_COMPARATIVE, confidence=CONF_COMPARATIVE)
    for name, key in (("net_income_m", "net_income"), ("cfo_m", "cfo")):
        ends = sorted(annual[key], reverse=True)
        if ends:
            out[name] = _obs(annual[key][ends[0]], listing, rule=RULE_COMPARATIVE, confidence=CONF_COMPARATIVE)
    # EBITDA only when operating income and D&A exist for the same fiscal year.
    common = sorted(set(annual["operating_income"]) & set(annual["d_and_a"]), reverse=True)
    if common:
        oi, da = annual["operating_income"][common[0]], annual["d_and_a"][common[0]]
        ob = _obs(oi, listing, rule=RULE_COMPARATIVE, confidence=CONF_COMPARATIVE)
        ob["value"] = (float(oi["val"]) + float(da["val"])) / 1_000_000
        ob["concept"] = f"{oi['_concept']}+{da['_concept']}"
        out["ebitda_m"] = ob
    instant: dict[str, dict[str, dict]] = {}
    for key, concepts in INSTANT_CONCEPTS.items():
        rows = [f for f in _facts(facts, concepts, usd) if not f.get("start") and _admissible(f, listing)]
        instant[key] = _earliest_filed_by_end(rows)
    # Balance-sheet date: the latest fiscal-year end at or before listing for
    # which an annual income-statement fact exists; fall back to any instant.
    fy_ends = set(rev_ends) | set(annual["net_income"])
    candidates = sorted((e for e in instant["cash"] if e in fy_ends), reverse=True) or sorted(instant["cash"], reverse=True)
    if candidates:
        out["cash_m"] = _obs(instant["cash"][candidates[0]], listing, rule=RULE_COMPARATIVE, confidence=CONF_COMPARATIVE)
    debt_ends = sorted((e for e in instant["debt"] if e in fy_ends), reverse=True) or sorted(instant["debt"], reverse=True)
    if debt_ends:
        out["debt_m"] = _obs(instant["debt"][debt_ends[0]], listing, rule=RULE_COMPARATIVE, confidence=CONF_COMPARATIVE)
    else:
        both = sorted(set(instant["debt_current"]) & set(instant["debt_noncurrent"]), reverse=True)
        both = [e for e in both if e in fy_ends] or both
        if both:
            c, n = instant["debt_current"][both[0]], instant["debt_noncurrent"][both[0]]
            ob = _obs(c, listing, rule=RULE_COMPARATIVE, confidence=CONF_COMPARATIVE)
            ob["value"] = (float(c["val"]) + float(n["val"])) / 1_000_000
            ob["concept"] = f"{c['_concept']}+{n['_concept']}"
            out["debt_m"] = ob
    shares = _facts(facts, ("EntityCommonStockSharesOutstanding",), ("shares",), taxonomies=("dei",))
    shares = [f for f in shares if _iso(f["filed"]) is not None and 0 <= _months_between(listing, _iso(f["filed"])) <= MAX_MONTHS_AFTER_LISTING]
    if shares:
        first = min(shares, key=lambda f: (f["filed"], f["end"]))
        ob = _obs(first, listing, rule=RULE_FIRST_REPORT, confidence=CONF_FIRST_REPORT, available_at=_iso(first["filed"]))
        ob["unit"] = "shares_m"
        out["post_issue_shares_m"] = ob
    return out


def apply_observations(db: Session, ipo: IPO, obs: dict, source_url: str) -> dict:
    """Persist observations as FeatureObservation rows (upsert on the unique
    key) and fill IPO columns that are currently None. Never overwrites a
    value another source already supplied. Returns {"observations": n,
    "columns_filled": [..]}."""
    from .pipeline import add_provenance  # local import: pipeline imports scoring which imports models
    filled: list[str] = []
    n = 0
    for name, ob in obs.items():
        existing = db.scalar(select(FeatureObservation).where(
            FeatureObservation.ipo_id == ipo.id, FeatureObservation.field_name == name,
            FeatureObservation.period_end == ob["period_end"], FeatureObservation.source_name == SOURCE_NAME))
        raw = {k: v for k, v in ob.items() if k != "value"}
        if existing is None:
            existing = FeatureObservation(ipo_id=ipo.id, field_name=name, source_name=SOURCE_NAME, period_end=ob["period_end"])
            db.add(existing)
        existing.value = ob["value"]
        existing.unit = ob.get("unit", "USD_m")
        existing.source_url = source_url
        existing.source_tier = 1
        existing.source_form = ob.get("source_form", "")
        existing.period_start = ob.get("period_start", "")
        existing.available_at = ob["available_at"]
        existing.availability_rule = ob["availability_rule"]
        existing.confidence = ob["confidence"]
        existing.observed_at = datetime.now(timezone.utc)
        existing.raw = raw
        n += 1
        col = IPO_COLUMNS.get(name)
        if col and getattr(ipo, col) is None:
            setattr(ipo, col, ob["value"])
            add_provenance(db, ipo, col, ob["value"], PROVENANCE_SOURCE, source_url, 1)
            filled.append(col)
    if filled:
        ipo.updated_at = datetime.now(timezone.utc)
    db.flush()
    return {"observations": n, "columns_filled": filled}
