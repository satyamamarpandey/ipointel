from __future__ import annotations
import re
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, func
from sqlalchemy.orm import Session
import httpx
from ..models import IPO, ScoreSnapshot, Provenance, IngestionRun, PerformanceSnapshot, FeatureObservation
from ..scoring import compute_score, feature_snapshot, FEATURE_SCHEMA_VERSION
from ..config import get_settings
from . import sec,nse,market,enrichment,nse_master
from .identity import canonical_name, classify_issue_type, board_for_issue_type, status_can_transition, sanitize_label, normalize_date, DATE_FIELDS
from .net_safety import validate_outbound_url

_NSE_ALLOWED_HOSTS={"nsearchives.nseindia.com","www.nseindia.com","nseindia.com","archives.nseindia.com"}
NSE_ARCHIVE_PAGE="https://www.nseindia.com/static/regulations/segment-wise-historical-reports-capital-primary-market"

# An India issue whose close date passed this long ago without a confirmed
# listing is no longer treated as active pipeline. It keeps its Closed status
# (nothing is invented about why) and the public build excludes it with an
# explicit reason - see scripts/build_pages.py.
INDIA_LISTING_GRACE_DAYS=45

def now(): return datetime.now(timezone.utc)

# Leftover EDGAR form suffix at the head of a company name: "1 - ACME, INC."
# from S-1, "11 - ..." from S-11, "1/A - ..." from an S-1/A amendment. Fixed
# at the source in sec.parse_atom; this is what repairs rows already stored.
# The spaces around the hyphen are required, and that is what makes it safe:
# a genuine numeric-leading name ("1-800-FLOWERS.COM", "3i Infotech",
# "5paisa Capital", "360 ONE WAM") never has " - " after the digits.
_SEC_FORM_ARTIFACT=re.compile(r"^\d{1,2}(?:/[A-Z]+)?\s+-\s+")
# Trailing EDGAR title artifact "(0001804792) (Filer)" left on rows stored by
# an older atom parser that did not capture the CIK group.
_SEC_CIK_ARTIFACT=re.compile(r"\s*\(\d{7,10}\)\s*\((?:Filer|Subject|Reporting Owner|Issuer)\)\s*$",re.I)

def clean_company_name(name:str,country:str="")->str:
    """Company names arrive from EDGAR atom titles and NSE/BSE report sheets,
    which carry tabs, newlines and doubled spaces. Normalising here keeps the
    dirt out of page titles, <meta>/og: tags and the public JSON feeds."""
    if not name:return ""
    cleaned=re.sub(r"\s+"," ",name.replace(" "," ")).strip()
    # SEC-only: NSE names never carry this artifact, and restricting it keeps
    # an Indian issuer that legitimately starts with digits out of reach.
    if country.lower() in ("united states","us","usa"):
        cleaned=_SEC_FORM_ARTIFACT.sub("",cleaned).strip()
        cleaned=_SEC_CIK_ARTIFACT.sub("",cleaned).strip()
    return cleaned

_ARTIFACT_WORDS={"ipo","sme ipo","total","grand total","company name","name of the company","issuer"}

def is_report_artifact_name(name:str|None)->bool:
    """True for a 'company name' that is a spreadsheet footnote or header
    token rather than an issuer: starts with an asterisk/hash footnote marker,
    is a bare column word, or has no letters at all."""
    if not name:return True
    s=" ".join(str(name).split()).strip()
    if s.startswith(("*","#","note","notes:")):return True
    if s.lower() in _ARTIFACT_WORDS:return True
    return not any(ch.isalpha() for ch in s)

def repair_company_names(db:Session)->int:
    """Idempotent backfill for rows stored before the parser was fixed. Runs
    inside refresh_all() rather than as a one-shot script so every deployment
    self-heals: the server worker and the GitHub Pages refresh (whose SQLite
    snapshot is restored from the data-state branch each run) both call it.
    Returns the number of rows actually changed - 0 on every run after the
    first, so it stays silent once the data is clean."""
    changed=0
    for ipo in db.scalars(select(IPO)).all():
        fixed=clean_company_name(ipo.company or "",ipo.country or "")
        if fixed and fixed!=ipo.company:
            ipo.company=fixed;changed+=1
    return changed

_DATE_FLAG_PREFIX="date_unparseable: "

def _set_flag(ipo:IPO,prefix:str,flag:str|None)->bool:
    """Replace any data_flags entry starting with `prefix` by `flag` (or drop
    it when flag is None). Returns True when the list changed. Never appends a
    duplicate, so repeated passes stay idempotent."""
    flags=[f for f in (ipo.data_flags or []) if not str(f).startswith(prefix)]
    if flag:flags.append(flag)
    if flags!=(ipo.data_flags or []):
        ipo.data_flags=flags;return True
    return False

def repair_dates(db:Session)->int:
    """Idempotent normalisation of every stored date column to YYYY-MM-DD
    (Phase 11). India rows arrived as "21-Oct-2022" / "2026-07-01 00:00:00",
    US rows as "20251022"; all parse, none sort. Values that do not parse are
    left exactly as they are (nothing is invented) and flagged once. Returns
    the number of rows changed - 0 on every run after the first."""
    changed=0
    for ipo in db.scalars(select(IPO)).all():
        row_changed=False;bad=[]
        for f in DATE_FIELDS:
            cur=getattr(ipo,f) or ""
            if not cur:continue
            iso=normalize_date(cur)
            if iso and iso!=cur:setattr(ipo,f,iso);row_changed=True
            elif not iso:bad.append(f"{f}={cur}")
        flag=(_DATE_FLAG_PREFIX+"; ".join(bad)) if bad else None
        if _set_flag(ipo,_DATE_FLAG_PREFIX,flag):row_changed=True
        if row_changed:changed+=1
    db.flush()
    return changed

def external_key(row):
    if row.get("country","").lower()=="india":return "IN:"+(row.get("symbol") or row.get("company","")).strip().lower()
    return "US:"+(row.get("cik") or row.get("symbol") or row.get("company","")).strip().lower()

class NameIndex:
    """Per-ingest lookup of existing India rows by canonical issuer name and by
    ISIN, so a report row without a symbol resolves to the row the live NSE
    feed created under "IN:<symbol>" instead of spawning a punctuation-variant
    duplicate. Stored external_keys are never rewritten (URL stability); this
    only decides WHICH existing row an incoming record updates. When several
    stored rows already share a canonical name (legacy duplicates), the one
    with a symbol wins, then the lowest id - deterministic, never destructive."""
    def __init__(self,db:Session,country:str="India"):
        self.by_name:dict[str,IPO]={}
        self.by_isin:dict[str,IPO]={}
        for ipo in db.scalars(select(IPO).where(IPO.country==country)).all():
            self.add(ipo)
    def _better(self,a:IPO,b:IPO)->IPO:
        if bool(a.symbol)!=bool(b.symbol):return a if a.symbol else b
        return a if a.id<=b.id else b
    def add(self,ipo:IPO):
        c=canonical_name(ipo.company)
        if c:
            cur=self.by_name.get(c)
            self.by_name[c]=ipo if cur is None else self._better(cur,ipo)
        if ipo.isin:
            self.by_isin.setdefault(ipo.isin.upper(),ipo)
    def find(self,company:str="",isin:str="")->IPO|None:
        if isin and isin.upper() in self.by_isin:return self.by_isin[isin.upper()]
        return self.by_name.get(canonical_name(company))

def resolve_existing(db:Session,row:dict,key:str,index:NameIndex|None)->IPO|None:
    ipo=db.scalar(select(IPO).where(IPO.external_key==key))
    if ipo or index is None:return ipo
    return index.find(row.get("company",""),row.get("isin",""))

def _numeric(v):
    try:return float(v)
    except (TypeError,ValueError):return None

def add_provenance(db:Session,ipo:IPO,field:str,value,source_name,url,tier:int):
    if value in (None,""):return
    q=db.scalar(select(Provenance).where(Provenance.ipo_id==ipo.id,Provenance.field_name==field,Provenance.source_url==url))
    if q:q.observed_value=str(value);q.observed_at=now();q.source_tier=tier;q.source_name=source_name
    else:q=Provenance(ipo_id=ipo.id,field_name=field,source_name=source_name,source_url=url,source_tier=tier,observed_value=str(value));db.add(q)
    db.flush()
    others=db.scalars(select(Provenance).where(Provenance.ipo_id==ipo.id,Provenance.field_name==field,Provenance.source_name!=source_name)).all()
    new_val=_numeric(value)
    conflict=False
    if new_val is not None:
        for o in others:
            ov=_numeric(o.observed_value)
            if ov is None:continue
            base=max(abs(new_val),abs(ov),1e-9)
            if abs(new_val-ov)/base>0.04:
                conflict=True;o.is_conflict=True
            else:
                o.is_conflict=False
    q.is_conflict=conflict

def _score_moved(last:ScoreSnapshot|None,score:dict)->bool:
    if last is None:return True
    if sanitize_label(last.recommendation)!=sanitize_label(score["recommendation"]):return True
    return any(abs(getattr(last,k)-score[k])>=0.1 for k in ("overall_score","listing_score","long_term_score","confidence"))

def _event_stage(created:bool,changed_fields:set,prev:dict,ipo:IPO,last:ScoreSnapshot|None,score:dict) -> str|None:
    """Decide (deterministically, no LLM/inference) which single event this
    prediction snapshot documents. Order matters - most specific/important
    wins when several fields moved in the same ingest pass.

    Structural events (discovery, filing, price band, final price, status
    change) always produce a snapshot: they are what the forward record must
    document even when the score itself happens not to move. Everything else
    (subscription ticks, minor field refreshes) only produces one when the
    score or recommendation actually changed - a subscription figure moving
    from 1.2341x to 1.2343x with an identical score is not a prediction
    event, and repeating it every refresh produced duplicate snapshots."""
    if created:
        return "ipo_discovered"
    if "status" in changed_fields and ipo.status in ("Listed","Withdrawn","Not IPO"):
        return "status_changed"
    if "final_price" in changed_fields and not prev["final_price"] and ipo.status.lower()!="listed":
        return "final_pre_listing"
    if "filing_url" in changed_fields:
        return "filing_ingested" if not prev["filing_url"] else "filing_amendment"
    if "price_low" in changed_fields or "price_high" in changed_fields:
        return "price_band_set"
    if "status" in changed_fields:
        return "status_changed"
    moved=_score_moved(last,score)
    if "anchor_quality" in changed_fields:
        return "anchor_data_added" if moved else None
    if changed_fields & {"qib_sub","nii_sub","retail_sub","total_sub"}:
        return "subscription_update" if moved else None
    if last and sanitize_label(last.recommendation)!=sanitize_label(score["recommendation"]):
        return "recommendation_changed"
    if not last or any(abs(getattr(last,k)-score[k])>=0.1 for k in ("overall_score","listing_score","long_term_score","confidence")):
        return "material_score_change"
    return None

UPSERT_FIELDS=["company","symbol","isin","country","exchange","board","sector","status","filing_date","open_date","close_date","listing_date","currency","price_low","price_high","final_price","issue_size_m","shares_offered_m","post_issue_shares_m","lot_size","revenue_m","revenue_prev_m","revenue_2y_ago_m","ebitda_m","net_income_m","cfo_m","debt_m","cash_m","fresh_issue_pct","ofs_pct","promoter_retention_pct","qib_sub","nii_sub","retail_sub","total_sub","gmp_pct","underwriter_quality","anchor_quality","market_regime","sector_regime","dual_class","lockup_days","market_overhang_pct","peer_median_pe","peer_median_ps","peer_median_ev_ebitda","filing_url","registrar","allotment_url"]

def upsert_ipo(db:Session,row:dict,source_name:str,source_url:str,tier:int,index:NameIndex|None=None):
    # Single choke point for every source, so no ingester can reintroduce a
    # dirty name. external_key already lower/strips, so this never moves a
    # row to a different key (no duplicate, no changed /ipo/<slug>/ URL).
    if row.get("company"):
        row={**row,"company":clean_company_name(row["company"],row.get("country",""))}
    # Canonical date storage (YYYY-MM-DD). The source spelling survives in
    # row["raw"]; a value that does not parse is passed through unchanged so
    # repair_dates() can flag it rather than silently dropping it here.
    dates={f:(normalize_date(row[f]) or row[f]) for f in DATE_FIELDS if row.get(f) not in (None,"")}
    if dates:row={**row,**dates}
    key=external_key(row); ipo=resolve_existing(db,row,key,index); created=False
    if not ipo:
        ipo=IPO(external_key=key,company=row.get("company") or "Unknown",country=row.get("country") or "Unknown");db.add(ipo);db.flush();created=True
        if index is not None:index.add(ipo)
    prev={"filing_url":ipo.filing_url,"final_price":ipo.final_price}
    changed_fields=set()
    for f in UPSERT_FIELDS:
        if f not in row or row[f] in (None,""):continue
        value=row[f]
        if f=="status" and not status_can_transition(ipo.status,value):
            continue  # a stale feed can never regress Listed/Withdrawn back to Open/Upcoming
        if f=="company" and not created and ipo.symbol and row.get("symbol","")=="" :
            # A report row without a symbol resolved (by name/ISIN) onto the
            # live-feed row: keep the live feed's spelling of the name so the
            # page title does not flip between "Ltd." and "Limited" each month.
            add_provenance(db,ipo,f,value,source_name,source_url,tier);continue
        if getattr(ipo,f)!=value: setattr(ipo,f,value);changed_fields.add(f)
        add_provenance(db,ipo,f,value,source_name,source_url,tier)
    if row.get("raw") and row["raw"]!=ipo.raw:ipo.raw=row["raw"]
    if row.get("data_flags") is not None and row["data_flags"]!=ipo.data_flags:ipo.data_flags=row["data_flags"]
    if created or changed_fields:ipo.updated_at=now()
    db.flush()
    conflicts=db.scalar(select(func.count()).select_from(Provenance).where(Provenance.ipo_id==ipo.id,Provenance.is_conflict==True)) or 0
    score=compute_score(ipo,conflicts)
    last=db.scalar(select(ScoreSnapshot).where(ScoreSnapshot.ipo_id==ipo.id).order_by(ScoreSnapshot.created_at.desc()).limit(1))
    stage=_event_stage(created,changed_fields,prev,ipo,last,score)
    if stage:
        prov_ids=list(db.scalars(select(Provenance.id).where(Provenance.ipo_id==ipo.id)))
        db.add(ScoreSnapshot(ipo_id=ipo.id,event_stage=stage,feature_schema_version=FEATURE_SCHEMA_VERSION,
                              feature_snapshot=feature_snapshot(ipo),provenance_ids=prov_ids,
                              is_forward=ipo.status.lower() not in ("listed","withdrawn","not ipo"),**score))
    return created or bool(changed_fields)

def _finish(db:Session,run:IngestionRun,seen:int,changed:int,warnings:list[str],metadata:dict|None=None):
    run=db.get(IngestionRun,run.id)
    run.status="ok" if not warnings else "partial";run.error=" | ".join(warnings)[:4000];run.rows_seen=seen;run.rows_changed=changed;run.finished_at=now()
    if metadata:run.metadata_json=metadata
    db.commit();return run

def _fail(db:Session,run:IngestionRun,e:Exception):
    db.rollback();run=db.get(IngestionRun,run.id);run.status="error";run.error=f"{type(e).__name__}: {e}"[:4000];run.finished_at=now();db.commit();return run

def ingest_sec(db:Session):
    s=get_settings(); run=IngestionRun(source="SEC EDGAR",status="running");db.add(run);db.commit();seen=changed=0;not_ipo=0
    try:
        rows,warnings=sec.fetch_recent_ipos(s.sec_user_agent)
        for row in rows:
            row.update({"country":"United States","exchange":"NASDAQ/NYSE/Other","board":"Mainboard","sector":"Unknown","status":"Filed","currency":"USD"})
            key=external_key(row);existing=db.scalar(select(IPO).where(IPO.external_key==key))
            already_classified=bool(existing) and any(str(f).startswith(sec.CLASSIFIED_MARKER) for f in (existing.data_flags or []))
            if existing and existing.filing_url==row.get("filing_url") and existing.filing_url and already_classified:
                # Same filing document as last run and already classified:
                # nothing in the prospectus text or XBRL facts can have
                # changed, so do not re-download them (SEC asks for restraint;
                # this was the bulk of our requests). The stored row keeps
                # every previously parsed value.
                enriched=dict(row)
            else:
                enriched=sec.enrich(row,s.sec_user_agent)
                if any(str(f).startswith(sec.NON_IPO_FLAG_PREFIX) for f in enriched.get("data_flags",[])):
                    enriched["status"]="Not IPO";not_ipo+=1
            seen+=1
            if upsert_ipo(db,enriched,"SEC EDGAR",row.get("filing_url") or "https://www.sec.gov/edgar/search/",1):changed+=1
        return _finish(db,run,seen,changed,warnings,{"classified_not_ipo":not_ipo})
    except Exception as e:return _fail(db,run,e)

def ingest_sec_priced(db:Session, lookback_days:int=5):
    import time as _time
    s=get_settings();run=IngestionRun(source="SEC Priced IPOs",status="running");db.add(run);db.commit();seen=changed=0;warnings=[];withdrawn=0;unpublished=[]
    try:
        today=datetime.now(timezone.utc).date()
        for i in range(lookback_days):
            day=today-timedelta(days=i)
            if day.weekday()>=5:continue  # Sat/Sun: SEC never publishes a daily-index file - not a fetch failure, don't warn or request
            try: idx,index_url=sec.master_index_if_published(day,s.sec_user_agent)
            except sec.DailyIndexUnavailable: unpublished.append(str(day));continue  # not yet published / holiday: not a source failure
            except Exception as e: warnings.append(f"{day}: {type(e).__name__}");continue
            # Registration withdrawal requests (form RW). Matched by CIK to a
            # Filed row we already track; the row becomes Withdrawn and is
            # never counted as an upcoming IPO again. No text parsing needed.
            for meta in idx.get("RW",[]):
                key="US:"+meta["cik"].strip().lower()
                ipo=db.scalar(select(IPO).where(IPO.external_key==key))
                if ipo and ipo.status in ("Filed","Upcoming","Open","Closed"):
                    seen+=1
                    if upsert_ipo(db,{"country":"United States","cik":meta["cik"],"company":ipo.company,"status":"Withdrawn","filing_url":meta["filing_url"]},"SEC RW",meta["filing_url"],1):changed+=1;withdrawn+=1
            for meta in idx.get("424B4",[]):
                try:
                    txt=sec.filing_text(meta["filing_url"],s.sec_user_agent);parsed=sec.parse_priced_ipo(txt)
                    if not parsed:continue
                    row={**meta,**parsed,"country":"United States","exchange":"NASDAQ/NYSE/Other","board":"Mainboard","sector":"Unknown","status":"Listed","currency":"USD","listing_date":meta.get("filing_date","")}
                    seen+=1
                    if upsert_ipo(db,row,"SEC 424B4",meta["filing_url"],1):changed+=1
                    _time.sleep(.12)
                except Exception as e: warnings.append(f"{meta.get('company','?')}: {type(e).__name__}")
        return _finish(db,run,seen,changed,warnings,{"withdrawn":withdrawn,"index_not_published":unpublished})
    except Exception as e:return _fail(db,run,e)

def ingest_secondary_enrichment(db:Session):
    s=get_settings();run=IngestionRun(source="Licensed enrichment feed",status="running");db.add(run);db.commit();seen=changed=0
    try:
        rows,warnings=enrichment.fetch_rows()
        index=NameIndex(db)
        for row in rows:
            country=row.get("country") or ("India" if str(row.get("symbol","")).endswith((".NS",".BO")) else "United States")
            row["country"]=country;seen+=1
            source_name=row.pop("source_name",None) or "Licensed enrichment feed";source_url=row.pop("source_url",None) or s.secondary_enrichment_url
            if upsert_ipo(db,row,source_name,source_url,3,index if country=="India" else None):changed+=1
        return _finish(db,run,seen,changed,warnings)
    except Exception as e:return _fail(db,run,e)

SUBSCRIPTION_FIELDS=("qib_sub","nii_sub","retail_sub","total_sub")
NSE_LIVE_SOURCE="NSE live issue API"
SUBSCRIPTION_MAX_DAY=5

def subscription_stage(open_date:str,today)->str:
    """"subscription_day_N": N = 1 + business days elapsed since the issue
    opened (capped). A Day 1 observation can never be confused with Day 3."""
    od=market.parse_date(open_date)
    if od is None:return "subscription_day_unknown"
    d=od.date();n=1
    while d<today and n<SUBSCRIPTION_MAX_DAY:
        d+=timedelta(days=1)
        if d.weekday()<5:n+=1
    return f"subscription_day_{n}"

def record_subscription_observations(db:Session,ipo:IPO,row:dict,today=None)->int:
    """Point-in-time demand observations for a live India issue (Phase 7).
    One FeatureObservation per category per day; re-observing the same day
    updates the value in place. Returns the number of rows written/updated."""
    today=today or now().date()
    iso=today.isoformat();stage=subscription_stage(row.get("open_date") or ipo.open_date or "",today)
    url=row.get("subscription_source_url") or nse.detail_url(ipo.symbol or row.get("symbol",""))
    n=0
    for f in SUBSCRIPTION_FIELDS:
        v=row.get(f)
        if v in (None,""):continue
        obs=db.scalar(select(FeatureObservation).where(FeatureObservation.ipo_id==ipo.id,FeatureObservation.field_name==f,
                                                        FeatureObservation.period_end==iso,FeatureObservation.source_name==NSE_LIVE_SOURCE))
        if obs is None:
            db.add(FeatureObservation(ipo_id=ipo.id,field_name=f,value=float(v),unit="x",source_name=NSE_LIVE_SOURCE,source_url=url,source_tier=1,
                                      source_form="NSE API",period_end=iso,available_at=iso,availability_rule="nse_live_feed",
                                      observed_at=now(),confidence=1.0,event_stage=stage,raw={"open_date":row.get("open_date") or ipo.open_date}))
            n+=1
        elif obs.value!=float(v):
            obs.value=float(v);obs.observed_at=now();obs.event_stage=stage;n+=1
    return n

def ingest_nse(db:Session):
    run=IngestionRun(source="NSE",status="running");db.add(run);db.commit();seen=changed=0;observations=0
    try:
        rows,warnings=nse.fetch_current()
        index=NameIndex(db)
        for row in rows:
            seen+=1
            if upsert_ipo(db,row,"NSE","https://www.nseindia.com/market-data/all-upcoming-issues-ipo",1,index):changed+=1
            if any(row.get(f) not in (None,"") for f in SUBSCRIPTION_FIELDS):
                ipo=resolve_existing(db,row,external_key(row),index)
                if ipo is not None:observations+=record_subscription_observations(db,ipo,row)
        db.flush()
        return _finish(db,run,seen,changed,warnings,{"subscription_observations":observations})
    except Exception as e:return _fail(db,run,e)

# ------------------------------------------------------- India identity ----
_SYMBOL_FLAG_PREFIX="symbol_resolution: "

def resolve_india_symbols(db:Session,masters:list|None=None,limit:int|None=None)->dict:
    """Phase 2: attach the exchange symbol to India rows that never had one,
    using NSE's own listed-security masters (Tier 1). Exact matches only -
    see nse_master.MasterIndex.resolve for the closed set of outcomes. Rows
    that already carry a symbol are cross-checked, never overwritten."""
    stats={k:0 for k in (nse_master.RESOLVED_ISIN,nse_master.RESOLVED_NAME_ISIN_PREFIX,nse_master.CONFLICTING,nse_master.AMBIGUOUS,nse_master.UNRESOLVED)}
    stats.update({"checked_existing":0,"existing_agree":0,"existing_conflict":0,"board_fixed":0})
    if masters is None:masters=nse_master.fetch_masters()
    index=nse_master.MasterIndex(masters)
    candidates=db.scalars(select(IPO).where(IPO.country=="India",IPO.status.in_(("Listed","Open","Closed","Upcoming"))).order_by(IPO.id)).all()
    done=0
    for ipo in candidates:
        if ipo.symbol:
            ok,note=index.check_symbol(ipo.isin,ipo.symbol)
            if ok is None:
                # ISIN unknown to the equity masters: a debt/other-instrument
                # ISIN stored on an equity IPO row (e.g. a bond series symbol
                # from a report) is repaired only on an exact issuer match.
                m,why=index.equity_for_non_equity_isin(ipo.isin,ipo.company)
                if m is not None:
                    raw=dict(ipo.raw or {});raw.setdefault("isin_as_reported",ipo.isin);raw.setdefault("symbol_as_reported",ipo.symbol);ipo.raw=raw
                    ipo.isin=m.isin;ipo.symbol=m.symbol
                    if ipo.board!=m.board:ipo.board=m.board;stats["board_fixed"]+=1
                    add_provenance(db,ipo,"symbol",m.symbol,m.source_name,m.source_url,1)
                    add_provenance(db,ipo,"isin",m.isin,m.source_name,m.source_url,1)
                    _set_flag(ipo,_SYMBOL_FLAG_PREFIX,f"{_SYMBOL_FLAG_PREFIX}RESOLVED_ISSUER_EQUITY: {why}")
                    stats["RESOLVED_ISSUER_EQUITY"]=stats.get("RESOLVED_ISSUER_EQUITY",0)+1
                continue
            stats["checked_existing"]+=1
            if ok:
                stats["existing_agree"]+=1;_set_flag(ipo,_SYMBOL_FLAG_PREFIX,None)
            else:
                stats["existing_conflict"]+=1;_set_flag(ipo,_SYMBOL_FLAG_PREFIX,f"{_SYMBOL_FLAG_PREFIX}{nse_master.CONFLICTING}: {note}")
            continue
        if limit is not None and done>=limit:break
        done+=1
        cls,m,reason=index.resolve(ipo.isin,ipo.company)
        stats[cls]+=1
        if m is None:
            _set_flag(ipo,_SYMBOL_FLAG_PREFIX,f"{_SYMBOL_FLAG_PREFIX}{cls}: {reason}")
            continue
        ipo.symbol=m.symbol
        add_provenance(db,ipo,"symbol",m.symbol,m.source_name,m.source_url,1)
        if cls==nse_master.RESOLVED_NAME_ISIN_PREFIX and m.isin and m.isin!=ipo.isin:
            raw=dict(ipo.raw or {});raw["isin_at_ipo"]=ipo.isin;ipo.raw=raw
            ipo.isin=m.isin
            add_provenance(db,ipo,"isin",m.isin,m.source_name,m.source_url,1)
        if ipo.board!=m.board:
            ipo.board=m.board;stats["board_fixed"]+=1
        _set_flag(ipo,_SYMBOL_FLAG_PREFIX,None)
        ipo.updated_at=now()
    db.commit()
    return stats

def ingest_nse_history(db:Session,max_reports=3):
    run=IngestionRun(source="NSE Primary Market Reports",status="running");db.add(run);db.commit();seen=changed=0;warnings=[];skipped_non_ipo=0
    try:
        links=nse.fetch_archive_links()[:max_reports]
    except Exception as e:
        return _fail(db,run,e)
    index=NameIndex(db)
    with httpx.Client(headers={"User-Agent":"Mozilla/5.0 IPOIntelligence/2.0"},timeout=30,follow_redirects=True) as c:
        for label,url in links:
            try:
                # url comes from an href parsed out of NSE's own archive
                # page (nse.archive_links), not a hardcoded literal.
                validate_outbound_url(url,allowed_hosts=_NSE_ALLOWED_HOSTS)
                r=c.get(url);r.raise_for_status()
                for x in nse.parse_monthly_xlsx(r.content):
                    issue_type=classify_issue_type(x.get("issue_type"))
                    if issue_type is None:
                        # Preferential allotments, QIPs, rights issues and
                        # warrant conversions share the sheet with IPOs. They
                        # are not IPOs and are not stored as such.
                        skipped_non_ipo+=1;continue
                    seen+=1
                    row={"company":x["company"],"symbol":x.get("symbol","") or "","isin":x.get("isin","") or "","country":"India",
                         "exchange":x.get("exchange") or "NSE/BSE","board":board_for_issue_type(issue_type,x.get("exchange","")),
                         "sector":x.get("sector") or None,"status":"Listed","currency":"INR","final_price":x.get("issue_price"),
                         "listing_date":x.get("listing_date","") or "","open_date":x.get("open_date","") or "","close_date":x.get("close_date","") or "",
                         "issue_size_m":x.get("issue_size_m"),"fresh_issue_pct":x.get("fresh_issue_pct"),"ofs_pct":x.get("ofs_pct"),
                         "registrar":x.get("registrar") or None,"raw":x.get("raw",{})}
                    if upsert_ipo(db,row,"NSE Primary Market Report",url,1,index):changed+=1
                    ipo=index.find(row["company"],row["isin"]) or db.scalar(select(IPO).where(IPO.external_key==external_key(row)))
                    if ipo and x.get("issue_price") and x.get("listing_price"):
                        ret=(x["listing_price"]/x["issue_price"]-1)*100
                        existing=db.scalar(select(PerformanceSnapshot).where(PerformanceSnapshot.ipo_id==ipo.id,PerformanceSnapshot.source_url==url,PerformanceSnapshot.listing_return_pct==ret))
                        if not existing: db.add(PerformanceSnapshot(ipo_id=ipo.id,as_of_date=now().date().isoformat(),close_price=x["listing_price"],listing_return_pct=ret,source_name="NSE Primary Market Report",source_url=url))
                db.commit()
            except Exception as e:
                db.rollback();warnings.append(f"{label}: {type(e).__name__}: {e}")
    return _finish(db,run,seen,changed,warnings,{"skipped_non_ipo":skipped_non_ipo})

def reconcile_lifecycle(db:Session)->dict:
    """Deterministic status repair over rows already stored, run every refresh
    (idempotent, silent once clean). Three rules, each backed by data already
    on the row or on another row from a Tier-1 source - nothing is inferred:

    1. An India row whose NSE report 'issue_type' is not an IPO (preferential,
       QIP, rights, warrants) becomes 'Not IPO'. Board/sector are corrected
       from the same report columns while we are there.
    2. An India Open/Upcoming/Closed row whose canonical issuer name matches a
       Listed row (the monthly report has no symbol, so the live feed's
       "IN:<symbol>" row and the report's "IN:<name>" row were two records of
       one listing) becomes Listed with that row's listing date/price. Neither
       row is deleted or re-keyed.
    3. An India Open/Upcoming row whose close date has passed becomes Closed
       (awaiting listing). It is still an active pipeline row; the public
       build decides separately whether it is too old to publish."""
    stats={"not_ipo":0,"listed_from_match":0,"closed":0,"board_fixed":0}
    today=now().date()
    india=db.scalars(select(IPO).where(IPO.country=="India")).all()
    listed_by_name:dict[str,IPO]={}
    for ipo in india:
        raw=ipo.raw or {}
        if ipo.status!="Not IPO" and is_report_artifact_name(ipo.company):
            # A monthly-report footnote ("*Shares issued by the Company are
            # partly paid up ...") or a bare column word ("IPO") parsed as an
            # issuer name. Not an issuer, so not an IPO row.
            ipo.status="Not IPO";_set_flag(ipo,"ipo_classification","ipo_classification: report artifact row, not an issuer");stats["not_ipo"]+=1;continue
        raw_type=raw.get("issue_type")
        if raw_type is not None and str(raw_type).strip() not in ("","None"):
            it=classify_issue_type(raw_type)
            if it is None and ipo.status!="Not IPO":
                ipo.status="Not IPO";stats["not_ipo"]+=1;continue
            if it is not None:
                board=board_for_issue_type(it,str(raw.get("exchange","")))
                if ipo.board!=board:ipo.board=board;stats["board_fixed"]+=1
                if (not ipo.sector or ipo.sector=="Unknown") and raw.get("industry") not in (None,"","None"):ipo.sector=str(raw["industry"]).strip()
                if not ipo.isin and raw.get("isin_number") not in (None,"","None"):ipo.isin=str(raw["isin_number"]).strip().upper()
        elif ipo.board=="EQ":
            ipo.board="Mainboard";stats["board_fixed"]+=1
        if ipo.status=="Listed":
            c=canonical_name(ipo.company)
            if c and (c not in listed_by_name or (ipo.listing_date and not listed_by_name[c].listing_date)):listed_by_name[c]=ipo
    for ipo in india:
        if ipo.status not in ("Open","Upcoming","Closed"):continue
        match=listed_by_name.get(canonical_name(ipo.company))
        if match is not None and match.id!=ipo.id:
            ipo.status="Listed"
            if not ipo.listing_date and match.listing_date:ipo.listing_date=match.listing_date
            if ipo.final_price is None and match.final_price is not None:ipo.final_price=match.final_price
            if not ipo.isin and match.isin:ipo.isin=match.isin
            src=db.scalar(select(Provenance).where(Provenance.ipo_id==match.id,Provenance.field_name=="listing_date").order_by(Provenance.observed_at.desc()).limit(1))
            add_provenance(db,ipo,"status","Listed","NSE Primary Market Report (matched issuer)",src.source_url if src else NSE_ARCHIVE_PAGE,1)
            if ipo.listing_date:add_provenance(db,ipo,"listing_date",ipo.listing_date,"NSE Primary Market Report (matched issuer)",src.source_url if src else NSE_ARCHIVE_PAGE,1)
            stats["listed_from_match"]+=1;continue
        if ipo.status in ("Open","Upcoming"):
            close=market.parse_date(ipo.close_date)
            if close and close.date()<today:
                ipo.status="Closed";stats["closed"]+=1
    db.commit()
    return stats

def market_refresh_candidates(db:Session,limit:int):
    """(rows with a symbol, India rows with only an ISIN) to fetch this pass.
    Listed rows that have never had a performance snapshot come first, so a
    bounded pass always extends coverage instead of re-fetching the same
    recently-updated rows forever; among equals, most recently updated first."""
    snapped=select(PerformanceSnapshot.ipo_id).distinct()
    ipos=db.scalars(select(IPO).where(IPO.status=="Listed",IPO.symbol!="").order_by(IPO.id.in_(snapped),IPO.updated_at.desc()).limit(limit)).all()
    unresolved=db.scalars(select(IPO).where(IPO.status=="Listed",IPO.symbol=="",IPO.isin!="",IPO.country=="India").order_by(IPO.id.in_(snapped),IPO.updated_at.desc()).limit(max(0,limit-len(ipos))+limit//2)).all()
    return ipos,unresolved

def refresh_market_performance(db:Session,limit=40):
    """Post-listing price series for Listed rows, delegated to
    services.performance (explicit attempt state per row, official NSE
    bhavcopy first for India, Yahoo as the Tier-3 fallback). Rows that only
    carry an ISIN are resolved by resolve_india_symbols() from NSE's masters
    in the daily full pass, so no Yahoo symbol lookup happens here any more.
    Returns the number of snapshots written, as before."""
    from . import performance
    s=get_settings()
    if not s.allow_secondary_market_data:return 0
    counts=performance.refresh_many(db,limit=limit,only_missing=False)
    return int(counts.get("ok",0))+int(counts.get("implausible",0))

def refresh_all(db:Session):
    repair_company_names(db)
    repair_dates(db)
    from . import xbrl_financials
    xbrl_financials.restore_publication_dates(db)
    reconcile_lifecycle(db)
    runs=[ingest_sec(db),ingest_sec_priced(db),ingest_nse(db)]
    if get_settings().secondary_enrichment_url:runs.append(ingest_secondary_enrichment(db))
    reconcile_lifecycle(db)
    return runs
