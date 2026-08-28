from __future__ import annotations
"""Queue-backed email delivery. Signups/alerts NEVER call the provider inline -
they call enqueue(), which is a fast local DB insert, and commit. This module's
process_queue() is what actually talks to the provider, called frequently by
the worker so delivery still feels prompt without coupling it to the HTTP
request/response cycle."""
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from ..models import EmailMessage, WaitlistLead, IPO, ScoreSnapshot
from ..config import get_settings
from . import email_provider as ep
from . import email_templates as tpl

def now():
    return datetime.now(timezone.utc)

def enqueue(db: Session, lead: WaitlistLead, template: str, priority: int, dedupe_key: str = "", ipo_id: int | None = None, score_snapshot_id: int | None = None, subject_hint: str = "") -> EmailMessage | None:
    msg = EmailMessage(lead_id=lead.id, ipo_id=ipo_id, score_snapshot_id=score_snapshot_id, email=lead.email,
                        template=template, dedupe_key=dedupe_key or template, subject=subject_hint,
                        priority=priority, status=ep.QUEUED)
    db.add(msg)
    try:
        db.flush()
        return msg
    except IntegrityError:
        db.rollback()
        return None  # already queued/sent for this (lead, template, dedupe_key)

def _render(db: Session, settings, msg: EmailMessage) -> tuple[str, str, str] | None:
    lead = db.get(WaitlistLead, msg.lead_id)
    if not lead:
        return None
    if msg.template == "welcome":
        return tpl.welcome_email(settings.public_base_url, lead.name, lead.referral_code, lead.markets, lead.unsubscribe_token)
    if msg.template in ("score_alert", "recommendation_alert", "valuation_alert", "red_flag_alert"):
        ipo = db.get(IPO, msg.ipo_id) if msg.ipo_id else None
        cur = db.get(ScoreSnapshot, msg.score_snapshot_id) if msg.score_snapshot_id else None
        if not ipo or not cur:
            return None
        prev_row = db.scalars(select(ScoreSnapshot).where(ScoreSnapshot.ipo_id == ipo.id, ScoreSnapshot.id != cur.id, ScoreSnapshot.created_at <= cur.created_at).order_by(ScoreSnapshot.created_at.desc()).limit(1)).first()
        prev = {"overall": prev_row.overall_score, "recommendation": prev_row.recommendation, "valuation": prev_row.valuation_label} if prev_row else {}
        cur_d = {"ipo_id": ipo.id, "overall": cur.overall_score, "listing": cur.listing_score, "long_term": cur.long_term_score,
                 "confidence": cur.confidence, "recommendation": cur.recommendation, "valuation": cur.valuation_label}
        kind = {"recommendation_alert": "recommendation", "valuation_alert": "valuation", "red_flag_alert": "red_flag"}.get(msg.template, "score")
        return tpl.alert_email(settings.public_base_url, kind, ipo.company, ipo.country, prev, cur_d, cur.rationale or [], cur.risks or [], lead.unsubscribe_token)
    if msg.template == "digest":
        from . import newsletter
        return newsletter.render_digest(db, settings, lead)
    return None

def _aware(dt):
    # SQLite doesn't persist tzinfo even on a DateTime(timezone=True) column,
    # so a value read back in a fresh query is naive UTC - normalize before
    # arithmetic against now() (which is always aware).
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

def _within_backoff(msg: EmailMessage) -> bool:
    if msg.attempt_count == 0:
        return False
    wait = min(3600, 2 ** msg.attempt_count * 30)  # 30s,60s,120s...capped at 1h
    last = _aware(msg.updated_at) or _aware(msg.queued_at)
    return (now() - last).total_seconds() < wait

def _counts_since(db: Session, since: datetime) -> int:
    return db.scalar(select(func.count()).select_from(EmailMessage).where(EmailMessage.status.in_([ep.SENT, ep.DELIVERED]), EmailMessage.sent_at >= since)) or 0

def process_queue(db: Session, limit: int = 25) -> dict:
    settings = get_settings()
    provider = ep.get_provider(settings)
    today_start = now().replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = today_start.replace(day=1)
    sent_today = _counts_since(db, today_start)
    sent_month = _counts_since(db, month_start)
    result = {"sent": 0, "failed": 0, "skipped_quota": 0, "skipped_suppressed": 0, "processed": 0}

    pending = db.scalars(select(EmailMessage).where(EmailMessage.status == ep.QUEUED).order_by(EmailMessage.priority.asc(), EmailMessage.queued_at.asc()).limit(limit * 3)).all()
    for msg in pending:
        if result["processed"] >= limit:
            break
        if _within_backoff(msg):
            continue
        lead = db.get(WaitlistLead, msg.lead_id)
        if not lead or lead.suppressed:
            msg.status = ep.SUPPRESSED
            msg.updated_at = now()
            result["skipped_suppressed"] += 1
            continue
        if msg.template != "welcome" and not lead.consent:
            msg.status = ep.UNSUBSCRIBED
            msg.updated_at = now()
            continue
        if sent_today >= settings.email_daily_soft_limit or sent_month >= settings.email_monthly_soft_limit:
            if msg.priority > ep.PRIORITY_TRANSACTIONAL:
                result["skipped_quota"] += 1
                continue  # leave QUEUED - transactional still gets a chance below, everything else waits

        rendered = _render(db, settings, msg)
        result["processed"] += 1
        if not rendered:
            msg.status = ep.FAILED
            msg.last_error = "template render failed (missing related record)"
            msg.failed_at = now()
            msg.updated_at = now()
            result["failed"] += 1
            continue
        subject, html, text = rendered
        msg.subject = subject
        msg.provider = settings.email_provider
        msg.status = ep.SENDING
        db.flush()
        r = provider.send(lead.email, subject, html, text)
        msg.attempt_count += 1
        msg.updated_at = now()
        if r.ok:
            msg.status = ep.SENT
            msg.provider_message_id = r.provider_message_id
            msg.sent_at = now()
            sent_today += 1
            sent_month += 1
            result["sent"] += 1
        else:
            msg.last_error = r.error[:2000]
            if not r.retryable or msg.attempt_count >= settings.email_max_attempts:
                msg.status = ep.FAILED
                msg.failed_at = now()
                result["failed"] += 1
            else:
                msg.status = ep.QUEUED  # retry next cycle, after backoff
    db.commit()
    return result
