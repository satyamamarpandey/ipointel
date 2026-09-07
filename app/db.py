from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import get_settings

settings = get_settings()
_is_sqlite = settings.database_url.startswith("sqlite")
connect_args = {"check_same_thread": False, "timeout": 30} if _is_sqlite else {}
# Pool sizing only applies to a real server. SQLite uses SingletonThreadPool/
# NullPool depending on the driver and rejects these arguments outright.
_pool_kwargs = {} if _is_sqlite else {
    "pool_size": settings.db_pool_size,
    "max_overflow": settings.db_max_overflow,
    # Bounded wait instead of hanging forever when every connection is busy:
    # a request that cannot get a connection should fail fast and free its
    # worker thread, not pile up behind an exhausted pool.
    "pool_timeout": settings.db_pool_timeout,
    # Recycle below the shortest idle timeout in the path (Postgres
    # idle_session_timeout, PgBouncer, or a cloud provider's silent 5-minute
    # NAT drop). pool_pre_ping catches a dead connection on checkout; this
    # keeps them from going stale in the first place.
    "pool_recycle": settings.db_pool_recycle_seconds,
}
engine = create_engine(settings.database_url, future=True, pool_pre_ping=True, connect_args=connect_args, **_pool_kwargs)
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
    """Dev/test convenience only. In production the schema is owned by
    Alembic (`alembic upgrade head`, run by the `migrate` service before web
    and worker start - see docker-compose.production.yml). create_all() there
    would silently re-create anything a missed migration left out, hiding
    drift between the deployed database and the migration history instead of
    failing loudly, and it never runs a data migration at all."""
    from . import models  # noqa: F401
    from .config import _PRODUCTION_ENV_VALUES
    if settings.app_env in _PRODUCTION_ENV_VALUES:
        return
    Base.metadata.create_all(bind=engine)
    if settings.database_url.startswith("sqlite"):
        _migrate_sqlite()

def session_scope():
    return SessionLocal()
