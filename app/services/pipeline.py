from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import select, func
from sqlalchemy.orm import Session
import httpx
from ..models import IPO, ScoreSnapshot, Provenance, IngestionRun, PerformanceSnapshot
from ..scoring import compute_score
from ..config import get_settings
from . import sec,nse,market,enrichment

def now(): return datetime.now(timezone.utc)

def external_key(row):
    if row.get("country","").lower()=="india":return "IN:"+(row.get("symbol") or row.get("company","")).strip().lower()
    return "US:"+(row.get("cik") or row.get("symbol") or row.get("company","")).strip().lower()

def add_provenance(db:Session,ipo:IPO,field:str,value,source_name,url,tier:int):
    if value in (None,""):return
    q=db.scalar(select(Provenance).where(Provenance.ipo_id==ipo.id,Provenance.field_name==field,Provenance.source_url==url))
    if q:q.observed_value=str(value);q.observed_at=now();q.source_tier=tier
    else:db.add(Provenance(ipo_id=ipo.id,field_name=field,source_name=source_name,source_url=url,source_tier=tier,observed_value=str(value)))

def upsert_ipo(db:Session,row:dict,source_name:str,source_url:str,tier:int):
    key=external_key(row); ipo=db.scalar(select(IPO).where(IPO.external_key==key)); created=False
    if not ipo:
        ipo=IPO(external_key=key,company=row.get("company") or "Unknown",country=row.get("country") or "Unknown");db.add(ipo);db.flush();created=True
    fields=["company","symbol","country","exchange","board","sector","status","filing_date","open_date","close_date","listing_date","currency","price_low","price_high","final_price","issue_size_m","shares_offered_m","post_issue_shares_m","lot_size","revenue_m","revenue_prev_m","revenue_2y_ago_m","ebitda_m","net_income_m","cfo_m","debt_m","cash_m","fresh_issue_pct","ofs_pct","promoter_retention_pct","qib_sub","nii_sub","retail_sub","total_sub","gmp_pct","underwriter_quality","anchor_quality","market_regime","sector_regime","dual_class","lockup_days","market_overhang_pct","peer_median_pe","peer_median_ps","peer_median_ev_ebitda","filing_url","registrar","allotment_url"]
    changed=False
    for f in fields:
        if f in row and row[f] not in (None,"") and getattr(ipo,f)!=row[f]: setattr(ipo,f,row[f]);changed=True
        if f in row and row[f] not in (None,""):add_provenance(db,ipo,f,row[f],source_name,source_url,tier)
    if row.get("raw"):ipo.raw=row["raw"]
    if row.get("data_flags") is not None:ipo.data_flags=row["data_flags"]
    ipo.updated_at=now();db.flush()
    conflicts=db.scalar(select(func.count()).select_from(Provenance).where(Provenance.ipo_id==ipo.id,Provenance.is_conflict==True)) or 0
    score=compute_score(ipo,conflicts)
    last=db.scalar(select(ScoreSnapshot).where(ScoreSnapshot.ipo_id==ipo.id).order_by(ScoreSnapshot.created_at.desc()).limit(1))
    if not last or any(abs(getattr(last,k)-score[k])>=0.1 for k in ("overall_score","listing_score","long_term_score","confidence")) or last.recommendation!=score["recommendation"]:
        db.add(ScoreSnapshot(ipo_id=ipo.id,**score))
    return created or changed

def ingest_sec(db:Session):
    s=get_settings(); run=IngestionRun(source="SEC EDGAR",status="running");db.add(run);db.commit();seen=changed=0
    try:
        rows,warnings=sec.fetch_recent_ipos(s.sec_user_agent)
        for row in rows:
            row.update({"country":"United States","exchange":"NASDAQ/NYSE/Other","board":"Mainboard","sector":"Unknown","status":"Filed","currency":"USD"})
            enriched=sec.enrich(row,s.sec_user_agent);seen+=1
            if upsert_ipo(db,enriched,"SEC EDGAR",row.get("filing_url") or "https://www.sec.gov/edgar/search/",1):changed+=1
        run.status="ok" if not warnings else "partial";run.error=" | ".join(warnings)[:4000];run.rows_seen=seen;run.rows_changed=changed;run.finished_at=now();db.commit()
    except Exception as e:db.rollback();run=db.get(IngestionRun,run.id);run.status="error";run.error=str(e)[:4000];run.finished_at=now();db.commit()
    return run

def ingest_sec_priced(db:Session, lookback_days:int=5):
    from datetime import timedelta
    import time as _time
    s=get_settings();run=IngestionRun(source="SEC Priced IPOs",status="running");db.add(run);db.commit();seen=changed=0;warnings=[]
    try:
        today=datetime.now(timezone.utc).date()
        for i in range(lookback_days):
            day=today-timedelta(days=i)
            try: rows,index_url=sec.master_index_for_date(day,s.sec_user_agent)
            except Exception as e: warnings.append(f"{day}: {type(e).__name__}");continue
            for meta in rows:
                try:
                    txt=sec.filing_text(meta["filing_url"],s.sec_user_agent);parsed=sec.parse_priced_ipo(txt)
                    if not parsed:continue
                    row={**meta,**parsed,"country":"United States","exchange":"NASDAQ/NYSE/Other","board":"Mainboard","sector":"Unknown","status":"Listed","currency":"USD","listing_date":meta.get("filing_date","")}
                    seen+=1
                    if upsert_ipo(db,row,"SEC 424B4",meta["filing_url"],1):changed+=1
                    _time.sleep(.12)
                except Exception as e: warnings.append(f"{meta.get('company','?')}: {type(e).__name__}")
        run.status="ok" if not warnings else "partial";run.error=" | ".join(warnings)[:4000];run.rows_seen=seen;run.rows_changed=changed;run.finished_at=now();db.commit()
    except Exception as e:
        db.rollback();run=db.get(IngestionRun,run.id);run.status="error";run.error=str(e)[:4000];run.finished_at=now();db.commit()
    return run

def ingest_secondary_enrichment(db:Session):
    s=get_settings();run=IngestionRun(source="Licensed enrichment feed",status="running");db.add(run);db.commit();seen=changed=0
    try:
        rows,warnings=enrichment.fetch_rows()
        for row in rows:
            country=row.get("country") or ("India" if str(row.get("symbol","")).endswith((".NS",".BO")) else "United States")
            row["country"]=country;seen+=1
            source_name=row.pop("source_name",None) or "Licensed enrichment feed";source_url=row.pop("source_url",None) or s.secondary_enrichment_url
            if upsert_ipo(db,row,source_name,source_url,3):changed+=1
        run.status="ok" if not warnings else "partial";run.error=" | ".join(warnings)[:4000];run.rows_seen=seen;run.rows_changed=changed;run.finished_at=now();db.commit()
    except Exception as e:
        db.rollback();run=db.get(IngestionRun,run.id);run.status="error";run.error=str(e)[:4000];run.finished_at=now();db.commit()
    return run

def ingest_nse(db:Session):
    run=IngestionRun(source="NSE",status="running");db.add(run);db.commit();seen=changed=0
    try:
        rows,warnings=nse.fetch_current()
        for row in rows:
            seen+=1
            if upsert_ipo(db,row,"NSE",f"https://www.nseindia.com/market-data/all-upcoming-issues-ipo",1):changed+=1
        run.status="ok" if not warnings else "partial";run.error=" | ".join(warnings)[:4000];run.rows_seen=seen;run.rows_changed=changed;run.finished_at=now();db.commit()
    except Exception as e:db.rollback();run=db.get(IngestionRun,run.id);run.status="error";run.error=str(e)[:4000];run.finished_at=now();db.commit()
    return run

def ingest_nse_history(db:Session,max_reports=3):
    run=IngestionRun(source="NSE Primary Market Reports",status="running");db.add(run);db.commit();seen=changed=0
    try:
        links=nse.fetch_archive_links()[:max_reports]
        with httpx.Client(headers={"User-Agent":"Mozilla/5.0 IPOIntelligence/2.0"},timeout=30,follow_redirects=True) as c:
            for label,url in links:
                r=c.get(url);r.raise_for_status()
                for x in nse.parse_monthly_xlsx(r.content):
                    seen+=1;row={"company":x["company"],"symbol":x.get("symbol","") or "","country":"India","exchange":"NSE/BSE","status":"Listed","currency":"INR","final_price":x.get("issue_price"),"listing_date":x.get("listing_date","") or "","issue_size_m":x.get("issue_size"),"raw":x.get("raw",{})}
                    if upsert_ipo(db,row,"NSE Primary Market Report",url,1):changed+=1
                    ipo=db.scalar(select(IPO).where(IPO.external_key==external_key(row)))
                    if ipo and x.get("issue_price") and x.get("listing_price"):
                        ret=(x["listing_price"]/x["issue_price"]-1)*100
                        existing=db.scalar(select(PerformanceSnapshot).where(PerformanceSnapshot.ipo_id==ipo.id,PerformanceSnapshot.source_url==url,PerformanceSnapshot.listing_return_pct==ret))
                        if not existing: db.add(PerformanceSnapshot(ipo_id=ipo.id,as_of_date=now().date().isoformat(),close_price=x["listing_price"],listing_return_pct=ret,source_name="NSE Primary Market Report",source_url=url))
        run.status="ok";run.rows_seen=seen;run.rows_changed=changed;run.finished_at=now();db.commit()
    except Exception as e:
        db.rollback();run=db.get(IngestionRun,run.id);run.status="error";run.error=str(e)[:4000];run.finished_at=now();db.commit()
    return run

def refresh_market_performance(db:Session,limit=40):
    s=get_settings()
    if not s.allow_secondary_market_data:return 0
    ipos=db.scalars(select(IPO).where(IPO.status=="Listed",IPO.symbol!="").order_by(IPO.updated_at.desc()).limit(limit)).all();n=0
    for ipo in ipos:
        try:
            h=market.fetch_yahoo_history(ipo.symbol,ipo.country)
            prices=h["prices"]
            if not prices:continue
            close=prices[-1]["close"]; base=ipo.final_price or ipo.price_high
            ret=(close/base-1)*100 if base else None
            snap=PerformanceSnapshot(ipo_id=ipo.id,as_of_date=datetime.fromtimestamp(prices[-1]["ts"],tz=timezone.utc).date().isoformat(),close_price=close,listing_return_pct=ret,source_name="Yahoo Finance fallback",source_url=h["url"])
            db.add(snap);n+=1
        except Exception:continue
    db.commit();return n

def refresh_all(db:Session):
    runs=[ingest_sec(db),ingest_sec_priced(db),ingest_nse(db)]
    if get_settings().secondary_enrichment_url:runs.append(ingest_secondary_enrichment(db))
    return runs
