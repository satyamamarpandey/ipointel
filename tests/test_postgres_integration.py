"""Postgres-specific verification.

The 90 tests elsewhere in this suite already run against Postgres too, for
free, whenever TEST_DATABASE_URL points at the isolated test cluster (see
conftest.py's clean_db fixture) - they exercise the app the same way
regardless of dialect. This file is for the narrower set of things that
either differ across dialects or that SQLite can silently mask (FK
enforcement in particular), so each test here is skipped under SQLite with
an explicit reason rather than faked with a different assertion.

test_unique_constraint_on_prediction_outcome_snapshot_id is the one
exception: SQLite enforces UNIQUE regardless of the foreign_keys pragma, so
it is left unskipped to prove parity across both dialects rather than
Postgres-only behavior.

Every test here runs against the isolated ipo_test database (see
tests/conftest.py / TEST_DATABASE_URL) - never the real migrated 'ipo'
database. tests/db_safety.py's guard would refuse the destructive fixture
setup outright if that were ever misconfigured.
"""
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import create_engine, select, func, text
from sqlalchemy.exc import IntegrityError

from app.db import engine, Base
from app.models import IPO, ScoreSnapshot, PredictionOutcome, ImmutableRecordError
from tests.db_safety import assert_safe_test_database_url

pg_only = pytest.mark.skipif(
    engine.dialect.name != "postgresql",
    reason="Exercises real Postgres server behavior (FK enforcement, native "
           "boolean/JSON/timestamptz storage) that SQLite either lacks or "
           "silently emulates differently.",
)


def _make_ipo(db, key, **kw):
    kw.setdefault("company", "PGIntCo")
    kw.setdefault("country", "India")
    ipo = IPO(external_key=key, **kw)
    db.add(ipo); db.commit(); db.refresh(ipo)
    return ipo


def _snapshot_kwargs(**kw):
    base = dict(overall_score=70, listing_score=70, long_term_score=70, confidence=80,
                listing_gain_probability=70, long_term_outperform_probability=70,
                recommendation="WATCH", horizon="BOTH", valuation_label="FAIR",
                event_stage="ipo_discovered")
    base.update(kw)
    return base


@pg_only
def test_json_fields_round_trip(db):
    ipo = _make_ipo(db, "pg-json-1")
    feature_snapshot = {"nested": {"a": [1, 2, 3], "b": None}, "flag": True, "price": 123.456}
    provenance_ids = [1, 2, 3, 4]
    sc = ScoreSnapshot(ipo_id=ipo.id, feature_snapshot=feature_snapshot, provenance_ids=provenance_ids,
                        **_snapshot_kwargs())
    db.add(sc); db.commit(); db.refresh(sc)
    db.expire_all()
    reloaded = db.get(ScoreSnapshot, sc.id)
    assert reloaded.feature_snapshot == feature_snapshot
    assert reloaded.provenance_ids == provenance_ids


@pg_only
def test_timestamptz_preserves_utc_instant(db):
    ist = timezone(timedelta(hours=5, minutes=30))
    local_dt = datetime(2026, 6, 1, 12, 0, 0, tzinfo=ist)  # == 06:30:00 UTC
    ipo = _make_ipo(db, "pg-tz-1", company="TzCo", first_seen_at=local_dt, updated_at=local_dt)
    db.expire_all()
    reloaded = db.get(IPO, ipo.id)
    assert reloaded.first_seen_at.astimezone(timezone.utc) == local_dt.astimezone(timezone.utc)


@pg_only
def test_boolean_round_trips_as_native_postgres_boolean(db):
    ipo = _make_ipo(db, "pg-bool-1")
    sc = ScoreSnapshot(ipo_id=ipo.id, is_forward=True, **_snapshot_kwargs())
    db.add(sc); db.commit()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT is_forward, pg_typeof(is_forward) FROM score_snapshots WHERE id=:id"),
            {"id": sc.id},
        ).one()
    assert row[0] is True
    assert str(row[1]) == "boolean"


@pg_only
def test_numeric_precision_no_drift(db):
    ipo = _make_ipo(db, "pg-num-1")
    value = 43.123456789012
    kwargs = _snapshot_kwargs()
    kwargs["overall_score"] = value
    sc = ScoreSnapshot(ipo_id=ipo.id, **kwargs)
    db.add(sc); db.commit(); db.refresh(sc)
    db.expire_all()
    reloaded = db.get(ScoreSnapshot, sc.id)
    assert reloaded.overall_score == value


@pg_only
def test_foreign_key_violation_is_enforced(db):
    sc = ScoreSnapshot(ipo_id=999_999_999, **_snapshot_kwargs())
    db.add(sc)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_unique_constraint_on_prediction_outcome_snapshot_id(db):
    # Not gated to Postgres - see module docstring.
    ipo = _make_ipo(db, "pg-uniq-1")
    sc = ScoreSnapshot(ipo_id=ipo.id, **_snapshot_kwargs())
    db.add(sc); db.commit(); db.refresh(sc)
    db.add(PredictionOutcome(score_snapshot_id=sc.id, ipo_id=ipo.id)); db.commit()
    db.add(PredictionOutcome(score_snapshot_id=sc.id, ipo_id=ipo.id))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


@pg_only
def test_immutable_prediction_then_outcome_attachment(db):
    ipo = _make_ipo(db, "pg-immut-1")
    sc = ScoreSnapshot(ipo_id=ipo.id, is_forward=True, **_snapshot_kwargs())
    db.add(sc); db.commit(); db.refresh(sc)
    fields_before = {c.name: getattr(sc, c.name) for c in ScoreSnapshot.__table__.columns}

    sc.overall_score = 1
    with pytest.raises(ImmutableRecordError):
        db.commit()
    db.rollback()

    db.add(PredictionOutcome(score_snapshot_id=sc.id, ipo_id=ipo.id, listing_open_return_pct=5.0))
    db.commit()

    db.expire_all()
    reloaded = db.get(ScoreSnapshot, sc.id)
    fields_after = {c.name: getattr(reloaded, c.name) for c in ScoreSnapshot.__table__.columns}
    assert fields_before == fields_after
    outcome = db.scalar(select(PredictionOutcome).where(PredictionOutcome.score_snapshot_id == sc.id))
    assert outcome is not None and outcome.listing_open_return_pct == 5.0


@pg_only
def test_failed_multi_row_transaction_rolls_back_entirely(db):
    _make_ipo(db, "pg-txn-1")  # pre-existing row whose external_key we'll collide with
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(IPO.__table__.insert().values(external_key="pg-txn-2", company="TxnCo2", country="India"))
            conn.execute(IPO.__table__.insert().values(external_key="pg-txn-1", company="Dup", country="India"))
    remaining = db.scalar(select(func.count()).select_from(IPO).where(IPO.external_key == "pg-txn-2"))
    assert remaining == 0


@pg_only
def test_migration_script_refuses_non_empty_isolated_test_target(db, tmp_path):
    """Runs the real scripts/migrate_sqlite_to_postgres.py as a subprocess,
    exactly as an operator would invoke it, against the isolated ipo_test
    database - never the real 'ipo' database - to prove the refuse-on-
    non-empty-target safety check still works end to end."""
    pg_url = engine.url.render_as_string(hide_password=False)
    assert_safe_test_database_url(pg_url)  # belt-and-braces

    _make_ipo(db, "pg-refuse-1")  # target now has >=1 row, so the script must refuse

    src_path = tmp_path / "src.db"
    src_engine = create_engine(f"sqlite:///{src_path}")
    Base.metadata.create_all(bind=src_engine)  # empty but schema-valid source
    src_engine.dispose()

    env = dict(os.environ, SQLITE_URL=f"sqlite:///{src_path}", DATABASE_URL=pg_url)
    result = subprocess.run(
        [sys.executable, "scripts/migrate_sqlite_to_postgres.py"],
        env=env, capture_output=True, text=True, cwd=os.getcwd(),
    )
    assert result.returncode != 0
    assert "Refusing to migrate into non-empty table" in (result.stdout + result.stderr)
