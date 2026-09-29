#!/usr/bin/env python
"""Walk-forward model research report (Phases 12-16).

Builds leakage-safe datasets per market from the current database, evaluates
trivial baselines and a ridge-logistic model out of sample by listing year,
and applies the release gate. Writes docs/ai_control/measurements/model_latest.json
and prints a Markdown summary. Read-only: never writes to the database.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import SessionLocal, init_db  # noqa: E402
from app.services import model_eval  # noqa: E402


def render(report: dict) -> str:
    L = [f"# Model research {report['generated_at'][:16]}Z", "", report["method"], ""]
    for country, m in report["markets"].items():
        L.append(f"## {country} (rows {m['rows']})")
        L.append("Feature coverage: " + ", ".join(f"{k} {v}%" for k, v in m["feature_coverage_pct"].items()))
        for target, t in m["targets"].items():
            oos, b = t["out_of_sample"], t["baselines"]
            L.append(f"### {target}: usable rows {t['usable_rows']}, out-of-sample n {oos.get('n', 0)}")
            if oos.get("n"):
                L.append(f"- model: AUC {oos['auc']} PR-AUC {oos['pr_auc']} Brier {oos['brier']} logloss {oos['log_loss']} ECE {oos['ece']} positive {oos['positive_rate_pct']}%")
                for name, bm in b.items():
                    if bm.get("n"):
                        L.append(f"- baseline {name}: AUC {bm['auc']} Brier {bm['brier']} logloss {bm['log_loss']}")
                g = t["release_gate"]
                L.append(f"- release gate: {'PASSED' if g['passed'] else 'NOT MET'} {g['checks']} fold stability {g['fold_stability_share']}")
                L.append("- folds: " + "; ".join(f"{f['year']}: n={f['n_test']} auc={f.get('auc')}" if not f.get("skipped") else f"{f['year']}: skipped" for f in t["folds"]))
            else:
                L.append("- no out-of-sample predictions (insufficient training history)")
        L.append("")
    return "\n".join(L)


def main() -> int:
    init_db()
    db = SessionLocal()
    try:
        report = model_eval.evaluate(db)
    finally:
        db.close()
    out = ROOT / "docs" / "ai_control" / "measurements"
    out.mkdir(parents=True, exist_ok=True)
    (out / "model_latest.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
