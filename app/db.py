from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import get_settings

settings = get_settings()
connect_args = {"check_same_thread": False, "timeout": 30} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, future=True, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

def _migrate_sqlite():
    """create_all only adds missing tables, never missing columns on an
    existing table. This adds any new nullable/defaulted columns the models
    have picked up since the dev DB was first created - safe, additive,
    no data loss. No-op on a fresh DB (create_all already has every column)."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if "waitlist_leads" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("waitlist_leads")}
    additions = {
        "suppressed": "BOOLEAN DEFAULT 0", "suppressed_reason": "VARCHAR(40) DEFAULT ''",
        "alert_score_change": "BOOLEAN DEFAULT 1", "alert_recommendation_change": "BOOLEAN DEFAULT 1",
        "alert_red_flag": "BOOLEAN DEFAULT 1", "alert_new_ipo": "BOOLEAN DEFAULT 0",
        "digest_weekly": "BOOLEAN DEFAULT 0", "last_digest_at": "DATETIME",
    }
    with engine.begin() as conn:
        for col, ddl in additions.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE waitlist_leads ADD COLUMN {col} {ddl}"))

def init_db():
    from . import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    if settings.database_url.startswith("sqlite"):
        _migrate_sqlite()

def session_scope():
    return SessionLocal()
