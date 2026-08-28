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

def test_waitlist_signup_queues_welcome_email(client,db):
    from app.models import EmailMessage,WaitlistLead
    from sqlalchemy import select
    p={'email':'emailqueue@example.com','name':'E','investor_type':'retail','markets':'both','consent':True,'website':''}
    r=client.post('/api/waitlist',json=p);assert r.status_code==200
    lead=db.scalar(select(WaitlistLead).where(WaitlistLead.email=='emailqueue@example.com'))
    msg=db.scalar(select(EmailMessage).where(EmailMessage.lead_id==lead.id))
    assert msg is not None and msg.template=='welcome' and msg.status=='QUEUED'

def test_signup_succeeds_even_though_email_provider_disabled(client):
    # ENABLE_EMAIL is false in the test environment (conftest doesn't turn it on) -
    # signup must still succeed; email just stays queued, never blocking or erroring the request.
    p={'email':'provideroutage@example.com','name':'P','investor_type':'retail','markets':'both','consent':True,'website':''}
    r=client.post('/api/waitlist',json=p)
    assert r.status_code==200 and r.json()['ok'] is True

def test_preferences_get_and_update(client,db):
    from app.models import WaitlistLead
    from sqlalchemy import select
    p={'email':'prefs@example.com','name':'Pr','investor_type':'retail','markets':'both','consent':True,'website':''}
    client.post('/api/waitlist',json=p)
    lead=db.scalar(select(WaitlistLead).where(WaitlistLead.email=='prefs@example.com'))
    g=client.get('/api/preferences',params={'token':lead.unsubscribe_token})
    assert g.status_code==200 and g.json()['digest_weekly'] is False
    u=client.post('/api/preferences',params={'token':lead.unsubscribe_token},json={'digest_weekly':True,'alert_new_ipo':True})
    assert u.status_code==200 and u.json()['digest_weekly'] is True
    db.refresh(lead);assert lead.digest_weekly is True

def test_preferences_invalid_token_404(client):
    assert client.get('/api/preferences',params={'token':'not-a-real-token'}).status_code==404

def test_unsubscribe_then_no_future_alert_email(client,db):
    from app.models import WaitlistLead,IPO,ScoreSnapshot,EmailMessage
    from app.services import alerts as alerts_svc
    from sqlalchemy import select
    p={'email':'unsubalert@example.com','name':'U','investor_type':'retail','markets':'both','consent':True,'website':''}
    client.post('/api/waitlist',json=p)
    lead=db.scalar(select(WaitlistLead).where(WaitlistLead.email=='unsubalert@example.com'))
    assert client.get('/unsubscribe',params={'token':lead.unsubscribe_token}).status_code==200
    db.refresh(lead);assert lead.consent is False
    ipo=IPO(external_key='unsub-ipo',company='UnsubCo',country='India',currency='INR');db.add(ipo);db.commit();db.refresh(ipo)
    sc=ScoreSnapshot(ipo_id=ipo.id,model_version='v',overall_score=75,listing_score=70,long_term_score=72,confidence=80,
        listing_gain_probability=70,long_term_outperform_probability=70,recommendation='INVEST SELECTIVELY',horizon='BOTH',
        valuation_label='FAIR',pillars={},rationale=[],risks=[],what_changes_verdict=[])
    db.add(sc);db.commit()
    alerts_svc.queue_score_alerts(db);db.commit()
    assert db.scalar(select(EmailMessage).where(EmailMessage.lead_id==lead.id,EmailMessage.template=='score_alert')) is None

def test_webhook_rejects_unsigned_payload(client):
    r=client.post('/api/webhooks/resend',json={'type':'email.sent','data':{}})
    assert r.status_code==401

def test_admin_email_stats_requires_token(client):
    assert client.get('/api/admin/email-stats').status_code==403

def test_source_health_includes_email(client):
    rows=client.get('/api/source-health').json()
    names=[r['source'] for r in rows]
    assert any('Email' in n for n in names)

def test_ipo_valuation_endpoint(client,db):
    ipo=_seed_ipo(db)
    r=client.get(f'/api/ipos/{ipo.id}/valuation');assert r.status_code==200
    body=r.json();assert 'scenario_dcf' in body and 'reverse_dcf' in body
    assert body['scenario_dcf']['available'] is True
