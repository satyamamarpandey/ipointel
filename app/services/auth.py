from __future__ import annotations
"""Beta access: passwordless magic-link login. No passwords are ever stored.
A LoginToken is single-use and short-lived; a successful redemption issues a
session (AuthSession), identified to the browser only by an HttpOnly cookie.
Only SHA-256 hashes of both token types are persisted - the raw values exist
only in the email link / the cookie, never at rest, so a DB read alone can
never be replayed as a login."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import WaitlistLead, LoginToken, AuthSession
from . import email_provider as ep
from . import email_templates as tpl

SESSION_COOKIE = "ipo_session"
LOGIN_TOKEN_TTL_MINUTES = 15
SESSION_TTL_DAYS = 14

def now(): return datetime.now(timezone.utc)

def _aware(dt):
    # SQLite doesn't persist tzinfo even on a DateTime(timezone=True) column,
    # so a value read back in a fresh query is naive UTC - normalize before
    # comparing against now() (which is always aware). Same issue/fix as
    # email_queue._aware().
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()

def create_login_token(db: Session, lead: WaitlistLead, purpose: str = "login") -> str:
    raw = secrets.token_urlsafe(32)
    db.add(LoginToken(lead_id=lead.id, token_hash=_hash(raw), purpose=purpose,
                       expires_at=now() + timedelta(minutes=LOGIN_TOKEN_TTL_MINUTES)))
    db.flush()
    return raw

def send_login_email(settings, lead: WaitlistLead, raw_token: str) -> ep.SendResult:
    """Sent synchronously (not queued): the secret token must have the
    smallest possible at-rest/in-flight exposure window, and a user waiting
    to sign in wants immediate feedback, not a 15s worker-poll delay."""
    login_url = f"{settings.public_base_url}/auth/callback?token={raw_token}"
    subject, html, text = tpl.login_link_email(settings.public_base_url, login_url, lead.unsubscribe_token)
    provider = ep.get_provider(settings)
    return provider.send(lead.email, subject, html, text)

def redeem_login_token(db: Session, raw_token: str) -> tuple[WaitlistLead | None, str]:
    row = db.scalar(select(LoginToken).where(LoginToken.token_hash == _hash(raw_token)))
    if not row:
        return None, "invalid_token"
    if row.used_at is not None:
        return None, "already_used"
    if row.revoked_at is not None:
        return None, "revoked"
    if _aware(row.expires_at) < now():
        return None, "expired"
    lead = db.get(WaitlistLead, row.lead_id)
    if not lead or lead.access_status == "DISABLED":
        return None, "disabled"
    row.used_at = now()
    if lead.access_status == "INVITED":
        lead.access_status = "ACTIVE"
    lead.last_login_at = now()
    db.flush()
    return lead, ""

def create_session(db: Session, lead: WaitlistLead, user_agent: str = "") -> str:
    raw = secrets.token_urlsafe(32)
    db.add(AuthSession(lead_id=lead.id, session_hash=_hash(raw), user_agent=user_agent[:300],
                        expires_at=now() + timedelta(days=SESSION_TTL_DAYS)))
    db.flush()
    return raw

def get_lead_from_session(db: Session, raw_session: str | None) -> WaitlistLead | None:
    if not raw_session:
        return None
    row = db.scalar(select(AuthSession).where(AuthSession.session_hash == _hash(raw_session)))
    if not row or row.revoked_at is not None or _aware(row.expires_at) < now():
        return None
    lead = db.get(WaitlistLead, row.lead_id)
    if not lead or lead.access_status != "ACTIVE":
        return None
    return lead

def revoke_all_sessions(db: Session, lead_id: int):
    for row in db.scalars(select(AuthSession).where(AuthSession.lead_id == lead_id, AuthSession.revoked_at.is_(None))):
        row.revoked_at = now()
