from __future__ import annotations
from datetime import datetime, timezone
import re
import httpx

BENCHMARKS = {"india": "^NSEI", "united states": "^GSPC"}
_UA={"User-Agent":"Mozilla/5.0 IPOIntelligence/2.0"}

def yahoo_symbol(symbol:str,country:str):
    if not symbol:return ""
    if country.lower()=="india" and not symbol.endswith((".NS",".BO")): return symbol+".NS"
    return symbol

def fetch_yahoo_history(symbol:str,country:str,period1:int=0,period2:int|None=None,raw_symbol:bool=False):
    """Secondary fallback. Every value returned from here must be labeled Tier 3 in provenance."""
    sym=symbol if raw_symbol else yahoo_symbol(symbol,country); period2=period2 or int(datetime.now().timestamp())
    url=f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1={period1}&period2={period2}&interval=1d&events=history"
    with httpx.Client(headers=_UA,timeout=15,follow_redirects=True) as c:
        r=c.get(url);r.raise_for_status();j=r.json();res=j.get("chart",{}).get("result") or []
        if not res:return {"url":url,"prices":[]}
        ts=res[0].get("timestamp") or []; quote=(res[0].get("indicators",{}).get("quote") or [{}])[0]
        closes=quote.get("close") or []; opens=quote.get("open") or []
        bars=[{"ts":t,"open":o,"close":c} for t,o,c in zip(ts,opens,closes,strict=False) if c is not None]
        return {"url":url,"prices":bars}

def resolve_symbol_by_isin(isin:str,country:str)->tuple[str,str]:
    """ISIN -> exchange ticker via Yahoo's search endpoint (Tier 3). Returns
    (symbol, url) or ("", url). For India only an NSE (".NS") listing is
    accepted; a BSE-only match is left unresolved rather than guessed."""
    url=f"https://query2.finance.yahoo.com/v1/finance/search?q={isin}&quotesCount=5&newsCount=0"
    if not isin:return "",url
    with httpx.Client(headers=_UA,timeout=15,follow_redirects=True) as c:
        r=c.get(url);r.raise_for_status();quotes=r.json().get("quotes") or []
    want=".NS" if country.lower()=="india" else None
    for q in quotes:
        sym=str(q.get("symbol") or "")
        if not sym or q.get("quoteType") not in (None,"EQUITY"):continue
        if want and not sym.endswith(want):continue
        return sym,url
    return "",url

def fetch_benchmark_history(country:str):
    sym=BENCHMARKS.get(country.lower())
    if not sym:return None
    return fetch_yahoo_history(sym,country,raw_symbol=True)

_DATE_FORMATS=("%Y-%m-%d","%Y-%m-%d %H:%M:%S","%d-%b-%Y","%d %b %Y","%d/%m/%Y","%Y%m%d","%d-%m-%Y","%d-%b-%y")

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

def windowed_returns(bars:list[dict],listing_dt:datetime,issue_price:float|None=None):
    """Given full daily price history and a listing date, compute the listing
    return plus 7d/1m/6m/12m/24m forward returns.

    Base price: the issue (offer) price when it is known - that is what an
    IPO investor actually paid, and it is the definition used everywhere the
    product says "listing return". Only when no issue price exists do the
    forward windows fall back to the listing-day close, and listing_return_pct
    is then left absent rather than substituted with an intraday figure.
    Nothing here ever anchors to "whatever the price is today"."""
    if not bars or listing_dt is None:return {}
    listing_ts=listing_dt.timestamp()
    listing_bar=bar_on_or_after(bars,listing_ts)
    if not listing_bar:return {}
    # A first bar more than 10 trading days after the stated listing date is
    # not the listing session (symbol reassigned, or history starts late).
    if listing_bar["ts"]-listing_ts>14*86400:return {}
    listing_close=listing_bar["close"] or None
    listing_open=listing_bar.get("open") or None
    out={"listing_date_used":datetime.fromtimestamp(listing_bar["ts"],tz=timezone.utc).date().isoformat(),"listing_close":listing_close,"listing_open":listing_open,"base":"issue_price" if issue_price else "listing_close"}
    base=issue_price if issue_price else listing_close
    if not base:return {}
    if issue_price and listing_close:
        out["listing_return_pct"]=(listing_close/issue_price-1)*100
    if issue_price and listing_open:
        out["listing_open_return_pct"]=(listing_open/issue_price-1)*100
    if listing_open and listing_close:
        out["listing_day_return_pct"]=(listing_close/listing_open-1)*100
    for label,days in (("return_7d_pct",7),("return_1m_pct",30),("return_30d_pct",30),("return_6m_pct",182),("return_12m_pct",365),("return_24m_pct",730)):
        target=listing_ts+days*86400
        if target>bars[-1]["ts"]+7*86400:
            continue  # window has not elapsed yet - absent, never zero, never "latest"
        b=bar_nearest_before_or_on(bars,target)
        if b and b["ts"]>listing_ts:
            out[label]=(b["close"]/base-1)*100
    latest=bars[-1]
    out["latest_close"]=latest["close"]
    out["latest_as_of"]=datetime.fromtimestamp(latest["ts"],tz=timezone.utc).date().isoformat()
    out["return_since_listing_pct"]=(latest["close"]/base-1)*100 if latest["close"] else None
    return out
