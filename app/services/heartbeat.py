from __future__ import annotations
"""Worker liveness, queryable from the DB rather than inferred from an OS
process existing. A stale heartbeat is what actually proves the worker has
stopped doing useful work (hung, crashed-looping, deadlocked)."""
import socket
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from ..models import WorkerHeartbeat

def now(): return datetime.now(timezone.utc)

def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

def _row(db: Session) -> WorkerHeartbeat:
    row = db.get(WorkerHeartbeat, 1)
    if not row:
        row = WorkerHeartbeat(id=1, instance=socket.gethostname())
        db.add(row)
        db.flush()
    return row

def beat(db: Session, current_job: str = "idle", success: bool = True, error: str = "", **field_touches):
    """Call at the start (current_job set, success ignored) and end (success/error) of every job.
    field_touches: e.g. last_sec_refresh_at=True to stamp that specific field to now()."""
    row = _row(db)
    row.current_job = current_job
    row.updated_at = now()
    if success:
        row.last_success_at = now()
        row.last_error = ""
    elif error:
        row.last_error = error[:2000]
    for field, touch in field_touches.items():
        if touch:
            setattr(row, field, now())
    db.commit()

def status(db: Session, stale_after_seconds: int) -> dict:
    row = db.get(WorkerHeartbeat, 1)
    if not row:
        return {"status": "FAILED", "reason": "worker has never reported in", "last_seen": None, "current_job": None,
                "last_success_at": None, "last_error": "worker has never reported in",
                "last_sec_refresh_at": None, "last_nse_refresh_at": None,
                "last_performance_update_at": None, "last_email_pass_at": None}
    age = (now() - _aware(row.updated_at)).total_seconds()
    if age > stale_after_seconds * 3:
        st = "FAILED"
    elif age > stale_after_seconds:
        st = "STALE"
    else:
        st = "LIVE"
    return {"status": st, "last_seen": row.updated_at.isoformat(), "current_job": row.current_job,
            "last_success_at": row.last_success_at.isoformat() if row.last_success_at else None,
            "last_error": row.last_error,
            "last_sec_refresh_at": row.last_sec_refresh_at.isoformat() if row.last_sec_refresh_at else None,
            "last_nse_refresh_at": row.last_nse_refresh_at.isoformat() if row.last_nse_refresh_at else None,
            "last_performance_update_at": row.last_performance_update_at.isoformat() if row.last_performance_update_at else None,
            "last_email_pass_at": row.last_email_pass_at.isoformat() if row.last_email_pass_at else None}
