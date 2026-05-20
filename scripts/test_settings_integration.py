"""
Tests for settings integration: conf.py settings wired into middleware and request.

Tests cover:
- SecurityHeadersMiddleware reads X_FRAME_OPTIONS, SECURE_CROSS_ORIGIN_OPENER_POLICY,
  SECURE_CSP, SECURE_REDIRECT_EXEMPT, SECURE_SSL_HOST, SECURE_PROXY_SSL_HEADER
- CSRFMiddleware reads CSRF_COOKIE_NAME, CSRF_COOKIE_DOMAIN, CSRF_COOKIE_PATH,
  CSRF_COOKIE_AGE, CSRF_HEADER_NAME, CSRF_TRUSTED_ORIGINS
- Request.is_secure reads SECURE_PROXY_SSL_HEADER
- Request.host reads USE_X_FORWARDED_HOST
- Request.port reads USE_X_FORWARDED_PORT
"""

# hyper-test: unit

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from hyperdjango.conf import DEFAULTS
from hyperdjango.request import Request
from hyperdjango.response import Response
from hyperdjango.standalone_middleware import (
    CSRFMiddleware,
    SecurityHeadersMiddleware,
)
from hyperdjango.testkit import check, finish, run_main


def run_async(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


async def dummy_handler(request):
    """Simple handler that returns 200 OK."""
    return Response.text("ok")


def make_request(**kwargs):
    """Create a test Request with given attributes."""
    defaults = {"method": "GET", "path": "/", "headers": {}}
    defaults.update(kwargs)
    return Request(**defaults)


class TestSecurityHeadersXFrameOptions(unittest.TestCase):
    """X_FRAME_OPTIONS setting controls X-Frame-Options header."""

    def test_default_deny(self):
        """Default X_FRAME_OPTIONS=DENY produces DENY header."""
        mw = SecurityHeadersMiddleware()
        req = make_request()
        resp = run_async(mw(req, dummy_handler))
        assert resp.headers.get("x-frame-options") == "DENY", resp.headers

    def test_sameorigin_from_setting(self):
        """X_FRAME_OPTIONS=SAMEORIGIN from conf produces SAMEORIGIN header."""
        with patch.dict(DEFAULTS, {"X_FRAME_OPTIONS": "SAMEORIGIN"}):
            mw = SecurityHeadersMiddleware()
            req = make_request()
            resp = run_async(mw(req, dummy_handler))
            assert resp.headers.get("x-frame-options") == "SAMEORIGIN", resp.headers

    def test_deny_from_setting(self):
        """X_FRAME_OPTIONS=DENY from conf produces DENY header."""
        with patch.dict(DEFAULTS, {"X_FRAME_OPTIONS": "DENY"}):
            mw = SecurityHeadersMiddleware()
            req = make_request()
            resp = run_async(mw(req, dummy_handler))
            assert resp.headers.get("x-frame-options") == "DENY", resp.headers

    def test_empty_disables_header(self):
        """X_FRAME_OPTIONS="" from conf omits the header."""
        with patch.dict(DEFAULTS, {"X_FRAME_OPTIONS": ""}):
            mw = SecurityHeadersMiddleware()
            req = make_request()
            resp = run_async(mw(req, dummy_handler))
            assert "x-frame-options" not in resp.headers, resp.headers

    def test_constructor_overrides_setting(self):
        """Constructor param overrides conf setting."""
        with patch.dict(DEFAULTS, {"X_FRAME_OPTIONS": "DENY"}):
            mw = SecurityHeadersMiddleware(frame_options="SAMEORIGIN")
            req = make_request()
            resp = run_async(mw(req, dummy_handler))
            assert resp.headers.get("x-frame-options") == "SAMEORIGIN", resp.headers


class TestSecurityHeadersCOOP(unittest.TestCase):
    """SECURE_CROSS_ORIGIN_OPENER_POLICY setting controls COOP header."""

    def test_default_same_origin(self):
        """Default COOP is same-origin."""
        mw = SecurityHeadersMiddleware()
        req = make_request()
        resp = run_async(mw(req, dummy_handler))
        assert resp.headers.get("cross-origin-opener-policy") == "same-origin"

    def test_custom_coop_from_setting(self):
        """Custom COOP from conf.py."""
        with patch.dict(
            DEFAULTS, {"SECURE_CROSS_ORIGIN_OPENER_POLICY": "same-origin-allow-popups"}
        ):
            mw = SecurityHeadersMiddleware()
            req = make_request()
            resp = run_async(mw(req, dummy_handler))
            assert (
                resp.headers.get("cross-origin-opener-policy")
                == "same-origin-allow-popups"
            )

    def test_empty_coop_disables(self):
        """Empty COOP disables the header."""
        with patch.dict(DEFAULTS, {"SECURE_CROSS_ORIGIN_OPENER_POLICY": ""}):
            mw = SecurityHeadersMiddleware()
            req = make_request()
            resp = run_async(mw(req, dummy_handler))
            assert "cross-origin-opener-policy" not in resp.headers


class TestSecurityHeadersCSP(unittest.TestCase):
    """SECURE_CSP setting controls Content-Security-Policy header."""

    def test_no_csp_by_default(self):
        """Default empty CSP dict means no CSP header."""
        mw = SecurityHeadersMiddleware()
        req = make_request()
        resp = run_async(mw(req, dummy_handler))
        assert "content-security-policy" not in resp.headers

    def test_csp_dict_from_setting(self):
        """CSP dict from conf.py builds directive string."""
        csp = {"default-src": "'self'", "img-src": "'self' data:"}
        with patch.dict(DEFAULTS, {"SECURE_CSP": csp}):
            mw = SecurityHeadersMiddleware()
            req = make_request()
            resp = run_async(mw(req, dummy_handler))
            header = resp.headers.get("content-security-policy")
            assert header is not None
            assert "default-src 'self'" in header
            assert "img-src 'self' data:" in header

    def test_csp_string_from_constructor(self):
        """CSP string passed to constructor used directly."""
        mw = SecurityHeadersMiddleware(csp="default-src 'none'")
        req = make_request()
        resp = run_async(mw(req, dummy_handler))
        assert resp.headers.get("content-security-policy") == "default-src 'none'"

    def test_csp_dict_from_constructor(self):
        """CSP dict passed to constructor builds directive string."""
        mw = SecurityHeadersMiddleware(
            csp={"script-src": "'self'", "style-src": "'unsafe-inline'"}
        )
        req = make_request()
        resp = run_async(mw(req, dummy_handler))
        header = resp.headers.get("content-security-policy")
        assert "script-src 'self'" in header
        assert "style-src 'unsafe-inline'" in header


class TestSecurityHeadersSSLRedirect(unittest.TestCase):
    """SSL redirect settings (SECURE_SSL_REDIRECT, SECURE_REDIRECT_EXEMPT, SECURE_SSL_HOST)."""

    def test_ssl_redirect_when_enabled(self):
        """SSL redirect sends 301 to HTTPS."""
        with patch.dict(DEFAULTS, {"SECURE_SSL_REDIRECT": True}):
            mw = SecurityHeadersMiddleware()
            req = make_request(headers={"host": "example.com"})
            resp = run_async(mw(req, dummy_handler))
            assert resp.status == 301
            assert "https://example.com/" in resp.headers.get("location", "")

    def test_ssl_redirect_with_custom_host(self):
        """SECURE_SSL_HOST redirects to specific host."""
        with patch.dict(
            DEFAULTS,
            {"SECURE_SSL_REDIRECT": True, "SECURE_SSL_HOST": "secure.example.com"},
        ):
            mw = SecurityHeadersMiddleware()
            req = make_request(path="/foo", headers={"host": "example.com"})
            resp = run_async(mw(req, dummy_handler))
            assert resp.status == 301
            assert "https://secure.example.com/foo" in resp.headers.get("location", "")

    def test_ssl_redirect_exempt_path(self):
        """SECURE_REDIRECT_EXEMPT skips SSL redirect for matching paths."""
        with patch.dict(
            DEFAULTS,
            {"SECURE_SSL_REDIRECT": True, "SECURE_REDIRECT_EXEMPT": [r"^/health"]},
        ):
            mw = SecurityHeadersMiddleware()
            req = make_request(path="/health/check", headers={"host": "example.com"})
            resp = run_async(mw(req, dummy_handler))
            assert resp.status == 200

    def test_ssl_redirect_non_exempt_path(self):
        """Non-exempt path still gets SSL redirected."""
        with patch.dict(
            DEFAULTS,
            {"SECURE_SSL_REDIRECT": True, "SECURE_REDIRECT_EXEMPT": [r"^/health"]},
        ):
            mw = SecurityHeadersMiddleware()
            req = make_request(path="/api/data", headers={"host": "example.com"})
            resp = run_async(mw(req, dummy_handler))
            assert resp.status == 301

    def test_ssl_redirect_skipped_when_secure(self):
        """Already-secure request skips SSL redirect."""
        with patch.dict(DEFAULTS, {"SECURE_SSL_REDIRECT": True}):
            mw = SecurityHeadersMiddleware()
            req = make_request(
                headers={"host": "example.com", "x-forwarded-proto": "https"},
                scope={"scheme": "https"},
            )
            resp = run_async(mw(req, dummy_handler))
            assert resp.status == 200


class TestSecurityHeadersProxySSL(unittest.TestCase):
    """SECURE_PROXY_SSL_HEADER in SecurityHeadersMiddleware."""

    def test_proxy_ssl_header_determines_secure(self):
        """Middleware uses SECURE_PROXY_SSL_HEADER to check is_secure for redirect."""
        with patch.dict(
            DEFAULTS,
            {
                "SECURE_SSL_REDIRECT": True,
                "SECURE_PROXY_SSL_HEADER": "X-Forwarded-Proto",
            },
        ):
            mw = SecurityHeadersMiddleware()
            req = make_request(
                headers={"host": "example.com", "x-forwarded-proto": "https"}
            )
            resp = run_async(mw(req, dummy_handler))
            # Should NOT redirect because proxy header says HTTPS
            assert resp.status == 200

    def test_proxy_ssl_header_not_https_redirects(self):
        """Non-HTTPS proxy header triggers redirect."""
        with patch.dict(
            DEFAULTS,
            {
                "SECURE_SSL_REDIRECT": True,
                "SECURE_PROXY_SSL_HEADER": "X-Forwarded-Proto",
            },
        ):
            mw = SecurityHeadersMiddleware()
            req = make_request(
                headers={"host": "example.com", "x-forwarded-proto": "http"}
            )
            resp = run_async(mw(req, dummy_handler))
            assert resp.status == 301


class TestCSRFCookieName(unittest.TestCase):
    """CSRF_COOKIE_NAME setting controls the cookie name."""

    def test_default_cookie_name(self):
        """Default cookie name from conf is 'csrftoken'."""
        mw = CSRFMiddleware()
        req = make_request(headers={})
        resp = run_async(mw(req, dummy_handler))
        # On GET, it sets the cookie
        cookie_header = resp.headers.get("set-cookie", "")
        assert "csrftoken=" in cookie_header, cookie_header

    def test_custom_cookie_name_from_setting(self):
        """Custom CSRF_COOKIE_NAME changes cookie name."""
        with patch.dict(DEFAULTS, {"CSRF_COOKIE_NAME": "my_csrf"}):
            mw = CSRFMiddleware()
            req = make_request(headers={})
            resp = run_async(mw(req, dummy_handler))
            cookie_header = resp.headers.get("set-cookie", "")
            assert "my_csrf=" in cookie_header, cookie_header

    def test_custom_cookie_name_from_constructor(self):
        """Constructor cookie_name overrides setting."""
        with patch.dict(DEFAULTS, {"CSRF_COOKIE_NAME": "from_setting"}):
            mw = CSRFMiddleware(cookie_name="from_constructor")
            req = make_request(headers={})
            resp = run_async(mw(req, dummy_handler))
            cookie_header = resp.headers.get("set-cookie", "")
            assert "from_constructor=" in cookie_header, cookie_header


class TestCSRFCookieAge(unittest.TestCase):
    """CSRF_COOKIE_AGE setting controls cookie max-age."""

    def test_custom_cookie_age_from_setting(self):
        """Custom CSRF_COOKIE_AGE appears in Set-Cookie."""
        with patch.dict(DEFAULTS, {"CSRF_COOKIE_AGE": 3600}):
            mw = CSRFMiddleware()
            req = make_request(headers={})
            resp = run_async(mw(req, dummy_handler))
            cookie_header = resp.headers.get("set-cookie", "")
            assert (
                "Max-Age=3600" in cookie_header
                or "max-age=3600" in cookie_header.lower()
            ), cookie_header


class TestCSRFHeaderName(unittest.TestCase):
    """CSRF_HEADER_NAME setting controls which header carries the token."""

    def test_custom_header_name_from_setting(self):
        """Custom CSRF_HEADER_NAME is used for token lookup."""
        with patch.dict(DEFAULTS, {"CSRF_HEADER_NAME": "X-My-CSRF"}):
            mw = CSRFMiddleware()
            # Generate a token
            token = mw._generate_token()
            # POST with token in custom header and matching cookie
            req = make_request(
                method="POST",
                headers={"x-my-csrf": token, "cookie": f"csrftoken={token}"},
            )
            resp = run_async(mw(req, dummy_handler))
            assert resp.status == 200, f"Expected 200, got {resp.status}"


class TestCSRFTrustedOrigins(unittest.TestCase):
    """CSRF_TRUSTED_ORIGINS setting allows skipping CSRF checks for trusted origins."""

    def test_trusted_origin_skips_csrf(self):
        """Request from trusted origin skips CSRF check."""
        with patch.dict(
            DEFAULTS, {"CSRF_TRUSTED_ORIGINS": ["https://trusted.example.com"]}
        ):
            mw = CSRFMiddleware()
            req = make_request(
                method="POST",
                headers={"origin": "https://trusted.example.com"},
            )
            resp = run_async(mw(req, dummy_handler))
            assert resp.status == 200, (
                f"Expected 200 for trusted origin, got {resp.status}"
            )

    def test_untrusted_origin_requires_csrf(self):
        """Request from untrusted origin requires CSRF token."""
        with patch.dict(
            DEFAULTS, {"CSRF_TRUSTED_ORIGINS": ["https://trusted.example.com"]}
        ):
            mw = CSRFMiddleware()
            req = make_request(
                method="POST",
                headers={"origin": "https://untrusted.example.com"},
            )
            resp = run_async(mw(req, dummy_handler))
            assert resp.status == 403

    def test_wildcard_subdomain_trusted(self):
        """Wildcard subdomain matching in CSRF_TRUSTED_ORIGINS."""
        with patch.dict(DEFAULTS, {"CSRF_TRUSTED_ORIGINS": ["https://*.example.com"]}):
            mw = CSRFMiddleware()
            req = make_request(
                method="POST",
                headers={"origin": "https://app.example.com"},
            )
            resp = run_async(mw(req, dummy_handler))
            assert resp.status == 200, (
                f"Expected 200 for wildcard match, got {resp.status}"
            )

    def test_empty_trusted_origins_requires_csrf(self):
        """Empty CSRF_TRUSTED_ORIGINS means all origins require CSRF."""
        with patch.dict(DEFAULTS, {"CSRF_TRUSTED_ORIGINS": []}):
            mw = CSRFMiddleware()
            req = make_request(
                method="POST",
                headers={"origin": "https://any.example.com"},
            )
            resp = run_async(mw(req, dummy_handler))
            assert resp.status == 403


class TestCSRFCookieDomain(unittest.TestCase):
    """CSRF_COOKIE_DOMAIN setting controls cookie domain."""

    def test_custom_domain_from_setting(self):
        """Custom CSRF_COOKIE_DOMAIN appears in Set-Cookie."""
        with patch.dict(DEFAULTS, {"CSRF_COOKIE_DOMAIN": ".example.com"}):
            mw = CSRFMiddleware()
            req = make_request(headers={})
            resp = run_async(mw(req, dummy_handler))
            cookie_header = resp.headers.get("set-cookie", "")
            assert ".example.com" in cookie_header, cookie_header


class TestCSRFCookiePath(unittest.TestCase):
    """CSRF_COOKIE_PATH setting controls cookie path."""

    def test_custom_path_from_setting(self):
        """Custom CSRF_COOKIE_PATH appears in Set-Cookie."""
        with patch.dict(DEFAULTS, {"CSRF_COOKIE_PATH": "/api/"}):
            mw = CSRFMiddleware()
            req = make_request(headers={})
            resp = run_async(mw(req, dummy_handler))
            cookie_header = resp.headers.get("set-cookie", "")
            assert (
                "Path=/api/" in cookie_header or "path=/api/" in cookie_header.lower()
            ), cookie_header


class TestRequestIsSecure(unittest.TestCase):
    """SECURE_PROXY_SSL_HEADER wired into Request.is_secure."""

    def test_proxy_header_https(self):
        """Request.is_secure returns True when proxy header says HTTPS."""
        with patch.dict(DEFAULTS, {"SECURE_PROXY_SSL_HEADER": "X-Forwarded-Proto"}):
            req = make_request(headers={"x-forwarded-proto": "https"})
            assert req.is_secure is True

    def test_proxy_header_http(self):
        """Request.is_secure returns False when proxy header says HTTP."""
        with patch.dict(DEFAULTS, {"SECURE_PROXY_SSL_HEADER": "X-Forwarded-Proto"}):
            req = make_request(headers={"x-forwarded-proto": "http"})
            assert req.is_secure is False

    def test_no_proxy_header_falls_back_to_scope(self):
        """Without proxy header setting, falls back to scope scheme."""
        with patch.dict(DEFAULTS, {"SECURE_PROXY_SSL_HEADER": ""}):
            req = make_request(headers={}, scope={"scheme": "https"})
            assert req.is_secure is True

    def test_no_proxy_header_falls_back_to_forwarded_proto(self):
        """Without proxy header setting, falls back to x-forwarded-proto."""
        with patch.dict(DEFAULTS, {"SECURE_PROXY_SSL_HEADER": ""}):
            req = make_request(headers={"x-forwarded-proto": "https"})
            assert req.is_secure is True

    def test_custom_proxy_header_name(self):
        """Custom proxy header name (e.g. X-Custom-Proto)."""
        with patch.dict(DEFAULTS, {"SECURE_PROXY_SSL_HEADER": "X-Custom-Proto"}):
            req = make_request(headers={"x-custom-proto": "https"})
            assert req.is_secure is True


class TestRequestHost(unittest.TestCase):
    """USE_X_FORWARDED_HOST wired into Request.host."""

    def test_forwarded_host_when_enabled(self):
        """Request.host returns X-Forwarded-Host when setting enabled."""
        with patch.dict(DEFAULTS, {"USE_X_FORWARDED_HOST": True}):
            req = make_request(
                headers={
                    "host": "internal.local",
                    "x-forwarded-host": "public.example.com",
                }
            )
            assert req.host == "public.example.com"

    def test_regular_host_when_disabled(self):
        """Request.host returns Host header when setting disabled."""
        with patch.dict(DEFAULTS, {"USE_X_FORWARDED_HOST": False}):
            req = make_request(
                headers={
                    "host": "internal.local",
                    "x-forwarded-host": "public.example.com",
                }
            )
            assert req.host == "internal.local"

    def test_forwarded_host_first_value(self):
        """Multiple X-Forwarded-Host values: use first."""
        with patch.dict(DEFAULTS, {"USE_X_FORWARDED_HOST": True}):
            req = make_request(headers={"x-forwarded-host": "first.com, second.com"})
            assert req.host == "first.com"


class TestRequestPort(unittest.TestCase):
    """USE_X_FORWARDED_PORT wired into Request.port."""

    def test_forwarded_port_when_enabled(self):
        """Request.port returns X-Forwarded-Port when setting enabled."""
        with patch.dict(DEFAULTS, {"USE_X_FORWARDED_PORT": True}):
            req = make_request(
                headers={"host": "example.com:8080", "x-forwarded-port": "443"}
            )
            assert req.port == "443"

    def test_port_from_host_when_disabled(self):
        """Request.port extracts port from Host header when setting disabled."""
        with patch.dict(DEFAULTS, {"USE_X_FORWARDED_PORT": False}):
            req = make_request(headers={"host": "example.com:8080"})
            assert req.port == "8080"


# ===========================================================================
# Module-level settings integration tests (email, cache, messages, storage,
# router, upload limits)
# ===========================================================================

from hyperdjango.cache import LocMemCache, make_cache_key
from hyperdjango.mail import (
    EmailMessage,
    clear_outbox,
    configure_mail,
    get_outbox,
)
from hyperdjango.messages import (
    DEBUG as MSG_DEBUG,
)
from hyperdjango.messages import (
    ERROR as MSG_ERROR,
)
from hyperdjango.messages import (
    INFO as MSG_INFO,
)
from hyperdjango.messages import (
    SUCCESS as MSG_SUCCESS,
)
from hyperdjango.messages import (
    WARNING as MSG_WARNING,
)
from hyperdjango.messages import (
    add_message,
    get_level_tag,
    get_messages,
)
from hyperdjango.storage import FileSystemStorage


class _FakeSession(dict):
    """Minimal session dict for message tests."""

    pass


class _FakeRequest:
    """Minimal request object with a session for message tests."""

    def __init__(self):
        self.session = _FakeSession()


# ---------------------------------------------------------------------------
# EMAIL tests
# ---------------------------------------------------------------------------


class TestEmailSubjectPrefix(unittest.TestCase):
    """EMAIL_SUBJECT_PREFIX is prepended to subject in EmailMessage.send()."""

    def setUp(self):
        configure_mail(backend="memory")
        clear_outbox()

    def test_prefix_prepended(self):
        """Subject gets prefix from settings."""
        with patch.dict(DEFAULTS, {"EMAIL_SUBJECT_PREFIX": "[Test] "}):
            msg = EmailMessage(
                subject="Hello",
                body="Body",
                recipients=["a@b.com"],
            )
            run_async(msg.send())
            sent = get_outbox()
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0].subject, "[Test] Hello")

    def test_prefix_not_doubled(self):
        """If subject already starts with prefix, don't prepend again."""
        with patch.dict(DEFAULTS, {"EMAIL_SUBJECT_PREFIX": "[Test] "}):
            msg = EmailMessage(
                subject="[Test] Hello",
                body="Body",
                recipients=["a@b.com"],
            )
            run_async(msg.send())
            sent = get_outbox()
            self.assertEqual(sent[0].subject, "[Test] Hello")

    def test_empty_prefix_no_change(self):
        """Empty prefix leaves subject unchanged."""
        with patch.dict(DEFAULTS, {"EMAIL_SUBJECT_PREFIX": ""}):
            msg = EmailMessage(
                subject="Hello",
                body="Body",
                recipients=["a@b.com"],
            )
            run_async(msg.send())
            sent = get_outbox()
            self.assertEqual(sent[0].subject, "Hello")


class TestServerEmail(unittest.TestCase):
    """SERVER_EMAIL used as default from_address when no from_email set."""

    def setUp(self):
        configure_mail(backend="memory")
        clear_outbox()

    def test_server_email_used_as_default(self):
        """When no from_email, SERVER_EMAIL from settings is used."""
        with patch.dict(DEFAULTS, {"SERVER_EMAIL": "server@example.com"}):
            msg = EmailMessage(
                subject="Hi",
                body="Body",
                recipients=["a@b.com"],
            )
            run_async(msg.send())
            sent = get_outbox()
            self.assertEqual(len(sent), 1)
            # Verify the from address logic by building MIME
            mime = sent[0]._build_mime("server@example.com")
            self.assertEqual(mime["From"], "server@example.com")

    def test_explicit_from_overrides_server_email(self):
        """Explicit from_email takes precedence over SERVER_EMAIL."""
        with patch.dict(DEFAULTS, {"SERVER_EMAIL": "server@example.com"}):
            msg = EmailMessage(
                subject="Hi",
                body="Body",
                from_email="custom@example.com",
                recipients=["a@b.com"],
            )
            run_async(msg.send())
            sent = get_outbox()
            self.assertEqual(len(sent), 1)


class TestEmailTimeout(unittest.TestCase):
    """EMAIL_TIMEOUT is passed to SMTP connection."""

    def test_timeout_from_settings(self):
        """EMAIL_TIMEOUT setting is read by mail module."""
        from hyperdjango.conf import get_setting

        with patch.dict(DEFAULTS, {"EMAIL_TIMEOUT": 42}):
            val = get_setting("EMAIL_TIMEOUT", 30)
            self.assertEqual(val, 42)


# ---------------------------------------------------------------------------
# CACHE tests
# ---------------------------------------------------------------------------


class TestCacheKeyPrefix(unittest.TestCase):
    """CACHE_KEY_PREFIX is prepended to all cache keys."""

    def test_prefix_in_key(self):
        """make_cache_key includes CACHE_KEY_PREFIX."""
        with patch.dict(DEFAULTS, {"CACHE_KEY_PREFIX": "myapp", "CACHE_VERSION": 1}):
            key = make_cache_key("user:42")
            self.assertTrue(key.startswith("myapp:"))
            self.assertIn("user:42", key)

    def test_empty_prefix(self):
        """Empty prefix still includes version."""
        with patch.dict(DEFAULTS, {"CACHE_KEY_PREFIX": "", "CACHE_VERSION": 1}):
            key = make_cache_key("user:42")
            self.assertTrue(key.startswith("v1:"))

    def test_locmem_get_set_uses_prefix(self):
        """LocMemCache.get/set keys are prefixed under the hood."""
        with patch.dict(DEFAULTS, {"CACHE_KEY_PREFIX": "test", "CACHE_VERSION": 1}):
            cache = LocMemCache()
            cache.set("hello", "world")
            self.assertEqual(cache.get("hello"), "world")
            # The internal store should have the prefixed key
            internal_keys = list(cache._cache.keys())
            self.assertTrue(any("test:" in k for k in internal_keys))

    def test_locmem_has_uses_prefix(self):
        """LocMemCache.has() uses prefixed key."""
        with patch.dict(DEFAULTS, {"CACHE_KEY_PREFIX": "pfx", "CACHE_VERSION": 1}):
            cache = LocMemCache()
            cache.set("exists", 1)
            self.assertTrue(cache.has("exists"))
            self.assertFalse(cache.has("missing"))

    def test_locmem_delete_uses_prefix(self):
        """LocMemCache.delete() uses prefixed key."""
        with patch.dict(DEFAULTS, {"CACHE_KEY_PREFIX": "pfx", "CACHE_VERSION": 1}):
            cache = LocMemCache()
            cache.set("key", "val")
            self.assertTrue(cache.delete("key"))
            self.assertIsNone(cache.get("key"))


class TestCacheVersion(unittest.TestCase):
    """CACHE_VERSION is included in cache keys."""

    def test_version_in_key(self):
        """Cache key includes version number."""
        with patch.dict(DEFAULTS, {"CACHE_KEY_PREFIX": "", "CACHE_VERSION": 3}):
            key = make_cache_key("data")
            self.assertIn("v3", key)

    def test_different_versions_different_keys(self):
        """Changing CACHE_VERSION produces different cache keys."""
        with patch.dict(DEFAULTS, {"CACHE_KEY_PREFIX": "app", "CACHE_VERSION": 1}):
            key_v1 = make_cache_key("data")
        with patch.dict(DEFAULTS, {"CACHE_KEY_PREFIX": "app", "CACHE_VERSION": 2}):
            key_v2 = make_cache_key("data")
        self.assertNotEqual(key_v1, key_v2)

    def test_version_isolates_cache_data(self):
        """LocMemCache with different versions can't see each other's data."""
        cache = LocMemCache()
        with patch.dict(DEFAULTS, {"CACHE_KEY_PREFIX": "", "CACHE_VERSION": 1}):
            cache.set("key", "version1")
        with patch.dict(DEFAULTS, {"CACHE_KEY_PREFIX": "", "CACHE_VERSION": 2}):
            self.assertIsNone(cache.get("key"))


# ---------------------------------------------------------------------------
# MESSAGE tests
# ---------------------------------------------------------------------------


class TestMessageLevel(unittest.TestCase):
    """MESSAGE_LEVEL filters low-priority messages."""

    def test_below_threshold_filtered(self):
        """Messages below MESSAGE_LEVEL are not stored."""
        with patch.dict(DEFAULTS, {"MESSAGE_LEVEL": 25}):
            req = _FakeRequest()
            add_message(req, MSG_DEBUG, "debug msg")  # 10 < 25
            add_message(req, MSG_INFO, "info msg")  # 20 < 25
            add_message(req, MSG_SUCCESS, "ok!")  # 25 >= 25
            msgs = get_messages(req)
            self.assertEqual(len(msgs), 1)
            self.assertEqual(msgs[0]["text"], "ok!")

    def test_at_threshold_stored(self):
        """Messages at exactly MESSAGE_LEVEL are stored."""
        with patch.dict(DEFAULTS, {"MESSAGE_LEVEL": 20}):
            req = _FakeRequest()
            add_message(req, MSG_INFO, "info msg")
            msgs = get_messages(req)
            self.assertEqual(len(msgs), 1)

    def test_above_threshold_stored(self):
        """Messages above MESSAGE_LEVEL are stored."""
        with patch.dict(DEFAULTS, {"MESSAGE_LEVEL": 20}):
            req = _FakeRequest()
            add_message(req, MSG_ERROR, "error msg")
            msgs = get_messages(req)
            self.assertEqual(len(msgs), 1)
            self.assertEqual(msgs[0]["level"], MSG_ERROR)

    def test_high_threshold_filters_all(self):
        """Very high MESSAGE_LEVEL filters all standard messages."""
        with patch.dict(DEFAULTS, {"MESSAGE_LEVEL": 100}):
            req = _FakeRequest()
            add_message(req, MSG_DEBUG, "d")
            add_message(req, MSG_INFO, "i")
            add_message(req, MSG_SUCCESS, "s")
            add_message(req, MSG_WARNING, "w")
            add_message(req, MSG_ERROR, "e")
            msgs = get_messages(req)
            self.assertEqual(len(msgs), 0)


class TestMessageTags(unittest.TestCase):
    """MESSAGE_TAGS controls CSS class mapping for message levels."""

    def test_default_tags(self):
        """Default tags map level numbers to strings."""
        with patch.dict(DEFAULTS, {"MESSAGE_TAGS": {}}):
            self.assertEqual(get_level_tag(MSG_SUCCESS), "success")
            self.assertEqual(get_level_tag(MSG_ERROR), "error")
            self.assertEqual(get_level_tag(MSG_WARNING), "warning")
            self.assertEqual(get_level_tag(MSG_INFO), "info")
            self.assertEqual(get_level_tag(MSG_DEBUG), "debug")

    def test_custom_tags_override(self):
        """MESSAGE_TAGS setting overrides default tags."""
        custom = {MSG_ERROR: "danger", MSG_SUCCESS: "ok"}
        with patch.dict(DEFAULTS, {"MESSAGE_TAGS": custom}):
            self.assertEqual(get_level_tag(MSG_ERROR), "danger")
            self.assertEqual(get_level_tag(MSG_SUCCESS), "ok")
            self.assertEqual(get_level_tag(MSG_INFO), "info")

    def test_tag_in_get_messages(self):
        """get_messages annotates each message with its tag."""
        with patch.dict(DEFAULTS, {"MESSAGE_TAGS": {}, "MESSAGE_LEVEL": 0}):
            req = _FakeRequest()
            add_message(req, MSG_SUCCESS, "ok!")
            msgs = get_messages(req)
            self.assertEqual(msgs[0]["tag"], "success")


# ---------------------------------------------------------------------------
# STORAGE tests
# ---------------------------------------------------------------------------


class TestMediaSettings(unittest.TestCase):
    """MEDIA_URL and MEDIA_ROOT configure FileSystemStorage defaults."""

    def test_media_url_default(self):
        """FileSystemStorage uses MEDIA_URL from settings."""
        with patch.dict(DEFAULTS, {"MEDIA_URL": "/uploads/", "MEDIA_ROOT": ""}):
            storage = FileSystemStorage()
            self.assertEqual(storage.base_url, "/uploads/")

    def test_media_root_default(self):
        """FileSystemStorage uses MEDIA_ROOT from settings."""
        with patch.dict(DEFAULTS, {"MEDIA_ROOT": "/var/media", "MEDIA_URL": "/media/"}):
            storage = FileSystemStorage()
            self.assertEqual(storage.location, str(Path("/var/media").resolve()))

    def test_explicit_overrides_settings(self):
        """Explicit constructor args override settings."""
        with patch.dict(
            DEFAULTS, {"MEDIA_ROOT": "/var/media", "MEDIA_URL": "/uploads/"}
        ):
            storage = FileSystemStorage(location="/custom", base_url="/custom-url/")
            self.assertEqual(storage.location, "/custom")
            self.assertEqual(storage.base_url, "/custom-url/")

    def test_url_generation(self):
        """Storage.url() uses the configured MEDIA_URL."""
        with patch.dict(DEFAULTS, {"MEDIA_URL": "/assets/", "MEDIA_ROOT": ""}):
            storage = FileSystemStorage()
            self.assertEqual(storage.url("photo.jpg"), "/assets/photo.jpg")


# ---------------------------------------------------------------------------
# ROUTER / APPEND_SLASH tests
# ---------------------------------------------------------------------------


class TestAppendSlash(unittest.TestCase):
    """APPEND_SLASH redirects /path to /path/ when enabled."""

    def _make_app(self):
        """Create a minimal HyperApp with a route at /hello/."""
        from hyperdjango import HyperApp

        app = HyperApp(title="test")

        @app.get("/hello/")
        async def hello(request):
            return Response.text("Hello!")

        app.router.finalize()
        return app

    def test_append_slash_redirect(self):
        """With APPEND_SLASH=True, /hello resolves to redirect sentinel."""
        with patch.dict(DEFAULTS, {"APPEND_SLASH": True}):
            app = self._make_app()
            from hyperdjango.router import _APPEND_SLASH_REDIRECT

            route, params = app.router.resolve("GET", "/hello")
            self.assertIs(route, _APPEND_SLASH_REDIRECT)
            self.assertEqual(params["redirect_to"], "/hello/")

    def test_append_slash_disabled(self):
        """With APPEND_SLASH=False, /hello serves content directly (no redirect)."""
        with patch.dict(DEFAULTS, {"APPEND_SLASH": False}):
            app = self._make_app()
            from hyperdjango.router import _APPEND_SLASH_REDIRECT

            route, params = app.router.resolve("GET", "/hello")
            # Native trie may fuzzy-match /hello to /hello/, but with
            # APPEND_SLASH=False it should NOT return the redirect sentinel
            self.assertIsNot(route, _APPEND_SLASH_REDIRECT)

    def test_exact_match_no_redirect(self):
        """A path that matches exactly doesn't trigger redirect."""
        with patch.dict(DEFAULTS, {"APPEND_SLASH": True}):
            app = self._make_app()
            route, params = app.router.resolve("GET", "/hello/")
            self.assertIsNotNone(route)
            from hyperdjango.router import _APPEND_SLASH_REDIRECT

            self.assertIsNot(route, _APPEND_SLASH_REDIRECT)

    def test_dispatch_returns_301(self):
        """Full dispatch returns 301 redirect response."""
        with patch.dict(DEFAULTS, {"APPEND_SLASH": True}):
            app = self._make_app()
            req = Request(method="GET", path="/hello")
            response = run_async(app._dispatch(req))
            self.assertEqual(response.status, 301)
            # Response.redirect stores header with lowercase "location"
            location = response.headers.get("location", "")
            self.assertEqual(location, "/hello/")


# ---------------------------------------------------------------------------
# UPLOAD LIMIT tests
# ---------------------------------------------------------------------------


class TestUploadLimits(unittest.TestCase):
    """DATA_UPLOAD_MAX_NUMBER_FIELDS and DATA_UPLOAD_MAX_NUMBER_FILES."""

    def test_too_many_form_fields_rejected(self):
        """Exceeding DATA_UPLOAD_MAX_NUMBER_FIELDS raises 400."""
        from hyperdjango.app import HTTPException

        with patch.dict(DEFAULTS, {"DATA_UPLOAD_MAX_NUMBER_FIELDS": 2}):
            body = "a=1&b=2&c=3"
            req = Request(
                method="POST",
                path="/",
                headers={"content-type": "application/x-www-form-urlencoded"},
                body=body.encode(),
            )
            with self.assertRaises(HTTPException) as ctx:
                run_async(req.form())
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("Too many form fields", ctx.exception.detail)

    def test_within_field_limit_ok(self):
        """Form within DATA_UPLOAD_MAX_NUMBER_FIELDS limit succeeds."""
        with patch.dict(DEFAULTS, {"DATA_UPLOAD_MAX_NUMBER_FIELDS": 10}):
            body = "a=1&b=2"
            req = Request(
                method="POST",
                path="/",
                headers={"content-type": "application/x-www-form-urlencoded"},
                body=body.encode(),
            )
            form = run_async(req.form())
            self.assertIn("a", form)
            self.assertIn("b", form)


# ---------------------------------------------------------------------------
# HSTS settings tests
# ---------------------------------------------------------------------------


class TestSecurityHeadersHSTS(unittest.TestCase):
    """SECURE_HSTS_SECONDS, SECURE_HSTS_INCLUDE_SUBDOMAINS, SECURE_HSTS_PRELOAD."""

    def test_hsts_disabled_by_default(self):
        """Default SECURE_HSTS_SECONDS=0 means no HSTS header."""
        mw = SecurityHeadersMiddleware()
        req = make_request()
        resp = run_async(mw(req, dummy_handler))
        assert "strict-transport-security" not in resp.headers

    def test_hsts_from_setting(self):
        """SECURE_HSTS_SECONDS > 0 enables HSTS with that max-age."""
        with patch.dict(DEFAULTS, {"SECURE_HSTS_SECONDS": 3600}):
            mw = SecurityHeadersMiddleware()
            req = make_request()
            resp = run_async(mw(req, dummy_handler))
            hsts = resp.headers.get("strict-transport-security", "")
            assert "max-age=3600" in hsts, hsts

    def test_hsts_include_subdomains(self):
        """SECURE_HSTS_INCLUDE_SUBDOMAINS adds includeSubDomains directive."""
        with patch.dict(
            DEFAULTS,
            {"SECURE_HSTS_SECONDS": 3600, "SECURE_HSTS_INCLUDE_SUBDOMAINS": True},
        ):
            mw = SecurityHeadersMiddleware()
            req = make_request()
            resp = run_async(mw(req, dummy_handler))
            hsts = resp.headers.get("strict-transport-security", "")
            assert "includeSubDomains" in hsts, hsts

    def test_hsts_preload(self):
        """SECURE_HSTS_PRELOAD adds preload directive."""
        with patch.dict(
            DEFAULTS, {"SECURE_HSTS_SECONDS": 31536000, "SECURE_HSTS_PRELOAD": True}
        ):
            mw = SecurityHeadersMiddleware()
            req = make_request()
            resp = run_async(mw(req, dummy_handler))
            hsts = resp.headers.get("strict-transport-security", "")
            assert "preload" in hsts, hsts

    def test_hsts_all_directives(self):
        """All three HSTS directives combined."""
        with patch.dict(
            DEFAULTS,
            {
                "SECURE_HSTS_SECONDS": 31536000,
                "SECURE_HSTS_INCLUDE_SUBDOMAINS": True,
                "SECURE_HSTS_PRELOAD": True,
            },
        ):
            mw = SecurityHeadersMiddleware()
            req = make_request()
            resp = run_async(mw(req, dummy_handler))
            hsts = resp.headers.get("strict-transport-security", "")
            assert "max-age=31536000" in hsts, hsts
            assert "includeSubDomains" in hsts, hsts
            assert "preload" in hsts, hsts

    def test_constructor_hsts_overrides_setting(self):
        """Constructor hsts=True with hsts_max_age overrides SECURE_HSTS_SECONDS."""
        with patch.dict(DEFAULTS, {"SECURE_HSTS_SECONDS": 100}):
            mw = SecurityHeadersMiddleware(hsts=True, hsts_max_age=9999)
            req = make_request()
            resp = run_async(mw(req, dummy_handler))
            hsts = resp.headers.get("strict-transport-security", "")
            assert "max-age=9999" in hsts, hsts

    def test_hsts_no_subdomains_by_default(self):
        """SECURE_HSTS_INCLUDE_SUBDOMAINS=False omits includeSubDomains."""
        with patch.dict(
            DEFAULTS,
            {"SECURE_HSTS_SECONDS": 3600, "SECURE_HSTS_INCLUDE_SUBDOMAINS": False},
        ):
            mw = SecurityHeadersMiddleware()
            req = make_request()
            resp = run_async(mw(req, dummy_handler))
            hsts = resp.headers.get("strict-transport-security", "")
            assert "includeSubDomains" not in hsts, hsts


# ---------------------------------------------------------------------------
# PREPEND_WWW tests
# ---------------------------------------------------------------------------


class TestPrependWWW(unittest.TestCase):
    """PREPEND_WWW setting redirects non-www to www."""

    def test_prepend_www_redirects(self):
        """PREPEND_WWW=True redirects example.com to www.example.com."""
        with patch.dict(DEFAULTS, {"PREPEND_WWW": True}):
            mw = SecurityHeadersMiddleware()
            req = make_request(path="/page", headers={"host": "example.com"})
            resp = run_async(mw(req, dummy_handler))
            assert resp.status == 301
            location = resp.headers.get("location", "")
            assert "www.example.com" in location, location

    def test_prepend_www_skips_www_host(self):
        """PREPEND_WWW=True does NOT redirect www.example.com."""
        with patch.dict(DEFAULTS, {"PREPEND_WWW": True}):
            mw = SecurityHeadersMiddleware()
            req = make_request(path="/page", headers={"host": "www.example.com"})
            resp = run_async(mw(req, dummy_handler))
            assert resp.status == 200

    def test_prepend_www_disabled(self):
        """PREPEND_WWW=False does not redirect."""
        with patch.dict(DEFAULTS, {"PREPEND_WWW": False}):
            mw = SecurityHeadersMiddleware()
            req = make_request(path="/page", headers={"host": "example.com"})
            resp = run_async(mw(req, dummy_handler))
            assert resp.status == 200

    def test_prepend_www_preserves_path(self):
        """PREPEND_WWW redirect preserves the request path."""
        with patch.dict(DEFAULTS, {"PREPEND_WWW": True}):
            mw = SecurityHeadersMiddleware()
            req = make_request(path="/foo/bar", headers={"host": "example.com"})
            resp = run_async(mw(req, dummy_handler))
            location = resp.headers.get("location", "")
            assert location.endswith("/foo/bar"), location


# ---------------------------------------------------------------------------
# STATICFILES settings tests
# ---------------------------------------------------------------------------

from hyperdjango.staticfiles import StaticFilesMiddleware


class TestStaticFilesSettings(unittest.TestCase):
    """STATIC_URL, STATIC_ROOT, STATIC_MAX_AGE, STATICFILES_DIRS wired into StaticFilesMiddleware."""

    def test_static_url_from_setting(self):
        """STATIC_URL setting changes the prefix."""
        with patch.dict(DEFAULTS, {"STATIC_URL": "/assets/"}):
            mw = StaticFilesMiddleware()
            assert mw.prefix == "/assets/", mw.prefix

    def test_static_url_default(self):
        """Default STATIC_URL=/static/ keeps prefix as /static/."""
        with patch.dict(DEFAULTS, {"STATIC_URL": "/static/"}):
            mw = StaticFilesMiddleware()
            assert mw.prefix == "/static/", mw.prefix

    def test_static_url_constructor_overrides(self):
        """Constructor prefix overrides STATIC_URL setting."""
        with patch.dict(DEFAULTS, {"STATIC_URL": "/assets/"}):
            mw = StaticFilesMiddleware(prefix="/custom/")
            assert mw.prefix == "/custom/", mw.prefix

    def test_static_max_age_from_setting(self):
        """STATIC_MAX_AGE setting changes max_age."""
        with patch.dict(DEFAULTS, {"STATIC_MAX_AGE": 7200}):
            mw = StaticFilesMiddleware()
            assert mw.max_age == 7200, mw.max_age

    def test_static_max_age_constructor_overrides(self):
        """Constructor max_age overrides STATIC_MAX_AGE setting."""
        with patch.dict(DEFAULTS, {"STATIC_MAX_AGE": 7200}):
            mw = StaticFilesMiddleware(max_age=999)
            assert mw.max_age == 999, mw.max_age

    def test_static_root_from_setting(self):
        """STATIC_ROOT setting sets static_root when not passed to constructor."""
        with patch.dict(DEFAULTS, {"STATIC_ROOT": "/var/static"}):
            mw = StaticFilesMiddleware()
            assert mw.static_root == "/var/static", mw.static_root

    def test_staticfiles_dirs_from_setting(self):
        """STATICFILES_DIRS setting populates static_dirs."""
        with patch.dict(
            DEFAULTS, {"STATICFILES_DIRS": ["/app/static", "/shared/static"]}
        ):
            mw = StaticFilesMiddleware()
            assert mw.static_dirs == ["/app/static", "/shared/static"], mw.static_dirs

    def test_staticfiles_dirs_constructor_overrides(self):
        """Constructor static_dirs takes priority over STATICFILES_DIRS."""
        with patch.dict(DEFAULTS, {"STATICFILES_DIRS": ["/setting/dir"]}):
            mw = StaticFilesMiddleware(static_dirs=["/constructor/dir"])
            assert mw.static_dirs == ["/constructor/dir"], mw.static_dirs


# ---------------------------------------------------------------------------
# SESSION settings tests
# ---------------------------------------------------------------------------

from hyperdjango.auth.sessions import InMemorySessionStore, SessionAuth


class TestSessionCookieAge(unittest.TestCase):
    """SESSION_COOKIE_AGE setting controls session max-age."""

    def test_session_cookie_age_from_setting(self):
        """SESSION_COOKIE_AGE wires into store.max_age when using default store."""
        with patch.dict(
            DEFAULTS, {"SESSION_COOKIE_AGE": 7200, "SESSION_COOKIE_NAME": "sessionid"}
        ):
            auth = SessionAuth(secret="test-secret")
            assert auth.store.max_age == 7200, auth.store.max_age

    def test_session_cookie_age_default(self):
        """Default SESSION_COOKIE_AGE is used when no explicit store."""
        with patch.dict(
            DEFAULTS, {"SESSION_COOKIE_AGE": 86400, "SESSION_COOKIE_NAME": "sessionid"}
        ):
            auth = SessionAuth(secret="test-secret")
            assert auth.store.max_age == 86400, auth.store.max_age

    def test_explicit_store_not_overridden(self):
        """When an explicit store is passed, SESSION_COOKIE_AGE does not override its max_age."""
        with patch.dict(
            DEFAULTS, {"SESSION_COOKIE_AGE": 9999, "SESSION_COOKIE_NAME": "sessionid"}
        ):
            custom_store = InMemorySessionStore(max_age=500)
            auth = SessionAuth(secret="test-secret", store=custom_store)
            assert auth.store.max_age == 500, auth.store.max_age


class TestSessionCookieName(unittest.TestCase):
    """SESSION_COOKIE_NAME setting controls the cookie name."""

    def test_session_cookie_name_from_setting(self):
        """SESSION_COOKIE_NAME from settings is used as cookie_name."""
        with patch.dict(
            DEFAULTS, {"SESSION_COOKIE_NAME": "mysession", "SESSION_COOKIE_AGE": 86400}
        ):
            auth = SessionAuth(secret="test-secret")
            assert auth.cookie_name == "mysession", auth.cookie_name

    def test_session_cookie_name_default(self):
        """Default SESSION_COOKIE_NAME is 'sessionid'."""
        with patch.dict(
            DEFAULTS, {"SESSION_COOKIE_NAME": "sessionid", "SESSION_COOKIE_AGE": 86400}
        ):
            auth = SessionAuth(secret="test-secret")
            assert auth.cookie_name == "sessionid", auth.cookie_name

    def test_constructor_overrides_setting(self):
        """Constructor cookie_name overrides SESSION_COOKIE_NAME setting."""
        with patch.dict(
            DEFAULTS,
            {"SESSION_COOKIE_NAME": "from_setting", "SESSION_COOKIE_AGE": 86400},
        ):
            auth = SessionAuth(secret="test-secret", cookie_name="from_constructor")
            assert auth.cookie_name == "from_constructor", auth.cookie_name


# ---------------------------------------------------------------------------
# EMAIL SETTINGS wiring tests (10 settings)
# ---------------------------------------------------------------------------


class TestEmailHostSetting(unittest.TestCase):
    """EMAIL_HOST wired into mail config."""

    def test_default_from_setting(self):
        """configure_mail() without args reads EMAIL_HOST from settings."""
        with patch.dict(DEFAULTS, {"EMAIL_HOST": "smtp.custom.com"}):
            configure_mail(backend="memory")
            from hyperdjango.mail import get_mail_config

            cfg = get_mail_config()
            self.assertEqual(cfg.host, "smtp.custom.com")

    def test_explicit_overrides_setting(self):
        """Explicit host= overrides EMAIL_HOST setting."""
        with patch.dict(DEFAULTS, {"EMAIL_HOST": "smtp.custom.com"}):
            configure_mail(backend="memory", host="override.com")
            from hyperdjango.mail import get_mail_config

            cfg = get_mail_config()
            self.assertEqual(cfg.host, "override.com")


class TestEmailPortSetting(unittest.TestCase):
    """EMAIL_PORT wired into mail config."""

    def test_default_from_setting(self):
        with patch.dict(DEFAULTS, {"EMAIL_PORT": 465}):
            configure_mail(backend="memory")
            from hyperdjango.mail import get_mail_config

            cfg = get_mail_config()
            self.assertEqual(cfg.port, 465)

    def test_explicit_overrides_setting(self):
        with patch.dict(DEFAULTS, {"EMAIL_PORT": 465}):
            configure_mail(backend="memory", port=2525)
            from hyperdjango.mail import get_mail_config

            cfg = get_mail_config()
            self.assertEqual(cfg.port, 2525)


class TestEmailHostUserSetting(unittest.TestCase):
    """EMAIL_HOST_USER wired into mail config."""

    def test_default_from_setting(self):
        with patch.dict(DEFAULTS, {"EMAIL_HOST_USER": "user@host.com"}):
            configure_mail(backend="memory")
            from hyperdjango.mail import get_mail_config

            cfg = get_mail_config()
            self.assertEqual(cfg.username, "user@host.com")

    def test_explicit_overrides_setting(self):
        with patch.dict(DEFAULTS, {"EMAIL_HOST_USER": "user@host.com"}):
            configure_mail(backend="memory", username="other@host.com")
            from hyperdjango.mail import get_mail_config

            cfg = get_mail_config()
            self.assertEqual(cfg.username, "other@host.com")


class TestEmailHostPasswordSetting(unittest.TestCase):
    """EMAIL_HOST_PASSWORD wired into mail config."""

    def test_default_from_setting(self):
        with patch.dict(DEFAULTS, {"EMAIL_HOST_PASSWORD": "secret123"}):
            configure_mail(backend="memory")
            from hyperdjango.mail import get_mail_config

            cfg = get_mail_config()
            self.assertEqual(cfg.password, "secret123")


class TestEmailUseTLSSetting(unittest.TestCase):
    """EMAIL_USE_TLS wired into mail config."""

    def test_default_from_setting(self):
        with patch.dict(DEFAULTS, {"EMAIL_USE_TLS": False}):
            configure_mail(backend="memory")
            from hyperdjango.mail import get_mail_config

            cfg = get_mail_config()
            self.assertFalse(cfg.use_tls)

    def test_explicit_overrides_setting(self):
        with patch.dict(DEFAULTS, {"EMAIL_USE_TLS": False}):
            configure_mail(backend="memory", use_tls=True)
            from hyperdjango.mail import get_mail_config

            cfg = get_mail_config()
            self.assertTrue(cfg.use_tls)


class TestEmailUseSSLSetting(unittest.TestCase):
    """EMAIL_USE_SSL wired into mail config."""

    def test_default_from_setting(self):
        with patch.dict(DEFAULTS, {"EMAIL_USE_SSL": True}):
            configure_mail(backend="memory")
            from hyperdjango.mail import get_mail_config

            cfg = get_mail_config()
            self.assertTrue(cfg.use_ssl)


class TestEmailBackendSetting(unittest.TestCase):
    """EMAIL_BACKEND wired into mail config."""

    def test_default_from_setting(self):
        with patch.dict(DEFAULTS, {"EMAIL_BACKEND": "console"}):
            configure_mail()
            from hyperdjango.mail import get_mail_config

            cfg = get_mail_config()
            self.assertEqual(cfg.backend, "console")

    def test_explicit_overrides_setting(self):
        with patch.dict(DEFAULTS, {"EMAIL_BACKEND": "console"}):
            configure_mail(backend="memory")
            from hyperdjango.mail import get_mail_config

            cfg = get_mail_config()
            self.assertEqual(cfg.backend, "memory")


class TestDefaultFromEmailSetting(unittest.TestCase):
    """DEFAULT_FROM_EMAIL wired into mail config."""

    def test_default_from_setting(self):
        with patch.dict(DEFAULTS, {"DEFAULT_FROM_EMAIL": "app@example.com"}):
            configure_mail(backend="memory")
            from hyperdjango.mail import get_mail_config

            cfg = get_mail_config()
            self.assertEqual(cfg.default_from, "app@example.com")


class TestEmailSSLCertfileSetting(unittest.TestCase):
    """EMAIL_SSL_CERTFILE is read during SMTP send."""

    def test_setting_is_readable(self):
        """EMAIL_SSL_CERTFILE setting is accessible via get_setting."""
        from hyperdjango.conf import get_setting as gs

        with patch.dict(DEFAULTS, {"EMAIL_SSL_CERTFILE": "/path/to/cert.pem"}):
            self.assertEqual(gs("EMAIL_SSL_CERTFILE", ""), "/path/to/cert.pem")


class TestEmailSSLKeyfileSetting(unittest.TestCase):
    """EMAIL_SSL_KEYFILE is read during SMTP send."""

    def test_setting_is_readable(self):
        """EMAIL_SSL_KEYFILE setting is accessible via get_setting."""
        from hyperdjango.conf import get_setting as gs

        with patch.dict(DEFAULTS, {"EMAIL_SSL_KEYFILE": "/path/to/key.pem"}):
            self.assertEqual(gs("EMAIL_SSL_KEYFILE", ""), "/path/to/key.pem")


# ---------------------------------------------------------------------------
# LOGGING SETTINGS wiring tests (2 settings)
# ---------------------------------------------------------------------------


class TestLogLevelSetting(unittest.TestCase):
    """LOG_LEVEL setting is read by logging auto-init."""

    def test_setting_available(self):
        """LOG_LEVEL default is INFO."""
        from hyperdjango.conf import get_setting as gs

        val = gs("LOG_LEVEL", "DEBUG")
        self.assertIn(val, ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"))

    def test_custom_level(self):
        """Custom LOG_LEVEL is returned by get_setting."""
        from hyperdjango.conf import get_setting as gs

        with patch.dict(DEFAULTS, {"LOG_LEVEL": "WARNING"}):
            self.assertEqual(gs("LOG_LEVEL", "DEBUG"), "WARNING")


class TestLogFormatSetting(unittest.TestCase):
    """LOG_FORMAT setting controls text vs json output."""

    def test_default_text(self):
        """Default LOG_FORMAT is 'text'."""
        from hyperdjango.conf import get_setting as gs

        self.assertEqual(gs("LOG_FORMAT", "text"), "text")

    def test_json_format(self):
        """LOG_FORMAT='json' is returned by get_setting."""
        from hyperdjango.conf import get_setting as gs

        with patch.dict(DEFAULTS, {"LOG_FORMAT": "json"}):
            self.assertEqual(gs("LOG_FORMAT", "text"), "json")


# ---------------------------------------------------------------------------
# CACHE SETTINGS wiring tests (3 settings)
# ---------------------------------------------------------------------------


class TestCacheBackendSetting(unittest.TestCase):
    """CACHE_BACKEND controls get_cache() default."""

    def test_memory_backend_returns_locmem(self):
        """CACHE_BACKEND='memory' returns LocMemCache from get_cache()."""
        from hyperdjango.cache import get_cache, set_cache

        set_cache(None)  # reset global
        with patch.dict(DEFAULTS, {"CACHE_BACKEND": "memory"}):
            c = get_cache()
            self.assertIsInstance(c, LocMemCache)

    def test_database_backend_without_db_falls_back(self):
        """CACHE_BACKEND='database' without set_cache() falls back to LocMemCache."""
        from hyperdjango.cache import get_cache, set_cache

        set_cache(None)
        with patch.dict(DEFAULTS, {"CACHE_BACKEND": "database"}):
            c = get_cache()
            self.assertIsInstance(c, LocMemCache)


class TestCacheTTLSetting(unittest.TestCase):
    """CACHE_TTL controls default TTL for cache operations."""

    def test_cached_decorator_uses_setting(self):
        """@cached() without explicit ttl uses CACHE_TTL setting."""
        from hyperdjango.cache import cached, set_cache

        with patch.dict(
            DEFAULTS, {"CACHE_TTL": 999, "CACHE_KEY_PREFIX": "", "CACHE_VERSION": 1}
        ):
            shared_cache = LocMemCache(max_size=100)
            set_cache(shared_cache)
            try:
                call_count = 0

                @cached()
                def compute(x):
                    nonlocal call_count
                    call_count += 1
                    return x * 2

                # First call computes
                result = compute(5)
                self.assertEqual(result, 10)
                self.assertEqual(call_count, 1)

                # Second call from cache
                result = compute(5)
                self.assertEqual(result, 10)
                self.assertEqual(call_count, 1)
            finally:
                set_cache(None)

    def test_database_cache_default_ttl(self):
        """DatabaseCache uses CACHE_TTL when no default_ttl given."""
        from hyperdjango.cache import DatabaseCache

        with patch.dict(DEFAULTS, {"CACHE_TTL": 600}):
            # Pass a dummy db object
            db = type("FakeDB", (), {})()
            cache = DatabaseCache(db)
            self.assertEqual(cache.default_ttl, 600)

    def test_database_cache_explicit_ttl_overrides(self):
        """Explicit default_ttl overrides CACHE_TTL setting."""
        from hyperdjango.cache import DatabaseCache

        with patch.dict(DEFAULTS, {"CACHE_TTL": 600}):
            db = type("FakeDB", (), {})()
            cache = DatabaseCache(db, default_ttl=120)
            self.assertEqual(cache.default_ttl, 120)


class TestCacheMaxBytesSetting(unittest.TestCase):
    """CACHE_MAX_BYTES controls LocMemCache max_size."""

    def test_max_bytes_determines_max_size(self):
        """CACHE_MAX_BYTES setting controls LocMemCache max_size when not explicit."""
        # 10 KB = 10240 bytes -> max_size = 10240 // 1024 = 10
        with patch.dict(DEFAULTS, {"CACHE_MAX_BYTES": 10240}):
            cache = LocMemCache()
            self.assertEqual(cache.max_size, 10)

    def test_explicit_max_size_overrides(self):
        """Explicit max_size overrides CACHE_MAX_BYTES setting."""
        with patch.dict(DEFAULTS, {"CACHE_MAX_BYTES": 10240}):
            cache = LocMemCache(max_size=500)
            self.assertEqual(cache.max_size, 500)

    def test_default_max_bytes(self):
        """Default CACHE_MAX_BYTES (256 MB) produces large max_size."""
        with patch.dict(DEFAULTS, {"CACHE_MAX_BYTES": 256 * 1024 * 1024}):
            cache = LocMemCache()
            self.assertEqual(cache.max_size, 256 * 1024)


# ── Auth: LOGIN_REDIRECT_URL / LOGOUT_REDIRECT_URL ──────────────────────────


class TestLoginRedirectURL(unittest.TestCase):
    """LOGIN_REDIRECT_URL wired into SessionAuth."""

    def test_default_login_redirect(self):
        """Default LOGIN_REDIRECT_URL is '/'."""
        from hyperdjango.auth.sessions import SessionAuth

        sa = SessionAuth(secret="test-secret")
        url = sa.get_login_redirect_url()
        self.assertEqual(url, "/")

    def test_custom_login_redirect(self):
        """LOGIN_REDIRECT_URL setting is respected."""
        from hyperdjango.auth.sessions import SessionAuth

        with patch.dict(DEFAULTS, {"LOGIN_REDIRECT_URL": "/dashboard/"}):
            sa = SessionAuth(secret="test-secret")
            self.assertEqual(sa.get_login_redirect_url(), "/dashboard/")


class TestLogoutRedirectURL(unittest.TestCase):
    """LOGOUT_REDIRECT_URL wired into SessionAuth."""

    def test_default_logout_redirect(self):
        """Default LOGOUT_REDIRECT_URL is '/'."""
        from hyperdjango.auth.sessions import SessionAuth

        sa = SessionAuth(secret="test-secret")
        url = sa.get_logout_redirect_url()
        self.assertEqual(url, "/")

    def test_custom_logout_redirect(self):
        """LOGOUT_REDIRECT_URL setting is respected."""
        from hyperdjango.auth.sessions import SessionAuth

        with patch.dict(DEFAULTS, {"LOGOUT_REDIRECT_URL": "/goodbye/"}):
            sa = SessionAuth(secret="test-secret")
            self.assertEqual(sa.get_logout_redirect_url(), "/goodbye/")

    def test_logout_and_redirect(self):
        """logout_and_redirect() returns redirect to LOGOUT_REDIRECT_URL."""
        from hyperdjango.auth.sessions import SessionAuth

        with patch.dict(DEFAULTS, {"LOGOUT_REDIRECT_URL": "/logged-out/"}):
            sa = SessionAuth(secret="test-secret")
            # Create a session first
            resp = Response.text("ok")
            sid = sa.login(resp, {"user_id": 1, "username": "alice"})
            # Logout and redirect
            redirect_resp = sa.logout_and_redirect(sid)
            self.assertEqual(redirect_resp.status, 302)
            self.assertIn("/logged-out/", redirect_resp.headers.get("location", ""))


# ── Auth: PASSWORD_HASHER ────────────────────────────────────────────────────


class TestPasswordHasherSetting(unittest.TestCase):
    """PASSWORD_HASHER wired into password hashing."""

    def test_default_argon2id(self):
        """Default PASSWORD_HASHER='argon2id' works normally."""
        from hyperdjango.auth.passwords import hash_password

        h = hash_password("testpassword123")
        self.assertIn("$argon2id$", h)

    def test_invalid_hasher_raises(self):
        """Non-argon2id PASSWORD_HASHER raises ValueError."""
        from hyperdjango.auth.passwords import hash_password

        with patch.dict(DEFAULTS, {"PASSWORD_HASHER": "bcrypt"}):
            with self.assertRaises(ValueError) as ctx:
                hash_password("testpassword")
            self.assertIn("bcrypt", str(ctx.exception))


# ── Auth: PASSWORD_MIN_LENGTH ────────────────────────────────────────────────


class TestPasswordMinLengthSetting(unittest.TestCase):
    """PASSWORD_MIN_LENGTH wired into password validators."""

    def test_default_min_length_8(self):
        """Default PASSWORD_MIN_LENGTH=8 rejects 7-char passwords."""
        import hyperdjango.auth.validators as v

        v._DEFAULT_VALIDATORS = None  # Reset cached validators
        with patch.dict(
            DEFAULTS, {"PASSWORD_MIN_LENGTH": 8, "AUTH_PASSWORD_VALIDATORS": []}
        ):
            validators = v.get_default_validators()
            min_v = [x for x in validators if isinstance(x, v.MinLengthValidator)]
            self.assertEqual(len(min_v), 1)
            self.assertEqual(min_v[0].min_length, 8)
        v._DEFAULT_VALIDATORS = None

    def test_custom_min_length_12(self):
        """PASSWORD_MIN_LENGTH=12 creates MinLengthValidator(min_length=12)."""
        import hyperdjango.auth.validators as v

        v._DEFAULT_VALIDATORS = None
        with patch.dict(
            DEFAULTS, {"PASSWORD_MIN_LENGTH": 12, "AUTH_PASSWORD_VALIDATORS": []}
        ):
            validators = v.get_default_validators()
            min_v = [x for x in validators if isinstance(x, v.MinLengthValidator)]
            self.assertEqual(min_v[0].min_length, 12)
        v._DEFAULT_VALIDATORS = None


# ── Auth: AUTH_PASSWORD_VALIDATORS ───────────────────────────────────────────


class TestAuthPasswordValidatorsSetting(unittest.TestCase):
    """AUTH_PASSWORD_VALIDATORS wired into get_default_validators()."""

    def test_empty_uses_builtins(self):
        """Empty AUTH_PASSWORD_VALIDATORS uses built-in chain."""
        import hyperdjango.auth.validators as v

        v._DEFAULT_VALIDATORS = None
        with patch.dict(
            DEFAULTS, {"AUTH_PASSWORD_VALIDATORS": [], "PASSWORD_MIN_LENGTH": 8}
        ):
            validators = v.get_default_validators()
            self.assertEqual(len(validators), 5)
        v._DEFAULT_VALIDATORS = None

    def test_custom_validator_by_instance(self):
        """AUTH_PASSWORD_VALIDATORS with instances uses those directly."""
        import hyperdjango.auth.validators as v

        v._DEFAULT_VALIDATORS = None
        custom = [v.MinLengthValidator(min_length=20)]
        with patch.dict(DEFAULTS, {"AUTH_PASSWORD_VALIDATORS": custom}):
            validators = v.get_default_validators()
            self.assertEqual(len(validators), 1)
            self.assertEqual(validators[0].min_length, 20)
        v._DEFAULT_VALIDATORS = None

    def test_custom_validator_by_dict(self):
        """AUTH_PASSWORD_VALIDATORS with dict entries resolves class path."""
        import hyperdjango.auth.validators as v

        v._DEFAULT_VALIDATORS = None
        configured = [
            {
                "NAME": "hyperdjango.auth.validators.MinLengthValidator",
                "OPTIONS": {"min_length": 15},
            }
        ]
        with patch.dict(DEFAULTS, {"AUTH_PASSWORD_VALIDATORS": configured}):
            validators = v.get_default_validators()
            self.assertEqual(len(validators), 1)
            self.assertIsInstance(validators[0], v.MinLengthValidator)
            self.assertEqual(validators[0].min_length, 15)
        v._DEFAULT_VALIDATORS = None


# ── App: SECRET_KEY, DEBUG, ALLOWED_HOSTS, MAX_BODY_SIZE ─────────────────────


class TestAppSecretKeySetting(unittest.TestCase):
    """SECRET_KEY wired into HyperApp."""

    def test_default_from_settings(self):
        """HyperApp reads SECRET_KEY from conf settings."""
        from hyperdjango.app import HyperApp

        with patch.dict(DEFAULTS, {"SECRET_KEY": "my-secret-123"}):
            app = HyperApp()
            self.assertEqual(app.secret_key, "my-secret-123")

    def test_constructor_overrides(self):
        """Constructor secret_key= overrides conf setting."""
        from hyperdjango.app import HyperApp

        with patch.dict(DEFAULTS, {"SECRET_KEY": "from-conf"}):
            app = HyperApp(secret_key="from-constructor")
            self.assertEqual(app.secret_key, "from-constructor")


class TestAppDebugSetting(unittest.TestCase):
    """DEBUG wired into HyperApp."""

    def test_debug_from_settings(self):
        """HyperApp reads DEBUG from conf settings."""
        from hyperdjango.app import HyperApp

        with patch.dict(DEFAULTS, {"DEBUG": True}):
            app = HyperApp()
            self.assertTrue(app.debug)

    def test_constructor_overrides_debug(self):
        """Constructor debug= overrides conf setting."""
        from hyperdjango.app import HyperApp

        with patch.dict(DEFAULTS, {"DEBUG": True}):
            app = HyperApp(debug=False)
            self.assertFalse(app.debug)


class TestAppAllowedHostsSetting(unittest.TestCase):
    """ALLOWED_HOSTS wired into HyperApp."""

    def test_allowed_hosts_from_settings(self):
        """HyperApp reads ALLOWED_HOSTS from conf settings."""
        from hyperdjango.app import HyperApp

        with patch.dict(DEFAULTS, {"ALLOWED_HOSTS": ["example.com"]}):
            app = HyperApp()
            self.assertEqual(app.allowed_hosts, ["example.com"])

    def test_constructor_overrides(self):
        """Constructor allowed_hosts= overrides conf setting."""
        from hyperdjango.app import HyperApp

        with patch.dict(DEFAULTS, {"ALLOWED_HOSTS": ["from-conf.com"]}):
            app = HyperApp(allowed_hosts=["from-constructor.com"])
            self.assertEqual(app.allowed_hosts, ["from-constructor.com"])


class TestAppMaxBodySizeSetting(unittest.TestCase):
    """MAX_BODY_SIZE wired into HyperApp."""

    def test_max_body_size_from_settings(self):
        """HyperApp reads MAX_BODY_SIZE from conf settings."""
        from hyperdjango.app import HyperApp

        with patch.dict(DEFAULTS, {"MAX_BODY_SIZE": 5 * 1024 * 1024}):
            app = HyperApp()
            self.assertEqual(app.max_body_size, 5 * 1024 * 1024)

    def test_constructor_overrides(self):
        """Constructor max_body_size= overrides conf setting."""
        from hyperdjango.app import HyperApp

        with patch.dict(DEFAULTS, {"MAX_BODY_SIZE": 5 * 1024 * 1024}):
            app = HyperApp(max_body_size=1024)
            self.assertEqual(app.max_body_size, 1024)


# ── Storage: MAX_UPLOAD_SIZE, FILE_UPLOAD_PERMISSIONS, etc. ──────────────────


class TestStorageMaxUploadSize(unittest.TestCase):
    """MAX_UPLOAD_SIZE wired into FileSystemStorage.save()."""

    def test_upload_exceeds_max_size(self):
        """Files larger than MAX_UPLOAD_SIZE are rejected."""
        from hyperdjango.storage import FileSystemStorage

        with patch.dict(DEFAULTS, {"MAX_UPLOAD_SIZE": 100}):
            storage = FileSystemStorage(
                location="/tmp/test-uploads", base_url="/media/"
            )
            content = b"x" * 200
            with self.assertRaises(ValueError) as ctx:
                run_async(storage.save("big.txt", content))
            self.assertIn("MAX_UPLOAD_SIZE", str(ctx.exception))

    def test_upload_within_limit(self):
        """Files within MAX_UPLOAD_SIZE are accepted."""
        import tempfile

        from hyperdjango.storage import FileSystemStorage

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict(
                DEFAULTS,
                {
                    "MAX_UPLOAD_SIZE": 1000,
                    "ALLOWED_UPLOAD_EXTENSIONS": [],
                    "FILE_UPLOAD_DIRECTORY_PERMISSIONS": 0o755,
                    "FILE_UPLOAD_PERMISSIONS": 0o644,
                    "FILE_UPLOAD_TEMP_DIR": "",
                },
            ),
        ):
            storage = FileSystemStorage(location=tmpdir, base_url="/media/")
            name = run_async(storage.save("small.txt", b"hello"))
            self.assertEqual(name, "small.txt")


class TestStorageAllowedExtensions(unittest.TestCase):
    """ALLOWED_UPLOAD_EXTENSIONS wired into FileSystemStorage.save()."""

    def test_disallowed_extension_rejected(self):
        """Extensions not in ALLOWED_UPLOAD_EXTENSIONS are rejected."""
        from hyperdjango.storage import FileSystemStorage

        with patch.dict(
            DEFAULTS,
            {
                "ALLOWED_UPLOAD_EXTENSIONS": [".jpg", ".png"],
                "MAX_UPLOAD_SIZE": 10 * 1024 * 1024,
            },
        ):
            storage = FileSystemStorage(
                location="/tmp/test-uploads", base_url="/media/"
            )
            with self.assertRaises(ValueError) as ctx:
                run_async(storage.save("evil.exe", b"content"))
            self.assertIn("ALLOWED_UPLOAD_EXTENSIONS", str(ctx.exception))

    def test_allowed_extension_accepted(self):
        """Extensions in ALLOWED_UPLOAD_EXTENSIONS are accepted."""
        import tempfile

        from hyperdjango.storage import FileSystemStorage

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict(
                DEFAULTS,
                {
                    "ALLOWED_UPLOAD_EXTENSIONS": [".jpg", ".png"],
                    "MAX_UPLOAD_SIZE": 10 * 1024 * 1024,
                    "FILE_UPLOAD_DIRECTORY_PERMISSIONS": 0o755,
                    "FILE_UPLOAD_PERMISSIONS": 0o644,
                    "FILE_UPLOAD_TEMP_DIR": "",
                },
            ),
        ):
            storage = FileSystemStorage(location=tmpdir, base_url="/media/")
            name = run_async(storage.save("photo.jpg", b"jpeg-content"))
            self.assertEqual(name, "photo.jpg")

    def test_empty_allowed_extensions_permits_all(self):
        """Empty ALLOWED_UPLOAD_EXTENSIONS allows any extension."""
        import tempfile

        from hyperdjango.storage import FileSystemStorage

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict(
                DEFAULTS,
                {
                    "ALLOWED_UPLOAD_EXTENSIONS": [],
                    "MAX_UPLOAD_SIZE": 10 * 1024 * 1024,
                    "FILE_UPLOAD_DIRECTORY_PERMISSIONS": 0o755,
                    "FILE_UPLOAD_PERMISSIONS": 0o644,
                    "FILE_UPLOAD_TEMP_DIR": "",
                },
            ),
        ):
            storage = FileSystemStorage(location=tmpdir, base_url="/media/")
            name = run_async(storage.save("anything.xyz", b"content"))
            self.assertEqual(name, "anything.xyz")


class TestStorageFileUploadPermissions(unittest.TestCase):
    """FILE_UPLOAD_PERMISSIONS wired into FileSystemStorage.save()."""

    def test_file_permissions_applied(self):
        """FILE_UPLOAD_PERMISSIONS is applied to saved files."""
        import stat
        import tempfile
        from pathlib import Path

        from hyperdjango.storage import FileSystemStorage

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict(
                DEFAULTS,
                {
                    "FILE_UPLOAD_PERMISSIONS": 0o600,
                    "MAX_UPLOAD_SIZE": 10 * 1024 * 1024,
                    "ALLOWED_UPLOAD_EXTENSIONS": [],
                    "FILE_UPLOAD_DIRECTORY_PERMISSIONS": 0o755,
                    "FILE_UPLOAD_TEMP_DIR": "",
                },
            ),
        ):
            storage = FileSystemStorage(location=tmpdir, base_url="/media/")
            name = run_async(storage.save("secret.txt", b"secret-content"))
            full_path = Path(tmpdir) / name
            mode = stat.S_IMODE(full_path.stat().st_mode)
            self.assertEqual(mode, 0o600)


class TestStorageTempDir(unittest.TestCase):
    """FILE_UPLOAD_TEMP_DIR wired into FileSystemStorage.save()."""

    def test_custom_temp_dir(self):
        """FILE_UPLOAD_TEMP_DIR is used for atomic writes."""
        import tempfile

        from hyperdjango.storage import FileSystemStorage

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            tempfile.TemporaryDirectory() as temp_upload_dir,
            patch.dict(
                DEFAULTS,
                {
                    "FILE_UPLOAD_TEMP_DIR": temp_upload_dir,
                    "MAX_UPLOAD_SIZE": 10 * 1024 * 1024,
                    "ALLOWED_UPLOAD_EXTENSIONS": [],
                    "FILE_UPLOAD_DIRECTORY_PERMISSIONS": 0o755,
                    "FILE_UPLOAD_PERMISSIONS": 0o644,
                },
            ),
        ):
            storage = FileSystemStorage(location=tmpdir, base_url="/media/")
            name = run_async(storage.save("test.txt", b"content"))
            self.assertEqual(name, "test.txt")


# ── Mail: ADMINS / MANAGERS ──────────────────────────────────────────────────


class TestMailAdminsSetting(unittest.TestCase):
    """ADMINS wired into mail_admins()."""

    def test_mail_admins_sends_to_configured(self):
        """mail_admins() sends to ADMINS email addresses."""
        from hyperdjango.mail import (
            clear_outbox,
            configure_mail,
            get_outbox,
            mail_admins,
        )

        configure_mail(backend="memory")
        clear_outbox()
        with patch.dict(
            DEFAULTS,
            {
                "ADMINS": [("Admin", "admin@example.com"), ("Dev", "dev@example.com")],
                "SERVER_EMAIL": "server@example.com",
                "EMAIL_BACKEND": "memory",
            },
        ):
            result = run_async(mail_admins("Error!", "Something broke"))
            self.assertTrue(result)
            outbox = get_outbox()
            self.assertEqual(len(outbox), 1)
            self.assertIn("admin@example.com", outbox[0].recipients)
            self.assertIn("dev@example.com", outbox[0].recipients)
        clear_outbox()

    def test_mail_admins_empty_returns_false(self):
        """mail_admins() returns False when ADMINS is empty."""
        from hyperdjango.mail import mail_admins

        with patch.dict(DEFAULTS, {"ADMINS": []}):
            result = run_async(mail_admins("Error!", "Something broke"))
            self.assertFalse(result)


class TestMailManagersSetting(unittest.TestCase):
    """MANAGERS wired into mail_managers()."""

    def test_mail_managers_sends_to_configured(self):
        """mail_managers() sends to MANAGERS email addresses."""
        from hyperdjango.mail import (
            clear_outbox,
            configure_mail,
            get_outbox,
            mail_managers,
        )

        configure_mail(backend="memory")
        clear_outbox()
        with patch.dict(
            DEFAULTS,
            {
                "MANAGERS": [("Manager", "mgr@example.com")],
                "SERVER_EMAIL": "server@example.com",
                "EMAIL_BACKEND": "memory",
            },
        ):
            result = run_async(mail_managers("Broken link", "/bad-url"))
            self.assertTrue(result)
            outbox = get_outbox()
            self.assertEqual(len(outbox), 1)
            self.assertIn("mgr@example.com", outbox[0].recipients)
        clear_outbox()

    def test_mail_managers_empty_returns_false(self):
        """mail_managers() returns False when MANAGERS is empty."""
        from hyperdjango.mail import mail_managers

        with patch.dict(DEFAULTS, {"MANAGERS": []}):
            result = run_async(mail_managers("Broken link", "/bad-url"))
            self.assertFalse(result)


# ── Middleware: DISALLOWED_USER_AGENTS ───────────────────────────────────────


class TestDisallowedUserAgents(unittest.TestCase):
    """DISALLOWED_USER_AGENTS wired into SecurityHeadersMiddleware."""

    def test_blocked_user_agent_gets_403(self):
        """Requests with a disallowed User-Agent get 403 Forbidden."""
        with patch.dict(
            DEFAULTS, {"DISALLOWED_USER_AGENTS": ["BadBot", "EvilCrawler"]}
        ):
            mw = SecurityHeadersMiddleware()
            req = make_request(headers={"user-agent": "BadBot/1.0"})
            resp = run_async(mw(req, dummy_handler))
            self.assertEqual(resp.status, 403)

    def test_allowed_user_agent_passes(self):
        """Requests with a normal User-Agent pass through."""
        with patch.dict(DEFAULTS, {"DISALLOWED_USER_AGENTS": ["BadBot"]}):
            mw = SecurityHeadersMiddleware()
            req = make_request(headers={"user-agent": "Mozilla/5.0 Chrome"})
            resp = run_async(mw(req, dummy_handler))
            self.assertEqual(resp.status, 200)

    def test_empty_disallowed_agents_allows_all(self):
        """Empty DISALLOWED_USER_AGENTS allows all User-Agents."""
        with patch.dict(DEFAULTS, {"DISALLOWED_USER_AGENTS": []}):
            mw = SecurityHeadersMiddleware()
            req = make_request(headers={"user-agent": "AnyBot/1.0"})
            resp = run_async(mw(req, dummy_handler))
            self.assertEqual(resp.status, 200)

    def test_regex_pattern_matching(self):
        """DISALLOWED_USER_AGENTS supports regex patterns."""
        with patch.dict(DEFAULTS, {"DISALLOWED_USER_AGENTS": [r".*[Ss]craper.*"]}):
            mw = SecurityHeadersMiddleware()
            req = make_request(headers={"user-agent": "MyScraper v2"})
            resp = run_async(mw(req, dummy_handler))
            self.assertEqual(resp.status, 403)


# ── App: HTTP_SERVER / THREAD_POOL_SIZE ──────────────────────────────────────


class TestAppServerSettings(unittest.TestCase):
    """HTTP_SERVER and THREAD_POOL_SIZE settings exist and are readable."""

    def test_http_server_default(self):
        """HTTP_SERVER defaults to 'auto'."""
        self.assertEqual(DEFAULTS["HTTP_SERVER"], "auto")

    def test_thread_pool_size_default(self):
        """THREAD_POOL_SIZE defaults to 24."""
        self.assertEqual(DEFAULTS["THREAD_POOL_SIZE"], 24)

    def test_app_reads_max_body_size(self):
        """HyperApp reads MAX_BODY_SIZE from settings."""
        from hyperdjango.app import HyperApp

        with patch.dict(DEFAULTS, {"MAX_BODY_SIZE": 2048}):
            app = HyperApp()
            self.assertEqual(app.max_body_size, 2048)


def _flatten(suite: unittest.TestSuite) -> list[unittest.TestCase]:
    """Depth-first list of the individual TestCases inside ``suite``, in the
    exact order unittest will execute them."""
    cases: list[unittest.TestCase] = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            cases.extend(_flatten(item))
        else:
            cases.append(item)
    return cases


def main() -> bool:
    # Run tests (unchanged execution: one TextTestRunner pass over the module),
    # then replay the per-test outcome through the counted harness so the runner
    # sees a real N-passed/M-failed tally instead of a single opaque exit code.
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    # Snapshot the ids BEFORE running: TestSuite drops each case from itself as
    # it completes (memory reclamation), so the suite is empty afterwards.
    case_ids = [case.id() for case in _flatten(suite)]
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    bad: dict[str, str] = {}
    for case, tb in list(result.failures) + list(result.errors):
        bad[case.id()] = tb.strip().splitlines()[-1]

    print()
    for case_id in case_ids:
        check(case_id, case_id not in bad, bad.get(case_id, ""))
    print()
    return finish()


if __name__ == "__main__":
    run_main(main)
