"""SSRF guard for URLs the app extracts from parsed external content (SEC
atom-feed hrefs in sec.filing_text, NSE archive-page hrefs in
pipeline.ingest_nse_history) before fetching them itself. Hardcoded literal
endpoints (e.g. f"{BASE}/api/ipo-current-issue") don't need this - there is
nothing in them for an attacker or a compromised upstream response to
influence.

Blocks non-http(s) schemes and any resolved target in loopback/RFC1918/
link-local/multicast/reserved ranges, including the cloud metadata
addresses (169.254.169.254, AWS IMDSv2's fd00:ec2::254).

Known residual gap: this resolves the hostname once here and does not pin
that IP for the actual outbound connection httpx makes a moment later, so it
does not fully close classic DNS-rebinding (a DNS answer that changes
between this check and the real connection). Closing that needs a custom
httpx transport that resolves once and connects to the pinned address - not
implemented here; documented rather than silently claimed as solved.
"""
from __future__ import annotations
import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeUrlError(ValueError):
    """Raised when a URL extracted from external content would fetch an
    internal, loopback, or otherwise disallowed target."""


_BLOCKED_HOSTNAME_SUFFIXES = (".internal", ".local")
_BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal"}
_METADATA_IPS = {"169.254.169.254"}


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return True
    if str(ip) in _METADATA_IPS or str(ip).lower().startswith("fd00:ec2::254"):
        return True
    return False


def validate_outbound_url(url: str, *, allowed_hosts: set[str] | None = None) -> None:
    """Raises UnsafeUrlError if url should not be fetched. allowed_hosts, if
    given, is an exact-match hostname allowlist (case-insensitive) - use it
    for any source where the expected domain set is known (SEC, NSE, etc)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrlError(f"Refusing non-http(s) scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise UnsafeUrlError("URL has no hostname")
    host_lower = host.lower()
    if host_lower in _BLOCKED_HOSTNAMES or any(host_lower.endswith(s) for s in _BLOCKED_HOSTNAME_SUFFIXES):
        raise UnsafeUrlError(f"Refusing blocked hostname: {host!r}")
    if allowed_hosts is not None and host_lower not in allowed_hosts:
        raise UnsafeUrlError(f"Host {host!r} is not in the expected allowlist: {sorted(allowed_hosts)}")

    literal_ip = None
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        pass  # host is a name, not a literal IP - resolve it below
    if literal_ip is not None:
        if _is_blocked_ip(literal_ip):
            raise UnsafeUrlError(f"Refusing literal IP target in a blocked range: {host}")
        return

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise UnsafeUrlError(f"Could not resolve host {host!r}: {e}")
    for family, _, _, _, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if _is_blocked_ip(ip):
            raise UnsafeUrlError(f"Refusing host {host!r} - resolves to blocked address {ip}")
