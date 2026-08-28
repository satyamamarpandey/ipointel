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
