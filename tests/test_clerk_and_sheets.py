"""Clerk identity mapping/access-rule enforcement, and the Google Sheets
outbox (enqueue idempotency, PENDING-CONFIGURATION behavior, retry/backoff).
Both integrations are designed to be fully inert without external
credentials - see app/services/clerk_auth.py and app/services/sheets_sync.py."""
import itertools
from datetime import timedelta
from sqlalchemy import select
from app.models import WaitlistLead, SheetsSyncOutbox
from app.services import clerk_auth as clerk_svc
from app.services import sheets_sync as sheets_svc
from app.config import get_settings

_ref = itertools.count()

def _lead(db, **kw):
    defaults = dict(email=f"c{next(_ref)}@example.com", referral_code=f"CREF{next(_ref)}", unsubscribe_token=f"ctok{next(_ref)}")
    defaults.update(kw)
    lead = WaitlistLead(**defaults)
    db.add(lead); db.commit(); db.refresh(lead)
    return lead

# ---------- Clerk identity mapping ----------

def test_new_clerk_identity_lands_as_waitlisted(db):
    lead = clerk_svc.sync_identity(db, clerk_user_id="user_abc", email="New@Example.com", name="New Person", provider="google")
    db.commit()
    assert lead.access_status == "WAITLISTED"
    assert lead.email == "new@example.com"  # normalized
    assert lead.clerk_user_id == "user_abc"
    assert lead.identity_provider == "google"

def test_clerk_identity_links_to_existing_lead_by_email_without_changing_access(db):
    lead = _lead(db, email="already@example.com", access_status="INVITED")
    synced = clerk_svc.sync_identity(db, clerk_user_id="user_xyz", email="already@example.com", provider="google")
    db.commit()
    assert synced.id == lead.id
    assert synced.access_status == "INVITED"  # Clerk never grants/changes beta access
    assert synced.clerk_user_id == "user_xyz"

def test_disabled_lead_stays_disabled_after_clerk_sync(db):
    lead = _lead(db, email="blocked@example.com", access_status="DISABLED")
    synced = clerk_svc.sync_identity(db, clerk_user_id="user_blocked", email="blocked@example.com")
    db.commit()
    assert synced.access_status == "DISABLED"

def test_clerk_identity_found_directly_by_clerk_user_id_on_repeat_login(db):
    first = clerk_svc.sync_identity(db, clerk_user_id="user_repeat", email="repeat@example.com")
    db.commit()
    second = clerk_svc.sync_identity(db, clerk_user_id="user_repeat", email="repeat@example.com")
    db.commit()
    assert first.id == second.id
    assert db.scalar(select(WaitlistLead).where(WaitlistLead.clerk_user_id == "user_repeat")) is not None

def test_user_deleted_unlinks_but_does_not_delete_lead(db):
    lead = clerk_svc.sync_identity(db, clerk_user_id="user_gone", email="gone@example.com")
    db.commit()
    clerk_svc.handle_user_deleted(db, "user_gone")
    db.commit()
    db.refresh(lead)
    assert lead.clerk_user_id == ""
    assert lead.identity_provider == ""
    assert db.get(WaitlistLead, lead.id) is not None  # row survives - it's the audit trail

def test_clerk_webhook_rejects_unsigned_request(client):
    r = client.post("/webhooks/clerk", json={"type": "user.created", "data": {}})
    assert r.status_code == 401

def test_clerk_config_exposes_only_publishable_key(client):
    r = client.get("/api/clerk/config")
    assert r.status_code == 200
    body = r.json()
    assert "publishable_key" in body and "secret_key" not in body and "clerk_secret_key" not in body

# ---------- Google Sheets outbox ----------

def test_sheets_enqueue_is_idempotent(db):
    lead = _lead(db)
    row1 = sheets_svc.enqueue(db, lead)
    row2 = sheets_svc.enqueue(db, lead)
    db.commit()
    assert row1.id == row2.id
    assert db.scalar(select(SheetsSyncOutbox).where(SheetsSyncOutbox.lead_id == lead.id).order_by(SheetsSyncOutbox.id)) is not None
    all_rows = db.scalars(select(SheetsSyncOutbox).where(SheetsSyncOutbox.lead_id == lead.id)).all()
    assert len(all_rows) == 1

def test_sheets_enqueue_new_row_starts_pending(db):
    lead = _lead(db)
    row = sheets_svc.enqueue(db, lead)
    db.commit()
    assert row.status == "PENDING"
    assert row.attempt_count == 0

def test_build_row_matches_column_order(db):
    lead = _lead(db, name="Jane", investor_type="professional", markets="india", source="direct",
                 campaign="launch2026", referred_by="REFX", page_path="/", consent=True, clerk_user_id="user_1")
    values = sheets_svc.build_row(lead)
    assert len(values) == len(sheets_svc.SHEET_COLUMNS)
    assert values[1] == lead.email
    assert values[2] == "Jane"
    assert values[6] == "launch2026"  # Campaign column
    assert values[9] == "yes"  # Consent column
    assert values[11] == "user_1"  # Clerk User ID column

def test_process_outbox_without_configuration_leaves_rows_pending(db):
    lead = _lead(db)
    sheets_svc.enqueue(db, lead)
    db.commit()
    settings = get_settings().model_copy(update={"google_sheets_enabled": False})
    result = sheets_svc.process_outbox(db, settings)
    assert result == {"synced": 0, "failed": 0}
    row = db.scalar(select(SheetsSyncOutbox).where(SheetsSyncOutbox.lead_id == lead.id))
    assert row.status == "PENDING"  # never fabricated as SYNCED

def test_sync_status_counts_reports_not_configured(db):
    lead = _lead(db)
    sheets_svc.enqueue(db, lead)
    db.commit()
    settings = get_settings().model_copy(update={"google_sheets_enabled": False})
    counts = sheets_svc.sync_status_counts(db, settings)
    assert counts["configured"] is False
    assert counts["total"] == 1
    assert counts["pending"] == 1
    assert counts["synced"] == 0

def test_process_outbox_marks_synced_on_success(db, monkeypatch):
    lead = _lead(db)
    sheets_svc.enqueue(db, lead)
    db.commit()
    monkeypatch.setattr(sheets_svc, "_append_row", lambda settings, values: None)
    settings = get_settings().model_copy(update={
        "google_sheets_enabled": True, "google_sheets_spreadsheet_id": "sheet123",
        "google_sheets_service_account_json": '{"fake":"creds"}'})
    result = sheets_svc.process_outbox(db, settings)
    assert result == {"synced": 1, "failed": 0}
    row = db.scalar(select(SheetsSyncOutbox).where(SheetsSyncOutbox.lead_id == lead.id))
    assert row.status == "SYNCED"
    assert row.synced_at is not None

def test_process_outbox_records_failure_and_backs_off(db, monkeypatch):
    lead = _lead(db)
    sheets_svc.enqueue(db, lead)
    db.commit()
    def boom(settings, values):
        raise RuntimeError("sheets api down")
    monkeypatch.setattr(sheets_svc, "_append_row", boom)
    settings = get_settings().model_copy(update={
        "google_sheets_enabled": True, "google_sheets_spreadsheet_id": "sheet123",
        "google_sheets_service_account_json": '{"fake":"creds"}'})
    result = sheets_svc.process_outbox(db, settings)
    assert result == {"synced": 0, "failed": 1}
    row = db.scalar(select(SheetsSyncOutbox).where(SheetsSyncOutbox.lead_id == lead.id))
    assert row.status == "FAILED" and row.attempt_count == 1 and "sheets api down" in row.last_error
    # Immediate retry within backoff window does nothing (skipped, not a second failure).
    result2 = sheets_svc.process_outbox(db, settings)
    assert result2 == {"synced": 0, "failed": 0}
    db.refresh(row)
    assert row.attempt_count == 1

def test_process_outbox_stops_retrying_after_max_attempts(db, monkeypatch):
    lead = _lead(db)
    row = sheets_svc.enqueue(db, lead)
    row.status = "FAILED"
    row.attempt_count = sheets_svc.MAX_ATTEMPTS
    row.updated_at = sheets_svc.now() - timedelta(hours=2)
    db.commit()
    calls = []
    monkeypatch.setattr(sheets_svc, "_append_row", lambda settings, values: calls.append(1))
    settings = get_settings().model_copy(update={
        "google_sheets_enabled": True, "google_sheets_spreadsheet_id": "sheet123",
        "google_sheets_service_account_json": '{"fake":"creds"}'})
    sheets_svc.process_outbox(db, settings)
    assert calls == []  # permanently failed rows are not retried automatically

def test_retry_failed_resets_pending_rows(db):
    lead = _lead(db)
    row = sheets_svc.enqueue(db, lead)
    row.status = "FAILED"; row.attempt_count = 5
    db.commit()
    n = sheets_svc.retry_failed(db)
    assert n == 1
    db.refresh(row)
    assert row.status == "PENDING" and row.attempt_count == 0

def test_waitlist_signup_enqueues_sheets_outbox_row(client, db):
    p = {"email": "sheetstest@example.com", "name": "S", "investor_type": "retail", "markets": "both",
         "consent": True, "website": "", "campaign": "spring", "page_path": "/"}
    r = client.post("/api/waitlist", json=p)
    assert r.status_code == 200
    lead = db.scalar(select(WaitlistLead).where(WaitlistLead.email == "sheetstest@example.com"))
    outbox_row = db.scalar(select(SheetsSyncOutbox).where(SheetsSyncOutbox.lead_id == lead.id))
    assert outbox_row is not None and outbox_row.status == "PENDING"
    assert lead.campaign == "spring" and lead.page_path == "/"

def test_public_highlights_is_unauthenticated(client):
    r = client.get("/api/public/highlights")
    assert r.status_code == 200
    assert "ipos" in r.json()

def test_clerk_session_bridge_503_when_not_configured(client):
    # CLERK_PUBLISHABLE_KEY is unset in the test environment - the endpoint
    # must fail closed, not pretend to authenticate anyone.
    r = client.post("/api/auth/clerk-session", json={"session_token": "whatever"})
    assert r.status_code == 503

# ---------- Clerk session-token verification (self-signed JWKS, no live Clerk needed) ----------

import base64, json as _json
import pytest

def _rsa_keypair():
    from cryptography.hazmat.primitives.asymmetric import rsa
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)

def _jwk_from_public_key(pub, kid):
    import jwt
    return {**_json.loads(jwt.algorithms.RSAAlgorithm(jwt.algorithms.RSAAlgorithm.SHA256).to_jwk(pub)), "kid": kid, "use": "sig", "alg": "RS256"}

def test_frontend_api_host_decodes_publishable_key():
    encoded = base64.b64encode(b"myapp.clerk.accounts.dev$").decode().rstrip("=")
    pk = f"pk_test_{encoded}"
    assert clerk_svc.frontend_api_host(pk) == "myapp.clerk.accounts.dev"

def test_frontend_api_host_rejects_malformed_key():
    with pytest.raises(clerk_svc.InvalidSessionToken):
        clerk_svc.frontend_api_host("not-a-real-key")

def test_verify_session_token_accepts_validly_signed_token(monkeypatch):
    import jwt
    priv = _rsa_keypair()
    jwk = _jwk_from_public_key(priv.public_key(), "test-kid-1")
    monkeypatch.setattr(clerk_svc, "_get_jwks", lambda host: {"keys": [jwk]})
    token = jwt.encode({"sub": "user_123", "iat": int(clerk_svc.time.time())}, priv, algorithm="RS256", headers={"kid": "test-kid-1"})
    encoded = base64.b64encode(b"test.clerk.accounts.dev$").decode().rstrip("=")
    claims = clerk_svc.verify_session_token(token, f"pk_test_{encoded}")
    assert claims["sub"] == "user_123"

def test_verify_session_token_rejects_wrong_signing_key(monkeypatch):
    import jwt
    priv = _rsa_keypair()
    other_priv = _rsa_keypair()
    jwk = _jwk_from_public_key(other_priv.public_key(), "test-kid-2")  # JWKS advertises a DIFFERENT key
    monkeypatch.setattr(clerk_svc, "_get_jwks", lambda host: {"keys": [jwk]})
    token = jwt.encode({"sub": "user_456"}, priv, algorithm="RS256", headers={"kid": "test-kid-2"})
    encoded = base64.b64encode(b"test.clerk.accounts.dev$").decode().rstrip("=")
    with pytest.raises(clerk_svc.InvalidSessionToken):
        clerk_svc.verify_session_token(token, f"pk_test_{encoded}")

def test_verify_session_token_rejects_unknown_kid(monkeypatch):
    import jwt
    priv = _rsa_keypair()
    monkeypatch.setattr(clerk_svc, "_get_jwks", lambda host: {"keys": []})
    token = jwt.encode({"sub": "user_789"}, priv, algorithm="RS256", headers={"kid": "missing-kid"})
    encoded = base64.b64encode(b"test.clerk.accounts.dev$").decode().rstrip("=")
    with pytest.raises(clerk_svc.InvalidSessionToken):
        clerk_svc.verify_session_token(token, f"pk_test_{encoded}")
