from __future__ import annotations
import httpx
from ..config import get_settings

ALLOWED={"gmp_pct","qib_sub","nii_sub","retail_sub","total_sub","peer_median_pe","peer_median_ps","peer_median_ev_ebitda","underwriter_quality","anchor_quality","registrar","allotment_url","market_regime","sector_regime","post_issue_shares_m","fresh_issue_pct","ofs_pct","promoter_retention_pct"}

def fetch_rows():
    s=get_settings()
    if not s.secondary_enrichment_url:return [],[]
    headers={"User-Agent":"IPOIntelligence/2.0"}
    if s.secondary_enrichment_token:headers["Authorization"]=f"Bearer {s.secondary_enrichment_token}"
    try:
        r=httpx.get(s.secondary_enrichment_url,headers=headers,timeout=20,follow_redirects=True);r.raise_for_status();j=r.json();rows=j if isinstance(j,list) else j.get("data",[])
        clean=[]
        for x in rows:
            if not isinstance(x,dict):continue
            y={k:v for k,v in x.items() if k in ALLOWED or k in {"company","symbol","country","source_url","source_name"}}
            if y.get("company") or y.get("symbol"):clean.append(y)
        return clean,[]
    except Exception as e:return [],[f"Licensed enrichment feed: {type(e).__name__}: {e}"]
