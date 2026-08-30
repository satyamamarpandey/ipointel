"""Beta access: magic-link login, invite/disable admin actions, session
protection of /app and the dashboard API, CSRF on logout, audit logging."""
import itertools
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from app.models import WaitlistLead, LoginToken, AuthSession, AdminAuditLog
from app.services import auth as auth_svc

_ref = itertools.count()

def _lead(db, **kw):
    defaults = dict(email=f"beta{next(_ref)}@example.com", referral_code=f"BREF{next(_ref)}",
                     unsubscribe_token=f"btok{next(_ref)}", access_status="WAITLISTED")
    defaults.update(kw)
    lead = WaitlistLead(**defaults)
    db.add(lead); db.commit(); db.refresh(lead)
    return lead

# ---------- page/API protection ----------

def test_app_redirects_to_login_when_unauthenticated(client):
    r = client.get("/app", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"].startswith("/login")

def test_app_loads_when_authenticated(authed_client):
    r = authed_client.get("/app")
    assert r.status_code == 200

def test_dashboard_api_401_without_session(client):
    assert client.get("/api/ipos").status_code == 401
    assert client.get("/api/source-health").status_code == 401
    assert client.get("/api/track-record").status_code == 401

def test_waitlist_signup_alone_does_not_grant_dashboard_access(client, db):
    p = {"email": "notyet@example.com", "name": "N", "investor_type": "retail", "markets": "both", "consent": True, "website": ""}
    assert client.post("/api/waitlist", json=p).status_code == 200
    lead = db.scalar(select(WaitlistLead).where(WaitlistLead.email == "notyet@example.com"))
    assert lead.access_status == "WAITLISTED"
    r = client.post("/api/auth/request-login", json={"email": "notyet@example.com"})
    assert r.status_code == 200  # generic response either way
    assert db.scalar(select(LoginToken).where(LoginToken.lead_id == lead.id)) is None  # but no token was actually issued

# ---------- magic link login ----------

def test_invited_user_can_request_and_redeem_login_link(client, db):
    lead = _lead(db, access_status="INVITED")
    raw = auth_svc.create_login_token(db, lead); db.commit()
    r = client.get(f"/auth/callback?token={raw}", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/app"
    assert auth_svc.SESSION_COOKIE in r.cookies
    db.refresh(lead)
    assert lead.access_status == "ACTIVE"  # first successful login activates an invited user
    assert lead.last_login_at is not None

def test_login_token_is_single_use(client, db):
    lead = _lead(db, access_status="ACTIVE")
    raw = auth_svc.create_login_token(db, lead); db.commit()
    first = client.get(f"/auth/callback?token={raw}", follow_redirects=False)
    assert first.status_code in (302, 307) and first.headers["location"] == "/app"
    second = client.get(f"/auth/callback?token={raw}", follow_redirects=False)
    assert second.headers["location"] == "/login?error=already_used"

def test_login_token_expiry_is_enforced(db):
    lead = _lead(db, access_status="ACTIVE")
    raw = auth_svc.create_login_token(db, lead)
    row = db.scalar(select(LoginToken).where(LoginToken.lead_id == lead.id))
    row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()
    result_lead, err = auth_svc.redeem_login_token(db, raw)
    assert result_lead is None and err == "expired"

def test_disabled_user_cannot_redeem_login_link(client, db):
    lead = _lead(db, access_status="DISABLED")
    raw = auth_svc.create_login_token(db, lead); db.commit()
    r = client.get(f"/auth/callback?token={raw}", follow_redirects=False)
    assert r.headers["location"] == "/login?error=disabled"

def test_disabled_user_session_is_rejected_even_if_not_expired(db):
    lead = _lead(db, access_status="ACTIVE")
    session_token = auth_svc.create_session(db, lead); db.commit()
    assert auth_svc.get_lead_from_session(db, session_token) is not None
    lead.access_status = "DISABLED"; db.commit()
    assert auth_svc.get_lead_from_session(db, session_token) is None

# ---------- admin invite / disable / enable ----------

def test_admin_invite_sets_status_and_creates_login_token(admin_client, db):
    lead = _lead(db, access_status="WAITLISTED")
    r = admin_client.post(f"/api/admin/users/{lead.id}/invite")
    assert r.status_code == 200 and r.json()["access_status"] == "INVITED"
    db.refresh(lead)
    assert lead.access_status == "INVITED"
    assert db.scalar(select(LoginToken).where(LoginToken.lead_id == lead.id, LoginToken.purpose == "invite")) is not None

def test_admin_disable_revokes_active_sessions(admin_client, db):
    lead = _lead(db, access_status="ACTIVE")
    session_token = auth_svc.create_session(db, lead); db.commit()
    assert auth_svc.get_lead_from_session(db, session_token) is not None
    r = admin_client.post(f"/api/admin/users/{lead.id}/disable")
    assert r.status_code == 200 and r.json()["access_status"] == "DISABLED"
    assert auth_svc.get_lead_from_session(db, session_token) is None

def test_admin_enable_reactivates_disabled_user(admin_client, db):
    lead = _lead(db, access_status="DISABLED")
    r = admin_client.post(f"/api/admin/users/{lead.id}/enable")
    assert r.status_code == 200 and r.json()["access_status"] == "INVITED"

def test_admin_endpoints_require_token(client, db):
    lead = _lead(db)
    assert client.get("/api/admin/users").status_code == 403
    assert client.post(f"/api/admin/users/{lead.id}/invite").status_code == 403
    assert client.post(f"/api/admin/users/{lead.id}/disable").status_code == 403

def test_admin_actions_are_audit_logged(admin_client, db):
    lead = _lead(db)
    admin_client.post(f"/api/admin/users/{lead.id}/invite")
    admin_client.post(f"/api/admin/users/{lead.id}/disable")
    rows = db.scalars(select(AdminAuditLog).order_by(AdminAuditLog.created_at.asc())).all()
    actions = [r.action for r in rows]
    assert "invite_created" in actions and "user_disabled" in actions

# ---------- CSRF on logout ----------

def test_logout_without_csrf_header_is_rejected(authed_client):
    r = authed_client.post("/api/auth/logout")
    assert r.status_code == 403

def test_logout_with_matching_csrf_succeeds(client, db):
    lead = _lead(db, access_status="ACTIVE")
    session_token = auth_svc.create_session(db, lead); db.commit()
    client.cookies.set(auth_svc.SESSION_COOKIE, session_token)
    client.cookies.set("csrf_token", "matching-value")
    r = client.post("/api/auth/logout", headers={"X-CSRF-Token": "matching-value"})
    assert r.status_code == 200
    assert auth_svc.get_lead_from_session(db, session_token) is None
