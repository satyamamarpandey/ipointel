import logging,time
from .db import init_db,SessionLocal
from .config import get_settings
from .services.pipeline import refresh_all,refresh_market_performance,ingest_nse_history
from .services.alerts import send_pending

logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")

def run_once(cycle:int=0):
    db=SessionLocal()
    try:
        runs=refresh_all(db)
        if cycle%4==0: refresh_market_performance(db)
        if cycle%96==0: ingest_nse_history(db,max_reports=2)
        send_pending(db)
        return runs
    finally:db.close()

def main():
    init_db();s=get_settings();cycle=0
    while True:
        try:
            run_once(cycle);logging.info("refresh cycle %s complete",cycle)
        except Exception:logging.exception("refresh cycle failed")
        cycle+=1;time.sleep(max(60,s.worker_interval_seconds))

if __name__=="__main__":main()
