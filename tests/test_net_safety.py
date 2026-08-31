"""SSRF guard unit tests. No real network/DNS calls - CI must not depend on
live connectivity (see .github/workflows/ci.yml). The two "allows a real
hostname" cases mock socket.getaddrinfo to return a fixed public IP so the
full resolution code path is still exercised without an actual DNS lookup;
every other case is rejected before resolution would even happen."""
import socket
import pytest
from app.services.net_safety import validate_outbound_url, UnsafeUrlError

def _fake_public_getaddrinfo(host, port, **kw):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

def test_allows_https_public_host(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_public_getaddrinfo)
    validate_outbound_url("https://www.sec.gov/Archives/edgar/data/1/x.htm")

def test_allows_http_public_host_with_allowlist(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_public_getaddrinfo)
    validate_outbound_url("https://data.sec.gov/api/x.json", allowed_hosts={"data.sec.gov"})

def test_refuses_host_not_in_allowlist():
    with pytest.raises(UnsafeUrlError, match="allowlist"):
        validate_outbound_url("https://evil.example.com/x", allowed_hosts={"www.sec.gov"})

def test_refuses_file_scheme():
    with pytest.raises(UnsafeUrlError, match="scheme"):
        validate_outbound_url("file:///etc/passwd")

def test_refuses_ftp_scheme():
    with pytest.raises(UnsafeUrlError, match="scheme"):
        validate_outbound_url("ftp://example.com/x")

def test_refuses_localhost_hostname():
    with pytest.raises(UnsafeUrlError, match="blocked hostname"):
        validate_outbound_url("http://localhost/x")

def test_refuses_loopback_literal_ip():
    with pytest.raises(UnsafeUrlError, match="blocked range"):
        validate_outbound_url("http://127.0.0.1/x")

def test_refuses_ipv6_loopback_literal():
    with pytest.raises(UnsafeUrlError, match="blocked range"):
        validate_outbound_url("http://[::1]/x")

def test_refuses_rfc1918_literal_ip():
    for ip in ("http://10.0.0.5/x", "http://172.16.0.5/x", "http://192.168.1.1/x"):
        with pytest.raises(UnsafeUrlError, match="blocked range"):
            validate_outbound_url(ip)

def test_refuses_cloud_metadata_ip():
    with pytest.raises(UnsafeUrlError, match="blocked range"):
        validate_outbound_url("http://169.254.169.254/latest/meta-data/")

def test_refuses_link_local_literal_ip():
    with pytest.raises(UnsafeUrlError, match="blocked range"):
        validate_outbound_url("http://169.254.1.1/x")

def test_refuses_internal_tld_suffix():
    with pytest.raises(UnsafeUrlError, match="blocked hostname"):
        validate_outbound_url("http://foo.internal/x")

def test_refuses_dot_local_suffix():
    with pytest.raises(UnsafeUrlError, match="blocked hostname"):
        validate_outbound_url("http://myserver.local/x")

def test_refuses_unresolvable_host(monkeypatch):
    def _raise_gaierror(host, port, **kw):
        raise socket.gaierror("mocked: name or service not known")
    monkeypatch.setattr(socket, "getaddrinfo", _raise_gaierror)
    with pytest.raises(UnsafeUrlError, match="Could not resolve"):
        validate_outbound_url("https://this-domain-should-not-exist-12345.invalid/x")

def test_no_allowlist_still_blocks_private_ranges():
    with pytest.raises(UnsafeUrlError):
        validate_outbound_url("http://10.1.2.3/x")  # no allowlist passed - IP-range check still applies
