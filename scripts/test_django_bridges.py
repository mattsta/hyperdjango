#!/usr/bin/env python3
"""Test all 4 Django integration bridges.

Tests that HyperDjango components plug INTO Django's extension points:
1. Template Backend — ZigTemplates via Django's TEMPLATES setting
2. Middleware — CORS, security, timing, rate-limit as Django middleware
3. Auth Backend — OAuth2 as Django AUTHENTICATION_BACKENDS
4. Manager — HyperManager with pipeline on Django models

Run: uv run hyper-test django_bridges
"""

# hyper-test: db_django

import os
import sys
import tempfile
from pathlib import Path

# Setup Django BEFORE imports
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")


def main():
    passed = 0
    failed = 0

    def check(name, condition, detail=""):
        nonlocal passed, failed
        if condition:
            print(f"  PASS: {name}")
            passed += 1
        else:
            print(f"  FAIL: {name} — {detail}")
            failed += 1

    # ── Bridge 1: Template Backend ────────────────────────────────────────
    print("\n=== Bridge 1: Django Template Backend ===")

    # Create a temp template directory with a test template
    with tempfile.TemporaryDirectory() as tmpdir:
        # Write a test template
        tmppath = Path(tmpdir)
        (tmppath / "hello.html").write_text(
            "<h1>Hello {{ name }}!</h1><p>{{ 2 + 3 }} items</p>"
        )

        (tmppath / "loop.html").write_text(
            "{% for item in items %}{{ item }} {% endfor %}"
        )

        (tmppath / "math.html").write_text("{{ price * qty }}")

        # Test ZigTemplates backend directly
        from hyperdjango.serving.template_backend import ZigTemplates

        backend = ZigTemplates(
            {
                "NAME": "zig",
                "DIRS": [tmpdir],
                "APP_DIRS": False,
                "OPTIONS": {
                    "autoescape": True,
                    "context_processors": [],
                },
            }
        )

        # get_template
        tmpl = backend.get_template("hello.html")
        check("get_template loads", tmpl is not None)

        html = tmpl.render({"name": "World"})
        check("render basic", "<h1>Hello World!</h1>" in html, f"got: {html}")
        check("render math expr", "5 items" in html, f"got: {html}")

        # Loop template
        tmpl2 = backend.get_template("loop.html")
        html2 = tmpl2.render({"items": ["a", "b", "c"]})
        check("render for loop", html2.strip() == "a b c", f"got: {html2}")

        # Math template
        tmpl3 = backend.get_template("math.html")
        html3 = tmpl3.render({"price": 10, "qty": 5})
        check("render math", html3.strip() == "50", f"got: {html3}")

        # from_string
        tmpl4 = backend.from_string("{{ x + y }}")
        html4 = tmpl4.render({"x": 3, "y": 4})
        check("from_string", html4.strip() == "7", f"got: {html4}")

        # TemplateDoesNotExist
        from django.template import TemplateDoesNotExist

        try:
            backend.get_template("nonexistent.html")
            check("template not found raises", False, "no exception raised")
        except TemplateDoesNotExist:
            check("template not found raises", True)

        # Origin
        tmpl5 = backend.get_template("hello.html")
        check("origin has name", tmpl5.origin.name.endswith("hello.html"))
        check("origin has template_name", tmpl5.origin.template_name == "hello.html")

    # ── Bridge 2: Django Middleware ────────────────────────────────────────
    print("\n=== Bridge 2: Django Middleware Wrappers ===")

    from django.http import HttpResponse
    from django.test import RequestFactory

    factory = RequestFactory()

    # CORS Middleware. CORS_ORIGINS defaults to [] (deny-all cross-origin — the
    # secure default); a header is added only for a configured/allowed origin.
    from hyperdjango.conf import DEFAULTS
    from hyperdjango.serving.django_middleware import HyperCORSMiddleware

    def dummy_view(request):
        return HttpResponse("OK")

    # Deny-by-default: no Access-Control-Allow-Origin when origins unconfigured.
    cors_denied = HyperCORSMiddleware(dummy_view)
    req = factory.get("/api/data", HTTP_ORIGIN="https://example.com")
    resp = cors_denied(req)
    check(
        "cors denies unconfigured origin (secure default)",
        resp.get("Access-Control-Allow-Origin") is None,
        f"headers: {dict(resp.items())}",
    )

    # Configured origins → the header is added.
    _prev = DEFAULTS.get("CORS_ORIGINS")
    DEFAULTS["CORS_ORIGINS"] = ["*"]
    try:
        cors = HyperCORSMiddleware(dummy_view)
        req = factory.get("/api/data", HTTP_ORIGIN="https://example.com")
        resp = cors(req)
        check(
            "cors adds origin header when configured",
            resp.get("Access-Control-Allow-Origin") is not None,
            f"headers: {dict(resp.items())}",
        )

        # Preflight
        req = factory.options("/api/data", HTTP_ORIGIN="https://example.com")
        resp = cors(req)
        check("cors preflight 204", resp.status_code == 204)
    finally:
        DEFAULTS["CORS_ORIGINS"] = _prev
    check(
        "cors preflight methods", "GET" in resp.get("Access-Control-Allow-Methods", "")
    )

    # Security: wildcard + credentials is the account-takeover combination and
    # must be refused at construction (same rule as the ASGI CORSMiddleware).
    DEFAULTS["CORS_ORIGINS"] = ["*"]
    try:
        import django.conf as _djconf

        _djconf.settings.HYPERDJANGO_CORS_CREDENTIALS = True
        raised = False
        try:
            HyperCORSMiddleware(dummy_view)
        except ValueError:
            raised = True
        finally:
            del _djconf.settings.HYPERDJANGO_CORS_CREDENTIALS
        check(
            "cors rejects wildcard + credentials (no arbitrary-origin reflection)",
            raised,
        )
    finally:
        DEFAULTS["CORS_ORIGINS"] = _prev

    # Security: an allowlisted origin echoed back must carry Vary: Origin so a
    # shared cache can't serve one origin's ACAO to another.
    DEFAULTS["CORS_ORIGINS"] = ["https://ok.example"]
    try:
        cors_allow = HyperCORSMiddleware(dummy_view)
        req = factory.get("/api/data", HTTP_ORIGIN="https://ok.example")
        resp = cors_allow(req)
        check(
            "cors echoes allowlisted origin",
            resp.get("Access-Control-Allow-Origin") == "https://ok.example",
        )
        check(
            "cors sets Vary: Origin on echoed origin", "Origin" in resp.get("Vary", "")
        )
        # A non-allowlisted origin gets no CORS header even with one configured.
        req2 = factory.get("/api/data", HTTP_ORIGIN="https://evil.example")
        resp2 = cors_allow(req2)
        check(
            "cors denies non-allowlisted origin",
            resp2.get("Access-Control-Allow-Origin") is None,
        )
    finally:
        DEFAULTS["CORS_ORIGINS"] = _prev

    # Security Middleware
    from hyperdjango.serving.django_middleware import HyperSecurityMiddleware

    sec = HyperSecurityMiddleware(dummy_view)
    req = factory.get("/")
    resp = sec(req)
    check("security nosniff", resp.get("X-Content-Type-Options") == "nosniff")
    check("security frame deny", resp.get("X-Frame-Options") == "DENY")
    # The Django path builds the SAME header set as the ASGI SecurityHeadersMiddleware
    # (one shared authority), so it must also carry Referrer-Policy + COOP by default.
    check(
        "security referrer-policy (parity with ASGI path)",
        resp.get("Referrer-Policy") == "same-origin",
    )
    check(
        "security cross-origin-opener-policy (parity with ASGI path)",
        resp.get("Cross-Origin-Opener-Policy") == "same-origin",
    )

    # Timing Middleware
    from hyperdjango.serving.django_middleware import HyperTimingMiddleware

    timing = HyperTimingMiddleware(dummy_view)
    req = factory.get("/")
    resp = timing(req)
    check("timing header", "X-Response-Time" in resp)
    check("timing format", resp["X-Response-Time"].endswith("s"))

    # Rate Limit Middleware
    # Override settings for testing
    from django.conf import settings

    from hyperdjango.serving.django_middleware import HyperRateLimitMiddleware

    settings.HYPERDJANGO_RATE_LIMIT_REQUESTS = 3
    settings.HYPERDJANGO_RATE_LIMIT_WINDOW = 60

    rate = HyperRateLimitMiddleware(dummy_view)
    for i in range(3):
        req = factory.get("/", REMOTE_ADDR="1.2.3.4")
        resp = rate(req)
    check("rate limit allows", resp.status_code == 200)

    req = factory.get("/", REMOTE_ADDR="1.2.3.4")
    resp = rate(req)
    check("rate limit blocks", resp.status_code == 429)
    check("rate limit retry-after", "Retry-After" in resp)

    # SECURITY: X-Forwarded-For must NOT be trusted without a configured proxy,
    # or an attacker sends a unique XFF per request and bypasses the limiter.
    # With the same peer (1.2.3.4) already over limit, a spoofed XFF stays 429.
    rate2 = HyperRateLimitMiddleware(dummy_view)
    for _ in range(3):
        rate2(factory.get("/", REMOTE_ADDR="9.9.9.9"))
    spoofed = factory.get("/", REMOTE_ADDR="9.9.9.9", HTTP_X_FORWARDED_FOR="1.1.1.1")
    resp = rate2(spoofed)
    check(
        "rate limit not bypassable via X-Forwarded-For spoofing",
        resp.status_code == 429,
    )

    # ── Bridge 3: Auth Backend ────────────────────────────────────────────
    print("\n=== Bridge 3: Django Auth Backend ===")

    from hyperdjango.serving.auth_backends import OAuth2Backend

    backend = OAuth2Backend()

    # authenticate with no args returns None
    result = backend.authenticate(None)
    check("no args returns None", result is None)

    # authenticate with empty email returns None
    result = backend.authenticate(
        None, oauth2_provider="google", oauth2_profile={"email": "", "name": "Test"}
    )
    check("empty email returns None", result is None)

    # SECURITY: a NON-empty but UNVERIFIED email must be rejected — linking an
    # account by an address the user never proved they own is account takeover.
    result = backend.authenticate(
        None,
        oauth2_provider="google",
        oauth2_profile={
            "email": "victim@example.com",
            "email_verified": False,
            "name": "X",
        },
    )
    check("unverified email returns None (no takeover)", result is None)
    # Missing email_verified is treated as unverified (deny by default).
    result = backend.authenticate(
        None,
        oauth2_provider="google",
        oauth2_profile={"email": "victim@example.com", "name": "X"},
    )
    check("missing email_verified returns None (fail closed)", result is None)

    # authenticate with valid profile (requires Django User model)
    # This would need a database — test the interface exists
    check("backend has authenticate", hasattr(backend, "authenticate"))
    check("backend has get_user", hasattr(backend, "get_user"))

    # ── Bridge 4: HyperManager ────────────────────────────────────────────
    print("\n=== Bridge 4: Django ORM HyperManager ===")

    # Verify it's a Django Manager subclass
    from django.db import models as django_models

    from hyperdjango.serving.django_managers import HyperManager

    check("is Django Manager", issubclass(HyperManager, django_models.Manager))
    check("has pipeline method", hasattr(HyperManager, "pipeline"))
    check("has bulk_load method", hasattr(HyperManager, "bulk_load"))
    check("has bulk_load_dict method", hasattr(HyperManager, "bulk_load_dict"))

    # Test pipeline fallback (no native extension needed)
    mgr = HyperManager()
    mgr.model = type(
        "FakeModel",
        (),
        {
            "_meta": type(
                "Meta",
                (),
                {
                    "db_table": "test",
                    "pk": type("PK", (), {"column": "id"})(),
                    "concrete_fields": [],
                },
            )()
        },
    )
    # Pipeline with empty list should work
    results = mgr.pipeline([])
    check("pipeline empty list", results == [])

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All Django bridge tests passed!")
    return failed


if __name__ == "__main__":
    import django

    django.setup()
    sys.exit(main())
