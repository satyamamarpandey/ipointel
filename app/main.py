from __future__ import annotations
import asyncio,secrets,time,csv,io,json
from contextlib import asynccontextmanager
from collections import defaultdict,deque
from datetime import datetime,timezone,timedelta
from pathlib import Path
from fastapi import FastAPI,Depends,HTTPException,Request,Query,Header
from fastapi.responses import FileResponse,StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select,func,or_
from sqlalchemy.orm import Session
from .config import get_settings
from .db import init_db,SessionLocal
from .models import IPO,ScoreSnapshot,Provenance,PerformanceSnapshot,IngestionRun,WaitlistLead,EmailMessage
from .schemas import WaitlistIn,WaitlistOut
from .services.email_queue import enqueue,process_queue
from .services import email_provider as ep
from .services import webhooks as webhooks_svc
from .services.backtest import summarize as backtest_summary
from .services.pipeline import refresh_all
from .services import redflags as redflags_svc
from .services import contradictions as contradictions_svc
from .services import dcf as dcf_svc
from .services import similarity as similarity_svc
from .services import sensitivity as sensitivity_svc
from .services import changes as changes_svc
from .services import walkforward as walkforward_svc

S=get_settings(); BASE=Path(__file__).parent; STATIC=BASE/"static"
@asynccontextmanager
async def lifespan(app):
    init_db()
    yield

app=FastAPI(title=S.app_name,version="2.0.0",docs_url="/api/docs",redoc_url=None,lifespan=lifespan)
app.mount("/static",StaticFiles(directory=STATIC),name="static")
rate=defaultdict(lambda:deque(maxlen=20))

def db_dep():
    db=SessionLocal()
    try:yield db
    finally:db.close()

def latest_score(db,ipo_id):return db.scalar(select(ScoreSnapshot).where(ScoreSnapshot.ipo_id==ipo_id).order_by(ScoreSnapshot.created_at.desc()).limit(1))
def perf(db,ipo_id):return db.scalar(select(PerformanceSnapshot).where(PerformanceSnapshot.ipo_id==ipo_id).order_by(PerformanceSnapshot.created_at.desc()).limit(1))
def ipo_json(db,ipo):
    sc=latest_score(db,ipo.id);pf=perf(db,ipo.id)
    return {"id":ipo.id,"company":ipo.company,"symbol":ipo.symbol,"country":ipo.country,"exchange":ipo.exchange,"board":ipo.board,"sector":ipo.sector,"status":ipo.status,"filing_date":ipo.filing_date,"open_date":ipo.open_date,"close_date":ipo.close_date,"listing_date":ipo.listing_date,"currency":ipo.currency,"price_low":ipo.price_low,"price_high":ipo.price_high,"final_price":ipo.final_price,"issue_size_m":ipo.issue_size_m,"lot_size":ipo.lot_size,"qib_sub":ipo.qib_sub,"nii_sub":ipo.nii_sub,"retail_sub":ipo.retail_sub,"total_sub":ipo.total_sub,"gmp_pct":ipo.gmp_pct,"filing_url":ipo.filing_url,"registrar":ipo.registrar,"allotment_url":ipo.allotment_url,"updated_at":ipo.updated_at.isoformat() if ipo.updated_at else None,"score":None if not sc else {"overall":sc.overall_score,"listing":sc.listing_score,"long_term":sc.long_term_score,"confidence":sc.confidence,"listing_probability":sc.listing_gain_probability,"long_term_probability":sc.long_term_outperform_probability,"recommendation":sc.recommendation,"horizon":sc.horizon,"valuation":sc.valuation_label,"fair_low":sc.fair_value_low,"fair_high":sc.fair_value_high,"pillars":sc.pillars,"rationale":sc.rationale,"risks":sc.risks,"what_changes_verdict":sc.what_changes_verdict,"model_version":sc.model_version,"created_at":sc.created_at.isoformat()},"performance":None if not pf else {"as_of":pf.as_of_date,"close":pf.close_price,"listing_return_pct":pf.listing_return_pct,"return_1m_pct":pf.return_1m_pct,"return_6m_pct":pf.return_6m_pct,"return_12m_pct":pf.return_12m_pct,"source":pf.source_name}}

@app.get("/")
def landing():return FileResponse(STATIC/"index.html")
@app.get("/app")
def dashboard():return FileResponse(STATIC/"app.html")
@app.get("/privacy")
def privacy():return FileResponse(STATIC/"privacy.html")
@app.get("/terms")
def terms():return FileResponse(STATIC/"terms.html")
@app.get("/preferences")
def preferences_page():return FileResponse(STATIC/"preferences.html")
@app.middleware("http")
async def security_headers(request:Request,call_next):
    response=await call_next(request)
    response.headers["X-Content-Type-Options"]="nosniff"
    response.headers["X-Frame-Options"]="DENY"
    response.headers["Referrer-Policy"]="strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"]="camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"]="default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    return response

@app.get("/health")
def health(db:Session=Depends(db_dep)):
    try:db.scalar(select(func.count()).select_from(IPO));return {"status":"ok","version":"2.0.0","time":datetime.now(timezone.utc).isoformat()}
    except Exception as e:raise HTTPException(503,str(e))

@app.post("/api/waitlist",response_model=WaitlistOut)
def waitlist(payload:WaitlistIn,request:Request,db:Session=Depends(db_dep)):
    if payload.website:return WaitlistOut(ok=True,message="Thanks — you're on the list.")
    if not payload.consent:raise HTTPException(400,"Consent is required to join the update list.")
    ip=request.client.host if request.client else "unknown";now=time.time();q=rate[ip]
    while q and now-q[0]>60:q.popleft()
    if len(q)>=6:raise HTTPException(429,"Too many signup attempts. Try again shortly.")
    q.append(now)
    email=str(payload.email).strip().lower();existing=db.scalar(select(WaitlistLead).where(WaitlistLead.email==email))
    if existing:
        if not existing.consent:
            existing.consent=True;existing.markets=payload.markets;existing.investor_type=payload.investor_type;existing.suppressed=False;db.commit()
            enqueue(db,existing,"welcome",ep.PRIORITY_TRANSACTIONAL);db.commit()
            return WaitlistOut(ok=True,message="You're back on the early-access list.",referral_code=existing.referral_code)
        return WaitlistOut(ok=True,message="You're already on the early-access list.",referral_code=existing.referral_code)
    code=secrets.token_urlsafe(6).replace("-","").replace("_","")[:8]
    lead=WaitlistLead(email=email,name=payload.name.strip(),investor_type=payload.investor_type,markets=payload.markets,consent=payload.consent,referral_code=code,referred_by=payload.referred_by,unsubscribe_token=secrets.token_urlsafe(24),source=payload.source)
    db.add(lead);db.commit()
    # Signup is durable before this line. Email is queued (fast local insert) and
    # delivered asynchronously by the worker - a slow/down provider never blocks signup.
    enqueue(db,lead,"welcome",ep.PRIORITY_TRANSACTIONAL);db.commit()
    return WaitlistOut(ok=True,message="Early access reserved. We'll notify you about launch and material IPO-score changes.",referral_code=code)


@app.get("/unsubscribe")
def unsubscribe(token:str,db:Session=Depends(db_dep)):
    lead=db.scalar(select(WaitlistLead).where(WaitlistLead.unsubscribe_token==token))
    if not lead:raise HTTPException(404,"Invalid unsubscribe link")
    lead.consent=False;db.commit()
    return FileResponse(STATIC/"unsubscribed.html")

PREF_FIELDS=["markets","alert_score_change","alert_recommendation_change","alert_red_flag","alert_new_ipo","digest_weekly"]

@app.get("/api/preferences")
def get_preferences(token:str,db:Session=Depends(db_dep)):
    lead=db.scalar(select(WaitlistLead).where(WaitlistLead.unsubscribe_token==token))
    if not lead:raise HTTPException(404,"Invalid preferences link")
    return {"email":lead.email,"consent":lead.consent,**{f:getattr(lead,f) for f in PREF_FIELDS}}

@app.post("/api/preferences")
def update_preferences(token:str,payload:dict,db:Session=Depends(db_dep)):
    lead=db.scalar(select(WaitlistLead).where(WaitlistLead.unsubscribe_token==token))
    if not lead:raise HTTPException(404,"Invalid preferences link")
    if "markets" in payload and payload["markets"] in ("india","us","both"):lead.markets=payload["markets"]
    for f in PREF_FIELDS[1:]:
        if f in payload:setattr(lead,f,bool(payload[f]))
    db.commit()
    return {"ok":True,**{f:getattr(lead,f) for f in PREF_FIELDS}}

@app.post("/api/webhooks/resend")
async def resend_webhook(request:Request,db:Session=Depends(db_dep)):
    body=await request.body()
    ok=webhooks_svc.verify_svix_signature(S.resend_webhook_secret,request.headers.get("svix-id",""),request.headers.get("svix-timestamp",""),request.headers.get("svix-signature",""),body)
    if not ok:raise HTTPException(401,"invalid or unsigned webhook payload")
    try:event=json.loads(body)
    except Exception:raise HTTPException(400,"invalid JSON")
    return webhooks_svc.handle_event(db,event)

@app.get("/api/summary")
def summary(db:Session=Depends(db_dep)):
    total=db.scalar(select(func.count()).select_from(IPO)) or 0
    open_n=db.scalar(select(func.count()).select_from(IPO).where(IPO.status.in_(["Open","Upcoming","Filed"]))) or 0
    listed=db.scalar(select(func.count()).select_from(IPO).where(IPO.status=="Listed")) or 0
    last=db.scalar(select(IngestionRun).order_by(IngestionRun.started_at.desc()).limit(1))
    high_conf=db.scalar(select(func.count()).select_from(ScoreSnapshot).where(ScoreSnapshot.confidence>=S.min_recommendation_confidence)) or 0
    return {"total":total,"active":open_n,"listed":listed,"high_confidence_scores":high_conf,"strict_reliability":S.strict_reliability,"min_confidence":S.min_recommendation_confidence,"last_ingestion":None if not last else {"source":last.source,"status":last.status,"started_at":last.started_at.isoformat(),"finished_at":last.finished_at.isoformat() if last.finished_at else None,"error":last.error}}

@app.get("/api/ipos")
def ipos(country:str="all",status:str="all",q:str="",limit:int=Query(100,ge=1,le=500),db:Session=Depends(db_dep)):
    stmt=select(IPO)
    if country!="all":stmt=stmt.where(IPO.country==country)
    if status!="all":stmt=stmt.where(IPO.status==status)
    if q:stmt=stmt.where(or_(IPO.company.ilike(f"%{q}%"),IPO.symbol.ilike(f"%{q}%")))
    rows=db.scalars(stmt.order_by(IPO.updated_at.desc()).limit(limit)).all()
    return [ipo_json(db,x) for x in rows]

@app.get("/api/ipos/{ipo_id}")
def ipo_detail(ipo_id:int,db:Session=Depends(db_dep)):
    ipo=db.get(IPO,ipo_id)
    if not ipo:raise HTTPException(404,"IPO not found")
    out=ipo_json(db,ipo)
    out["fundamentals"]={k:getattr(ipo,k) for k in ["revenue_m","revenue_prev_m","revenue_2y_ago_m","ebitda_m","net_income_m","cfo_m","debt_m","cash_m","fresh_issue_pct","ofs_pct","promoter_retention_pct","dual_class","lockup_days","market_overhang_pct","peer_median_pe","peer_median_ps"]}
    out["provenance"]=[{"field":p.field_name,"source":p.source_name,"url":p.source_url,"tier":p.source_tier,"value":p.observed_value,"observed_at":p.observed_at.isoformat(),"conflict":p.is_conflict} for p in sorted(ipo.provenance,key=lambda p:(p.source_tier,p.field_name))]
    hist=db.scalars(select(ScoreSnapshot).where(ScoreSnapshot.ipo_id==ipo.id).order_by(ScoreSnapshot.created_at.asc())).all()
    out["score_history"]=[{"at":x.created_at.isoformat(),"overall":x.overall_score,"listing":x.listing_score,"long_term":x.long_term_score,"confidence":x.confidence,"recommendation":x.recommendation} for x in hist[-30:]]
    flags=redflags_svc.evaluate(ipo)
    out["red_flags"]={"summary":redflags_svc.summarize(flags),"flags":flags}
    out["contradictions"]=contradictions_svc.evaluate(ipo,ipo.provenance)
    out["sensitivity"]=sensitivity_svc.analyze(ipo)
    return out

@app.get("/api/ipos/{ipo_id}/changes")
def ipo_changes(ipo_id:int,db:Session=Depends(db_dep)):
    ipo=db.get(IPO,ipo_id)
    if not ipo:raise HTTPException(404,"IPO not found")
    return {"ipo_id":ipo_id,"timeline":changes_svc.timeline(db,ipo_id)}

@app.get("/api/ipos/{ipo_id}/similar")
def ipo_similar(ipo_id:int,db:Session=Depends(db_dep)):
    ipo=db.get(IPO,ipo_id)
    if not ipo:raise HTTPException(404,"IPO not found")
    return similarity_svc.find_similar(db,ipo)

@app.get("/api/ipos/{ipo_id}/valuation")
def ipo_valuation_detail(ipo_id:int,db:Session=Depends(db_dep)):
    ipo=db.get(IPO,ipo_id)
    if not ipo:raise HTTPException(404,"IPO not found")
    return {"scenario_dcf":dcf_svc.scenario_dcf(ipo),"reverse_dcf":dcf_svc.reverse_dcf(ipo)}

@app.get("/api/model-performance")
def model_performance(db:Session=Depends(db_dep)):
    return walkforward_svc.evaluate(db)

@app.get("/api/performance")
def performance(country:str="all",limit:int=Query(200,ge=1,le=500),db:Session=Depends(db_dep)):
    stmt=select(IPO).where(IPO.status=="Listed")
    if country!="all":stmt=stmt.where(IPO.country==country)
    rows=db.scalars(stmt.order_by(IPO.updated_at.desc()).limit(limit)).all();return [ipo_json(db,x) for x in rows]

@app.get("/api/source-health")
def source_health(db:Session=Depends(db_dep)):
    out=[]
    tiers={"SEC EDGAR":1,"SEC Priced IPOs":1,"NSE":1,"NSE Primary Market Reports":1,"Licensed enrichment feed":3}
    for source,tier in tiers.items():
        r=db.scalar(select(IngestionRun).where(IngestionRun.source==source).order_by(IngestionRun.started_at.desc()).limit(1))
        out.append({"source":source,"tier":tier,"status":"never run" if not r else r.status,"last_run":None if not r else r.started_at.isoformat(),"rows":0 if not r else r.rows_seen,"error":"" if not r else r.error})
    last_sent=db.scalar(select(EmailMessage).where(EmailMessage.status.in_([ep.SENT,ep.DELIVERED])).order_by(EmailMessage.sent_at.desc()).limit(1))
    last_hard_failed=db.scalar(select(EmailMessage).where(EmailMessage.status==ep.FAILED).order_by(EmailMessage.updated_at.desc()).limit(1))
    last_erroring=db.scalar(select(EmailMessage).where(EmailMessage.attempt_count>0,EmailMessage.last_error!="").order_by(EmailMessage.updated_at.desc()).limit(1))
    provider=ep.get_provider(S)
    if not S.enable_email or isinstance(provider,ep.DisabledEmailProvider):email_status="DISABLED"
    elif last_hard_failed and (not last_sent or last_hard_failed.updated_at>last_sent.sent_at):email_status="FAILED"
    elif last_erroring and (not last_sent or last_erroring.updated_at>last_sent.sent_at):email_status="DEGRADED"  # retrying but not yet exhausted
    else:email_status="LIVE"
    last_error_msg=last_hard_failed.last_error if last_hard_failed else (last_erroring.last_error if last_erroring else "")
    out.append({"source":f"Email ({S.email_provider.upper()})" if S.enable_email else "Email (disabled)","tier":2,"status":email_status,"last_run":last_sent.sent_at.isoformat() if last_sent and last_sent.sent_at else None,"rows":db.scalar(select(func.count()).select_from(EmailMessage).where(EmailMessage.status.in_([ep.SENT,ep.DELIVERED]))) or 0,"error":last_error_msg})
    return out

@app.get("/api/admin/email-stats")
def email_stats(x_admin_token:str|None=Header(default=None),db:Session=Depends(db_dep)):
    if S.admin_token and (not x_admin_token or not secrets.compare_digest(x_admin_token,S.admin_token)):raise HTTPException(403,"Invalid admin token")
    today=datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0);month=today.replace(day=1)
    def count(**where):
        stmt=select(func.count()).select_from(EmailMessage)
        for k,v in where.items():stmt=stmt.where(getattr(EmailMessage,k)==v)
        return db.scalar(stmt) or 0
    sent_today=db.scalar(select(func.count()).select_from(EmailMessage).where(EmailMessage.status.in_([ep.SENT,ep.DELIVERED]),EmailMessage.sent_at>=today)) or 0
    sent_month=db.scalar(select(func.count()).select_from(EmailMessage).where(EmailMessage.status.in_([ep.SENT,ep.DELIVERED]),EmailMessage.sent_at>=month)) or 0
    return {"provider":S.email_provider,"enabled":S.enable_email,
      "sent_today":sent_today,"sent_month":sent_month,
      "delivered":count(status=ep.DELIVERED),"failed":count(status=ep.FAILED),"queued":count(status=ep.QUEUED),
      "bounced":count(status=ep.BOUNCED),"complained":count(status=ep.COMPLAINED),"suppressed":count(status=ep.SUPPRESSED),
      "daily_soft_limit":S.email_daily_soft_limit,"daily_remaining":max(0,S.email_daily_soft_limit-sent_today),
      "monthly_soft_limit":S.email_monthly_soft_limit,"monthly_remaining":max(0,S.email_monthly_soft_limit-sent_month)}

@app.post("/api/admin/email/process")
def admin_process_email_queue(x_admin_token:str|None=Header(default=None),db:Session=Depends(db_dep)):
    if S.admin_token and (not x_admin_token or not secrets.compare_digest(x_admin_token,S.admin_token)):raise HTTPException(403,"Invalid admin token")
    return process_queue(db)

@app.get("/api/backtest")
def backtest(db:Session=Depends(db_dep)):return backtest_summary(db)

@app.get("/api/admin/waitlist.csv")
def waitlist_export(x_admin_token:str|None=Header(default=None),db:Session=Depends(db_dep)):
    if S.admin_token and (not x_admin_token or not secrets.compare_digest(x_admin_token,S.admin_token)):raise HTTPException(403,"Invalid admin token")
    rows=db.scalars(select(WaitlistLead).order_by(WaitlistLead.created_at.desc())).all();buf=io.StringIO();w=csv.writer(buf);w.writerow(["email","name","investor_type","markets","consent","referral_code","referred_by","source","created_at"]);
    for x in rows:w.writerow([x.email,x.name,x.investor_type,x.markets,x.consent,x.referral_code,x.referred_by,x.source,x.created_at.isoformat()])
    return StreamingResponse(iter([buf.getvalue()]),media_type="text/csv",headers={"Content-Disposition":"attachment; filename=early-access.csv"})

@app.post("/api/admin/refresh")
def refresh(x_admin_token:str|None=Header(default=None),db:Session=Depends(db_dep)):
    if S.admin_token and (not x_admin_token or not secrets.compare_digest(x_admin_token,S.admin_token)):raise HTTPException(403,"Invalid admin token")
    runs=refresh_all(db);return [{"source":r.source,"status":r.status,"rows_seen":r.rows_seen,"rows_changed":r.rows_changed,"error":r.error} for r in runs]

@app.get("/api/events")
async def events(request:Request):
    async def gen():
        last=None
        while True:
            if await request.is_disconnected():break
            with SessionLocal() as db:
                stamp=db.scalar(select(func.max(IPO.updated_at)))
            cur=stamp.isoformat() if stamp else "empty"
            if cur!=last:yield f"event: data\ndata: {cur}\n\n";last=cur
            else:yield ": heartbeat\n\n"
            await asyncio.sleep(15)
    return StreamingResponse(gen(),media_type="text/event-stream",headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})
