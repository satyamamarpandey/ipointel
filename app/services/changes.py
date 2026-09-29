from __future__ import annotations
"""Point-in-time score-change attribution. Diffs consecutive ScoreSnapshot rows that
were persisted as-of the time each ingestion actually happened (app.services.pipeline
only writes a new snapshot when the score moved). Nothing here is recomputed
retroactively from today's data."""
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import ScoreSnapshot
from .identity import sanitize_label

def timeline(db: Session, ipo_id: int) -> list[dict]:
    snaps = db.scalars(select(ScoreSnapshot).where(ScoreSnapshot.ipo_id == ipo_id).order_by(ScoreSnapshot.created_at.asc())).all()
    out = []
    prev = None
    for s in snaps:
        entry = {
            "at": s.created_at.isoformat(), "overall": s.overall_score, "listing": s.listing_score,
            "long_term": s.long_term_score, "confidence": s.confidence, "recommendation": sanitize_label(s.recommendation),
            "model_version": s.model_version, "event": s.event_stage or "",
        }
        if prev is None:
            entry["delta_overall"] = None
            entry["drivers"] = ["Initial score at first ingestion. No prior snapshot to compare."]
            entry["attribution"] = ["new event: " + (s.event_stage or "initial score")]
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
            # Every score movement is attributed to at least one named cause:
            # a changed input feature, a model version, or the event that
            # triggered this snapshot. Never an unexplained move.
            attribution = []
            fa, fb = (prev.feature_snapshot or {}), (s.feature_snapshot or {})
            changed = sorted(k for k in set(fa) | set(fb) if fa.get(k) != fb.get(k))
            if changed:
                attribution.append("feature: " + ", ".join(changed[:8]) + (" ..." if len(changed) > 8 else ""))
            if prev.model_version != s.model_version:
                attribution.append(f"model version: {prev.model_version} -> {s.model_version}")
            if prev.feature_schema_version != s.feature_schema_version:
                attribution.append(f"feature schema: {prev.feature_schema_version or 'none'} -> {s.feature_schema_version or 'none'}")
            if s.event_stage:
                attribution.append("event: " + s.event_stage)
            if not attribution:
                attribution.append("no input change recorded: identical features and model, re-evaluation only")
            entry["attribution"] = attribution
            if sanitize_label(prev.recommendation) != sanitize_label(s.recommendation):
                entry["recommendation_change"] = f"{sanitize_label(prev.recommendation)} -> {sanitize_label(s.recommendation)}"
        out.append(entry)
        prev = s
    return out
