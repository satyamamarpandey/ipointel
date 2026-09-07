import logging,signal,time
from .db import init_db,SessionLocal
from .config import get_settings,validate_production_settings
from .services.pipeline import refresh_all,refresh_market_performance,ingest_nse_history
from .services.alerts import send_pending
from .services.email_queue import process_queue
from .services.newsletter import queue_weekly_digests
from .services.outcomes import sync_prediction_outcomes
from .services import sheets_sync
from .services import heartbeat as hb

logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")

EMAIL_POLL_SECONDS=15

class _Shutdown:
    """`docker compose down/restart` sends SIGTERM and hard-kills ~10s later.
    Without this the worker took that kill wherever it happened to be - which
    could be mid-ingest, between the provider accepting an email and the row
    being marked SENT, leaving a message that gets sent twice on restart.
    Flipping a flag lets the loop stop at its next checkpoint instead."""
    def __init__(self):
        self.requested=False
        for sig in (signal.SIGTERM,signal.SIGINT):
            try:signal.signal(sig,self._handle)
            except ValueError:pass  # not on the main thread (tests/embedded use)
    def _handle(self,signum,frame):
        if self.requested:  # a second signal means "stop arguing", so honour it
            logging.warning("second shutdown signal (%s) - exiting immediately",signum);raise SystemExit(1)
        logging.info("shutdown signal (%s) received - finishing current step, then exiting",signum)
        self.requested=True
    def sleep(self,seconds:float)->bool:
        """Interruptible sleep. Returns False as soon as shutdown is asked for."""
        deadline=time.monotonic()+seconds
        while not self.requested:
            remaining=deadline-time.monotonic()
            if remaining<=0:return True
            time.sleep(min(1.0,remaining))
        return False

def run_once(cycle:int=0):
    db=SessionLocal()
    try:
        hb.beat(db,current_job="refresh_all")
        runs=refresh_all(db)
        hb.beat(db,current_job="refresh_all",last_sec_refresh_at=True,last_nse_refresh_at=True)
        if cycle%4==0:
            refresh_market_performance(db)
            sync_prediction_outcomes(db)
            hb.beat(db,current_job="market_performance",last_performance_update_at=True)
        if cycle%96==0: ingest_nse_history(db,max_reports=2)
        if cycle%96==0: queue_weekly_digests(db)
        send_pending(db)
        hb.beat(db,current_job="idle")
        return runs
    except Exception as e:
        hb.beat(db,current_job="refresh_all",success=False,error=f"{type(e).__name__}: {e}")
        raise
    finally:db.close()

def process_email_once():
    with SessionLocal() as db:
        r=process_queue(db)
        hb.beat(db,current_job="idle",last_email_pass_at=True)
        return r

def process_sheets_once():
    with SessionLocal() as db:
        return sheets_sync.process_outbox(db,get_settings())

def main():
    s=get_settings();validate_production_settings(s);init_db();cycle=0
    stop=_Shutdown()
    while not stop.requested:
        try:
            run_once(cycle);logging.info("refresh cycle %s complete",cycle)
        except Exception:logging.exception("refresh cycle failed")
        cycle+=1
        interval=max(60,s.worker_interval_seconds);elapsed=0
        while elapsed<interval and not stop.requested:
            wait=min(EMAIL_POLL_SECONDS,interval-elapsed)
            if not stop.sleep(wait):break
            elapsed+=wait
            try:
                r=process_email_once()
                if r["sent"] or r["failed"]:logging.info("email queue: %s",r)
            except Exception:logging.exception("email queue processing failed")
            try:
                r=process_sheets_once()
                if r["synced"] or r["failed"]:logging.info("sheets sync: %s",r)
            except Exception:logging.exception("sheets sync processing failed")
    logging.info("worker stopped cleanly after %s refresh cycles",cycle)

if __name__=="__main__":main()
