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
    tables = insp.get_table_names()
    per_table_additions = {
        "waitlist_leads": {
            "suppressed": "BOOLEAN DEFAULT 0", "suppressed_reason": "VARCHAR(40) DEFAULT ''",
            "alert_score_change": "BOOLEAN DEFAULT 1", "alert_recommendation_change": "BOOLEAN DEFAULT 1",
            "alert_red_flag": "BOOLEAN DEFAULT 1", "alert_new_ipo": "BOOLEAN DEFAULT 0",
            "digest_weekly": "BOOLEAN DEFAULT 0", "last_digest_at": "DATETIME",
            "access_status": "VARCHAR(20) DEFAULT 'WAITLISTED'", "last_login_at": "DATETIME",
            "clerk_user_id": "VARCHAR(80) DEFAULT ''", "identity_provider": "VARCHAR(20) DEFAULT ''",
            "campaign": "VARCHAR(80) DEFAULT ''", "page_path": "VARCHAR(160) DEFAULT ''",
        },
        "score_snapshots": {
            # is_forward defaults to 0 for this migration deliberately: we have no positive
            # evidence pre-existing rows were genuinely forward predictions (vs backfilled),
            # and overclaiming prospective track record is worse than undercounting it. Every
            # NEW row from this point on sets is_forward explicitly (see pipeline.upsert_ipo).
            "feature_schema_version": "VARCHAR(20) DEFAULT ''", "event_stage": "VARCHAR(40) DEFAULT ''",
            "is_forward": "BOOLEAN DEFAULT 0", "feature_snapshot": "JSON", "provenance_ids": "JSON",
        },
    }
    with engine.begin() as conn:
        for table, additions in per_table_additions.items():
            if table not in tables:
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            for col, ddl in additions.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))

def init_db():
    from . import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    if settings.database_url.startswith("sqlite"):
        _migrate_sqlite()

def session_scope():
    return SessionLocal()
