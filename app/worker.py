import logging,time
from .db import init_db,SessionLocal
from .config import get_settings
from .services.pipeline import refresh_all,refresh_market_performance,ingest_nse_history
from .services.alerts import send_pending
from .services.email_queue import process_queue
from .services.newsletter import queue_weekly_digests
from .services.outcomes import sync_prediction_outcomes

logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")

EMAIL_POLL_SECONDS=15

def run_once(cycle:int=0):
    db=SessionLocal()
    try:
        runs=refresh_all(db)
        if cycle%4==0: refresh_market_performance(db)
        if cycle%4==0: sync_prediction_outcomes(db)
        if cycle%96==0: ingest_nse_history(db,max_reports=2)
        if cycle%96==0: queue_weekly_digests(db)
        send_pending(db)
        return runs
    finally:db.close()

def process_email_once():
    with SessionLocal() as db:
        return process_queue(db)

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
