from __future__ import annotations
import asyncio,secrets,time,csv,io,json,logging,uuid
from contextlib import asynccontextmanager
from collections import defaultdict,deque
from datetime import datetime,timezone,timedelta
from pathlib import Path
from fastapi import FastAPI,Depends,HTTPException,Request,Query,Header
from fastapi.responses import FileResponse,StreamingResponse,RedirectResponse,JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select,func,or_
from sqlalchemy.orm import Session
from .config import get_settings,validate_production_settings
from .db import init_db,SessionLocal
from .models import IPO,ScoreSnapshot,Provenance,PerformanceSnapshot,IngestionRun,WaitlistLead,EmailMessage,PredictionOutcome,AdminAuditLog,SheetsSyncOutbox
from .schemas import WaitlistIn,WaitlistOut
from .services.email_queue import enqueue,process_queue
from .services import email_provider as ep
from .services import webhooks as webhooks_svc
from .services import auth as auth_svc
from .services import sheets_sync as sheets_svc
from .services import clerk_auth as clerk_svc
from .services import heartbeat as heartbeat_svc
from .services.backtest import summarize as backtest_summary
from .services.pipeline import refresh_all
from .services import redflags as redflags_svc
from .services import contradictions as contradictions_svc
from .services import dcf as dcf_svc
from .services import similarity as similarity_svc
from .services import sensitivity as sensitivity_svc
from .services import changes as changes_svc
from .services import walkforward as walkforward_svc

logging.basicConfig(level=logging.INFO,format="%(message)s")
S=get_settings(); validate_production_settings(S); BASE=Path(__file__).parent; STATIC=BASE/"static"
@asynccontextmanager
async def lifespan(app):
    init_db()
    yield

app=FastAPI(title=S.app_name,version="2.0.0",docs_url="/api/docs",redoc_url=None,lifespan=lifespan)

@app.exception_handler(Exception)
async def unhandled_exception_handler(request:Request,exc:Exception):
    # Never leak exception details to the client - the traceback is logged
    # here (server-side only), keyed by the same request_id the client sees,
    # so an operator can find it without exposing it publicly. Starlette's
    # ExceptionMiddleware intercepts the exception before it would reach the
    # security_headers middleware's own try/except, so this is where the
    # web-request error path is actually logged, not there.
    request_id=getattr(request.state,"request_id","unknown")
    logging.getLogger("app.access").exception(json.dumps({"level":"error","component":"web",
        "request_id":request_id,"route":request.url.path,"status":500,"msg":"unhandled exception"}))
    return JSONResponse(status_code=500,content={"error":"internal_server_error","request_id":request_id})

app.mount("/static",StaticFiles(directory=STATIC),name="static")
# CSP is widened for Clerk's own domains only when a publishable key is actually
# configured - an inactive integration should not enlarge the attack surface.
if S.clerk_publishable_key:
    _CSP="default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' https://*.clerk.accounts.dev https://*.clerk.com; img-src 'self' data: https://img.clerk.com; connect-src 'self' https://*.clerk.accounts.dev https://*.clerk.com; frame-src https://*.clerk.accounts.dev https://*.clerk.com; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
else:
    _CSP="default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
rate=defaultdict(lambda:deque(maxlen=20))
auth_rate=defaultdict(lambda:deque(maxlen=20))

def db_dep():
    db=SessionLocal()
    try:yield db
    finally:db.close()

def require_admin(x_admin_token:str|None=Header(default=None)):
    if S.admin_token and (not x_admin_token or not secrets.compare_digest(x_admin_token,S.admin_token)):raise HTTPException(403,"Invalid admin token")

def require_active_lead(request:Request,db:Session=Depends(db_dep))->WaitlistLead:
    """Gates the dashboard's data API. A beta session cookie only exists for
    an ACTIVE lead (see auth_svc.get_lead_from_session) - WAITLISTED/INVITED/
    DISABLED never reach here even with a stale cookie."""
    lead=auth_svc.get_lead_from_session(db,request.cookies.get(auth_svc.SESSION_COOKIE))
    if not lead:raise HTTPException(401,"Beta access required. Sign in at /login.")
    return lead

def audit(db:Session,action:str,target:str="",**meta):
    db.add(AdminAuditLog(action=action,target=target,meta=meta))

def rate_limited(bucket,key:str,limit:int,window_s:int=60)->bool:
    now=time.time();q=bucket[key]
    while q and now-q[0]>window_s:q.popleft()
    if len(q)>=limit:return True
    q.append(now);return False

def latest_score(db,ipo_id):return db.scalar(select(ScoreSnapshot).where(ScoreSnapshot.ipo_id==ipo_id).order_by(ScoreSnapshot.created_at.desc()).limit(1))
def perf(db,ipo_id):return db.scalar(select(PerformanceSnapshot).where(PerformanceSnapshot.ipo_id==ipo_id).order_by(PerformanceSnapshot.created_at.desc()).limit(1))
def ipo_json(db,ipo):
    sc=latest_score(db,ipo.id);pf=perf(db,ipo.id)
    return {"id":ipo.id,"company":ipo.company,"symbol":ipo.symbol,"country":ipo.country,"exchange":ipo.exchange,"board":ipo.board,"sector":ipo.sector,"status":ipo.status,"filing_date":ipo.filing_date,"open_date":ipo.open_date,"close_date":ipo.close_date,"listing_date":ipo.listing_date,"currency":ipo.currency,"price_low":ipo.price_low,"price_high":ipo.price_high,"final_price":ipo.final_price,"issue_size_m":ipo.issue_size_m,"lot_size":ipo.lot_size,"qib_sub":ipo.qib_sub,"nii_sub":ipo.nii_sub,"retail_sub":ipo.retail_sub,"total_sub":ipo.total_sub,"gmp_pct":ipo.gmp_pct,"filing_url":ipo.filing_url,"registrar":ipo.registrar,"allotment_url":ipo.allotment_url,"updated_at":ipo.updated_at.isoformat() if ipo.updated_at else None,"score":None if not sc else {"overall":sc.overall_score,"listing":sc.listing_score,"long_term":sc.long_term_score,"confidence":sc.confidence,"listing_probability":sc.listing_gain_probability,"long_term_probability":sc.long_term_outperform_probability,"recommendation":sc.recommendation,"horizon":sc.horizon,"valuation":sc.valuation_label,"fair_low":sc.fair_value_low,"fair_high":sc.fair_value_high,"pillars":sc.pillars,"rationale":sc.rationale,"risks":sc.risks,"what_changes_verdict":sc.what_changes_verdict,"model_version":sc.model_version,"created_at":sc.created_at.isoformat()},"performance":None if not pf else {"as_of":pf.as_of_date,"close":pf.close_price,"listing_return_pct":pf.listing_return_pct,"return_1m_pct":pf.return_1m_pct,"return_6m_pct":pf.return_6m_pct,"return_12m_pct":pf.return_12m_pct,"source":pf.source_name}}

@app.get("/")
def landing():return FileResponse(STATIC/"index.html")
@app.get("/app")
def dashboard(request:Request,db:Session=Depends(db_dep)):
    lead=auth_svc.get_lead_from_session(db,request.cookies.get(auth_svc.SESSION_COOKIE))
    if not lead:return RedirectResponse("/login?next=/app")
    return FileResponse(STATIC/"app.html")
@app.get("/privacy")
def privacy():return FileResponse(STATIC/"privacy.html")
@app.get("/terms")
def terms():return FileResponse(STATIC/"terms.html")
@app.get("/preferences")
def preferences_page():return FileResponse(STATIC/"preferences.html")
@app.get("/login")
def login_page():return FileResponse(STATIC/"login.html")
@app.get("/admin")
def admin_page():return FileResponse(STATIC/"admin.html")

@app.get("/auth/callback")
def auth_callback(token:str,request:Request,db:Session=Depends(db_dep)):
    lead,err=auth_svc.redeem_login_token(db,token)
    if not lead:
        db.commit()
        return RedirectResponse(f"/login?error={err}")
    session_token=auth_svc.create_session(db,lead,request.headers.get("user-agent",""))
    db.commit()
    resp=RedirectResponse("/app")
    secure=request.url.scheme=="https"
    resp.set_cookie(auth_svc.SESSION_COOKIE,session_token,httponly=True,secure=secure,samesite="lax",max_age=auth_svc.SESSION_TTL_DAYS*86400,path="/")
    resp.set_cookie("csrf_token",secrets.token_urlsafe(24),httponly=False,secure=secure,samesite="lax",max_age=auth_svc.SESSION_TTL_DAYS*86400,path="/")
    return resp

@app.post("/api/auth/request-login")
def request_login(payload:dict,request:Request,db:Session=Depends(db_dep)):
    ip=request.client.host if request.client else "unknown"
    if rate_limited(auth_rate,ip,5,600):raise HTTPException(429,"Too many sign-in attempts. Try again later.")
    email=str(payload.get("email","")).strip().lower()
    lead=db.scalar(select(WaitlistLead).where(WaitlistLead.email==email)) if email else None
    # Same response whether or not the email exists/has access - avoids leaking who is on the beta list.
    if lead and lead.access_status in ("INVITED","ACTIVE"):
        raw=auth_svc.create_login_token(db,lead)
        db.commit()
        auth_svc.send_login_email(S,lead,raw)
    else:
        db.commit()
    return {"ok":True,"message":"If that email has beta access, a sign-in link is on its way."}

@app.post("/api/auth/logout")
def logout(request:Request,x_csrf_token:str|None=Header(default=None),db:Session=Depends(db_dep)):
    session_token=request.cookies.get(auth_svc.SESSION_COOKIE)
    lead=auth_svc.get_lead_from_session(db,session_token)
    if lead:
        cookie_csrf=request.cookies.get("csrf_token")
        if not cookie_csrf or not x_csrf_token or not secrets.compare_digest(cookie_csrf,x_csrf_token):raise HTTPException(403,"CSRF check failed")
        auth_svc.revoke_all_sessions(db,lead.id);db.commit()
    resp=JSONResponse({"ok":True})
    resp.delete_cookie(auth_svc.SESSION_COOKIE,path="/");resp.delete_cookie("csrf_token",path="/")
    return resp

@app.post("/api/auth/clerk-session")
def clerk_session(payload:dict,request:Request,db:Session=Depends(db_dep)):
    """Bridges a Clerk-authenticated browser session (Google/Apple/email via
    Clerk) into our own beta-gated session cookie. Clerk answers who the
    user is; WaitlistLead.access_status still answers whether they get in -
    a non-ACTIVE lead never receives a session cookie here, matching the
    magic-link path in /auth/callback."""
    if not S.clerk_publishable_key:raise HTTPException(503,"Clerk is not configured")
    token=str(payload.get("session_token",""))
    try:claims=clerk_svc.verify_session_token(token,S.clerk_publishable_key)
    except clerk_svc.InvalidSessionToken:raise HTTPException(401,"invalid Clerk session")
    clerk_user_id=claims.get("sub","")
    lead=db.scalar(select(WaitlistLead).where(WaitlistLead.clerk_user_id==clerk_user_id))
    if not lead:
        # Identity sync (via /webhooks/clerk) may not have landed yet - ask the client to retry shortly rather than guessing at an email from an unverified claim.
        return JSONResponse({"ok":False,"access_status":"PENDING_SYNC","message":"Setting up your account - retry in a moment."},status_code=202)
    if lead.access_status!="ACTIVE":
        message={"WAITLISTED":"You're on the early-access list.","INVITED":"Check your email to activate your invite.","DISABLED":"This account has been disabled."}.get(lead.access_status,"Access pending.")
        return {"ok":False,"access_status":lead.access_status,"message":message}
    session_token=auth_svc.create_session(db,lead,request.headers.get("user-agent",""))
    lead.last_login_at=auth_svc.now();db.commit()
    resp=JSONResponse({"ok":True,"access_status":"ACTIVE"})
    secure=request.url.scheme=="https"
    resp.set_cookie(auth_svc.SESSION_COOKIE,session_token,httponly=True,secure=secure,samesite="lax",max_age=auth_svc.SESSION_TTL_DAYS*86400,path="/")
    resp.set_cookie("csrf_token",secrets.token_urlsafe(24),httponly=False,secure=secure,samesite="lax",max_age=auth_svc.SESSION_TTL_DAYS*86400,path="/")
    return resp

@app.get("/api/auth/me")
def auth_me(request:Request,db:Session=Depends(db_dep)):
    lead=auth_svc.get_lead_from_session(db,request.cookies.get(auth_svc.SESSION_COOKIE))
    if not lead:return {"authenticated":False}
    return {"authenticated":True,"email":lead.email,"access_status":lead.access_status}
access_log=logging.getLogger("app.access")

@app.middleware("http")
async def security_headers(request:Request,call_next):
    request_id=request.headers.get("X-Request-Id") or uuid.uuid4().hex[:16]
    request.state.request_id=request_id
    t0=time.monotonic()
    try:
        response=await call_next(request)
        status=response.status_code
    except Exception:
        duration_ms=round((time.monotonic()-t0)*1000,1)
        access_log.exception(json.dumps({"level":"error","component":"web","request_id":request_id,
            "route":request.url.path,"duration_ms":duration_ms,"status":500,"msg":"unhandled exception"}))
        raise
    duration_ms=round((time.monotonic()-t0)*1000,1)
    access_log.info(json.dumps({"level":"info","component":"web","request_id":request_id,
        "route":request.url.path,"method":request.method,"duration_ms":duration_ms,"status":status}))
    response.headers["X-Request-Id"]=request_id
    response.headers["X-Content-Type-Options"]="nosniff"
    response.headers["X-Frame-Options"]="DENY"
    response.headers["Referrer-Policy"]="strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"]="camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"]=_CSP
    if request.url.scheme=="https":  # never claim HSTS over a plain-HTTP connection - it would be a lie the browser might cache
        response.headers["Strict-Transport-Security"]="max-age=31536000; includeSubDomains"
    return response

@app.get("/health")
def health(db:Session=Depends(db_dep)):
    try:db.scalar(select(func.count()).select_from(IPO));return {"status":"ok","version":"2.0.0","time":datetime.now(timezone.utc).isoformat()}
    except Exception as e:
        logging.getLogger("app.access").warning(json.dumps({"level":"warning","component":"web","route":"/health","msg":f"{type(e).__name__}: {e}"}))
        raise HTTPException(503,"database unavailable")

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
    lead=WaitlistLead(email=email,name=payload.name.strip(),investor_type=payload.investor_type,markets=payload.markets,consent=payload.consent,referral_code=code,referred_by=payload.referred_by,unsubscribe_token=secrets.token_urlsafe(24),source=payload.source,campaign=payload.campaign,page_path=payload.page_path)
    db.add(lead);db.commit()
    # Signup is durable before this line. Email is queued (fast local insert) and
    # delivered asynchronously by the worker - a slow/down provider never blocks signup.
    # Google Sheets mirror is the same pattern: enqueue only (outbox row), never
    # call the Sheets API inline - see sheets_svc.process_outbox in worker.py.
    enqueue(db,lead,"welcome",ep.PRIORITY_TRANSACTIONAL)
    sheets_svc.enqueue(db,lead)
    db.commit()
    return WaitlistOut(ok=True,message="Early access reserved. We'll notify you about launch and material IPO-score changes.",referral_code=code)

_EVENT_NAMES={"landing_view","early_access_modal_shown","early_access_modal_dismissed","early_access_started",
    "early_access_completed","sign_in_started","sign_in_completed","dashboard_opened"}

@app.post("/api/events")
def track_event(payload:dict,request:Request):
    """First-party, privacy-conscious conversion tracking (see spec section
    38) - logged only, no third-party beacon, no persistent table. Unknown
    event names are dropped rather than logged verbatim (bounded log volume,
    no arbitrary client-controlled log injection beyond a fixed vocabulary)."""
    name=str(payload.get("name",""))
    if name not in _EVENT_NAMES:return {"ok":False}
    ip=request.client.host if request.client else "unknown"
    if rate_limited(rate,f"events:{ip}",60):return {"ok":False}
    access_log.info(json.dumps({"level":"info","component":"analytics","event":name,"path":str(payload.get("path",""))[:200]}))
    return {"ok":True}

@app.get("/api/public/highlights")
def public_highlights(db:Session=Depends(db_dep)):
    """Unauthenticated preview for the landing page (see static/index.html's
    'Upcoming now' strip and hero product card). Deliberately trimmed - no
    fundamentals, provenance, or rationale, none of which is available
    without a beta session (see require_active_lead on /api/ipos/*)."""
    stmt=select(IPO).where(IPO.status.in_(["Open","Upcoming","Filed"])).order_by(IPO.updated_at.desc()).limit(5)
    rows=db.scalars(stmt).all()
    out=[]
    for ipo in rows:
        sc=latest_score(db,ipo.id)
        if not sc:continue
        out.append({"company":ipo.company,"country":ipo.country,"status":ipo.status,"open_date":ipo.open_date,"close_date":ipo.close_date,
            "overall":sc.overall_score,"listing":sc.listing_score,"long_term":sc.long_term_score,"confidence":sc.confidence,"valuation":sc.valuation_label})
    return {"ipos":out}

@app.get("/api/clerk/config")
def clerk_config():
    """Publishable-key-only - a Clerk publishable key is designed to be
    embedded in frontend JS (it identifies the Clerk application, it does
    not authenticate as it). The secret key never leaves the server."""
    return {"publishable_key":S.clerk_publishable_key,"configured":bool(S.clerk_publishable_key)}

@app.post("/webhooks/clerk")
async def clerk_webhook(request:Request,db:Session=Depends(db_dep)):
    body=await request.body()
    ok=webhooks_svc.verify_svix_signature(S.clerk_webhook_secret,request.headers.get("svix-id",""),request.headers.get("svix-timestamp",""),request.headers.get("svix-signature",""),body)
    if not ok:raise HTTPException(401,"invalid or unsigned webhook payload")
    try:event=json.loads(body)
    except Exception:raise HTTPException(400,"invalid JSON")
    etype=event.get("type","");data=event.get("data",{}) or {}
    if etype=="user.created" or etype=="user.updated":
        emails=data.get("email_addresses") or []
        email=next((e.get("email_address") for e in emails if e.get("id")==data.get("primary_email_address_id")),None) or (emails[0].get("email_address") if emails else "")
        if not email:return {"handled":False,"type":etype,"reason":"no email on payload"}
        name=" ".join(filter(None,[data.get("first_name"),data.get("last_name")])).strip()
        provider=""
        accounts=data.get("external_accounts") or []
        if accounts:provider=accounts[0].get("provider","").replace("oauth_","")
        lead=clerk_svc.sync_identity(db,clerk_user_id=data.get("id",""),email=email,name=name,provider=provider)
        sheets_svc.enqueue(db,lead)
        db.commit()
        return {"handled":True,"type":etype,"access_status":lead.access_status}
    if etype=="user.deleted":
        clerk_svc.handle_user_deleted(db,data.get("id",""))
        db.commit()
        return {"handled":True,"type":etype}
    return {"handled":False,"type":etype}


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
def summary(db:Session=Depends(db_dep),_lead:WaitlistLead=Depends(require_active_lead)):
    total=db.scalar(select(func.count()).select_from(IPO)) or 0
    open_n=db.scalar(select(func.count()).select_from(IPO).where(IPO.status.in_(["Open","Upcoming","Filed"]))) or 0
    listed=db.scalar(select(func.count()).select_from(IPO).where(IPO.status=="Listed")) or 0
    last=db.scalar(select(IngestionRun).order_by(IngestionRun.started_at.desc()).limit(1))
    high_conf=db.scalar(select(func.count()).select_from(ScoreSnapshot).where(ScoreSnapshot.confidence>=S.min_recommendation_confidence)) or 0
    return {"total":total,"active":open_n,"listed":listed,"high_confidence_scores":high_conf,"strict_reliability":S.strict_reliability,"min_confidence":S.min_recommendation_confidence,"last_ingestion":None if not last else {"source":last.source,"status":last.status,"started_at":last.started_at.isoformat(),"finished_at":last.finished_at.isoformat() if last.finished_at else None,"error":last.error}}

@app.get("/api/ipos")
def ipos(country:str="all",status:str="all",q:str="",limit:int=Query(100,ge=1,le=500),db:Session=Depends(db_dep),_lead:WaitlistLead=Depends(require_active_lead)):
    stmt=select(IPO)
    if country!="all":stmt=stmt.where(IPO.country==country)
    if status!="all":stmt=stmt.where(IPO.status==status)
    if q:stmt=stmt.where(or_(IPO.company.ilike(f"%{q}%"),IPO.symbol.ilike(f"%{q}%")))
    rows=db.scalars(stmt.order_by(IPO.updated_at.desc()).limit(limit)).all()
    return [ipo_json(db,x) for x in rows]

@app.get("/api/ipos/{ipo_id}")
def ipo_detail(ipo_id:int,db:Session=Depends(db_dep),_lead:WaitlistLead=Depends(require_active_lead)):
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
def ipo_changes(ipo_id:int,db:Session=Depends(db_dep),_lead:WaitlistLead=Depends(require_active_lead)):
    ipo=db.get(IPO,ipo_id)
    if not ipo:raise HTTPException(404,"IPO not found")
    return {"ipo_id":ipo_id,"timeline":changes_svc.timeline(db,ipo_id)}

@app.get("/api/ipos/{ipo_id}/similar")
def ipo_similar(ipo_id:int,db:Session=Depends(db_dep),_lead:WaitlistLead=Depends(require_active_lead)):
    ipo=db.get(IPO,ipo_id)
    if not ipo:raise HTTPException(404,"IPO not found")
    return similarity_svc.find_similar(db,ipo)

@app.get("/api/ipos/{ipo_id}/valuation")
def ipo_valuation_detail(ipo_id:int,db:Session=Depends(db_dep),_lead:WaitlistLead=Depends(require_active_lead)):
    ipo=db.get(IPO,ipo_id)
    if not ipo:raise HTTPException(404,"IPO not found")
    return {"scenario_dcf":dcf_svc.scenario_dcf(ipo),"reverse_dcf":dcf_svc.reverse_dcf(ipo)}

@app.get("/api/model-performance")
def model_performance(db:Session=Depends(db_dep),_lead:WaitlistLead=Depends(require_active_lead)):
    return walkforward_svc.evaluate(db)

@app.get("/api/performance")
def performance(country:str="all",limit:int=Query(200,ge=1,le=500),db:Session=Depends(db_dep),_lead:WaitlistLead=Depends(require_active_lead)):
    stmt=select(IPO).where(IPO.status=="Listed")
    if country!="all":stmt=stmt.where(IPO.country==country)
    rows=db.scalars(stmt.order_by(IPO.updated_at.desc()).limit(limit)).all();return [ipo_json(db,x) for x in rows]

def _source_health_rows(db:Session)->list[dict]:
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
    hb_status=heartbeat_svc.status(db,S.worker_interval_seconds)
    out.append({"source":"Worker","tier":1,"status":hb_status["status"],"last_run":hb_status["last_seen"],"rows":0,"error":hb_status["last_error"]})
    return out

@app.get("/api/source-health")
def source_health(db:Session=Depends(db_dep),_lead:WaitlistLead=Depends(require_active_lead)):
    return _source_health_rows(db)

@app.get("/api/admin/ops-summary",dependencies=[Depends(require_admin)])
def admin_ops_summary(db:Session=Depends(db_dep)):
    hb_status=heartbeat_svc.status(db,S.worker_interval_seconds)
    total_forward=db.scalar(select(func.count()).select_from(ScoreSnapshot).where(ScoreSnapshot.is_forward==True)) or 0  # noqa: E712
    today=datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0)
    new_today=db.scalar(select(func.count()).select_from(ScoreSnapshot).where(ScoreSnapshot.is_forward==True,ScoreSnapshot.created_at>=today)) or 0  # noqa: E712
    final_pre_listing=db.scalar(select(func.count()).select_from(ScoreSnapshot).where(ScoreSnapshot.event_stage=="final_pre_listing")) or 0
    recent_failed_runs=db.scalars(select(IngestionRun).where(IngestionRun.status=="error").order_by(IngestionRun.started_at.desc()).limit(10)).all()
    return {
        "source_health":_source_health_rows(db),
        "worker":hb_status,
        "predictions":{"total_forward":total_forward,"new_today":new_today,"final_pre_listing":final_pre_listing},
        "recent_ingestion_failures":[{"source":r.source,"started_at":r.started_at.isoformat(),"error":r.error[:300]} for r in recent_failed_runs],
    }

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
def backtest(db:Session=Depends(db_dep),_lead:WaitlistLead=Depends(require_active_lead)):return backtest_summary(db)

@app.get("/api/track-record")
def track_record(limit:int=100,db:Session=Depends(db_dep),_lead:WaitlistLead=Depends(require_active_lead)):
    """The live, immutable forward track record - distinct from /api/backtest
    (which includes retrofitted/backfilled historical scoring). Every row here
    is a ScoreSnapshot written while the IPO's outcome was NOT yet known."""
    rows=db.scalars(select(ScoreSnapshot).where(ScoreSnapshot.is_forward==True).order_by(ScoreSnapshot.created_at.desc()).limit(limit)).all()  # noqa: E712
    out=[]
    for sc in rows:
        ipo=db.get(IPO,sc.ipo_id)
        if not ipo:continue
        outcome=db.scalar(select(PredictionOutcome).where(PredictionOutcome.score_snapshot_id==sc.id))
        out.append({
            "ipo_id":ipo.id,"company":ipo.company,"country":ipo.country,"status":ipo.status,
            "predicted_at":sc.created_at.isoformat(),"event_stage":sc.event_stage,
            "model_version":sc.model_version,"feature_schema_version":sc.feature_schema_version,
            "overall_score":sc.overall_score,"listing_score":sc.listing_score,"long_term_score":sc.long_term_score,
            "listing_gain_probability":sc.listing_gain_probability,"long_term_outperform_probability":sc.long_term_outperform_probability,
            "recommendation":sc.recommendation,"valuation_label":sc.valuation_label,"confidence":sc.confidence,
            "outcome_known":ipo.status=="Listed",
            "outcome": None if not outcome else {
                "listing_open_return_pct":outcome.listing_open_return_pct,"listing_close_return_pct":outcome.listing_close_return_pct,
                "return_7d_pct":outcome.return_7d_pct,"return_30d_pct":outcome.return_30d_pct,"return_6m_pct":outcome.return_6m_pct,
                "return_12m_pct":outcome.return_12m_pct,"return_24m_pct":outcome.return_24m_pct,
                "benchmark_relative_return_pct":outcome.benchmark_relative_return_pct,
            },
        })
    total_forward=db.scalar(select(func.count()).select_from(ScoreSnapshot).where(ScoreSnapshot.is_forward==True)) or 0  # noqa: E712
    graded=db.scalar(select(func.count()).select_from(PredictionOutcome)) or 0
    return {"note":"Every row is a prediction made while the IPO's outcome was not yet known, never rewritten after the fact. Separate from /api/backtest, which also includes retrofitted historical scoring.",
            "total_forward_predictions":total_forward,"graded_with_outcome":graded,"predictions":out}

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

@app.get("/api/admin/sheets-status",dependencies=[Depends(require_admin)])
def admin_sheets_status(db:Session=Depends(db_dep)):
    return sheets_svc.sync_status_counts(db,S)

@app.post("/api/admin/sheets-retry",dependencies=[Depends(require_admin)])
def admin_sheets_retry(db:Session=Depends(db_dep)):
    n=sheets_svc.retry_failed(db)
    audit(db,"sheets_retry_triggered",rows_reset=n);db.commit()
    return {"ok":True,"rows_reset":n}

@app.get("/api/admin/users",dependencies=[Depends(require_admin)])
def admin_list_users(db:Session=Depends(db_dep)):
    rows=db.scalars(select(WaitlistLead).order_by(WaitlistLead.created_at.desc())).all()
    return [{"id":x.id,"email":x.email,"name":x.name,"investor_type":x.investor_type,"markets":x.markets,
             "access_status":x.access_status,"created_at":x.created_at.isoformat(),
             "last_login_at":x.last_login_at.isoformat() if x.last_login_at else None} for x in rows]

@app.post("/api/admin/users/{lead_id}/invite",dependencies=[Depends(require_admin)])
def admin_invite_user(lead_id:int,db:Session=Depends(db_dep)):
    lead=db.get(WaitlistLead,lead_id)
    if not lead:raise HTTPException(404,"No such user")
    if lead.access_status=="DISABLED":raise HTTPException(400,"User is disabled - re-enable first")
    lead.access_status="INVITED"
    raw=auth_svc.create_login_token(db,lead,purpose="invite")
    audit(db,"invite_created",lead.email,lead_id=lead.id)
    db.commit()
    result=auth_svc.send_login_email(S,lead,raw)
    return {"ok":True,"access_status":lead.access_status,"email_sent":result.ok,"email_error":"" if result.ok else result.error}

@app.post("/api/admin/users/{lead_id}/disable",dependencies=[Depends(require_admin)])
def admin_disable_user(lead_id:int,db:Session=Depends(db_dep)):
    lead=db.get(WaitlistLead,lead_id)
    if not lead:raise HTTPException(404,"No such user")
    lead.access_status="DISABLED"
    auth_svc.revoke_all_sessions(db,lead.id)
    audit(db,"user_disabled",lead.email,lead_id=lead.id)
    db.commit()
    return {"ok":True,"access_status":lead.access_status}

@app.post("/api/admin/users/{lead_id}/enable",dependencies=[Depends(require_admin)])
def admin_enable_user(lead_id:int,db:Session=Depends(db_dep)):
    lead=db.get(WaitlistLead,lead_id)
    if not lead:raise HTTPException(404,"No such user")
    lead.access_status="INVITED" if lead.access_status=="DISABLED" else lead.access_status
    audit(db,"user_re_enabled",lead.email,lead_id=lead.id)
    db.commit()
    return {"ok":True,"access_status":lead.access_status}

@app.get("/api/admin/audit-log",dependencies=[Depends(require_admin)])
def admin_audit_log(limit:int=Query(200,ge=1,le=1000),db:Session=Depends(db_dep)):
    rows=db.scalars(select(AdminAuditLog).order_by(AdminAuditLog.created_at.desc()).limit(limit)).all()
    return [{"id":x.id,"actor":x.actor,"action":x.action,"target":x.target,"meta":x.meta,"created_at":x.created_at.isoformat()} for x in rows]

@app.get("/api/events")
async def events(request:Request,_lead:WaitlistLead=Depends(require_active_lead)):
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
