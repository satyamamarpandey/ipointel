def test_health(client):
    assert client.get('/health').status_code==200

def test_waitlist_signup_and_duplicate(client):
    p={'email':'person@example.com','name':'P','investor_type':'retail','markets':'both','consent':True,'website':''}
    a=client.post('/api/waitlist',json=p);b=client.post('/api/waitlist',json=p)
    assert a.status_code==200 and a.json()['referral_code'];assert b.status_code==200 and b.json()['referral_code']==a.json()['referral_code']

def test_empty_ipos_are_honest(client):
    r=client.get('/api/ipos');assert r.status_code==200 and r.json()==[]

def test_admin_refresh_requires_token(client):
    r=client.post('/api/admin/refresh');assert r.status_code==403

def test_unsubscribe_and_rejoin(client):
    from app.db import SessionLocal
    from app.models import WaitlistLead
    from sqlalchemy import select
    p={'email':'leave@example.com','name':'L','investor_type':'retail','markets':'india','consent':True,'website':''}
    assert client.post('/api/waitlist',json=p).status_code==200
    with SessionLocal() as db:
        lead=db.scalar(select(WaitlistLead).where(WaitlistLead.email=='leave@example.com'));token=lead.unsubscribe_token
    assert client.get('/unsubscribe',params={'token':token}).status_code==200
    with SessionLocal() as db:
        lead=db.scalar(select(WaitlistLead).where(WaitlistLead.email=='leave@example.com'));assert lead.consent is False
    assert client.post('/api/waitlist',json=p).json()['message'].startswith("You're back")

def test_source_health_marks_enrichment_as_tier3(client):
    rows=client.get('/api/source-health').json();tier={x['source']:x['tier'] for x in rows};assert tier['Licensed enrichment feed']==3

def test_model_performance_empty_db_does_not_crash(client):
    r=client.get('/api/model-performance');assert r.status_code==200
    body=r.json();assert 'India' in body and 'United States' in body
    assert body['India']['listing_model']['status'].startswith('insufficient sample')

def _seed_ipo(db):
    from app.models import IPO
    ipo=IPO(external_key='api-test-1',company='ApiTestCo',country='India',currency='INR',
            price_low=95,price_high=100,final_price=100,post_issue_shares_m=100,
            revenue_m=2500,revenue_prev_m=1900,ebitda_m=500,net_income_m=260,cfo_m=310,
            debt_m=150,cash_m=250,fresh_issue_pct=80,ofs_pct=20,qib_sub=65,nii_sub=30,
            retail_sub=12,total_sub=35,filing_url='https://www.nseindia.com/x',status='Filed')
    db.add(ipo);db.commit();db.refresh(ipo);return ipo

def test_ipo_detail_includes_new_engines(client,db):
    ipo=_seed_ipo(db)
    r=client.get(f'/api/ipos/{ipo.id}');assert r.status_code==200
    body=r.json()
    assert 'red_flags' in body and 'flags' in body['red_flags']
    assert 'contradictions' in body
    assert 'sensitivity' in body and 'current_recommendation' in body['sensitivity']

def test_ipo_changes_endpoint(client,db):
    ipo=_seed_ipo(db)
    r=client.get(f'/api/ipos/{ipo.id}/changes');assert r.status_code==200
    assert r.json()['ipo_id']==ipo.id

def test_ipo_similar_endpoint_handles_no_peers(client,db):
    ipo=_seed_ipo(db)
    r=client.get(f'/api/ipos/{ipo.id}/similar');assert r.status_code==200
    assert r.json()['available'] is False

def test_ipo_valuation_endpoint(client,db):
    ipo=_seed_ipo(db)
    r=client.get(f'/api/ipos/{ipo.id}/valuation');assert r.status_code==200
    body=r.json();assert 'scenario_dcf' in body and 'reverse_dcf' in body
    assert body['scenario_dcf']['available'] is True
