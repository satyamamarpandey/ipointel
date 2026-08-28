import os,tempfile
os.environ["DATABASE_URL"]="sqlite:///./data/test.db"
os.environ["STRICT_RELIABILITY"]="true"
os.environ["MIN_RECOMMENDATION_CONFIDENCE"]="70"
import pytest
from fastapi.testclient import TestClient
from app.db import Base,engine,SessionLocal
from app.main import app

@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(bind=engine);Base.metadata.create_all(bind=engine);yield
@pytest.fixture
def client():return TestClient(app)
@pytest.fixture
def db():
    x=SessionLocal();yield x;x.close()
