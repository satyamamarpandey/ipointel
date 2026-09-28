from __future__ import annotations
import re, time, xml.etree.ElementTree as ET
import httpx
from .net_safety import validate_outbound_url

BASE="https://www.sec.gov"
DATA="https://data.sec.gov"
_ALLOWED_HOSTS={"www.sec.gov","sec.gov","data.sec.gov"}

# SEC fair-access policy: identify yourself, stay under 10 requests/second,
# back off on 403/429. We never retry aggressively - three attempts with
# exponential backoff, then give up and let the caller record a warning.
_RETRY_STATUSES={403,429,500,502,503,504}
_MAX_ATTEMPTS=3
_BACKOFF_BASE_SECONDS=1.0
_MIN_INTERVAL_SECONDS=0.11  # ~9 req/s ceiling across the process
_last_request_at=0.0

NON_IPO_FLAG_PREFIX="non_ipo_registration"
# Appended to data_flags once the IPO-vs-not classification has actually run
# for a filing, so the next refresh can skip re-downloading an unchanged one.
CLASSIFIED_MARKER="ipo_classification: checked"
PERIODIC_FORMS={"10-K","10-Q","10-K/A","10-Q/A","20-F","20-F/A","40-F","40-F/A"}

def _client(ua:str):
    return httpx.Client(headers={"User-Agent":ua,"Accept-Language":"en-US,en;q=0.9","Accept-Encoding":"gzip, deflate"},timeout=httpx.Timeout(20.0,connect=10.0),follow_redirects=True)

def _get(c:httpx.Client,url:str,sleep=time.sleep)->httpx.Response:
    """GET with SEC-polite pacing and bounded exponential backoff. Raises the
    last httpx error if every attempt fails; never loops indefinitely."""
    global _last_request_at
    last_exc:Exception|None=None
    for attempt in range(_MAX_ATTEMPTS):
        wait=_MIN_INTERVAL_SECONDS-(time.monotonic()-_last_request_at)
        if wait>0:sleep(wait)
        try:
            _last_request_at=time.monotonic()
            r=c.get(url)
            if r.status_code in _RETRY_STATUSES and attempt<_MAX_ATTEMPTS-1:
                sleep(_BACKOFF_BASE_SECONDS*(2**attempt));last_exc=httpx.HTTPStatusError(f"HTTP {r.status_code}",request=r.request,response=r);continue
            r.raise_for_status();return r
        except (httpx.TimeoutException,httpx.TransportError) as e:
            last_exc=e
            if attempt<_MAX_ATTEMPTS-1:sleep(_BACKOFF_BASE_SECONDS*(2**attempt))
    assert last_exc is not None
    raise last_exc

def parse_atom(xml_text:str, form:str):
    root=ET.fromstring(xml_text); ns={"a":"http://www.w3.org/2005/Atom"}; out=[]
    for entry in root.findall("a:entry",ns):
        title=(entry.findtext("a:title",default="",namespaces=ns) or "").strip()
        updated=(entry.findtext("a:updated",default="",namespaces=ns) or "")[:10]
        link=entry.find("a:link",ns); url=link.attrib.get("href","") if link is not None else ""
        # EDGAR titles look like "S-1 - ACME, INC. (0001234567) (Filer)", and
        # for amendments "S-1/A - ACME, INC. (...)". The separator is the
        # hyphen AFTER the form name - but the form name contains one of its
        # own ("S-1"), so the previous `^[^-]+-` stopped at that inner hyphen
        # and left the form's numeric suffix glued to the company: every S-1
        # became "1 - ACME, INC." and every S-11 "11 - ...". Anchor on the
        # actual form instead, with its optional /A amendment suffix.
        m=re.search(rf"^{re.escape(form)}(?:/[A-Z]+)?\s*-\s*(.*?)\s*\((\d{{7,10}})\)",title)
        company=m.group(1).strip() if m else re.sub(rf"^{re.escape(form)}(?:/[A-Z]+)?","",title).strip(" -")
        cik=m.group(2).lstrip("0") if m else ""
        if company: out.append({"company":company,"cik":cik,"filing_date":updated,"filing_url":url,"form":form})
    return out

def fetch_recent_ipos(user_agent:str, count=100):
    rows=[]; warnings=[]; seen=set()
    with _client(user_agent) as c:
        for form in ("S-1","F-1"):
            url=f"{BASE}/cgi-bin/browse-edgar?action=getcurrent&type={form}&company=&dateb=&owner=include&start=0&count={count}&output=atom"
            try:
                r=_get(c,url)
                for row in parse_atom(r.text,form):
                    key=(row["company"].lower(),row["cik"])
                    if key not in seen: seen.add(key); rows.append(row)
            except Exception as e: warnings.append(f"SEC {form}: {type(e).__name__}: {e}")
    return rows,warnings

def parse_price_range(text:str):
    patterns=[
      r"initial public offering price(?: is| will be)? expected to be between\s*\$([0-9.]+)\s+and\s+\$([0-9.]+)",
      r"price to the public\s*\$?([0-9.]+)",
      r"offering price between\s*\$([0-9.]+)\s+and\s+\$([0-9.]+)",
    ]
    low=high=None
    flat=re.sub(r"\s+"," ",text)
    for p in patterns:
        m=re.search(p,flat,re.I)
        if m:
            low=float(m.group(1)); high=float(m.group(2)) if m.lastindex and m.lastindex>1 else low; break
    return low,high

def filing_text(url:str,user_agent:str):
    # url comes from an href parsed out of SEC's own atom-feed content
    # (parse_atom above), not a hardcoded literal - validate before fetching.
    validate_outbound_url(url,allowed_hosts=_ALLOWED_HOSTS)
    with _client(user_agent) as c:
        r=_get(c,url); return re.sub(r"<[^>]+>"," ",r.text)

def fetch_companyfacts(cik:str,user_agent:str):
    with _client(user_agent) as c:
        r=_get(c,f"{DATA}/api/xbrl/companyfacts/CIK{str(cik).zfill(10)}.json"); return r.json()

def latest_fact(facts:dict, concepts:list[str], taxonomies=("us-gaap","ifrs-full")):
    candidates=[]
    for tax in taxonomies:
        for concept in concepts:
            node=facts.get("facts",{}).get(tax,{}).get(concept,{})
            for _unit,vals in node.get("units",{}).items():
                for x in vals:
                    if x.get("val") is not None:
                        candidates.append(x)
    if not candidates:return None
    candidates.sort(key=lambda x:(x.get("filed",""),x.get("end","")),reverse=True)
    try:return float(candidates[0]["val"])/1_000_000
    except (TypeError,ValueError,KeyError):return None

def is_ipo_registration_text(text:str)->bool:
    """An S-1/F-1 for an initial public offering says so, always. A resale
    registration by an already-public company, a follow-on or a shelf does
    not describe itself as an initial public offering."""
    return "initial public offering" in text.lower()

def already_reporting(facts:dict)->bool:
    """True when the XBRL company-facts history contains a periodic report
    (10-K/10-Q/20-F/40-F): the registrant already has public securities and
    this S-1 is not its IPO. A genuine pre-IPO filer has no such history."""
    for tax in facts.get("facts",{}).values():
        for concept in tax.values():
            for vals in concept.get("units",{}).values():
                for x in vals:
                    if x.get("form") in PERIODIC_FORMS:return True
    return False

def enrich(row:dict,user_agent:str):
    out=dict(row); flags=[]
    if row.get("filing_url"):
        try:
            txt=filing_text(row["filing_url"],user_agent)
            lo,hi=parse_price_range(txt); out["price_low"]=lo; out["price_high"]=hi
            lowtxt=txt.lower()
            out["dual_class"] = ("dual class" in lowtxt or "dual-class" in lowtxt) if "class" in lowtxt else None
            m=re.search(r"lock-up[^.]{0,120}?([0-9]{2,3})\s+days",lowtxt,re.I); out["lockup_days"]=int(m.group(1)) if m else None
            if len(lowtxt)>2000:
                flags.append(CLASSIFIED_MARKER)
                if not is_ipo_registration_text(lowtxt):
                    flags.append(f"{NON_IPO_FLAG_PREFIX}: filing never describes an initial public offering")
        except Exception as e: flags.append(f"SEC filing enrichment failed: {type(e).__name__}")
    if row.get("cik"):
        try:
            f=fetch_companyfacts(row["cik"],user_agent)
            out["revenue_m"]=latest_fact(f,["RevenueFromContractWithCustomerExcludingAssessedTax","Revenues","SalesRevenueNet"])
            out["net_income_m"]=latest_fact(f,["NetIncomeLoss","ProfitLoss"])
            out["cfo_m"]=latest_fact(f,["NetCashProvidedByUsedInOperatingActivities"])
            out["cash_m"]=latest_fact(f,["CashAndCashEquivalentsAtCarryingValue","CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"])
            out["debt_m"]=latest_fact(f,["LongTermDebtCurrent","LongTermDebtNoncurrent","LongTermDebt"])
            out["post_issue_shares_m"]=latest_fact(f,["EntityCommonStockSharesOutstanding"],taxonomies=("dei",))
            if already_reporting(f):
                flags.append(f"{NON_IPO_FLAG_PREFIX}: registrant already files periodic reports (10-K/10-Q/20-F)")
        except httpx.HTTPStatusError as e:
            # 404 from companyfacts is the normal case for a first-time filer
            # (no XBRL history yet) - not an enrichment failure.
            if e.response is None or e.response.status_code!=404:flags.append(f"SEC XBRL enrichment failed: {type(e).__name__}")
        except Exception as e: flags.append(f"SEC XBRL enrichment failed: {type(e).__name__}")
    out["data_flags"]=flags; return out

def parse_master_index_forms(text:str,forms:tuple[str,...]=("424B4","RW")):
    """Rows of the EDGAR daily master index grouped by form type. Only the
    requested forms are returned (default: priced prospectuses and
    registration withdrawal requests)."""
    out={f:[] for f in forms}
    for line in text.splitlines():
        if "|" not in line:continue
        parts=line.split("|")
        if len(parts)<5:continue
        cik,name,form,date,filename=parts[:5]
        form=form.strip()
        if form in out:out[form].append({"cik":cik.strip(),"company":name.strip(),"filing_date":date.strip(),"filename":filename.strip(),"form":form,"filing_url":"https://www.sec.gov/Archives/"+filename.strip().lstrip("/")})
    return out

def parse_master_index(text:str):
    return parse_master_index_forms(text,("424B4",))["424B4"]

def master_index_for_date(day,user_agent:str):
    q=(day.month-1)//3+1
    url=f"https://www.sec.gov/Archives/edgar/daily-index/{day.year}/QTR{q}/master.{day.strftime('%Y%m%d')}.idx"
    with _client(user_agent) as c:
        r=_get(c,url);return parse_master_index_forms(r.text),url

def parse_priced_ipo(text:str):
    flat=re.sub(r"\s+"," ",text)
    lowtxt=flat.lower()
    if "initial public offering" not in lowtxt:return None
    lo,hi=parse_price_range(flat)
    # A 424B4 often states the exact public offering price more clearly than an S-1 range.
    exact=None
    for p in [r"initial public offering price[^$]{0,100}\$([0-9.]+)",r"public offering price[^$]{0,80}\$([0-9.]+)\s+per share"]:
        m=re.search(p,flat,re.I)
        if m: exact=float(m.group(1));break
    sym=""
    for p in [r"under the symbol [\"“']?([A-Z]{1,6})",r"trading symbol\s*[:\-]?\s*([A-Z]{1,6})"]:
        m=re.search(p,flat,re.I)
        if m:sym=m.group(1).upper();break
    return {"symbol":sym,"final_price":exact or hi or lo,"price_low":lo,"price_high":hi}
