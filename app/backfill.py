from __future__ import annotations
import argparse,time
from datetime import datetime,timezone,timedelta
from .db import init_db,SessionLocal
from .config import get_settings
from .services import sec
from .services.pipeline import upsert_ipo,refresh_market_performance

def main():
    ap=argparse.ArgumentParser(description="Backfill priced U.S. IPOs from SEC 424B4 daily indexes")
    ap.add_argument("--days",type=int,default=365)
    ap.add_argument("--max-filings",type=int,default=2000)
    args=ap.parse_args();init_db();db=SessionLocal();s=get_settings();count=0
    try:
        today=datetime.now(timezone.utc).date()
        for i in range(args.days):
            day=today-timedelta(days=i)
            try:rows,_=sec.master_index_for_date(day,s.sec_user_agent)
            except Exception:continue
            for meta in rows:
                if count>=args.max_filings:break
                try:
                    txt=sec.filing_text(meta["filing_url"],s.sec_user_agent);parsed=sec.parse_priced_ipo(txt)
                    if not parsed:continue
                    row={**meta,**parsed,"country":"United States","exchange":"NASDAQ/NYSE/Other","board":"Mainboard","sector":"Unknown","status":"Listed","currency":"USD","listing_date":meta.get("filing_date","")}
                    upsert_ipo(db,row,"SEC 424B4",meta["filing_url"],1);db.commit();count+=1;print(count,day,row["company"],row.get("symbol"),row.get("final_price"));time.sleep(.12)
                except Exception as e:db.rollback();print("skip",meta.get("company"),type(e).__name__)
            if count>=args.max_filings:break
        refresh_market_performance(db,limit=min(500,count))
    finally:db.close()
    print(f"Backfill complete: {count} priced IPOs discovered")
if __name__=="__main__":main()
