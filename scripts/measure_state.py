#!/usr/bin/env python
"""Re-measure the production dataset. Prints a Markdown report and writes
docs/ai_control/measurements/latest.json so every phase compares against
numbers taken from the database, never from memory.

Run against the production snapshot:
    git fetch origin data-state && git show FETCH_HEAD:data/ipo.db > data/ipo.db
    python scripts/measure_state.py
"""
from __future__ import annotations
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select, func  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import IPO, ScoreSnapshot, PerformanceSnapshot, IngestionRun  # noqa: E402
from app.services import walkforward  # noqa: E402
from app.services.forward_grading import forward_ledger, ledger_counts  # noqa: E402
from scripts.build_pages import classify  # noqa: E402

FEATURE_FIELDS = {
    "identity": lambda i: bool(i.isin or i.symbol),
    "symbol": lambda i: bool(i.symbol),
    "offer_price": lambda i: (i.final_price or 0) > 0,
    "price_band": lambda i: i.price_high is not None,
    "revenue": lambda i: i.revenue_m is not None,
    "growth": lambda i: i.revenue_m is not None and i.revenue_prev_m is not None,
    "ebitda": lambda i: i.ebitda_m is not None,
    "pat": lambda i: i.net_income_m is not None,
    "cash_flow": lambda i: i.cfo_m is not None,
    "debt": lambda i: i.debt_m is not None,
    "valuation": lambda i: i.post_issue_shares_m is not None and (i.peer_median_pe is not None or i.peer_median_ps is not None),
    "issue_size": lambda i: i.issue_size_m is not None,
    "issue_structure": lambda i: i.fresh_issue_pct is not None or i.ofs_pct is not None,
    "subscription": lambda i: i.total_sub is not None,
    "subscription_by_category": lambda i: i.qib_sub is not None,
}


def pct(n: int, d: int) -> float | None:
    return round(n / d * 100, 1) if d else None


def coverage(rows: list[IPO], perf_ids: set[int]) -> dict:
    n = len(rows)
    out = {"n": n}
    for name, fn in FEATURE_FIELDS.items():
        k = sum(1 for r in rows if fn(r))
        out[name] = {"n": k, "pct": pct(k, n)}
    k = sum(1 for r in rows if r.id in perf_ids)
    out["performance"] = {"n": k, "pct": pct(k, n)}
    return out


def score_distribution(db, country: str) -> dict:
    rows = db.execute(select(ScoreSnapshot.listing_score, ScoreSnapshot.confidence).join(IPO, IPO.id == ScoreSnapshot.ipo_id)
                      .where(IPO.country == country, IPO.status == "Listed")).all()
    if not rows:
        return {"n": 0}
    ls = sorted(r[0] for r in rows)
    cf = sorted(r[1] for r in rows)
    q = lambda xs, p: xs[min(len(xs) - 1, int(p * len(xs)))]  # noqa: E731
    return {"n": len(ls), "listing_score_p10_p50_p90": [q(ls, .1), q(ls, .5), q(ls, .9)],
            "confidence_p10_p50_p90": [q(cf, .1), q(cf, .5), q(cf, .9)],
            "distinct_listing_scores": len(set(round(x, 1) for x in ls))}


def main() -> int:
    init_db()
    db = SessionLocal()
    now = datetime.now(timezone.utc)
    try:
        c = classify(db, now)
        perf_ids = set(db.scalars(select(PerformanceSnapshot.ipo_id).distinct()).all())
        all_rows = db.scalars(select(IPO)).all()
        by = lambda country, status=None: [r for r in all_rows if r.country == country and (status is None or r.status == status)]  # noqa: E731
        report: dict = {"generated_at": now.isoformat(), "total_rows": len(all_rows)}
        for country, key in (("India", "india"), ("United States", "us")):
            listed = by(country, "Listed")
            active = [r for r in all_rows if r.country == country and r.status in ("Filed", "Upcoming", "Open", "Closed", "Priced")]
            published_upcoming = [r for r in c["upcoming"] if r.country == country]
            published_history = [r for r in c["history_in"] if r.country == country]
            block = {
                "upcoming_discovered": len(active), "upcoming_published": len(published_upcoming),
                "listed": len(listed), "listed_published_5y": len(published_history),
                "symbols_resolved": sum(1 for r in listed if r.symbol), "symbols_missing": sum(1 for r in listed if not r.symbol),
                "isin_present": sum(1 for r in listed if r.isin),
                "performance_present": sum(1 for r in listed if r.id in perf_ids), "performance_missing": sum(1 for r in listed if r.id not in perf_ids),
                "final_price_present": sum(1 for r in listed if (r.final_price or 0) > 0), "final_price_missing": sum(1 for r in listed if not ((r.final_price or 0) > 0)),
                "not_ipo": len(by(country, "Not IPO")), "withdrawn": len(by(country, "Withdrawn")),
                "status_counts": {s: len(by(country, s)) for s in sorted({r.status for r in by(country)})},
                "feature_coverage": {"upcoming": coverage(active, perf_ids), "listed": coverage(listed, perf_ids)},
            }
            if country == "India":
                block["mainboard_listed"] = sum(1 for r in listed if r.board == "Mainboard")
                block["sme_listed"] = sum(1 for r in listed if r.board == "SME")
                block["feature_coverage"]["mainboard"] = coverage([r for r in listed if r.board == "Mainboard"], perf_ids)
                block["feature_coverage"]["sme"] = coverage([r for r in listed if r.board == "SME"], perf_ids)
            report[key] = block
        ledger = forward_ledger(db, now.date())
        report["forward"] = ledger_counts(ledger)
        report["forward"]["by_event_stage"] = dict(sorted({}.items()))
        stages: dict[str, int] = {}
        for row in ledger:
            stages[row.get("event_stage") or ""] = stages.get(row.get("event_stage") or "", 0) + 1
        report["forward"]["by_event_stage"] = stages
        dup = db.execute(select(ScoreSnapshot.ipo_id, ScoreSnapshot.event_stage, ScoreSnapshot.overall_score, ScoreSnapshot.listing_score, ScoreSnapshot.confidence, ScoreSnapshot.recommendation, func.count())
                         .group_by(ScoreSnapshot.ipo_id, ScoreSnapshot.event_stage, ScoreSnapshot.overall_score, ScoreSnapshot.listing_score, ScoreSnapshot.confidence, ScoreSnapshot.recommendation)
                         .having(func.count() > 1)).all()
        report["snapshots"] = {"total": db.scalar(select(func.count()).select_from(ScoreSnapshot)),
                               "ipos_with_snapshot": db.scalar(select(func.count(func.distinct(ScoreSnapshot.ipo_id)))),
                               "duplicate_groups": len(dup), "duplicate_rows_beyond_first": sum(r[-1] - 1 for r in dup)}
        from app.services import prospectus_financials
        report["us_financial_sources"] = {"prospectus_vs_xbrl": prospectus_financials.agreement_with_xbrl(db)}
        model = walkforward.evaluate(db)
        report["model"] = {}
        for country in ("India", "United States"):
            m = model[country]
            lb, tb = m["listing_model"], m["long_term_model"]
            report["model"][country] = {
                "listed_with_score": m["total_listed_with_score"],
                "listing": {k: lb.get(k) for k in ("sample_size", "status", "auc", "brier_score", "log_loss", "positive_rate_actual_pct", "avg_predicted_probability_pct", "calibration", "band_breakdown")},
                "long_term": {k: tb.get(k) for k in ("sample_size", "status", "auc", "brier_score", "positive_rate_actual_pct", "avg_predicted_probability_pct")},
                "score_distribution": score_distribution(db, country),
            }
        health = []
        for src in ("SEC EDGAR", "SEC Priced IPOs", "NSE", "NSE Primary Market Reports", "SEC 424B4 backfill"):
            run = db.scalar(select(IngestionRun).where(IngestionRun.source == src).order_by(IngestionRun.started_at.desc()).limit(1))
            health.append({"source": src, "status": run.status if run else "never run", "last_run": run.started_at.isoformat() if run else None, "rows": run.rows_seen if run else 0})
        report["source_health"] = health
        report["publish"] = {"upcoming": len(c["upcoming"]), "history_5y": len(c["history_in"]), "excluded": len(c["excluded"]), "withdrawn": len(c["withdrawn"])}
    finally:
        db.close()
    out = ROOT / "docs" / "ai_control" / "measurements"
    out.mkdir(parents=True, exist_ok=True)
    (out / "latest.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    (out / f"{now.date().isoformat()}.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(render(report))
    return 0


def render(r: dict) -> str:
    L = [f"# Measurement {r['generated_at'][:16]}Z", ""]
    for key, label in (("india", "INDIA"), ("us", "US")):
        b = r[key]
        L.append(f"## {label}")
        L.append(f"- upcoming discovered / published: {b['upcoming_discovered']} / {b['upcoming_published']}")
        L.append(f"- Listed: {b['listed']} (published in 5y window: {b['listed_published_5y']})")
        L.append(f"- symbols resolved / missing: {b['symbols_resolved']} / {b['symbols_missing']}")
        L.append(f"- performance present / missing: {b['performance_present']} / {b['performance_missing']}")
        L.append(f"- final price present / missing: {b['final_price_present']} / {b['final_price_missing']}")
        if key == "india":
            L.append(f"- Mainboard / SME listed: {b['mainboard_listed']} / {b['sme_listed']}")
        L.append(f"- Not IPO: {b['not_ipo']}, Withdrawn: {b['withdrawn']}")
        cov = b["feature_coverage"]["listed"]
        L.append("- Listed feature coverage: " + ", ".join(f"{k} {v['pct']}%" for k, v in cov.items() if isinstance(v, dict)))
        cov = b["feature_coverage"]["upcoming"]
        L.append("- Upcoming feature coverage: " + ", ".join(f"{k} {v['pct']}%" for k, v in cov.items() if isinstance(v, dict)))
        L.append("")
    f = r["forward"]
    L.append("## FORWARD")
    L.append(f"- total genuine forward predictions: {f['total']}")
    L.append("- " + ", ".join(f"{k} {v}" for k, v in f["by_category"].items()))
    L.append("- top blocked reasons: " + "; ".join(f"{k} ({v})" for k, v in list(f["blocked_reasons"].items())[:6]))
    s = r["snapshots"]
    L.append(f"- snapshots: {s['total']} for {s['ipos_with_snapshot']} IPOs; duplicate groups {s['duplicate_groups']} (redundant rows {s['duplicate_rows_beyond_first']})")
    L.append("")
    L.append("## MODEL")
    for country, m in r["model"].items():
        lb = m["listing"]
        L.append(f"- {country} listing: n={lb['sample_size']} positive={lb['positive_rate_actual_pct']}% AUC={lb['auc']} Brier={lb['brier_score']} avg_pred={lb['avg_predicted_probability_pct']}% | {lb['status']}")
        L.append(f"  score distribution: {m['score_distribution']}")
        tb = m["long_term"]
        L.append(f"- {country} long-term (12m): n={tb['sample_size']} positive={tb['positive_rate_actual_pct']}% AUC={tb['auc']} Brier={tb['brier_score']}")
    L.append("")
    L.append("## SOURCES")
    for h in r["source_health"]:
        L.append(f"- {h['source']}: {h['status']} at {h['last_run']} (rows {h['rows']})")
    return "\n".join(L)


if __name__ == "__main__":
    sys.exit(main())
