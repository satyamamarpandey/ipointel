import os,tempfile
os.environ["DATABASE_URL"]="sqlite:///./data/test.db"
os.environ["STRICT_RELIABILITY"]="true"
os.environ["MIN_RECOMMENDATION_CONFIDENCE"]="70"
import itertools
import pytest
from fastapi.testclient import TestClient
from app.db import Base,engine,SessionLocal
from app.main import app
from app.models import WaitlistLead
from app.services import auth as auth_svc

_ref_counter = itertools.count()

@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(bind=engine);Base.metadata.create_all(bind=engine)
    from app.main import rate as _rate,auth_rate as _auth_rate
    _rate.clear();_auth_rate.clear()  # TestClient reuses one fake IP across every test in the run
    yield
@pytest.fixture
def client():return TestClient(app)
@pytest.fixture
def db():
    x=SessionLocal();yield x;x.close()

@pytest.fixture
def admin_client():
    from app.config import get_settings
    c=TestClient(app)
    c.headers.update({"X-Admin-Token":get_settings().admin_token})
    return c

@pytest.fixture
def authed_client(db):
    """A TestClient already logged in as an ACTIVE beta user - most dashboard
    API tests care about the data, not the auth flow itself (which has its
    own dedicated tests in test_auth.py)."""
    lead=WaitlistLead(email=f"authed{next(_ref_counter)}@example.com",name="Authed",
                       referral_code=f"AUTHREF{next(_ref_counter)}",unsubscribe_token=f"authtok{next(_ref_counter)}",
                       access_status="ACTIVE")
    db.add(lead);db.commit();db.refresh(lead)
    session_token=auth_svc.create_session(db,lead)
    db.commit()
    c=TestClient(app)
    c.cookies.set(auth_svc.SESSION_COOKIE,session_token)
    c.lead=lead
    return c
