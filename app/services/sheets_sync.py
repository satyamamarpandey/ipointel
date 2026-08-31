from __future__ import annotations
"""Outbox-backed Google Sheets mirror for early-access leads. PostgreSQL/
SQLite is always the source of truth - enqueue() is a fast local upsert that
commits before this module ever touches the network. process_outbox() is
what actually calls the Sheets API, called by the worker on the same cadence
as email delivery (see worker.py), so a slow/down/misconfigured Sheets
integration never blocks or loses a signup (see main.waitlist())."""
from datetime import datetime, timezone
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from ..models import SheetsSyncOutbox, WaitlistLead
from ..config import Settings

SHEET_COLUMNS = ["Timestamp", "Email", "Name", "Investor Type", "Market Preference", "Source",
                  "Campaign", "Referral", "Page", "Consent", "Access Status", "Clerk User ID", "Lead ID"]
MAX_ATTEMPTS = 8

def now() -> datetime:
    return datetime.now(timezone.utc)

def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

def enqueue(db: Session, lead: WaitlistLead) -> SheetsSyncOutbox:
    """Idempotent: one outbox row per lead (unique lead_id). Safe to call on
    every signup - an existing PENDING/FAILED row is left to be retried, an
    already-SYNCED row is untouched (we don't re-mirror on every login)."""
    row = db.scalar(select(SheetsSyncOutbox).where(SheetsSyncOutbox.lead_id == lead.id))
    if row is None:
        row = SheetsSyncOutbox(lead_id=lead.id, status="PENDING")
        db.add(row)
        db.flush()
    return row

def _within_backoff(row: SheetsSyncOutbox) -> bool:
    if row.attempt_count == 0:
        return False
    wait = min(3600, 2 ** row.attempt_count * 30)  # 30s,60s,120s...capped at 1h
    last = _aware(row.updated_at)
    return (now() - last).total_seconds() < wait

def build_row(lead: WaitlistLead) -> list[str]:
    return [
        lead.created_at.isoformat() if lead.created_at else "",
        lead.email, lead.name, lead.investor_type, lead.markets,
        lead.source, lead.campaign, lead.referred_by, lead.page_path,
        "yes" if lead.consent else "no", lead.access_status,
        lead.clerk_user_id, str(lead.id),
    ]

def _mint_access_token(settings: Settings) -> str:
    """Server-side only - the service-account JSON is a config-only value
    (app/config.py) and is never sent to the frontend. Raises if credentials
    are missing/invalid; callers must catch."""
    import json
    from google.oauth2 import service_account
    import google.auth.transport.requests
    info = json.loads(settings.google_sheets_service_account_json)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token

def _append_row(settings: Settings, values: list[str]) -> None:
    """Raises on any non-2xx response - callers record the error and retry."""
    import httpx
    token = _mint_access_token(settings)
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{settings.google_sheets_spreadsheet_id}/values/Sheet1!A1:append"
    r = httpx.post(url, params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
                    headers={"Authorization": f"Bearer {token}"}, json={"values": [values]}, timeout=15)
    r.raise_for_status()

def sheets_configured(settings: Settings) -> bool:
    return bool(settings.google_sheets_enabled and settings.google_sheets_spreadsheet_id and settings.google_sheets_service_account_json)

def sync_status_counts(db: Session, settings: Settings) -> dict:
    rows = db.execute(select(SheetsSyncOutbox.status, func.count()).group_by(SheetsSyncOutbox.status)).all()
    counts = {status: n for status, n in rows}
    last = db.scalar(select(SheetsSyncOutbox.synced_at).where(SheetsSyncOutbox.status == "SYNCED").order_by(SheetsSyncOutbox.synced_at.desc()).limit(1))
    total = db.scalar(select(func.count()).select_from(SheetsSyncOutbox)) or 0
    return {
        "configured": sheets_configured(settings),
        "total": total, "synced": counts.get("SYNCED", 0),
        "pending": counts.get("PENDING", 0), "failed": counts.get("FAILED", 0),
        "last_synced_at": last.isoformat() if last else None,
    }

def process_outbox(db: Session, settings: Settings, limit: int = 25) -> dict:
    result = {"synced": 0, "failed": 0}
    if not sheets_configured(settings):
        return result  # PENDING CONFIGURATION - rows stay PENDING, nothing is fabricated as synced
    pending = db.scalars(select(SheetsSyncOutbox).where(SheetsSyncOutbox.status.in_(["PENDING", "FAILED"]))
                          .order_by(SheetsSyncOutbox.created_at.asc()).limit(limit * 3)).all()
    processed = 0
    for row in pending:
        if processed >= limit:
            break
        if row.status == "FAILED" and row.attempt_count >= MAX_ATTEMPTS:
            continue  # permanently failed - visible to admin via sync_status_counts, not retried forever
        if _within_backoff(row):
            continue
        lead = db.get(WaitlistLead, row.lead_id)
        if not lead:
            row.status = "FAILED"; row.last_error = "lead no longer exists"; row.updated_at = now()
            continue
        processed += 1
        try:
            _append_row(settings, build_row(lead))
            row.status = "SYNCED"; row.synced_at = now(); row.last_error = ""
            result["synced"] += 1
        except Exception as e:
            row.attempt_count += 1
            row.last_error = f"{type(e).__name__}: {e}"[:2000]
            row.status = "FAILED"
            result["failed"] += 1
        row.updated_at = now()
    db.commit()
    return result

def retry_failed(db: Session) -> int:
    """Admin-triggered manual retry: resets attempt_count so backoff doesn't
    block the next process_outbox() pass."""
    rows = db.scalars(select(SheetsSyncOutbox).where(SheetsSyncOutbox.status == "FAILED")).all()
    for row in rows:
        row.status = "PENDING"; row.attempt_count = 0; row.updated_at = now()
    db.commit()
    return len(rows)
