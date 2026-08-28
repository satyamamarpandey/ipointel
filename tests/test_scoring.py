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
