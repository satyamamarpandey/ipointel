from __future__ import annotations
"""Weekly digest: selection logic + queueing. Rendering itself lives in
email_templates.digest_email; this module only decides who gets one, and what
goes in it, both computed fresh at send time from real data."""
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import IPO, ScoreSnapshot, WaitlistLead
from . import redflags as redflags_svc
from . import email_provider as ep
from . import email_templates as tpl
from .email_queue import enqueue

def _top_opportunities(db: Session, country: str, limit: int = 5) -> list[dict]:
    ipos = db.scalars(select(IPO).where(IPO.country == country, IPO.status.in_(["Open", "Upcoming", "Filed"]))).all()
    rows = []
    for ipo in ipos:
        sc = db.scalars(select(ScoreSnapshot).where(ScoreSnapshot.ipo_id == ipo.id).order_by(ScoreSnapshot.created_at.desc()).limit(1)).first()
        if sc and sc.confidence >= 50:
            rows.append({"company": ipo.company, "overall": sc.overall_score, "listing": sc.listing_score,
                         "long_term": sc.long_term_score, "valuation": sc.valuation_label, "confidence": sc.confidence})
    rows.sort(key=lambda r: -r["overall"])
    return rows[:limit]

def _biggest_changes(db: Session, since: datetime, limit: int = 6) -> list[dict]:
    snaps = db.scalars(select(ScoreSnapshot).where(ScoreSnapshot.created_at >= since).order_by(ScoreSnapshot.created_at.desc())).all()
    out = []
    for sc in snaps:
        prev = db.scalars(select(ScoreSnapshot).where(ScoreSnapshot.ipo_id == sc.ipo_id, ScoreSnapshot.id != sc.id, ScoreSnapshot.created_at < sc.created_at).order_by(ScoreSnapshot.created_at.desc()).limit(1)).first()
        if not prev:
            continue
        delta = sc.overall_score - prev.overall_score
        if abs(delta) < 5:
            continue
        ipo = db.get(IPO, sc.ipo_id)
        out.append({"delta": delta, "label": f"{ipo.company}: {prev.overall_score:.0f} -> {sc.overall_score:.0f} ({delta:+.0f})"})
    out.sort(key=lambda r: -abs(r["delta"]))
    return out[:limit]

def _new_filings(db: Session, since: datetime, limit: int = 6) -> list[dict]:
    ipos = db.scalars(select(IPO).where(IPO.first_seen_at >= since).order_by(IPO.first_seen_at.desc()).limit(limit)).all()
    return [{"company": f"{i.company} ({i.country})"} for i in ipos]

def _critical_red_flags(db: Session, limit: int = 6) -> list[dict]:
    ipos = db.scalars(select(IPO).where(IPO.status.in_(["Open", "Upcoming", "Filed"]))).all()
    out = []
    for ipo in ipos:
        flags = redflags_svc.evaluate(ipo)
        crit = [f for f in flags if f["severity"] in ("CRITICAL", "HIGH")]
        if crit:
            out.append({"label": f"{ipo.company}: {crit[0]['title']}"})
    return out[:limit]

def render_digest(db: Session, settings, lead: WaitlistLead) -> tuple[str, str, str]:
    since = datetime.now(timezone.utc) - timedelta(days=7)
    india = _top_opportunities(db, "India") if lead.markets in ("india", "both") else []
    us = _top_opportunities(db, "United States") if lead.markets in ("us", "both") else []
    return tpl.digest_email(settings.public_base_url, india, us, _biggest_changes(db, since), _new_filings(db, since), _critical_red_flags(db), lead.unsubscribe_token)

def queue_weekly_digests(db: Session) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    leads = db.scalars(select(WaitlistLead).where(WaitlistLead.consent == True, WaitlistLead.digest_weekly == True, WaitlistLead.suppressed == False)).all()
    n = 0
    for lead in leads:
        if lead.last_digest_at and lead.last_digest_at > cutoff:
            continue
        week_key = datetime.now(timezone.utc).strftime("%G-W%V")
        msg = enqueue(db, lead, "digest", ep.PRIORITY_DIGEST, dedupe_key=week_key)
        if msg:
            lead.last_digest_at = datetime.now(timezone.utc)
            n += 1
    db.commit()
    return n
