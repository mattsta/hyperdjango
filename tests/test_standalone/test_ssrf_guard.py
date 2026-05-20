"""SSRF guard — hyperdjango.net.validate_public_url.

Uses literal IPs (getaddrinfo resolves them to themselves) so the test needs no
network. Covers the internal ranges an attacker points a user-supplied URL at.

Run: uv run pytest tests/test_standalone/test_ssrf_guard.py -q
"""

import pytest

from hyperdjango.net import UnsafeURLError, _ip_is_blocked, validate_public_url


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "169.254.169.254",  # cloud metadata (link-local)
        "10.0.0.5",  # private
        "172.16.0.1",  # private
        "192.168.1.1",  # private
        "0.0.0.0",  # unspecified
        "100.64.0.1",  # CGNAT
        "::1",  # IPv6 loopback
        "fe80::1",  # IPv6 link-local
        "::ffff:127.0.0.1",  # IPv4-mapped loopback (bypass attempt)
        "not-an-ip",  # unparseable → refuse
    ],
)
def test_internal_ips_blocked(ip):
    assert _ip_is_blocked(ip) is True


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34"])
def test_public_ips_allowed(ip):
    assert _ip_is_blocked(ip) is False


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # metadata SSRF
        "http://127.0.0.1:6379/",  # internal Redis
        "http://10.0.0.5/admin",  # private range
        "http://[::1]:8000/",  # IPv6 loopback
        "file:///etc/passwd",  # non-http scheme
        "gopher://evil/",  # non-http scheme
        "http:///nohost",  # no host
    ],
)
def test_unsafe_urls_refused(url):
    with pytest.raises(UnsafeURLError):
        validate_public_url(url)


def test_public_literal_ip_url_allowed():
    # A public literal IP is allowed (no DNS needed).
    assert validate_public_url("http://8.8.8.8/") == "http://8.8.8.8/"
