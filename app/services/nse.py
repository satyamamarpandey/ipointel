from __future__ import annotations
import re
from io import BytesIO
import httpx
from bs4 import BeautifulSoup
from openpyxl import load_workbook

BASE="https://www.nseindia.com"
ARCHIVE_PAGE=f"{BASE}/static/regulations/segment-wise-historical-reports-capital-primary-market"

def _num(v):
    if v in (None,"","-","--"):return None
    if isinstance(v,(int,float)):return float(v)
    m=re.search(r"-?\d+(?:\.\d+)?",str(v).replace(",","").replace("₹",""));return float(m.group()) if m else None

def _band(v)->tuple[float|None,float|None]:
    """NSE prints the price as "Rs.78 to Rs.82" (book-built band) or "Rs.95"
    (fixed price). Returns (low, high); a fixed price gives low == high."""
    if v in (None,"","-","--"):return None,None
    if isinstance(v,(int,float)):return float(v),float(v)
    nums=[float(x) for x in re.findall(r"\d+(?:\.\d+)?",str(v).replace(",",""))]
    if not nums:return None,None
    return min(nums[:2]),max(nums[:2])

def _board(*hints)->str:
    joined=" ".join(str(h or "") for h in hints).lower()
    return "SME" if ("sme" in joined or "emerge" in joined) else "Mainboard"

def extract_list(payload):
    if isinstance(payload,list):return [x for x in payload if isinstance(x,dict)]
    if isinstance(payload,dict):
        for k in ("data","records","issues","result","results"):
            if isinstance(payload.get(k),list):return payload[k]
        out=[]
        for v in payload.values():
            if isinstance(v,list) and (not v or isinstance(v[0],dict)):out.extend(v)
        return out
    return []

def normalize(d:dict,status="Upcoming"):
    company=str(d.get("companyName") or d.get("company") or d.get("issuerName") or d.get("name") or d.get("symbol") or "Unknown IPO")
    symbol=str(d.get("symbol") or d.get("issueSymbol") or "")
    offered=_num(d.get("noOfSharesOffered") or d.get("sharesOffered") or d.get("issueSize"))
    bid=_num(d.get("noOfsharesBid") or d.get("noOfSharesBid") or d.get("sharesBid"))
    total=_num(d.get("noOfTime") or d.get("subscription") or d.get("totalSubscription"))
    if total is None and offered and bid: total=bid/offered
    def anynum(*keys):
        for k in keys:
            if d.get(k) not in (None, "", "-", "--"):
                return _num(d.get(k))
        return None
    band=_band(d.get("issuePrice"))
    return {
      "company":company,"symbol":symbol,"isin":str(d.get("isin") or ""),"country":"India","exchange":"NSE/BSE",
      # NSE's live feed says "EQ" for a mainboard equity issue and "SME" for
      # an Emerge issue - "EQ" is a series code, not a board name.
      "board":_board(d.get("issueType"),d.get("series"),d.get("board"),d.get("category")),
      "status":status,"sector":str(d.get("industry") or d.get("sector") or "Unknown"),"currency":"INR",
      "price_low":_num(d.get("issuePriceMin") or d.get("priceBandMin") or d.get("minPrice") or d.get("floorPrice")) or band[0],
      "price_high":_num(d.get("issuePriceMax") or d.get("priceBandMax") or d.get("maxPrice") or d.get("capPrice")) or band[1],
      # A single printed price is a fixed-price issue: that IS the offer price.
      **({"final_price":band[0]} if band[0] is not None and band[0]==band[1] and not any(d.get(k) for k in ("issuePriceMin","priceBandMin","minPrice","floorPrice","issuePriceMax","priceBandMax","maxPrice","capPrice")) else {}),
      "lot_size":int(_num(d.get("marketLot") or d.get("lotSize") or d.get("minimumBidQuantity")) or 0) or None,
      "total_sub":total,"qib_sub":anynum("qibSubscription","qib","qibSub","qibNoOfTime"),"nii_sub":anynum("niiSubscription","hniSubscription","nii","niiSub","niiNoOfTime"),"retail_sub":anynum("retailSubscription","retail","retailSub","retailNoOfTime"),"open_date":str(d.get("issueStartDate") or d.get("openDate") or ""),"close_date":str(d.get("issueEndDate") or d.get("closeDate") or ""),
      "shares_offered_m":offered/1_000_000 if offered and offered>100_000 else offered,"raw":d
    }

def detail_url(symbol:str,series:str="EQ")->str:
    return f"{BASE}/api/ipo-detail?symbol={symbol}&series={series or 'EQ'}"

# Category labels as NSE's per-issue bid table prints them. Sub-rows such as
# "1(a) Foreign Institutional Investors" roll up into the parent and are not
# separate model inputs.
_CATEGORY_FIELDS=(("qualified institutional","qib_sub"),("non institutional","nii_sub"),("non-institutional","nii_sub"),("retail","retail_sub"))

def parse_bid_details(payload)->dict:
    """{qib_sub, nii_sub, retail_sub} (times subscribed) from NSE's
    /api/ipo-detail response. Only top-level categories are read (srNo
    without a letter suffix); missing or blank values stay absent."""
    out={}
    if not isinstance(payload,dict):return out
    for row in payload.get("bidDetails") or []:
        if not isinstance(row,dict):continue
        sr=str(row.get("srNo") or "")
        if "(" in sr:continue  # sub-category (FIIs, mutual funds, ...)
        cat=str(row.get("category") or "").lower()
        times=_num(row.get("noOfTime"))
        if times is None:continue
        for needle,field in _CATEGORY_FIELDS:
            if needle in cat and field not in out:out[field]=times;break
    return out

def fetch_current():
    headers={"User-Agent":"Mozilla/5.0 IPOIntelligence/2.0","Accept":"application/json,text/plain,*/*","Referer":f"{BASE}/market-data/all-upcoming-issues-ipo"}
    rows=[]; warnings=[]; seen=set()
    with httpx.Client(headers=headers,timeout=20,follow_redirects=True) as c:
        try:c.get(BASE)
        except Exception:pass
        for label,url,status in [("current",f"{BASE}/api/ipo-current-issue","Open"),("upcoming",f"{BASE}/api/all-upcoming-issues?category=ipo","Upcoming")]:
            try:
                r=c.get(url);r.raise_for_status()
                for d in extract_list(r.json()):
                    x=normalize(d,status);key=(x["company"].lower(),x["symbol"].lower())
                    if key in seen:continue
                    seen.add(key)
                    if status=="Open" and x["symbol"]:
                        # Per-category demand (QIB / NII / Retail) lives on the
                        # issue's own endpoint. Observed live, at event time -
                        # the only defensible source for it.
                        durl=detail_url(x["symbol"],str(d.get("series") or "EQ"))
                        try:
                            dr=c.get(durl);dr.raise_for_status()
                            cats=parse_bid_details(dr.json())
                            if cats:x={**x,**cats,"subscription_source_url":durl}
                        except Exception as e:warnings.append(f"NSE detail {x['symbol']}: {type(e).__name__}: {e}")
                    rows.append(x)
            except Exception as e:warnings.append(f"NSE {label}: {type(e).__name__}: {e}")
    return rows,warnings

PAST_ISSUES_URL=f"{BASE}/api/public-past-issues"
PAST_ISSUE_EQUITY_TYPES={"EQ","BE","SME","SM","ST"}

def normalize_past_issue(d:dict)->dict|None:
    """One row of NSE's past-issues list (official): final issue price and
    listing date for an equity issue. None for debt and other instruments."""
    from .identity import normalize_date
    sym=str(d.get("symbol") or "").strip().upper()
    stype=str(d.get("securityType") or "").strip().upper()
    if not sym or stype not in PAST_ISSUE_EQUITY_TYPES:return None
    lo,hi=_band(d.get("priceRange"))
    return {"symbol":sym,"company":str(d.get("company") or d.get("companyName") or "").strip(),
            "close_date":normalize_date(d.get("ipoEndDate")),"open_date":normalize_date(d.get("ipoStartDate")),
            "listing_date":normalize_date(d.get("listingDate")),"final_price":_num(d.get("issuePrice")),
            "price_low":lo,"price_high":hi,"security_type":stype}

def fetch_past_issues()->list[dict]:
    headers={"User-Agent":"Mozilla/5.0 IPOIntelligence/2.0","Accept":"application/json,text/plain,*/*","Referer":f"{BASE}/market-data/all-upcoming-issues-ipo"}
    with httpx.Client(headers=headers,timeout=30,follow_redirects=True) as c:
        try:c.get(BASE)
        except Exception:pass
        r=c.get(PAST_ISSUES_URL);r.raise_for_status()
        data=r.json()
    return [x for x in (normalize_past_issue(d) for d in (data if isinstance(data,list) else extract_list(data))) if x]

def archive_links(html:str):
    soup=BeautifulSoup(html,"html.parser"); out=[]
    for a in soup.find_all("a",href=True):
        label=" ".join(a.stripped_strings)
        if "Primary Market Monthly Report" in label and ".xlsx" in (a.get("href") or ""):
            href=a["href"]
            if href.startswith("//"):href="https:"+href
            elif href.startswith("/"):href=BASE+href
            out.append((label,href))
    return out

def fetch_archive_links():
    with httpx.Client(headers={"User-Agent":"Mozilla/5.0 IPOIntelligence/2.0"},timeout=20,follow_redirects=True) as c:
        r=c.get(ARCHIVE_PAGE);r.raise_for_status();return archive_links(r.text)

def _clean_header(v)->str:
    return " ".join(str(v).replace("_"," ").split()).lower() if v is not None else ""

def _text(v):
    if v in (None,"","NA","None","-"):return ""
    return " ".join(str(v).split())

def _pct(part,total):
    if part is None or not total:return None
    return max(0.0,min(100.0,part/total*100))

def parse_monthly_xlsx(content:bytes):
    """Rows of NSE's Primary Market Monthly Report, one per issue. The sheet
    mixes IPOs with preferential allotments, QIPs, rights issues and warrant
    conversions, so every row carries the sheet's own 'issue_type' text and
    the caller decides (identity.classify_issue_type) what counts as an IPO.

    Everything returned here was known before listing (issue size, fresh vs
    OFS split, open/close dates, sector, ISIN, price) - genuine pre-IPO
    features for the historical model - except listing_price, which the sheet
    does not carry in the formats seen so far (returns come from market data)."""
    wb=load_workbook(BytesIO(content),data_only=True,read_only=True); rows=[]
    for ws in wb.worksheets:
        raw=list(ws.iter_rows(values_only=True))
        if not raw:continue
        header_idx=None; headers=[]
        for i,row in enumerate(raw[:25]):
            vals=[_clean_header(x) for x in row]
            joined=" | ".join(vals)
            if any(k in joined for k in ("company","issuer","issue name")) and any(k in joined for k in ("price","listing","issue")):
                header_idx=i;headers=vals;break
        if header_idx is None:continue
        for row in raw[header_idx+1:]:
            if not any(x not in (None,"") for x in row):continue
            d={headers[i]:row[i] for i in range(min(len(headers),len(row))) if headers[i]}
            company=next((str(v) for k,v in d.items() if v and any(t in k for t in ("company","issuer","issue name"))),"")
            if not company:continue
            def pick(*terms,_row=d,exclude=()):
                for k,v in _row.items():
                    if all(t in k for t in terms) and not any(x in k for x in exclude):return v
                return None
            symbol=_text(next((v for k,v in d.items() if v and "symbol" in k),""))
            listing_date=_text(next((v for k,v in d.items() if v and "listing" in k and "date" in k),""))
            size_crores=_num(pick("issue size","crore"))
            total_shares=_num(pick("total issue size"))
            fresh_shares=_num(pick("fresh issue"))
            ofs_shares=_num(pick("offer for sale"))
            rows.append({
                "company":company,"symbol":symbol,"listing_date":listing_date,
                "issue_price":_num(pick("issue price")),
                "listing_price":_num(pick("listing","price")),
                # crores of INR -> millions of INR (1 crore = 10 million)
                "issue_size_m":size_crores*10 if size_crores is not None else None,
                "fresh_issue_pct":_pct(fresh_shares,total_shares),
                "ofs_pct":_pct(ofs_shares,total_shares),
                "open_date":_text(pick("issue open")),"close_date":_text(pick("issue close")),
                "isin":_text(pick("isin",exclude=("descriptor",))).upper(),
                "issue_type":_text(pick("issue type")),
                "sector":_text(pick("industry")) or None,
                "exchange":_text(pick("exchange")) or "NSE/BSE",
                "registrar":_text(pick("registrar")) or None,
                "sheet":ws.title,"raw":{str(k):str(v) for k,v in d.items()},
            })
    return rows
