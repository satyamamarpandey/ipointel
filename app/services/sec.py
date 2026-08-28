from __future__ import annotations
import re, xml.etree.ElementTree as ET
from datetime import datetime, timezone
import httpx

BASE="https://www.sec.gov"
DATA="https://data.sec.gov"

def _client(ua:str):
    return httpx.Client(headers={"User-Agent":ua,"Accept-Language":"en-US,en;q=0.9"},timeout=20,follow_redirects=True)

def parse_atom(xml_text:str, form:str):
    root=ET.fromstring(xml_text); ns={"a":"http://www.w3.org/2005/Atom"}; out=[]
    for entry in root.findall("a:entry",ns):
        title=(entry.findtext("a:title",default="",namespaces=ns) or "").strip()
        updated=(entry.findtext("a:updated",default="",namespaces=ns) or "")[:10]
        link=entry.find("a:link",ns); url=link.attrib.get("href","") if link is not None else ""
        m=re.search(r"^[^-]+-\s*(.*?)\s*\((\d{7,10})\)",title)
        company=m.group(1).strip() if m else title.replace(form,"").strip(" -")
        cik=m.group(2).lstrip("0") if m else ""
        if company: out.append({"company":company,"cik":cik,"filing_date":updated,"filing_url":url,"form":form})
    return out

def fetch_recent_ipos(user_agent:str, count=100):
    rows=[]; warnings=[]; seen=set()
    with _client(user_agent) as c:
        for form in ("S-1","F-1"):
            url=f"{BASE}/cgi-bin/browse-edgar?action=getcurrent&type={form}&company=&dateb=&owner=include&start=0&count={count}&output=atom"
            try:
                r=c.get(url); r.raise_for_status()
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
    with _client(user_agent) as c:
        r=c.get(url); r.raise_for_status(); return re.sub(r"<[^>]+>"," ",r.text)

def fetch_companyfacts(cik:str,user_agent:str):
    with _client(user_agent) as c:
        r=c.get(f"{DATA}/api/xbrl/companyfacts/CIK{str(cik).zfill(10)}.json"); r.raise_for_status(); return r.json()

def latest_fact(facts:dict, concepts:list[str], taxonomies=("us-gaap","ifrs-full")):
    candidates=[]
    for tax in taxonomies:
        for concept in concepts:
            node=facts.get("facts",{}).get(tax,{}).get(concept,{})
            for unit,vals in node.get("units",{}).items():
                for x in vals:
                    if x.get("val") is not None:
                        candidates.append(x)
    if not candidates:return None
    candidates.sort(key=lambda x:(x.get("filed",""),x.get("end","")),reverse=True)
    try:return float(candidates[0]["val"])/1_000_000
    except:return None

def enrich(row:dict,user_agent:str):
    out=dict(row); flags=[]
    if row.get("filing_url"):
        try:
            txt=filing_text(row["filing_url"],user_agent)
            lo,hi=parse_price_range(txt); out["price_low"]=lo; out["price_high"]=hi
            lowtxt=txt.lower()
            out["dual_class"] = ("dual class" in lowtxt or "dual-class" in lowtxt) if "class" in lowtxt else None
            m=re.search(r"lock-up[^.]{0,120}?([0-9]{2,3})\s+days",lowtxt,re.I); out["lockup_days"]=int(m.group(1)) if m else None
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
        except Exception as e: flags.append(f"SEC XBRL enrichment failed: {type(e).__name__}")
    out["data_flags"]=flags; return out

def parse_master_index(text:str):
    out=[]
    for line in text.splitlines():
        if "|" not in line:continue
        parts=line.split("|")
        if len(parts)<5:continue
        cik,name,form,date,filename=parts[:5]
        if form.strip()=="424B4":out.append({"cik":cik.strip(),"company":name.strip(),"filing_date":date.strip(),"filename":filename.strip(),"filing_url":"https://www.sec.gov/Archives/"+filename.strip().lstrip("/")})
    return out

def master_index_for_date(day,user_agent:str):
    q=(day.month-1)//3+1
    url=f"https://www.sec.gov/Archives/edgar/daily-index/{day.year}/QTR{q}/master.{day.strftime('%Y%m%d')}.idx"
    with _client(user_agent) as c:
        r=c.get(url);r.raise_for_status();return parse_master_index(r.text),url

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
