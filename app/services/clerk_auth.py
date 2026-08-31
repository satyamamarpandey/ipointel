from __future__ import annotations
"""Clerk identity integration. Clerk answers "who is this user" (email,
name, verified status, social provider); our own WaitlistLead.access_status
answers "is this person allowed into the private beta" (see models.py).
Clerk is never trusted as the sole source of beta authorization - a brand
new Clerk identity always lands as WAITLISTED here, same as a direct
waitlist signup, and a DISABLED lead stays locked out even if Clerk
authenticates them successfully (see main.py's dashboard/session gating).

Webhook signature verification reuses services.webhooks.verify_svix_signature -
Clerk webhooks use the same Svix envelope Resend's already-tested verifier
implements, so there is no reason to duplicate that logic here."""
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import WaitlistLead

def now():
    return datetime.now(timezone.utc)

def sync_identity(db: Session, *, clerk_user_id: str, email: str, name: str = "", provider: str = "") -> WaitlistLead:
    """Get-or-create the local WaitlistLead for a Clerk identity. Never sets
    access_status to ACTIVE/INVITED here - only admin invite (or an existing
    local magic-link invite) can do that (see main.py admin_invite_user)."""
    email = email.strip().lower()
    lead = db.scalar(select(WaitlistLead).where(WaitlistLead.clerk_user_id == clerk_user_id)) if clerk_user_id else None
    if lead is None:
        lead = db.scalar(select(WaitlistLead).where(WaitlistLead.email == email))
    if lead is None:
        import secrets
        lead = WaitlistLead(email=email, name=name.strip(), consent=True, source="clerk",
                             referral_code=secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8],
                             unsubscribe_token=secrets.token_urlsafe(24),
                             clerk_user_id=clerk_user_id, identity_provider=provider)
        db.add(lead)
        db.flush()
        return lead
    if clerk_user_id and not lead.clerk_user_id:
        lead.clerk_user_id = clerk_user_id
        lead.identity_provider = provider
    if name and not lead.name:
        lead.name = name.strip()
    db.flush()
    return lead

def handle_user_deleted(db: Session, clerk_user_id: str) -> None:
    """Unlinks (never deletes) the local lead - WaitlistLead is the audit
    trail of who was ever granted/denied beta access, which must survive an
    identity-provider-side account deletion."""
    lead = db.scalar(select(WaitlistLead).where(WaitlistLead.clerk_user_id == clerk_user_id))
    if lead:
        lead.clerk_user_id = ""
        lead.identity_provider = ""
        db.flush()

# ---------------------------------------------------------------------------
# Session-token verification (Google/Apple sign-in bridge). Clerk's own
# session JWT is verified against Clerk's *public* JWKS (no secret key
# needed for this step - only the frontend API host, derived from the
# publishable key). This only ever succeeds against a real, configured Clerk
# application; with no CLERK_PUBLISHABLE_KEY set it is entirely inert.
# ---------------------------------------------------------------------------
import base64
import time

class InvalidSessionToken(Exception):
    pass

_jwks_cache: dict[str, tuple[float, dict]] = {}
_JWKS_TTL_SECONDS = 3600

def frontend_api_host(publishable_key: str) -> str:
    """Clerk publishable keys encode their frontend API host as
    base64("<host>$") after the pk_test_/pk_live_ prefix."""
    if not publishable_key or "_" not in publishable_key:
        raise InvalidSessionToken("malformed publishable key")
    encoded = publishable_key.split("_", 2)[-1]
    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        decoded = base64.b64decode(padded).decode()
    except Exception as e:
        raise InvalidSessionToken(f"could not decode publishable key: {e}")
    return decoded.rstrip("$")

def _get_jwks(host: str) -> dict:
    import httpx
    cached = _jwks_cache.get(host)
    if cached and time.time() - cached[0] < _JWKS_TTL_SECONDS:
        return cached[1]
    r = httpx.get(f"https://{host}/.well-known/jwks.json", timeout=10)
    r.raise_for_status()
    jwks = r.json()
    _jwks_cache[host] = (time.time(), jwks)
    return jwks

def verify_session_token(token: str, publishable_key: str) -> dict:
    """Verifies a Clerk session JWT's RS256 signature against Clerk's public
    JWKS and returns its claims. Raises InvalidSessionToken on any failure -
    callers must never trust an unverified token's claims (see
    main.clerk_session, which only reads `sub` from the return value)."""
    import jwt
    host = frontend_api_host(publishable_key)
    try:
        header = jwt.get_unverified_header(token)
        jwks = _get_jwks(host)
        key_data = next((k for k in jwks.get("keys", []) if k.get("kid") == header.get("kid")), None)
        if not key_data:
            raise InvalidSessionToken("no matching JWKS key for token")
        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(key_data)
        claims = jwt.decode(token, public_key, algorithms=["RS256"], options={"verify_aud": False})
        return claims
    except InvalidSessionToken:
        raise
    except Exception as e:
        raise InvalidSessionToken(f"{type(e).__name__}: {e}")
