from __future__ import annotations
"""Decides WHICH material events get an email queued, for WHICH leads,
respecting per-lead alert-type preferences and market filter. Never sends
anything itself - only calls email_queue.enqueue(), which is a fast local
insert. Delivery happens in email_queue.process_queue(), called separately
so a slow/unavailable provider never blocks scoring or ingestion."""
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import WaitlistLead, IPO, ScoreSnapshot
from ..config import get_settings
from . import redflags as redflags_svc
from . import email_provider as ep
from .email_queue import enqueue

def _material_kind(score: ScoreSnapshot, previous: ScoreSnapshot | None) -> str | None:
    if "NO RECOMMENDATION" in score.recommendation:
        return None
    if previous is None:
        return "score" if (score.confidence >= get_settings().min_recommendation_confidence and score.overall_score >= 60) else None
    if score.recommendation != previous.recommendation:
        return "recommendation"
    if score.valuation_label != previous.valuation_label and score.valuation_label != "INSUFFICIENT DATA":
        return "valuation"
    if abs(score.overall_score - previous.overall_score) >= 5 or abs(score.listing_score - previous.listing_score) >= 7 or abs(score.long_term_score - previous.long_term_score) >= 7:
        return "score"
    return None

def _matches_market(lead: WaitlistLead, country: str) -> bool:
    if lead.markets == "india":
        return country == "India"
    if lead.markets == "us":
        return country == "United States"
    return True

def _wants(lead: WaitlistLead, kind: str) -> bool:
    return {"recommendation": lead.alert_recommendation_change, "valuation": lead.alert_score_change,
            "score": lead.alert_score_change, "red_flag": lead.alert_red_flag}.get(kind, False)

def queue_score_alerts(db: Session, limit_scores: int = 30) -> dict:
    scores = db.scalars(select(ScoreSnapshot).order_by(ScoreSnapshot.created_at.desc()).limit(limit_scores)).all()
    leads = db.scalars(select(WaitlistLead).where(WaitlistLead.consent == True, WaitlistLead.suppressed == False)).all()
    queued = 0
    template_by_kind = {"recommendation": "recommendation_alert", "valuation": "valuation_alert", "score": "score_alert"}
    priority_by_kind = {"recommendation": ep.PRIORITY_RECOMMENDATION, "valuation": ep.PRIORITY_SCORE_ALERT, "score": ep.PRIORITY_SCORE_ALERT}
    for sc in scores:
        ipo = db.get(IPO, sc.ipo_id)
        if not ipo:
            continue
        prev = db.scalars(select(ScoreSnapshot).where(ScoreSnapshot.ipo_id == ipo.id, ScoreSnapshot.id != sc.id, ScoreSnapshot.created_at <= sc.created_at).order_by(ScoreSnapshot.created_at.desc()).limit(1)).first()
        kind = _material_kind(sc, prev)
        if not kind:
            continue
        template = template_by_kind[kind]
        for lead in leads:
            if not _matches_market(lead, ipo.country) or not _wants(lead, kind):
                continue
            msg = enqueue(db, lead, template, priority_by_kind[kind], dedupe_key=str(sc.id), ipo_id=ipo.id, score_snapshot_id=sc.id)
            if msg:
                queued += 1
    return {"queued": queued}

def queue_red_flag_alerts(db: Session) -> dict:
    ipos = db.scalars(select(IPO).where(IPO.status.in_(["Open", "Upcoming", "Filed"]))).all()
    leads = db.scalars(select(WaitlistLead).where(WaitlistLead.consent == True, WaitlistLead.suppressed == False, WaitlistLead.alert_red_flag == True)).all()
    queued = 0
    for ipo in ipos:
        flags = redflags_svc.evaluate(ipo)
        if not any(f["severity"] == "CRITICAL" for f in flags):
            continue
        sc = db.scalars(select(ScoreSnapshot).where(ScoreSnapshot.ipo_id == ipo.id).order_by(ScoreSnapshot.created_at.desc()).limit(1)).first()
        if not sc:
            continue
        for lead in leads:
            if not _matches_market(lead, ipo.country):
                continue
            msg = enqueue(db, lead, "red_flag_alert", ep.PRIORITY_RECOMMENDATION, dedupe_key=f"ipo-{ipo.id}", ipo_id=ipo.id, score_snapshot_id=sc.id)
            if msg:
                queued += 1
    return {"queued": queued}

def send_pending(db: Session) -> dict:
    settings = get_settings()
    if not settings.enable_email:
        return {"queued": 0, "note": "email disabled"}
    a = queue_score_alerts(db)
    b = queue_red_flag_alerts(db)
    db.commit()
    return {"queued": a["queued"] + b["queued"]}
