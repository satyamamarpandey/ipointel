import httpx
from app.services import email_provider as ep
from app.services import webhooks as webhooks_svc
from app.services.email_queue import enqueue, process_queue
from app.services import alerts as alerts_svc
from app.models import WaitlistLead, IPO, ScoreSnapshot, EmailMessage
from sqlalchemy import select

# ---------- provider abstraction ----------

def test_disabled_provider_never_pretends_to_send():
    p = ep.DisabledEmailProvider()
    r = p.send("a@example.com", "s", "<p>h</p>", "h")
    assert r.ok is False
    ok, msg = p.health_check()
    assert ok is False

def test_get_provider_disabled_when_email_disabled():
    class S:
        enable_email = False
        email_provider = "resend"
        resend_api_key = "re_x"
        freeresend_base_url = ""
        freeresend_api_key = ""
        resend_from = "a@b.com"
        email_from = ""
    assert isinstance(ep.get_provider(S()), ep.DisabledEmailProvider)

def test_get_provider_disabled_when_no_api_key():
    class S:
        enable_email = True
        email_provider = "resend"
        resend_api_key = ""
        freeresend_base_url = ""
        freeresend_api_key = ""
        resend_from = "a@b.com"
        email_from = ""
    assert isinstance(ep.get_provider(S()), ep.DisabledEmailProvider)

def test_get_provider_mailpit_needs_no_credential():
    class S:
        enable_email = True
        email_provider = "mailpit"
        resend_api_key = ""
        freeresend_base_url = ""
        freeresend_api_key = ""
        resend_from = "a@b.com"
        email_from = "IPO Intelligence <noreply@ipo.local>"
        smtp_host = "127.0.0.1"
        smtp_port = 1025
    p = ep.get_provider(S())
    assert isinstance(p, ep.MailpitEmailProvider)
    assert not isinstance(p, ep.DisabledEmailProvider)

def test_get_provider_freeresend_disabled_without_base_url_or_key():
    class S:
        enable_email = True
        email_provider = "freeresend"
        resend_api_key = ""
        freeresend_base_url = ""
        freeresend_api_key = ""
        resend_from = "a@b.com"
        email_from = ""
    assert isinstance(ep.get_provider(S()), ep.DisabledEmailProvider)

def test_get_provider_unknown_value_is_disabled_not_silent_fallback():
    class S:
        enable_email = True
        email_provider = "carrier-pigeon"
        resend_api_key = "re_x"
        freeresend_base_url = "http://x"
        freeresend_api_key = "k"
        resend_from = "a@b.com"
        email_from = ""
    assert isinstance(ep.get_provider(S()), ep.DisabledEmailProvider)

# ---------- Mailpit / SMTP provider ----------
# These hit a REAL local Mailpit instance (127.0.0.1:1025 SMTP / 127.0.0.1:8025
# API) when one happens to be running - exactly what STEP 20 of the email spec
# asks for ("use its local API in integration tests"). They skip cleanly in any
# environment (e.g. CI) where Mailpit isn't up, so the suite never depends on
# an external process to pass.
import socket, uuid
import httpx as _httpx
import pytest

def _mailpit_up():
    try:
        with socket.create_connection(("127.0.0.1", 8025), timeout=0.5):
            return True
    except OSError:
        return False

MAILPIT_UP = _mailpit_up()
requires_mailpit = pytest.mark.skipif(not MAILPIT_UP, reason="Mailpit not running on 127.0.0.1:8025/1025")

@requires_mailpit
def test_mailpit_provider_send_lands_in_real_inbox_via_api():
    provider = ep.MailpitEmailProvider("127.0.0.1", 1025, "IPO Intelligence <noreply@ipo.local>")
    unique_subject = f"pytest mailpit check {uuid.uuid4()}"
    to = "pytest@example.com"
    r = provider.send(to, unique_subject, "<p>hello from pytest</p><a href='http://x/unsubscribe'>Unsubscribe</a>", "hello from pytest")
    assert r.ok is True and r.provider_message_id
    found = None
    for _ in range(20):
        listing = _httpx.get("http://127.0.0.1:8025/api/v1/search", params={"query": f'subject:"{unique_subject}"'}, timeout=2).json()
        if listing.get("messages"):
            found = listing["messages"][0]
            break
        import time; time.sleep(0.25)
    assert found is not None, "message never showed up in Mailpit within 5s"
    detail = _httpx.get(f"http://127.0.0.1:8025/api/v1/message/{found['ID']}", timeout=2).json()
    assert detail["Subject"] == unique_subject
    assert detail["To"][0]["Address"] == to
    assert "Unsubscribe" in detail["HTML"]
    _httpx.request("DELETE", "http://127.0.0.1:8025/api/v1/messages", json={"IDs": [found["ID"]]}, timeout=2)

def test_smtp_provider_send_failure_when_nothing_listening():
    provider = ep.MailpitEmailProvider("127.0.0.1", 1, "IPO Intelligence <noreply@ipo.local>", timeout=1.0)
    r = provider.send("qa@example.com", "Hi", "<p>hi</p>", "hi")
    assert r.ok is False and r.retryable is True

def test_smtp_provider_health_check_reflects_listener_state():
    provider = ep.MailpitEmailProvider("127.0.0.1", 1, "x@y.com", timeout=1.0)
    ok, _ = provider.health_check()
    assert ok is False

def _mock_transport(status_code, json_body=None, text_body=""):
    def handler(request):
        if json_body is not None:
            return httpx.Response(status_code, json=json_body)
        return httpx.Response(status_code, text=text_body)
    return httpx.MockTransport(handler)

def test_resend_provider_send_success(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    provider = ep.ResendEmailProvider("re_test", "from@example.com", transport=_mock_transport(200, {"id": "msg_123"}))
    r = provider.send("to@example.com", "Subject", "<p>hi</p>", "hi")
    assert r.ok is True and r.provider_message_id == "msg_123"

def test_resend_provider_auth_error_not_retryable(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    provider = ep.ResendEmailProvider("bad_key", "from@example.com", transport=_mock_transport(401, {"message": "invalid key"}))
    r = provider.send("to@example.com", "Subject", "<p>hi</p>", "hi")
    assert r.ok is False and r.retryable is False and "auth" in r.error.lower()

def test_resend_provider_validation_error_not_retryable(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    provider = ep.ResendEmailProvider("re_test", "from@example.com", transport=_mock_transport(422, {"message": "invalid to address"}))
    r = provider.send("not-an-email", "Subject", "<p>hi</p>", "hi")
    assert r.ok is False and r.retryable is False

def test_resend_provider_server_error_is_retryable(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    provider = ep.ResendEmailProvider("re_test", "from@example.com", max_retries=2, transport=_mock_transport(500, text_body="server error"))
    r = provider.send("to@example.com", "Subject", "<p>hi</p>", "hi")
    assert r.ok is False and r.retryable is True

# ---------- webhook signature verification ----------

def test_webhook_rejects_missing_secret():
    assert webhooks_svc.verify_svix_signature("", "id1", "1700000000", "v1,abc", b"{}") is False

def test_webhook_valid_signature_accepted():
    import base64, hashlib, hmac, time
    secret_bytes = b"0" * 32
    secret = "whsec_" + base64.b64encode(secret_bytes).decode()
    svix_id, ts, body = "msg_1", str(int(time.time())), b'{"type":"email.sent"}'
    signed = f"{svix_id}.{ts}.".encode() + body
    sig = base64.b64encode(hmac.new(secret_bytes, signed, hashlib.sha256).digest()).decode()
    assert webhooks_svc.verify_svix_signature(secret, svix_id, ts, f"v1,{sig}", body) is True

def test_webhook_invalid_signature_rejected():
    import base64, time
    secret = "whsec_" + base64.b64encode(b"0" * 32).decode()
    assert webhooks_svc.verify_svix_signature(secret, "msg_1", str(int(time.time())), "v1,bogus", b"{}") is False

def test_webhook_stale_timestamp_rejected():
    import base64, hashlib, hmac
    secret_bytes = b"1" * 32
    secret = "whsec_" + base64.b64encode(secret_bytes).decode()
    old_ts = "1000000000"
    body = b'{"type":"email.sent"}'
    signed = f"msg_1.{old_ts}.".encode() + body
    sig = base64.b64encode(hmac.new(secret_bytes, signed, hashlib.sha256).digest()).decode()
    assert webhooks_svc.verify_svix_signature(secret, "msg_1", old_ts, f"v1,{sig}", body) is False

# ---------- queue + suppression + preferences (DB-backed) ----------

import itertools
_ref_counter = itertools.count()

def _lead(db, **kw):
    defaults = dict(email="q@example.com", name="Q", referral_code=f"REF{next(_ref_counter)}", unsubscribe_token="tok123", consent=True)
    defaults.update(kw)
    lead = WaitlistLead(**defaults)
    db.add(lead); db.commit(); db.refresh(lead)
    return lead

def test_enqueue_is_idempotent(db):
    lead = _lead(db)
    a = enqueue(db, lead, "welcome", ep.PRIORITY_TRANSACTIONAL)
    db.commit()
    b = enqueue(db, lead, "welcome", ep.PRIORITY_TRANSACTIONAL)
    assert a is not None and b is None
    n = db.scalar(select(EmailMessage).where(EmailMessage.lead_id == lead.id))
    assert n is not None

def test_process_queue_disabled_provider_leaves_message_queued(db, monkeypatch):
    # Spec requirement: provider unavailable -> signup still succeeds, email stays queued/retryable, never silently dropped.
    from app.config import get_settings
    monkeypatch.setenv("ENABLE_EMAIL", "false")
    get_settings.cache_clear()
    lead = _lead(db)
    enqueue(db, lead, "welcome", ep.PRIORITY_TRANSACTIONAL)
    db.commit()
    result = process_queue(db)
    assert result["sent"] == 0 and result["failed"] == 0
    msg = db.scalar(select(EmailMessage).where(EmailMessage.lead_id == lead.id))
    assert msg.status == ep.QUEUED and msg.attempt_count == 1
    get_settings.cache_clear()

def test_process_queue_sends_with_fake_provider(db, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("ENABLE_EMAIL", "true")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    get_settings.cache_clear()
    class Fake:
        def send(self, *a, **kw): return ep.SendResult(ok=True, provider_message_id="fake_1")
        def health_check(self): return True, "ok"
    monkeypatch.setattr("app.services.email_queue.ep.get_provider", lambda s: Fake())
    lead = _lead(db, email="fake@example.com", unsubscribe_token="tokfake")
    enqueue(db, lead, "welcome", ep.PRIORITY_TRANSACTIONAL)
    db.commit()
    result = process_queue(db)
    assert result["sent"] == 1
    msg = db.scalar(select(EmailMessage).where(EmailMessage.lead_id == lead.id))
    assert msg.status == ep.SENT and msg.provider_message_id == "fake_1"
    get_settings.cache_clear()

def test_process_queue_skips_suppressed_lead(db, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("ENABLE_EMAIL", "true")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    get_settings.cache_clear()
    class Fake:
        def send(self, *a, **kw): return ep.SendResult(ok=True, provider_message_id="x")
        def health_check(self): return True, "ok"
    monkeypatch.setattr("app.services.email_queue.ep.get_provider", lambda s: Fake())
    lead = _lead(db, email="sup@example.com", unsubscribe_token="toksup", suppressed=True, suppressed_reason="bounced")
    enqueue(db, lead, "welcome", ep.PRIORITY_TRANSACTIONAL)
    db.commit()
    result = process_queue(db)
    assert result["sent"] == 0 and result["skipped_suppressed"] == 1
    msg = db.scalar(select(EmailMessage).where(EmailMessage.lead_id == lead.id))
    assert msg.status == ep.SUPPRESSED
    get_settings.cache_clear()

def test_process_queue_skips_unsubscribed_for_non_transactional(db, monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("ENABLE_EMAIL", "true")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    get_settings.cache_clear()
    class Fake:
        def send(self, *a, **kw): return ep.SendResult(ok=True, provider_message_id="x")
        def health_check(self): return True, "ok"
    monkeypatch.setattr("app.services.email_queue.ep.get_provider", lambda s: Fake())
    lead = _lead(db, email="unsub@example.com", unsubscribe_token="tokunsub", consent=False)
    enqueue(db, lead, "score_alert", ep.PRIORITY_SCORE_ALERT)
    db.commit()
    result = process_queue(db)
    assert result["sent"] == 0
    msg = db.scalar(select(EmailMessage).where(EmailMessage.lead_id == lead.id))
    assert msg.status == ep.UNSUBSCRIBED
    get_settings.cache_clear()

def test_process_queue_respects_priority_order(db, monkeypatch):
    from app.config import get_settings
    from datetime import datetime, timezone, timedelta
    monkeypatch.setenv("ENABLE_EMAIL", "true")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    get_settings.cache_clear()
    # A synchronous Fake provider can process both messages within the same
    # wall-clock tick, making sent_at a tie that SQLite breaks by row order
    # rather than send order - force strictly increasing timestamps so
    # ordering by sent_at reflects actual processing order.
    tick = itertools.count()
    base = datetime.now(timezone.utc)
    monkeypatch.setattr("app.services.email_queue.now", lambda: base + timedelta(seconds=next(tick)))
    order = []
    class Fake:
        def send(self, to, subject, html, text, headers=None):
            order.append(subject); return ep.SendResult(ok=True, provider_message_id="x")
        def health_check(self): return True, "ok"
    monkeypatch.setattr("app.services.email_queue.ep.get_provider", lambda s: Fake())
    lead = _lead(db, email="order@example.com", unsubscribe_token="tokorder")
    enqueue(db, lead, "welcome", ep.PRIORITY_DIGEST, dedupe_key="d1")       # queued first, lower priority (higher number)
    enqueue(db, lead, "welcome", ep.PRIORITY_TRANSACTIONAL, dedupe_key="d2")  # queued second, highest priority
    db.commit()
    process_queue(db)
    assert len(order) == 2
    msgs = db.scalars(select(EmailMessage).where(EmailMessage.lead_id == lead.id).order_by(EmailMessage.sent_at.asc())).all()
    assert msgs[0].dedupe_key == "d2"  # the transactional one, despite being queued second, sends first
    assert msgs[0].priority < msgs[1].priority

def test_webhook_bounce_suppresses_lead(db):
    lead = _lead(db, email="bounce@example.com", unsubscribe_token="tokbounce")
    msg = EmailMessage(lead_id=lead.id, email=lead.email, template="welcome", dedupe_key="welcome",
                        provider_message_id="pmid_1", status=ep.SENT)
    db.add(msg); db.commit()
    webhooks_svc.handle_event(db, {"type": "email.bounced", "data": {"email_id": "pmid_1", "to": ["bounce@example.com"], "reason": "hard bounce"}})
    db.refresh(lead); db.refresh(msg)
    assert lead.suppressed is True and lead.suppressed_reason == "bounced"
    assert msg.status == ep.BOUNCED

def test_webhook_complaint_suppresses_lead(db):
    lead = _lead(db, email="complain@example.com", unsubscribe_token="tokcomplain")
    msg = EmailMessage(lead_id=lead.id, email=lead.email, template="score_alert", dedupe_key="1",
                        provider_message_id="pmid_2", status=ep.SENT)
    db.add(msg); db.commit()
    webhooks_svc.handle_event(db, {"type": "email.complained", "data": {"email_id": "pmid_2", "to": ["complain@example.com"]}})
    db.refresh(lead)
    assert lead.suppressed is True and lead.suppressed_reason == "complained"

def test_webhook_delivered_updates_status(db):
    lead = _lead(db, email="delivered@example.com", unsubscribe_token="tokdeliv")
    msg = EmailMessage(lead_id=lead.id, email=lead.email, template="welcome", dedupe_key="welcome",
                        provider_message_id="pmid_3", status=ep.SENT)
    db.add(msg); db.commit()
    webhooks_svc.handle_event(db, {"type": "email.delivered", "data": {"email_id": "pmid_3"}})
    db.refresh(msg)
    assert msg.status == ep.DELIVERED and msg.delivered_at is not None

# ---------- alert materiality / queueing ----------

def test_queue_score_alerts_respects_preferences(db):
    ipo = IPO(external_key="e1", company="AlertCo", country="India", currency="INR")
    db.add(ipo); db.commit(); db.refresh(ipo)
    sc = ScoreSnapshot(ipo_id=ipo.id, model_version="v", overall_score=75, listing_score=70, long_term_score=72,
                        confidence=80, listing_gain_probability=70, long_term_outperform_probability=70,
                        recommendation="INVEST SELECTIVELY", horizon="BOTH", valuation_label="FAIR", pillars={}, rationale=[], risks=[], what_changes_verdict=[])
    db.add(sc); db.commit(); db.refresh(sc)
    wants = _lead(db, email="wants@example.com", unsubscribe_token="tokwants", alert_score_change=True)
    optout = _lead(db, email="optout@example.com", unsubscribe_token="tokoptout", alert_score_change=False)
    result = alerts_svc.queue_score_alerts(db)
    db.commit()
    assert result["queued"] == 1
    assert db.scalar(select(EmailMessage).where(EmailMessage.lead_id == wants.id)) is not None
    assert db.scalar(select(EmailMessage).where(EmailMessage.lead_id == optout.id)) is None

def test_queue_score_alerts_filters_by_market(db):
    ipo = IPO(external_key="e2", company="USCo", country="United States", currency="USD")
    db.add(ipo); db.commit(); db.refresh(ipo)
    sc = ScoreSnapshot(ipo_id=ipo.id, model_version="v", overall_score=75, listing_score=70, long_term_score=72,
                        confidence=80, listing_gain_probability=70, long_term_outperform_probability=70,
                        recommendation="INVEST SELECTIVELY", horizon="BOTH", valuation_label="FAIR", pillars={}, rationale=[], risks=[], what_changes_verdict=[])
    db.add(sc); db.commit(); db.refresh(sc)
    india_only = _lead(db, email="india@example.com", unsubscribe_token="tokindia", markets="india")
    result = alerts_svc.queue_score_alerts(db)
    db.commit()
    assert db.scalar(select(EmailMessage).where(EmailMessage.lead_id == india_only.id)) is None
