from __future__ import annotations
from datetime import datetime
import csv, io
import httpx

def yahoo_symbol(symbol:str,country:str):
    if not symbol:return ""
    if country.lower()=="india" and not symbol.endswith((".NS",".BO")): return symbol+".NS"
    return symbol

def fetch_yahoo_history(symbol:str,country:str,period1:int=0,period2:int|None=None):
    """Secondary fallback. Every value returned from here must be labeled Tier 3 in provenance."""
    sym=yahoo_symbol(symbol,country); period2=period2 or int(datetime.now().timestamp())
    url=f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1={period1}&period2={period2}&interval=1d&events=history"
    with httpx.Client(headers={"User-Agent":"Mozilla/5.0 IPOIntelligence/2.0"},timeout=15,follow_redirects=True) as c:
        r=c.get(url);r.raise_for_status();j=r.json();res=j.get("chart",{}).get("result") or []
        if not res:return {"url":url,"prices":[]}
        ts=res[0].get("timestamp") or []; closes=(res[0].get("indicators",{}).get("quote") or [{}])[0].get("close") or []
        return {"url":url,"prices":[{"ts":t,"close":p} for t,p in zip(ts,closes) if p is not None]}
