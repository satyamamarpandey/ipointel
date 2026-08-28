from __future__ import annotations
"""Email provider abstraction. Business logic (queueing, templates, alert
materiality) never talks to Resend/FreeResend/SMTP directly - it calls
get_provider().send(...) and reacts to a SendResult. Swapping providers is a
config change (EMAIL_PROVIDER), not a code change."""
import smtplib
import socket
import time
import uuid
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Protocol
import httpx

# Priority: lower number = sent first when the queue is constrained.
PRIORITY_TRANSACTIONAL = 0   # signup confirmation / welcome
PRIORITY_RECOMMENDATION = 1  # recommendation-band change (WATCH -> INVEST etc.)
PRIORITY_SCORE_ALERT = 2     # material score/valuation/red-flag change
PRIORITY_DIGEST = 3          # weekly digest

QUEUED, SENDING, SENT, DELIVERED, DELAYED, BOUNCED, COMPLAINED, FAILED, SUPPRESSED, UNSUBSCRIBED = (
    "QUEUED", "SENDING", "SENT", "DELIVERED", "DELAYED", "BOUNCED", "COMPLAINED", "FAILED", "SUPPRESSED", "UNSUBSCRIBED",
)

@dataclass
class SendResult:
    ok: bool
    provider_message_id: str = ""
    error: str = ""
    retryable: bool = True  # False for e.g. invalid address / invalid API key - no point retrying

class EmailProvider(Protocol):
    def send(self, to: str, subject: str, html: str, text: str, headers: dict | None = None) -> SendResult: ...
    def health_check(self) -> tuple[bool, str]: ...

def _strip_header_injection(s: str) -> str:
    return s.replace("\r", " ").replace("\n", " ").strip()

class DisabledEmailProvider:
    """EMAIL_ENABLED=false or no key configured. Never silently pretends to send."""
    def send(self, to, subject, html, text, headers=None) -> SendResult:
        return SendResult(ok=False, error="email provider disabled", retryable=True)
    def health_check(self):
        return False, "disabled"

class ResendEmailProvider:
    BASE = "https://api.resend.com"

    def __init__(self, api_key: str, from_addr: str, timeout: float = 15.0, max_retries: int = 3, transport: httpx.BaseTransport | None = None):
        self.api_key = api_key
        self.from_addr = from_addr
        self.timeout = timeout
        self.max_retries = max_retries
        self.transport = transport  # test hook only; None uses a real network connection

    def _client(self) -> httpx.Client:
        return httpx.Client(headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}, timeout=self.timeout, transport=self.transport)

    def send(self, to: str, subject: str, html: str, text: str, headers: dict | None = None) -> SendResult:
        payload = {"from": self.from_addr, "to": [to], "subject": _strip_header_injection(subject), "html": html, "text": text}
        if headers:
            payload["headers"] = headers
        last_error = ""
        for attempt in range(self.max_retries):
            try:
                with self._client() as c:
                    r = c.post(f"{self.BASE}/emails", json=payload)
                if r.status_code == 200 or r.status_code == 201:
                    return SendResult(ok=True, provider_message_id=r.json().get("id", ""))
                if r.status_code == 401 or r.status_code == 403:
                    return SendResult(ok=False, error=f"auth error ({r.status_code})", retryable=False)
                if r.status_code == 422:
                    return SendResult(ok=False, error=f"validation error: {r.text[:300]}", retryable=False)
                if r.status_code == 429:
                    last_error = "rate limited"
                    time.sleep(min(8, 2 ** attempt))
                    continue
                last_error = f"HTTP {r.status_code}: {r.text[:300]}"
                time.sleep(min(8, 2 ** attempt))
            except httpx.TimeoutException:
                last_error = "timeout"
                time.sleep(min(8, 2 ** attempt))
            except httpx.HTTPError as e:
                last_error = f"{type(e).__name__}: {e}"
                time.sleep(min(8, 2 ** attempt))
        return SendResult(ok=False, error=last_error or "unknown error", retryable=True)

    def health_check(self) -> tuple[bool, str]:
        try:
            with self._client() as c:
                r = c.get(f"{self.BASE}/domains")
            if r.status_code in (200, 201):
                return True, "ok"
            if r.status_code in (401, 403):
                return False, "auth error"
            return False, f"HTTP {r.status_code}"
        except httpx.HTTPError as e:
            return False, f"{type(e).__name__}: {e}"

class SMTPEmailProvider:
    """Generic SMTP transport - no vendor account required. Used directly for
    EMAIL_PROVIDER=smtp (a real mail server you already have credentials for),
    and subclassed by MailpitEmailProvider for the zero-credential local case."""
    def __init__(self, host: str, port: int, from_addr: str, use_tls: bool = False, username: str = "", password: str = "", timeout: float = 10.0):
        self.host = host
        self.port = port
        self.from_addr = from_addr
        self.use_tls = use_tls
        self.username = username
        self.password = password
        self.timeout = timeout

    def send(self, to: str, subject: str, html: str, text: str, headers: dict | None = None) -> SendResult:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = _strip_header_injection(subject)
        msg["From"] = self.from_addr
        msg["To"] = _strip_header_injection(to)
        message_id = f"<{uuid.uuid4()}@{self.host}>"
        msg["Message-ID"] = message_id
        if headers:
            for k, v in headers.items():
                msg[k] = _strip_header_injection(str(v))
        msg.attach(MIMEText(text, "plain"))
        msg.attach(MIMEText(html, "html"))
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as client:
                if self.use_tls:
                    client.starttls()
                if self.username:
                    client.login(self.username, self.password)
                client.sendmail(self.from_addr, [to], msg.as_string())
            return SendResult(ok=True, provider_message_id=message_id)
        except (smtplib.SMTPException, OSError, socket.error) as e:
            return SendResult(ok=False, error=f"{type(e).__name__}: {e}", retryable=True)

    def health_check(self) -> tuple[bool, str]:
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout):
                return True, "ok"
        except OSError as e:
            return False, f"{type(e).__name__}: {e}"

class MailpitEmailProvider(SMTPEmailProvider):
    """Mailpit (github.com/axllent/mailpit): a local SMTP catch-all + web
    inbox. No API key, no external account, no internet delivery - purely for
    development/CI so the full signup -> queue -> worker -> send pipeline can
    be exercised and inspected without any vendor dependency."""
    def __init__(self, host: str, port: int, from_addr: str, timeout: float = 10.0):
        super().__init__(host, port, from_addr, use_tls=False, username="", password="", timeout=timeout)

class FreeResendEmailProvider:
    """FreeResend (github.com/eibrahim/freeresend) speaks a Resend-compatible
    API, so this reuses the exact same request shape against a configurable
    self-hosted base URL. Not the default - see README 'Email' for why."""
    def __init__(self, base_url: str, api_key: str, from_addr: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.from_addr = from_addr
        self.timeout = timeout

    def send(self, to: str, subject: str, html: str, text: str, headers: dict | None = None) -> SendResult:
        payload = {"from": self.from_addr, "to": [to], "subject": _strip_header_injection(subject), "html": html, "text": text}
        try:
            with httpx.Client(headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}, timeout=self.timeout) as c:
                r = c.post(f"{self.base_url}/emails", json=payload)
            if r.status_code in (200, 201):
                return SendResult(ok=True, provider_message_id=r.json().get("id", ""))
            return SendResult(ok=False, error=f"HTTP {r.status_code}: {r.text[:300]}", retryable=r.status_code >= 500)
        except httpx.HTTPError as e:
            return SendResult(ok=False, error=f"{type(e).__name__}: {e}")

    def health_check(self) -> tuple[bool, str]:
        try:
            with httpx.Client(timeout=self.timeout) as c:
                r = c.get(f"{self.base_url}/health")
            return (r.status_code == 200), f"HTTP {r.status_code}"
        except httpx.HTTPError as e:
            return False, f"{type(e).__name__}: {e}"

def get_provider(settings) -> EmailProvider:
    if not settings.enable_email:
        return DisabledEmailProvider()
    from_addr = settings.email_from or settings.resend_from
    if settings.email_provider == "mailpit":
        return MailpitEmailProvider(settings.smtp_host, settings.smtp_port, from_addr)
    if settings.email_provider == "smtp":
        return SMTPEmailProvider(settings.smtp_host, settings.smtp_port, from_addr, use_tls=settings.smtp_tls, username=settings.smtp_user, password=settings.smtp_password)
    if settings.email_provider == "freeresend":
        if not (settings.freeresend_base_url and settings.freeresend_api_key):
            return DisabledEmailProvider()
        return FreeResendEmailProvider(settings.freeresend_base_url, settings.freeresend_api_key, from_addr)
    if settings.email_provider == "resend":
        if not settings.resend_api_key:
            return DisabledEmailProvider()
        return ResendEmailProvider(settings.resend_api_key, from_addr)
    return DisabledEmailProvider()  # unknown EMAIL_PROVIDER value - never silently fall through to a different provider
