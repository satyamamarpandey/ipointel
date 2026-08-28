from __future__ import annotations
from datetime import datetime, timezone
import re
import httpx

BENCHMARKS = {"india": "^NSEI", "united states": "^GSPC"}

def yahoo_symbol(symbol:str,country:str):
    if not symbol:return ""
    if country.lower()=="india" and not symbol.endswith((".NS",".BO")): return symbol+".NS"
    return symbol

def fetch_yahoo_history(symbol:str,country:str,period1:int=0,period2:int|None=None,raw_symbol:bool=False):
    """Secondary fallback. Every value returned from here must be labeled Tier 3 in provenance."""
    sym=symbol if raw_symbol else yahoo_symbol(symbol,country); period2=period2 or int(datetime.now().timestamp())
    url=f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1={period1}&period2={period2}&interval=1d&events=history"
    with httpx.Client(headers={"User-Agent":"Mozilla/5.0 IPOIntelligence/2.0"},timeout=15,follow_redirects=True) as c:
        r=c.get(url);r.raise_for_status();j=r.json();res=j.get("chart",{}).get("result") or []
        if not res:return {"url":url,"prices":[]}
        ts=res[0].get("timestamp") or []; quote=(res[0].get("indicators",{}).get("quote") or [{}])[0]
        closes=quote.get("close") or []; opens=quote.get("open") or []
        bars=[{"ts":t,"open":o,"close":c} for t,o,c in zip(ts,opens,closes) if c is not None]
        return {"url":url,"prices":bars}

def fetch_benchmark_history(country:str):
    sym=BENCHMARKS.get(country.lower())
    if not sym:return None
    return fetch_yahoo_history(sym,country,raw_symbol=True)

_DATE_FORMATS=("%Y-%m-%d","%Y-%m-%d %H:%M:%S","%d-%b-%Y","%d %b %Y","%d/%m/%Y","%Y%m%d")

def parse_date(s:str):
    if not s:return None
    s=str(s).strip()
    m=re.match(r"^(\d{4}-\d{2}-\d{2})",s)
    if m:s=m.group(1)
    for fmt in _DATE_FORMATS:
        try:return datetime.strptime(s,fmt).replace(tzinfo=timezone.utc)
        except ValueError:continue
    return None

def bar_on_or_after(bars:list[dict],target_ts:float):
    for b in bars:
        if b["ts"]>=target_ts:return b
    return None

def bar_nearest_before_or_on(bars:list[dict],target_ts:float):
    best=None
    for b in bars:
        if b["ts"]<=target_ts:best=b
        else:break
    return best

def windowed_returns(bars:list[dict],listing_dt:datetime):
    """Given full daily price history and a listing date, compute listing-day return
    base plus 1/6/12-month forward returns, each anchored to the actual listing-day
    close (never to 'whatever the price is today')."""
    if not bars or listing_dt is None:return {}
    listing_ts=listing_dt.timestamp()
    listing_bar=bar_on_or_after(bars,listing_ts)
    if not listing_bar:return {}
    base=listing_bar["close"] or listing_bar["open"]
    if not base:return {}
    out={"listing_date_used":datetime.fromtimestamp(listing_bar["ts"],tz=timezone.utc).date().isoformat(),"listing_close":listing_bar["close"],"listing_open":listing_bar.get("open")}
    if listing_bar.get("open") and listing_bar.get("close"):
        out["listing_day_return_pct"]=(listing_bar["close"]/listing_bar["open"]-1)*100
    for label,days in (("return_7d_pct",7),("return_1m_pct",30),("return_30d_pct",30),("return_6m_pct",182),("return_12m_pct",365),("return_24m_pct",730)):
        target=listing_ts+days*86400
        b=bar_nearest_before_or_on(bars,target) or (bars[-1] if bars[-1]["ts"]<=target+7*86400 else None)
        if b and b["ts"]>listing_ts:
            out[label]=(b["close"]/base-1)*100
    latest=bars[-1]
    out["latest_close"]=latest["close"]
    out["latest_as_of"]=datetime.fromtimestamp(latest["ts"],tz=timezone.utc).date().isoformat()
    out["return_since_listing_pct"]=(latest["close"]/base-1)*100 if latest["close"] else None
    return out
