from __future__ import annotations
"""Point-in-time score-change attribution. Diffs consecutive ScoreSnapshot rows that
were persisted as-of the time each ingestion actually happened (app.services.pipeline
only writes a new snapshot when the score moved) — nothing here is recomputed
retroactively from today's data."""
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import ScoreSnapshot

def timeline(db: Session, ipo_id: int) -> list[dict]:
    snaps = db.scalars(select(ScoreSnapshot).where(ScoreSnapshot.ipo_id == ipo_id).order_by(ScoreSnapshot.created_at.asc())).all()
    out = []
    prev = None
    for s in snaps:
        entry = {
            "at": s.created_at.isoformat(), "overall": s.overall_score, "listing": s.listing_score,
            "long_term": s.long_term_score, "confidence": s.confidence, "recommendation": s.recommendation,
            "model_version": s.model_version,
        }
        if prev is None:
            entry["delta_overall"] = None
            entry["drivers"] = ["Initial score at first ingestion — no prior snapshot to compare."]
        else:
            delta = round(s.overall_score - prev.overall_score, 1)
            entry["delta_overall"] = delta
            drivers = []
            pillars_a, pillars_b = (prev.pillars or {}), (s.pillars or {})
            for k in pillars_b:
                if k in pillars_a:
                    d = round(pillars_b[k] - pillars_a[k], 1)
                    if abs(d) >= 1.0:
                        drivers.append({"pillar": k, "delta": d})
            drivers.sort(key=lambda x: -abs(x["delta"]))
            entry["drivers"] = drivers[:6]
            if prev.recommendation != s.recommendation:
                entry["recommendation_change"] = f"{prev.recommendation} -> {s.recommendation}"
        out.append(entry)
        prev = s
    return out
