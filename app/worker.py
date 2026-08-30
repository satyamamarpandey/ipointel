import logging,time
from .db import init_db,SessionLocal
from .config import get_settings
from .services.pipeline import refresh_all,refresh_market_performance,ingest_nse_history
from .services.alerts import send_pending
from .services.email_queue import process_queue
from .services.newsletter import queue_weekly_digests
from .services.outcomes import sync_prediction_outcomes
from .services import heartbeat as hb

logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")

EMAIL_POLL_SECONDS=15

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

def main():
    init_db();s=get_settings();cycle=0
    while True:
        try:
            run_once(cycle);logging.info("refresh cycle %s complete",cycle)
        except Exception:logging.exception("refresh cycle failed")
        cycle+=1
        interval=max(60,s.worker_interval_seconds);elapsed=0
        while elapsed<interval:
            wait=min(EMAIL_POLL_SECONDS,interval-elapsed);time.sleep(wait);elapsed+=wait
            try:
                r=process_email_once()
                if r["sent"] or r["failed"]:logging.info("email queue: %s",r)
            except Exception:logging.exception("email queue processing failed")

if __name__=="__main__":main()
