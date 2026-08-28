from __future__ import annotations
"""Resend delivers webhooks signed the Svix way (Resend uses Svix under the
hood). Verification here is fail-closed: no configured secret, or a bad/stale
signature, and the payload is rejected outright - it is never trusted
unsigned, per the security requirement this exists to satisfy."""
import base64
import hashlib
import hmac
import time
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import EmailMessage, WaitlistLead
from . import email_provider as ep

TOLERANCE_SECONDS = 300

def verify_svix_signature(secret: str, svix_id: str, svix_timestamp: str, svix_signature: str, body: bytes) -> bool:
    if not secret or not svix_id or not svix_timestamp or not svix_signature:
        return False
    try:
        ts = int(svix_timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts) > TOLERANCE_SECONDS:
        return False
    key = secret[6:] if secret.startswith("whsec_") else secret
    try:
        key_bytes = base64.b64decode(key)
    except Exception:
        return False
    signed_content = f"{svix_id}.{svix_timestamp}.".encode() + body
    expected = base64.b64encode(hmac.new(key_bytes, signed_content, hashlib.sha256).digest()).decode()
    for part in svix_signature.split():
        candidate = part.split(",", 1)[1] if "," in part else part
        if hmac.compare_digest(candidate, expected):
            return True
    return False

def _now():
    return datetime.now(timezone.utc)

def handle_event(db: Session, event: dict) -> dict:
    etype = event.get("type", "")
    data = event.get("data", {}) or {}
    provider_id = data.get("email_id") or data.get("id") or ""
    to = data.get("to")
    recipient = (to[0] if isinstance(to, list) and to else to) or ""
    msg = db.scalar(select(EmailMessage).where(EmailMessage.provider_message_id == provider_id)) if provider_id else None

    if etype == "email.sent":
        if msg and msg.status not in (ep.DELIVERED,):
            msg.status = ep.SENT
    elif etype == "email.delivered":
        if msg:
            msg.status = ep.DELIVERED
            msg.delivered_at = _now()
    elif etype == "email.delivery_delayed":
        if msg:
            msg.status = ep.DELAYED
    elif etype == "email.failed":
        if msg:
            msg.status = ep.FAILED
            msg.failed_at = _now()
            msg.last_error = str(data.get("reason") or "provider reported failure")[:2000]
    elif etype == "email.bounced":
        if msg:
            msg.status = ep.BOUNCED
            msg.failed_at = _now()
            msg.last_error = str(data.get("reason") or "bounced")[:2000]
        _suppress_recipient(db, recipient or (msg.email if msg else ""), "bounced")
    elif etype == "email.complained":
        if msg:
            msg.status = ep.COMPLAINED
        _suppress_recipient(db, recipient or (msg.email if msg else ""), "complained")
    else:
        return {"handled": False, "type": etype}
    db.commit()
    return {"handled": True, "type": etype, "email_message_id": msg.id if msg else None}

def _suppress_recipient(db: Session, email: str, reason: str):
    if not email:
        return
    lead = db.scalar(select(WaitlistLead).where(WaitlistLead.email == email.lower()))
    if lead and not lead.suppressed:
        lead.suppressed = True
        lead.suppressed_reason = reason
