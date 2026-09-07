from app.models import IPO
from app.scoring import compute_score,valuation

def strong():
    return IPO(external_key='x',company='StrongCo',country='India',currency='INR',price_high=100,post_issue_shares_m=100,revenue_m=2500,revenue_prev_m=1900,revenue_2y_ago_m=1500,ebitda_m=500,net_income_m=260,cfo_m=310,debt_m=150,cash_m=250,fresh_issue_pct=80,ofs_pct=20,promoter_retention_pct=70,qib_sub=65,nii_sub=30,retail_sub=12,total_sub=35,anchor_quality=4.5,market_regime=4,sector_regime=4,peer_median_pe=55,peer_median_ps=5,filing_url='https://www.nseindia.com/test')

def test_strong_ipo_scores_high_and_has_confidence():
    s=compute_score(strong());assert s['overall_score']>65;assert s['listing_score']>65;assert s['confidence']>=70

def test_sparse_data_refuses_recommendation():
    x=IPO(external_key='y',company='Sparse',country='United States',price_high=20)
    s=compute_score(x);assert 'NO RECOMMENDATION' in s['recommendation'];assert s['confidence']<70

def test_valuation_peer_compare():
    v=valuation(strong());assert v.label in {'UNDERPRICED','FAIR','OVERPRICED'};assert v.fair_low is not None


# ---- primary-source detection is host-based, not substring-based ----------
# `"nse" in url.lower()` promoted any URL merely containing those letters to
# full primary-source confidence, and that figure gates whether a
# recommendation is shown at all.

from app.scoring import is_primary_source_url


def test_real_regulator_and_exchange_urls_count_as_primary():
    for url in ("https://www.sec.gov/Archives/edgar/data/1/x.htm",
                "https://sec.gov/x",
                "https://nsearchives.nseindia.com/content/a.xlsx",
                "https://www.nseindia.com/x",
                "https://www.sebi.gov.in/filings/x.pdf"):
        assert is_primary_source_url(url) is True, url


def test_lookalike_domains_are_not_primary():
    """The substring test accepted every one of these."""
    for url in ("https://sec.gov.evil.example.com/filing",
                "https://notsec.gov.co/x",
                "https://nseindia.com.phish.example/x"):
        assert is_primary_source_url(url) is False, url


def test_unrelated_domains_that_merely_contain_the_letters_are_not_primary():
    """"consensus" and "nonsense" both contain "nse"."""
    for url in ("https://consensus-research.example/report",
                "https://nonsense.example/filing",
                "https://example.com/redirect?to=nseindia.com",
                "https://example.com/sec.gov/fake"):
        assert is_primary_source_url(url) is False, url


def test_missing_or_malformed_urls_are_not_primary():
    for url in (None, "", "not a url", "http://["):
        assert is_primary_source_url(url) is False, repr(url)
