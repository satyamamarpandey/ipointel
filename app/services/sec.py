from __future__ import annotations
import html, re, time, xml.etree.ElementTree as ET
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

# A dollar amount as filings print it: "18.00", "1,250.50", "4". The old
# "[0-9.]+" also swallowed the sentence's full stop ("$1.00." -> "1.00."),
# which made float() raise and flagged the whole source run as partial.
_MONEY=r"[0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?"

def _money(raw:str):
    try: return float(raw.replace(",",""))
    except (TypeError,ValueError): return None

# A per-share IPO price outside this band is a table total ("$13,050,000") or
# a par value ("$0.0001"), never an offering price.
PRICE_MIN,PRICE_MAX=0.5,500.0

def _price(raw:str):
    v=_money(raw)
    return v if v is not None and PRICE_MIN<=v<=PRICE_MAX else None

def _price_match(pattern:str,scope:str):
    """First match whose captured amount is a plausible per-share price and
    whose gap text is not a par-value clause ("... par value $0.0001")."""
    for m in re.finditer(pattern,scope,re.I):
        if "par value" in scope[max(0,m.start()):m.start(1)].lower():continue
        v=_price(m.group(1))
        if v is not None:return v
    return None

def parse_price_range(text:str):
    patterns=[
      rf"initial public offering price(?: is| will be)? expected to be between\s*\$\s*({_MONEY})\s+and\s+\$\s*({_MONEY})",
      rf"price to (?:the )?public\s*(?:per (?:share|unit|ads)\s*)?\$?\s*({_MONEY})",
      rf"offering price between\s*\$({_MONEY})\s+and\s+\$({_MONEY})",
    ]
    low=high=None
    flat=flatten_filing_text(text)
    for p in patterns:
        m=re.search(p,flat,re.I)
        if m:
            low=_money(m.group(1)); high=_money(m.group(2)) if m.lastindex and m.lastindex>1 else low
            if low is not None: break
    return low,high

def filing_text(url:str,user_agent:str):
    # url comes from an href parsed out of SEC's own atom-feed content
    # (parse_atom above), not a hardcoded literal - validate before fetching.
    validate_outbound_url(url,allowed_hosts=_ALLOWED_HOSTS)
    with _client(user_agent) as c:
        r=_get(c,url); return re.sub(r"<[^>]+>"," ",r.text)

def _decode(raw:bytes)->str:
    """EDGAR submissions are UTF-8 or Windows-1252 (smart quotes around ticker
    symbols). Try strict UTF-8 first; a truncated multi-byte tail or cp1252
    bytes fall back to cp1252, which never raises."""
    try:return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:return raw[:-3].decode("utf-8")
        except UnicodeDecodeError:return raw.decode("cp1252","replace")

# Every prospectus cover page ends with the Item 501 price table ("Price to
# public ... Underwriting discounts"). Once that has been read, the cover
# statements (IPO or not, symbol, price) are in hand and downloading stops.
_COVER_DONE = re.compile(r"price to (?:the )?public|underwriting discounts? and commissions|proceeds,? before expenses,? to", re.I)
_CHECK_EVERY = 96 * 1024


def _stream_head(c:httpx.Client,url:str,max_bytes:int,sleep=time.sleep)->tuple[str,bool]:
    """Like _get, but reads at most max_bytes of the body (stopping earlier
    once the prospectus cover page is in hand) and closes the connection.
    Returns (text, truncated): truncated is True when the cap was hit before
    the body ended, i.e. the cover page may lie beyond what was read."""
    global _last_request_at
    last_exc:Exception|None=None
    for attempt in range(_MAX_ATTEMPTS):
        wait=_MIN_INTERVAL_SECONDS-(time.monotonic()-_last_request_at)
        if wait>0:sleep(wait)
        try:
            _last_request_at=time.monotonic()
            with c.stream("GET",url) as r:
                if r.status_code in _RETRY_STATUSES and attempt<_MAX_ATTEMPTS-1:
                    sleep(_BACKOFF_BASE_SECONDS*(2**attempt));last_exc=httpx.HTTPStatusError(f"HTTP {r.status_code}",request=r.request,response=r);continue
                r.raise_for_status()
                buf=bytearray();next_check=_CHECK_EVERY;truncated=False
                for chunk in r.iter_bytes():
                    buf.extend(chunk)
                    if len(buf)>=max_bytes:truncated=True;break
                    if len(buf)>=next_check:
                        next_check+=_CHECK_EVERY
                        if _COVER_DONE.search(re.sub(r"<[^>]+>"," ",_decode(bytes(buf)))):break  # cover page read: not a truncation
                return _decode(bytes(buf[:max_bytes])),truncated
        except (httpx.TimeoutException,httpx.TransportError) as e:
            last_exc=e
            if attempt<_MAX_ATTEMPTS-1:sleep(_BACKOFF_BASE_SECONDS*(2**attempt))
    assert last_exc is not None
    raise last_exc

def filing_head(url:str,user_agent:str,max_bytes:int=1_500_000)->tuple[str,bool]:
    """(tag-stripped head of a filing, truncated?). Used by the historical
    424B4 backfill/repair where downloading thousands of full submissions
    would be wasteful for both sides; the cover page carries the price,
    symbol and the IPO statement."""
    validate_outbound_url(url,allowed_hosts=_ALLOWED_HOSTS)
    with _client(user_agent) as c:
        text,truncated=_stream_head(c,url,max_bytes)
        return re.sub(r"<[^>]+>"," ",text),truncated

_DOC_END=b"</DOCUMENT>"

def filing_first_document(url:str,user_agent:str,max_bytes:int=12_000_000,sleep=time.sleep)->tuple[str,bool]:
    """Raw (not tag-stripped) text of an EDGAR .txt submission up to the end
    of its first <DOCUMENT> (the prospectus itself; exhibits and uuencoded
    graphics follow). Returns (text, truncated): truncated when max_bytes
    was hit first. Same pacing and backoff as every other SEC request."""
    global _last_request_at
    validate_outbound_url(url,allowed_hosts=_ALLOWED_HOSTS)
    last_exc:Exception|None=None
    with _client(user_agent) as c:
        for attempt in range(_MAX_ATTEMPTS):
            wait=_MIN_INTERVAL_SECONDS-(time.monotonic()-_last_request_at)
            if wait>0:sleep(wait)
            try:
                _last_request_at=time.monotonic()
                with c.stream("GET",url) as r:
                    if r.status_code in _RETRY_STATUSES and attempt<_MAX_ATTEMPTS-1:
                        sleep(_BACKOFF_BASE_SECONDS*(2**attempt));last_exc=httpx.HTTPStatusError(f"HTTP {r.status_code}",request=r.request,response=r);continue
                    r.raise_for_status()
                    buf=bytearray()
                    for chunk in r.iter_bytes():
                        start=max(0,len(buf)-len(_DOC_END))
                        buf.extend(chunk)
                        end=buf.find(_DOC_END,start)
                        if end>=0:return _decode(bytes(buf[:end+len(_DOC_END)])),False
                        if len(buf)>=max_bytes:return _decode(bytes(buf[:max_bytes])),True
                    return _decode(bytes(buf)),False
            except (httpx.TimeoutException,httpx.TransportError) as e:
                last_exc=e
                if attempt<_MAX_ATTEMPTS-1:sleep(_BACKOFF_BASE_SECONDS*(2**attempt))
    assert last_exc is not None
    raise last_exc

def filing_text_head(url:str,user_agent:str,max_bytes:int=1_500_000)->str:
    return filing_head(url,user_agent,max_bytes)[0]

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

class DailyIndexUnavailable(Exception):
    """EDGAR has no daily-index file for that date (not yet published, market
    holiday). Distinct from a real fetch failure so callers can skip quietly."""

_quarter_listing_cache:dict[tuple[int,int],set[str]]={}

def daily_index_files(year:int,quarter:int,user_agent:str)->set[str]|None:
    """Names of the master.*.idx files EDGAR lists for a quarter (its own
    directory index.json), cached per process. None when the listing itself
    could not be fetched, so callers fall back to per-file probing."""
    key=(year,quarter)
    if key in _quarter_listing_cache:return _quarter_listing_cache[key]
    try:
        with _client(user_agent) as c:
            r=_get(c,f"https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{quarter}/index.json")
        names={i.get("name","") for i in r.json().get("directory",{}).get("item",[]) if str(i.get("name","")).startswith("master.")}
    except Exception:
        return None
    _quarter_listing_cache[key]=names
    return names

def master_index_if_published(day,user_agent:str):
    """master_index_for_date(), but a day EDGAR has no index file for (not yet
    published, market holiday) raises DailyIndexUnavailable instead of an
    HTTP error. Existence is checked against EDGAR's quarterly directory
    listing; a 403 whose body is an S3 AccessDenied page is the fallback
    signal, while a 403 rate-limit page ("Request Rate Threshold Exceeded")
    stays a real error so a day is never silently skipped under throttling."""
    q=(day.month-1)//3+1
    listing=daily_index_files(day.year,q,user_agent)
    if listing is not None and f"master.{day.strftime('%Y%m%d')}.idx" not in listing:
        raise DailyIndexUnavailable(str(day))
    try: return master_index_for_date(day,user_agent)
    except httpx.HTTPStatusError as e:
        resp=e.response
        if resp is not None and resp.status_code==404: raise DailyIndexUnavailable(str(day)) from e
        if resp is not None and resp.status_code==403 and listing is None:
            body=(resp.text or "")[:2000]
            if "rate threshold" not in body.lower(): raise DailyIndexUnavailable(str(day)) from e
        raise

# A follow-on prospectus routinely mentions the issuer's *past* IPO ("since
# our initial public offering in 2019 ..."), so the phrase alone is not
# enough. Real IPO prospectuses say one of these on the cover page.
_SECURITY = r"(?:shares of )?(?:our |the )?(?:class [ab] )?(?:common stock|common shares|ordinary shares|units|american depositary|ads|shares|securities)(?! ?(?:purchase )?warrants)"
_IPO_COVER_PATTERNS=(
    r"this is (?:an|our|the) initial public offering",
    r"this prospectus (?:relates to|describes|covers|is for) (?:our|the) initial public offering",
    r"we are offering .{0,120}? in (?:our|this) initial public offering",
    rf"prior to this offering,? there (?:has|had) been no (?:established )?public (?:trading )?market for {_SECURITY}",
    rf"there is (?:currently )?no (?:established )?public (?:trading )?market for {_SECURITY}",
    rf"(?:currently,? )?no (?:established )?public (?:trading )?market (?:currently )?exists for {_SECURITY}",
    rf"no (?:established )?public (?:trading )?market for {_SECURITY} currently exists",
)

def flatten_filing_text(text:str)->str:
    """Decode HTML entities and collapse whitespace so cover-page phrases and
    quoted symbols ("under the symbol &#147;FWRG&#148;") match literally."""
    return re.sub(r"\s+"," ",html.unescape(text or ""))

# Item 501(b)(4) makes an already-listed issuer print the last reported sale
# price of its security; an IPO prospectus cannot. These mark a follow-on.
_FOLLOW_ON_PATTERNS=(
    r"last reported sales? price",
    r"closing (?:sale )?price of (?:our|the) [a-z ]{0,40}(?:on|as reported (?:on|by)) (?:the )?(?:nasdaq|nyse|new york stock exchange|otc)",
    r"(?:is|are) (?:currently )?(?:listed|quoted|traded) on (?:the )?(?:nasdaq|nyse|new york stock exchange|otc)[^.]{0,100}under the (?:ticker )?symbol",
)
# Weaker IPO evidence, accepted only together with an IPO mention or an
# explicit "this is a ... public offering of" cover sentence: the dilution
# section's IPO-price statement, or a pending/approved *initial* listing.
_IPO_SUPPORT_PATTERNS=(
    r"initial public offering price[^.]{0,80}exceeds the[^.]{0,60}tangible book value",
    r"(?:has|have) been approved for listing",
    r"(?:has|have) applied (?:to list|for (?:the )?listing)",
    r"approved to (?:have|list) our [a-z ]{0,40}(?:listed|shares|stock)",
    r"(?:has|have) been approved to (?:be )?list(?:ed)? on",
)
_PUBLIC_OFFERING_COVER=r"this is (?:a|an) (?:firm[- ]commitment |underwritten |best[- ]efforts |self[- ]underwritten )?(?:initial )?public offering of"

_COVER_FALLBACK_CHARS=30_000

def cover_region(flat_text:str)->str:
    """The prospectus cover page: everything up to (and just past) the Item 501
    price table. Follow-on and IPO statements are judged there only; a SPAC or
    carve-out prospectus routinely says deeper in the summary that some *other*
    company "is listed on Nasdaq under the symbol ...", which must not count."""
    m=_COVER_DONE.search(flat_text)
    return flat_text[:m.end()+1500] if m else flat_text[:_COVER_FALLBACK_CHARS]

def classify_prospectus(flat_text:str)->str:
    """'ipo' | 'follow_on' | 'unknown' for a 424B4 prospectus (flattened text).

    'unknown' is a real outcome: the caller must not store the filing as an
    IPO, nor reclassify an existing row on evidence it did not read."""
    low=flat_text.lower()
    cover=cover_region(low)
    # An IPO prospectus cannot quote a last sale price of the security being
    # offered, so a follow-on marker on the cover settles it before any cover phrase.
    if any(re.search(p,cover) for p in _FOLLOW_ON_PATTERNS):return "follow_on"
    if any(re.search(p,cover) for p in _IPO_COVER_PATTERNS):return "ipo"
    support=any(re.search(p,low) for p in _IPO_SUPPORT_PATTERNS)
    if support and ("initial public offering" in cover or re.search(_PUBLIC_OFFERING_COVER,cover)):return "ipo"
    return "unknown"

def is_ipo_prospectus(flat_text:str)->bool:
    return classify_prospectus(flat_text)=="ipo"

def parse_priced_ipo(text:str):
    flat=flatten_filing_text(text)
    if not is_ipo_prospectus(flat):return None
    lo,hi=parse_price_range(flat)
    # A 424B4 states the exact public offering price on its cover page. Look
    # there first (the Item 501 table and the "offering price is $X" sentence);
    # only then fall back to the whole text, and never to the loose "at a
    # price of $X per unit" wording outside the cover, which also describes
    # private-placement warrants sold to a SPAC sponsor for $0.20 or $1.00.
    cover=cover_region(flat)
    exact=None
    cover_patterns=[rf"initial public offering price[^$]{{0,100}}\$\s*({_MONEY})",
                    rf"public offering price[^$]{{0,80}}\$\s*({_MONEY})\s+per (?:share|unit|ads)",
                    rf"each unit has an offering price of \$\s*({_MONEY})",
                    rf"offering price of \$\s*({_MONEY}) per unit",
                    # Item 501 table cells: "Public offering price $ 10.00", "Price to public $ 6.25", "Offering price per share $1.00"
                    rf"(?:public )?offering price(?: per (?:share|unit|ads))?\s*(?:\(\d\))?\s*\$\s*({_MONEY})",
                    rf"price to (?:the )?public\s*(?:per (?:share|unit|ads)\s*)?\$?\s*({_MONEY})",
                    # table laid out header row then value row: "Price to Public ... Per Unit $ 10.00 $ 0.55"
                    rf"price to (?:the )?public.{{0,160}}?per (?:share|unit|ads)\s*\$\s*({_MONEY})"]
    # Loose wording only as a last resort, and never a warrant's exercise price
    # ("each warrant is exercisable ... at a price of $11.50 per share").
    _LOOSE=rf"(?:offering price|at a price) of \$\s*({_MONEY}) per (?:class [ab] )?(?:ordinary |common |depositary )?(?:share|unit|ads)"
    def _loose_price(scope:str):
        for m in re.finditer(_LOOSE,scope,re.I):
            before=scope[max(0,m.start()-160):m.start()].lower()
            if "warrant" in before or "exercis" in before or "option" in before or "par value" in before:continue
            v=_price(m.group(1))
            if v is not None:return v
        return None
    for scope,pats in ((cover,cover_patterns),(flat,cover_patterns[:2])):
        for p in pats:
            exact=_price_match(p,scope)
            if exact is not None:break
        if exact is not None:break
    if exact is None:exact=_loose_price(cover)
    if exact is None:exact=parse_offer_price_v4(flat)
    sym=""
    # The prefix is case-insensitive, the symbol itself is not: "under the
    # symbol" followed by lowercase prose ("our") is not a ticker.
    for p in [r"(?i:under the (?:ticker )?symbols? )[\"“'‘�]?([A-Z]{1,6})(?=[\"”’'�.,;: ]|$)",r"(?i:trading symbol\s*[:\-]?\s*)[\"“'‘�]?([A-Z]{1,6})(?=[\"”’'�.,;: ]|$)"]:
        m=re.search(p,flat)
        if m:sym=m.group(1);break
    return {"symbol":sym,"final_price":exact or hi or lo,"price_low":lo,"price_high":hi}


# ----------------------------------------------------------------------------
# v4 (2026-09-29): additive cover-page readers used as a LAST fallback by
# parse_priced_ipo and by scripts/repair_us_prices.py. Nothing above changes.
#
# What the earlier readers missed, measured on the 40 US Listed rows without a
# final price: a currency prefix ("price us$18.00 per ads", "usd$4.13 per
# common unit"), zero-width characters inside the Item 501 table ("price
# ​ $ ​ 5.80"), "ipo price $ 4.00" wording, a fixed-price
# self-underwritten offering ("offered at the fixed price of $2.00 per
# share") and, above all, offerings that have no offer price at all: direct
# listings (a reference price is not an offer price), resale-only
# registrations, debt offerings and de-SPAC proxy/prospectuses.
_ZERO_WIDTH=re.compile("[​‌‍⁠﻿]")
_CUR=r"(?:us\s?d?\s?)?\$"

def clean_cover_text(flat_text:str)->str:
    """Flattened text with zero-width characters removed and whitespace
    re-collapsed, so table cells separated by ​ read as prose."""
    return re.sub(r"\s+"," ",_ZERO_WIDTH.sub("",flat_text or ""))

_PRICE_PATTERNS_V4=(
    rf"(?:total )?(?:initial )?public offering price\s*(?:\(\d\)\s*)?{_CUR}\s*({_MONEY})",
    rf"(?:the )?(?:ipo|initial public offering|public offering) price (?:of (?:our|the) [a-z ]{{0,40}}?)?(?:is|of|was|will be)\s*{_CUR}\s*({_MONEY})\s*per",
    rf"(?:^|[.;] )price\s*{_CUR}\s*({_MONEY})\s*per (?:share|ads|adss|unit|common share|ordinary share|class [ab])",
    rf"ipo price\s*(?:\(\d\)\s*)?{_CUR}\s*({_MONEY})",
    rf"price to (?:the )?public.{{0,160}}?per (?:share|unit|ads|common share|ordinary share)\s*{_CUR}\s*({_MONEY})",
    rf"(?:offer(?:ed)?|sold|sell)[a-z ]{{0,30}}?at (?:a|the) fixed (?:offering )?price of {_CUR}\s*({_MONEY}) per (?:share|unit)",
    rf"public offering price of {_CUR}\s*({_MONEY}) per (?:common |ordinary )?(?:share|unit|ads)",
    rf"offering price of the [a-z ]{{0,30}}?(?:in this offering )?is\s*{_CUR}\s*({_MONEY}) per",
)
# Strict subset safe to apply beyond the cover page: a combined filing prints a
# resale prospectus first and the IPO prospectus (with its own Item 501 table)
# hundreds of thousands of characters later.
# Table cells and declarative "the price is" sentences only: the dilution
# section's "based upon a public offering price of $X" is an assumed midpoint.
_PRICE_PATTERNS_V4_DEEP=(_PRICE_PATTERNS_V4[0],_PRICE_PATTERNS_V4[3],
    rf"(?:ipo|initial public offering|public offering) price (?:of the [a-z ]{{0,30}}?in this offering )?(?:is|was)\s*{_CUR}\s*({_MONEY})\s*per")
_DEEP_CHARS=400_000

def parse_offer_price_v4(flat_text:str)->float|None:
    """Offer price from the cover page using the v4 patterns, or None. Never
    returns a per-share amount from a par-value or warrant clause."""
    low=flat_text.lower()
    cover=clean_cover_text(cover_region(low))
    def _scan(scope,patterns):
        for p in patterns:
            for m in re.finditer(p,scope,re.I):
                before=scope[max(0,m.start()-120):m.start()]
                if "warrant" in before or "exercis" in before or "par value" in before:continue
                v=_price(m.group(1))
                if v is not None:return v
        return None
    v=_scan(cover,_PRICE_PATTERNS_V4)
    if v is None:v=_scan(clean_cover_text(low[:_DEEP_CHARS]),_PRICE_PATTERNS_V4_DEEP)
    return v

DIRECT_LISTING="direct_listing"
RESALE_ONLY="resale_only"
DEBT_OFFERING="debt_offering"
MERGER_PROXY="merger_proxy"

_DIRECT_LISTING_PATTERNS=(
    r"in connection with (?:our|the) direct listing",
    r"unlike an initial public offering, the resale",
    r"(?:indicative )?current reference price",
)
_RESALE_ONLY_PATTERNS=(
    r"this prospectus relates to the (?:resale|offer and sale|offer and resale) (?:from time to time )?(?:of|by)",
    r"relates to the resale (?:of|by)",
)
# A combined filing prints the resale prospectus first and the IPO prospectus
# after it; these phrases on the resale cover mean a primary offering exists.
_PRIMARY_OFFERING_HINTS=r"\bwe are offering\b|shares offered by (?:us|the company)\b|(?:ipo|primary offering|initial public offering) prospectus|in (?:our|the) initial public offering"
_DEBT_PATTERNS=(
    r"per note\s+total\s+public offering price\s*[0-9.]+\s*%",
    r"public offering price\s*(?:\(\d\)\s*)?[0-9]{2,3}\.[0-9]{1,3}\s*%",
)
_MERGER_PROXY_PATTERNS=(
    r"proxy statement/prospectus",
    r"proxy statement and prospectus",
    r"business combination agreement",
)
_FOLLOW_ON_PATTERNS_V4=(
    r"closing sales? price of (?:our|the) [a-z ]{0,60}?(?:as reported on|on) (?:the )?(?:nasdaq|nyse|new york stock exchange)",
    # Present tense on a junior venue: the security already trades somewhere.
    r"(?:is|are) (?:currently |presently )?(?:quoted|listed|traded|trading) on (?:the )?(?:tsx venture|tsxv|tsx|otcqb|otcqx|otc markets|otc pink|cse)[^.]{0,140}under the (?:ticker |trading )?symbol",
    r"(?:quoted|listed|trading|traded) on (?:the )?(?:tsx venture exchange|tsxv|otcqb venture market|otcqb|otcqx)[^.]{0,140}under the (?:ticker |trading )?symbol",
    # Past tense on a national exchange: the shares began trading before this prospectus.
    r"(?:began|commenced|started) trading on (?:the )?(?:nasdaq|nyse|new york stock exchange)[^.]{0,80}under the (?:ticker |trading )?symbol",
)
SPIN_OFF="spin_off_distribution"
_SPIN_OFF_PATTERNS=(
    r"in connection with the (?:planned |proposed )?distribution \(the [^)]{0,60}spin-off",
    r"will be distributed in the spin-off",
    r"spin-off[^.]{0,200}\brecord date\b",
)

def classify_offering_type(flat_text:str)->str:
    """One of DIRECT_LISTING | RESALE_ONLY | DEBT_OFFERING | MERGER_PROXY |
    'follow_on' | 'ipo' | 'unknown' for a 424B4, judged on the cover page.
    Extends classify_prospectus without changing it: a direct listing is a
    genuine first listing with NO offer price; resale-only, debt and de-SPAC
    documents are not IPOs of the registrant's equity."""
    low=clean_cover_text(flat_text.lower())
    cover=cover_region(low)
    if any(re.search(p,cover) for p in _MERGER_PROXY_PATTERNS):return MERGER_PROXY
    if any(re.search(p,cover) for p in _DEBT_PATTERNS):return DEBT_OFFERING
    if any(re.search(p,cover) for p in _DIRECT_LISTING_PATTERNS):return DIRECT_LISTING
    if any(re.search(p,cover) for p in _SPIN_OFF_PATTERNS) and not re.search(r"\bwe are offering\b",cover):return SPIN_OFF
    if any(re.search(p,cover) for p in _FOLLOW_ON_PATTERNS_V4):return "follow_on"
    base=classify_prospectus(flat_text)
    if base=="follow_on":return base
    if any(re.search(p,cover) for p in _RESALE_ONLY_PATTERNS) and not re.search(_PRIMARY_OFFERING_HINTS,cover):
        return RESALE_ONLY
    return base


_SYMBOL_PATTERNS=(r"(?i:under the (?:ticker )?symbols? )[\"“'‘�]?([A-Z]{1,6})(?=[\"”’'�.,;: ]|$)",
                  r"(?i:trading symbol\s*[:\-]?\s*)[\"“'‘�]?([A-Z]{1,6})(?=[\"”’'�.,;: ]|$)")

def parse_symbol(flat_text:str)->str:
    """Exchange symbol quoted on a prospectus cover ("under the symbol “AMPL”"),
    or "". Same rule parse_priced_ipo applies, exposed for documents that are
    not priced IPOs (direct listings)."""
    for p in _SYMBOL_PATTERNS:
        m=re.search(p,flat_text or "")
        if m:return m.group(1)
    return ""
