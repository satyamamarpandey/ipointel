from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import String, Float, Integer, DateTime, Boolean, Text, ForeignKey, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column, relationship, Session
from sqlalchemy.types import JSON
from .db import Base

class ImmutableRecordError(Exception):
    """Raised when code tries to modify a row that must never change after
    creation (e.g. a forward prediction snapshot). See ScoreSnapshot below."""

def utcnow():
    return datetime.now(timezone.utc)

class WaitlistLead(Base):
    __tablename__ = "waitlist_leads"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160), default="")
    investor_type: Mapped[str] = mapped_column(String(40), default="retail")
    markets: Mapped[str] = mapped_column(String(40), default="both")
    consent: Mapped[bool] = mapped_column(Boolean, default=True)
    referral_code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    referred_by: Mapped[str] = mapped_column(String(40), default="")
    unsubscribe_token: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(80), default="direct")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suppressed: Mapped[bool] = mapped_column(Boolean, default=False)
    suppressed_reason: Mapped[str] = mapped_column(String(40), default="")
    alert_score_change: Mapped[bool] = mapped_column(Boolean, default=True)
    alert_recommendation_change: Mapped[bool] = mapped_column(Boolean, default=True)
    alert_red_flag: Mapped[bool] = mapped_column(Boolean, default=True)
    alert_new_ipo: Mapped[bool] = mapped_column(Boolean, default=False)
    digest_weekly: Mapped[bool] = mapped_column(Boolean, default=False)
    last_digest_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    access_status: Mapped[str] = mapped_column(String(20), default="WAITLISTED", index=True)  # WAITLISTED|INVITED|ACTIVE|DISABLED
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    clerk_user_id: Mapped[str] = mapped_column(String(80), default="", index=True)
    identity_provider: Mapped[str] = mapped_column(String(20), default="")  # ""=local magic-link | google | apple | email (via Clerk)
    campaign: Mapped[str] = mapped_column(String(80), default="")
    page_path: Mapped[str] = mapped_column(String(160), default="")

class SheetsSyncOutbox(Base):
    """Outbox row mirroring one WaitlistLead into Google Sheets. PostgreSQL/
    SQLite stays the source of truth for the waitlist; this table only tracks
    delivery of that fact to a convenience spreadsheet. One row per lead
    (unique lead_id) makes retries idempotent - we update the same row rather
    than enqueue duplicates."""
    __tablename__ = "sheets_sync_outbox"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("waitlist_leads.id"), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)  # PENDING|SYNCED|FAILED|DISABLED
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    sheet_row_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class LoginToken(Base):
    """A single-use magic-link token. Only the SHA-256 hash is stored - the
    raw token exists only in the email and the URL the user clicks, never at
    rest, so a DB read alone can never be used to log in as someone else."""
    __tablename__ = "login_tokens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("waitlist_leads.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    purpose: Mapped[str] = mapped_column(String(20), default="login")  # login|invite
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class AuthSession(Base):
    """A logged-in beta session. Only the SHA-256 hash of the session token
    is stored; the raw token lives only in the HttpOnly session cookie."""
    __tablename__ = "auth_sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("waitlist_leads.id"), index=True)
    session_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_agent: Mapped[str] = mapped_column(String(300), default="")

class WorkerHeartbeat(Base):
    """Singleton row (id=1) the worker process upserts every cycle so
    Source Health / admin can tell whether it's actually alive, not just
    whether an OS process happens to exist."""
    __tablename__ = "worker_heartbeat"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instance: Mapped[str] = mapped_column(String(80), default="")
    current_job: Mapped[str] = mapped_column(String(80), default="idle")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_sec_refresh_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_nse_refresh_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_performance_update_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_email_pass_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class AdminAuditLog(Base):
    """Every sensitive admin action, append-only (never updated)."""
    __tablename__ = "admin_audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(80), default="admin")
    action: Mapped[str] = mapped_column(String(60), index=True)
    target: Mapped[str] = mapped_column(String(200), default="")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

class IPO(Base):
    __tablename__ = "ipos"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_key: Mapped[str] = mapped_column(String(220), unique=True, index=True)
    company: Mapped[str] = mapped_column(String(220), index=True)
    symbol: Mapped[str] = mapped_column(String(32), default="", index=True)
    country: Mapped[str] = mapped_column(String(32), index=True)
    exchange: Mapped[str] = mapped_column(String(80), default="")
    board: Mapped[str] = mapped_column(String(40), default="Mainboard")
    sector: Mapped[str] = mapped_column(String(120), default="Unknown")
    status: Mapped[str] = mapped_column(String(40), default="Filed", index=True)
    filing_date: Mapped[str] = mapped_column(String(20), default="")
    open_date: Mapped[str] = mapped_column(String(20), default="")
    close_date: Mapped[str] = mapped_column(String(20), default="")
    listing_date: Mapped[str] = mapped_column(String(20), default="")
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    price_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    final_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    issue_size_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    shares_offered_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    post_issue_shares_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    lot_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revenue_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_prev_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_2y_ago_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    ebitda_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_income_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    cfo_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    debt_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    cash_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    fresh_issue_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    ofs_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    promoter_retention_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    qib_sub: Mapped[float | None] = mapped_column(Float, nullable=True)
    nii_sub: Mapped[float | None] = mapped_column(Float, nullable=True)
    retail_sub: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_sub: Mapped[float | None] = mapped_column(Float, nullable=True)
    gmp_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    underwriter_quality: Mapped[float | None] = mapped_column(Float, nullable=True)
    anchor_quality: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_regime: Mapped[float | None] = mapped_column(Float, nullable=True)
    sector_regime: Mapped[float | None] = mapped_column(Float, nullable=True)
    dual_class: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    lockup_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    market_overhang_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    peer_median_pe: Mapped[float | None] = mapped_column(Float, nullable=True)
    peer_median_ps: Mapped[float | None] = mapped_column(Float, nullable=True)
    peer_median_ev_ebitda: Mapped[float | None] = mapped_column(Float, nullable=True)
    filing_url: Mapped[str] = mapped_column(Text, default="")
    registrar: Mapped[str] = mapped_column(String(160), default="")
    allotment_url: Mapped[str] = mapped_column(Text, default="")
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    data_flags: Mapped[list] = mapped_column(JSON, default=list)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    scores: Mapped[list["ScoreSnapshot"]] = relationship(back_populates="ipo", cascade="all, delete-orphan")
    provenance: Mapped[list["Provenance"]] = relationship(back_populates="ipo", cascade="all, delete-orphan")

class ScoreSnapshot(Base):
    """An immutable, point-in-time prediction. This IS the forward-prediction
    record (spec: "PredictionSnapshot") - once written it is never updated or
    deleted (enforced below by _reject_score_snapshot_mutation). A new
    ScoreSnapshot row is inserted whenever the model's inputs or output move
    in a way event_stage names; nothing here is ever rewritten by a later
    refresh, so this table is the true prospective track record.

    is_forward distinguishes a genuine forward prediction (the IPO's status
    was still pre-listing at the moment this row was written - the outcome
    was NOT yet known) from a retrospective one (scored via app/backfill.py
    or a "priced in the last few days" ingest pass, where status was already
    Listed - useful for backtesting, but not prospective evidence)."""
    __tablename__ = "score_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ipo_id: Mapped[int] = mapped_column(ForeignKey("ipos.id"), index=True)
    model_version: Mapped[str] = mapped_column(String(40), default="v2.0")
    feature_schema_version: Mapped[str] = mapped_column(String(20), default="")
    event_stage: Mapped[str] = mapped_column(String(40), default="", index=True)
    is_forward: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    feature_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    provenance_ids: Mapped[list] = mapped_column(JSON, default=list)
    overall_score: Mapped[float] = mapped_column(Float)
    listing_score: Mapped[float] = mapped_column(Float)
    long_term_score: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    listing_gain_probability: Mapped[float] = mapped_column(Float)
    long_term_outperform_probability: Mapped[float] = mapped_column(Float)
    recommendation: Mapped[str] = mapped_column(String(80))
    horizon: Mapped[str] = mapped_column(String(80))
    valuation_label: Mapped[str] = mapped_column(String(40))
    fair_value_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    fair_value_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    pillars: Mapped[dict] = mapped_column(JSON, default=dict)
    rationale: Mapped[list] = mapped_column(JSON, default=list)
    risks: Mapped[list] = mapped_column(JSON, default=list)
    what_changes_verdict: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    ipo: Mapped[IPO] = relationship(back_populates="scores")
    outcome: Mapped["PredictionOutcome | None"] = relationship(back_populates="snapshot", uselist=False)

@event.listens_for(Session, "before_flush")
def _reject_score_snapshot_mutation(session, flush_context, instances):
    for obj in session.dirty:
        if isinstance(obj, ScoreSnapshot) and session.is_modified(obj, include_collections=False):
            raise ImmutableRecordError(
                f"ScoreSnapshot {obj.id} is an immutable forward-prediction record and cannot be modified after creation. "
                "Attach new information as a new ScoreSnapshot row (or, for realized returns, a PredictionOutcome row)."
            )

class PredictionOutcome(Base):
    """Realized returns for a forward prediction, attached AFTER listing
    without ever touching the original ScoreSnapshot. One row per snapshot
    (the IPO's last is_forward=True snapshot - its final pre-listing call).
    Unlike ScoreSnapshot this row IS updated in place as more return windows
    become observable (7d now, 6m/12m/24m only once enough time has passed)."""
    __tablename__ = "prediction_outcomes"
    __table_args__ = (UniqueConstraint("score_snapshot_id", name="uq_outcome_snapshot"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    score_snapshot_id: Mapped[int] = mapped_column(ForeignKey("score_snapshots.id"), unique=True, index=True)
    ipo_id: Mapped[int] = mapped_column(ForeignKey("ipos.id"), index=True)
    listing_open_return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    listing_close_return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_7d_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_30d_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_6m_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_12m_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_24m_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    benchmark_relative_return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_name: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    snapshot: Mapped[ScoreSnapshot] = relationship(back_populates="outcome")

class Provenance(Base):
    __tablename__ = "provenance"
    __table_args__ = (UniqueConstraint("ipo_id", "field_name", "source_url", name="uq_provenance"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ipo_id: Mapped[int] = mapped_column(ForeignKey("ipos.id"), index=True)
    field_name: Mapped[str] = mapped_column(String(100), index=True)
    source_name: Mapped[str] = mapped_column(String(100))
    source_url: Mapped[str] = mapped_column(Text, default="")
    source_tier: Mapped[int] = mapped_column(Integer, default=2)
    observed_value: Mapped[str] = mapped_column(Text, default="")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    is_conflict: Mapped[bool] = mapped_column(Boolean, default=False)
    ipo: Mapped[IPO] = relationship(back_populates="provenance")

class PerformanceSnapshot(Base):
    __tablename__ = "performance_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ipo_id: Mapped[int] = mapped_column(ForeignKey("ipos.id"), index=True)
    as_of_date: Mapped[str] = mapped_column(String(20), index=True)
    close_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    listing_return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_1m_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_6m_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    return_12m_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    benchmark_return_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_name: Mapped[str] = mapped_column(String(100), default="")
    source_url: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class IngestionRun(Base):
    __tablename__ = "ingestion_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(100), index=True)
    status: Mapped[str] = mapped_column(String(30), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rows_seen: Mapped[int] = mapped_column(Integer, default=0)
    rows_changed: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)

class EmailMessage(Base):
    """Every email the app sends or queues, of every kind (confirmation, alert,
    digest). One row per recipient per logical event - the (lead_id, template,
    dedupe_key) constraint is what makes alert delivery idempotent."""
    __tablename__ = "email_messages"
    __table_args__ = (UniqueConstraint("lead_id", "template", "dedupe_key", name="uq_lead_template_dedupe"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("waitlist_leads.id"), index=True)
    ipo_id: Mapped[int | None] = mapped_column(ForeignKey("ipos.id"), nullable=True, index=True)
    score_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("score_snapshots.id"), nullable=True, index=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    template: Mapped[str] = mapped_column(String(40), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(80), default="")
    subject: Mapped[str] = mapped_column(String(200), default="")
    provider: Mapped[str] = mapped_column(String(20), default="")  # set to the EMAIL_PROVIDER actually used at send time
    provider_message_id: Mapped[str] = mapped_column(String(100), default="", index=True)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=2, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
