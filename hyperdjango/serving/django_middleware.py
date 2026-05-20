"""
Django middleware wrappers for HyperDjango's native middleware.

Drop-in replacements for common Django middleware packages:
- HyperCORSMiddleware → replaces django-cors-headers
- HyperSecurityMiddleware → replaces Django's SecurityMiddleware (enhanced)
- HyperTimingMiddleware → adds X-Response-Time header
- HyperRateLimitMiddleware → per-IP rate limiting (canonical token-bucket backend)
- HyperPerformanceMiddleware → query tracking + N+1 detection + dashboard

Usage in Django settings:

    MIDDLEWARE = [
        'hyperdjango.serving.django_middleware.HyperSecurityMiddleware',
        'hyperdjango.serving.django_middleware.HyperCORSMiddleware',
        'hyperdjango.serving.django_middleware.HyperTimingMiddleware',
        'hyperdjango.serving.django_middleware.HyperRateLimitMiddleware',
        'hyperdjango.serving.django_middleware.HyperPerformanceMiddleware',
        'django.middleware.common.CommonMiddleware',
        'django.contrib.sessions.middleware.SessionMiddleware',
        'django.contrib.auth.middleware.AuthenticationMiddleware',
        ...
    ]

    # Optional configuration in settings.py:
    HYPERDJANGO_CORS_ORIGINS = ['https://example.com']  # default: ['*']
    HYPERDJANGO_CORS_CREDENTIALS = True                  # default: False
    HYPERDJANGO_RATE_LIMIT_REQUESTS = 100                # requests per window
    HYPERDJANGO_RATE_LIMIT_WINDOW = 60                   # seconds
    HYPERDJANGO_LOAD_TEST = False                        # True disables throttling
    HYPERDJANGO_SLOW_QUERY_MS = 100                      # slow query threshold
"""

import re
import time
from collections import Counter

from django.conf import settings
from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.utils.cache import patch_vary_headers

from hyperdjango.client_ip import resolve_client_ip
from hyperdjango.conf import get_setting
from hyperdjango.cors import CorsPolicy
from hyperdjango.performance import PerformanceMiddleware, set_perf_middleware
from hyperdjango.ratelimit import (
    PROBLEM_QUOTA_EXCEEDED,
    InMemoryRateLimitBackend,
    QuotaPolicy,
    ServiceLimit,
    _log_load_test_bypass_once,
    build_problem_detail,
    format_ratelimit,
    format_ratelimit_policy,
)
from hyperdjango.security_headers import build_security_headers
from hyperdjango.serving.admin import analyze_queries


class HyperCORSMiddleware:
    """CORS middleware for Django. Zero external dependencies.

    Replaces django-cors-headers with built-in CORS handling.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        # Honor the declared CORS_ORIGINS setting (Django HYPERDJANGO_CORS_ORIGINS /
        # env HYPER_CORS_ORIGINS / default []). Default is DENY-all cross-origin —
        # never the fail-open ["*"] the old direct getattr used.
        self.origins = get_setting("CORS_ORIGINS")
        # dynamic-attr: optional Django settings attr, absent unless the deploying project defines it
        self.methods = getattr(
            settings,
            "HYPERDJANGO_CORS_METHODS",
            ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        )
        # dynamic-attr: optional Django settings attr, absent unless the deploying project defines it
        self.headers = getattr(settings, "HYPERDJANGO_CORS_HEADERS", ["*"])
        # dynamic-attr: optional Django settings attr, absent unless the deploying project defines it
        self.credentials = getattr(settings, "HYPERDJANGO_CORS_CREDENTIALS", False)
        # dynamic-attr: optional Django settings attr, absent unless the deploying project defines it
        self.max_age = getattr(settings, "HYPERDJANGO_CORS_MAX_AGE", 86400)
        # The same CORS decision authority the ASGI CORSMiddleware uses — it
        # rejects the wildcard+credentials account-takeover combination here and
        # drives the per-request Origin decision below.
        self._policy = CorsPolicy(self.origins, self.credentials)

    def __call__(self, request):
        origin = request.META.get("HTTP_ORIGIN", "")

        # Handle preflight
        if request.method == "OPTIONS" and origin:
            response = HttpResponse(status=204)
            self._set_cors_headers(response, origin)
            response["Access-Control-Allow-Methods"] = ", ".join(self.methods)
            response["Access-Control-Allow-Headers"] = ", ".join(self.headers)
            response["Access-Control-Max-Age"] = str(self.max_age)
            return response

        response = self.get_response(request)

        if origin:
            self._set_cors_headers(response, origin)

        return response

    def _set_cors_headers(self, response, origin):
        decision = self._policy.resolve(origin)
        if decision is None:
            return
        response["Access-Control-Allow-Origin"] = decision.allow_origin
        if decision.allow_credentials:
            response["Access-Control-Allow-Credentials"] = "true"
        if decision.vary_origin:
            # Echoing a specific origin → the response varies by Origin, so a
            # shared cache can't serve one origin's ACAO to another.
            patch_vary_headers(response, ("Origin",))


class HyperSecurityMiddleware:
    """Security headers middleware for Django.

    Adds standard security headers to all responses.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        # Resolve from the same conf settings as the ASGI SecurityHeadersMiddleware
        # (get_setting bridges to Django's HYPERDJANGO_* settings) and build the
        # one shared header set, so Django responses carry the SAME protections
        # (Referrer-Policy, Cross-Origin-Opener-Policy, HSTS, CSP, …) as the ASGI
        # path instead of a hand-maintained subset that silently drifts weaker.
        self._headers = build_security_headers(
            nosniff=get_setting("SECURE_CONTENT_TYPE_NOSNIFF"),
            frame_options=get_setting("X_FRAME_OPTIONS"),
            hsts_seconds=int(get_setting("SECURE_HSTS_SECONDS")),
            hsts_include_subdomains=get_setting("SECURE_HSTS_INCLUDE_SUBDOMAINS"),
            hsts_preload=get_setting("SECURE_HSTS_PRELOAD"),
            csp=get_setting("SECURE_CSP"),
            referrer_policy=get_setting("SECURE_REFERRER_POLICY"),
            # permissions-policy has no conf setting (ASGI exposes it via a
            # constructor param only), so it is not set on the Django path.
            permissions_policy=None,
            cross_origin_opener_policy=get_setting("SECURE_CROSS_ORIGIN_OPENER_POLICY"),
        )

    def __call__(self, request):
        response = self.get_response(request)
        for name, value in self._headers.items():
            response[name] = value
        return response


class HyperTimingMiddleware:
    """Response timing middleware for Django.

    Adds X-Response-Time header to all responses.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        start = time.perf_counter()
        response = self.get_response(request)
        elapsed = time.perf_counter() - start
        response["X-Response-Time"] = f"{elapsed:.6f}s"
        return response


class HyperRateLimitMiddleware:
    """Per-IP rate limiting for the Django middleware chain.

    Delegates to the canonical ``InMemoryRateLimitBackend`` (the same O(1)
    windowed-token-bucket engine the ASGI ``hyperdjango.ratelimit.RateLimitMiddleware``
    uses), so both entry points share one algorithm and one memory bound. This
    class only adapts it to Django's sync middleware protocol and response type.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        # Request ceiling per window (mirrors the sibling
        # HYPERDJANGO_RATE_LIMIT_WINDOW read below).
        # dynamic-attr: optional Django settings attr, absent unless the deploying project defines it
        self.limit = getattr(settings, "HYPERDJANGO_RATE_LIMIT_REQUESTS", 100)
        # dynamic-attr: optional Django settings attr, absent unless the deploying project defines it
        self.window = getattr(settings, "HYPERDJANGO_RATE_LIMIT_WINDOW", 60)
        # LOAD_TEST bypass. Honors HYPERDJANGO_LOAD_TEST (Django) and
        # HYPER_LOAD_TEST (env) via get_setting.
        self._load_test = bool(get_setting("LOAD_TEST"))
        if self._load_test:
            _log_load_test_bypass_once()
        # The canonical rate-limit engine: an O(1) windowed token bucket, sharded
        # 16 ways, with a hard per-shard LRU bucket cap (RATELIMIT_MAX_BUCKETS) so
        # a flood of distinct IPs can't OOM the worker. Shared with the ASGI path.
        self._backend = InMemoryRateLimitBackend()
        # dynamic-attr: optional Django settings attr, absent unless the deploying project defines it
        self._include_ietf = getattr(
            settings, "HYPERDJANGO_RATELIMIT_IETF_HEADERS", True
        )
        # dynamic-attr: optional Django settings attr, absent unless the deploying project defines it
        self._include_legacy = getattr(
            settings, "HYPERDJANGO_RATELIMIT_LEGACY_HEADERS", True
        )
        # dynamic-attr: optional Django settings attr, absent unless the deploying project defines it
        self._include_problem_details = getattr(
            settings, "HYPERDJANGO_RATELIMIT_PROBLEM_DETAILS", True
        )

    def _set_headers(
        self, response, policies: list[QuotaPolicy], limits: list[ServiceLimit]
    ) -> None:
        """Set IETF and/or legacy rate limit headers on a Django response."""
        if self._include_ietf:
            response["RateLimit-Policy"] = format_ratelimit_policy(policies)
            response["RateLimit"] = format_ratelimit(limits)
        if self._include_legacy:
            if policies:
                response["X-RateLimit-Limit"] = str(policies[0].quota)
            if limits:
                response["X-RateLimit-Remaining"] = str(limits[0].remaining)

    def __call__(self, request):
        if self._load_test:
            return self.get_response(request)

        client_ip = self._get_client_ip(request)
        allowed, remaining, reset = self._backend.check_and_increment(
            client_ip, self.limit, self.window
        )

        policies = [QuotaPolicy(name="default", quota=self.limit, window=self.window)]

        if not allowed:
            retry_after = reset if reset > 0 else 1
            limits = [
                ServiceLimit(policy_name="default", remaining=0, reset=retry_after)
            ]
            if self._include_problem_details and self._include_ietf:
                body = build_problem_detail(
                    problem_type=PROBLEM_QUOTA_EXCEEDED,
                    title="Rate limit exceeded",
                    status=429,
                    detail="Quota exceeded for policy default",
                    violated_policies=["default"],
                )
                body["retry_after"] = retry_after
                response = JsonResponse(
                    body, status=429, content_type="application/problem+json"
                )
            else:
                # Unified {"detail","status"} shape; retry_after is carried by
                # the Retry-After header (set below), not a bespoke body field.
                response = JsonResponse(
                    {"detail": "Rate limit exceeded", "status": 429},
                    status=429,
                )
            response["Retry-After"] = str(retry_after)
            self._set_headers(response, policies, limits)
            return response

        limits = [ServiceLimit(policy_name="default", remaining=max(0, remaining))]
        response = self.get_response(request)
        self._set_headers(response, policies, limits)
        return response

    def _get_client_ip(self, request):
        # Same spoofing-resistant trust policy as the ASGI Request.client_ip:
        # X-Forwarded-For / X-Real-IP are honored only behind a configured
        # trusted proxy, so an attacker can't present a unique IP per request to
        # sidestep the limiter. Without that config, the socket peer is used.
        return resolve_client_ip(
            request.META.get("REMOTE_ADDR") or "unknown",
            request.META.get("HTTP_X_FORWARDED_FOR"),
            request.META.get("HTTP_X_REAL_IP"),
        )


class HyperPerformanceMiddleware:
    """Query performance tracking for Django.

    Tracks database queries per request, detects N+1 patterns,
    provides a dashboard at /debug/performance.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        # dynamic-attr: optional Django settings attr, absent unless the deploying project defines it
        self.slow_threshold = getattr(settings, "HYPERDJANGO_SLOW_QUERY_MS", 100)
        # dynamic-attr: optional Django settings attr, absent unless the deploying project defines it
        self.dashboard_path = getattr(
            settings, "HYPERDJANGO_PERF_DASHBOARD_PATH", "/debug/performance"
        )
        # dynamic-attr: DEBUG may be absent on a minimally-configured Django settings object
        self.enabled = getattr(settings, "DEBUG", False)

        self._perf = PerformanceMiddleware(
            slow_query_threshold_ms=self.slow_threshold,
            dashboard_path=self.dashboard_path,
            enabled=self.enabled,
        )
        set_perf_middleware(self._perf)

    def __call__(self, request):
        if not self.enabled:
            return self.get_response(request)

        # Dashboard endpoint
        if request.path == self.dashboard_path:
            stats = self._perf.get_stats()
            return self._render_dashboard(stats)
        if request.path == f"{self.dashboard_path}/json":
            return JsonResponse(self._perf.get_stats())

        # Track queries via Django's connection
        initial_count = len(connection.queries) if settings.DEBUG else 0

        start = time.perf_counter()
        response = self.get_response(request)
        elapsed = time.perf_counter() - start

        if settings.DEBUG:
            queries = connection.queries[initial_count:]
            query_count = len(queries)
            total_ms = sum(float(q.get("time", 0)) * 1000 for q in queries)

            # Attach stats to request for admin overlay
            request._hyper_perf_stats = analyze_queries(queries)

            response["X-Query-Count"] = str(query_count)
            response["X-Query-Time"] = f"{total_ms:.1f}ms"

            # N+1 detection
            sql_patterns = Counter()
            for q in queries:
                normalized = re.sub(r"'[^']*'", "'?'", q.get("sql", ""))
                normalized = re.sub(r"\b\d+\b", "?", normalized)
                sql_patterns[normalized] += 1

            n_plus_one = [p for p, c in sql_patterns.items() if c >= 5]
            if n_plus_one:
                response["X-N-Plus-One"] = str(len(n_plus_one))

        return response

    def _render_dashboard(self, stats):
        html = (
            "<h1>Performance Dashboard</h1><p>Enable DEBUG=True for query tracking.</p>"
        )
        return HttpResponse(html, content_type="text/html")


class HyperAutoPrefetchMiddleware:
    """Auto-detect N+1 query patterns and suggest fixes.

    Learns which views produce N+1 patterns by analyzing query logs.
    On subsequent requests to the same view, adds X-N-Plus-One-Suggestion
    headers with the recommended select_related/prefetch_related calls.

    Usage in Django settings:
        MIDDLEWARE = [
            'hyperdjango.serving.django_middleware.HyperAutoPrefetchMiddleware',
            ...
        ]

    Requires DEBUG = True (uses Django's connection.queries).
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self._learned = {}  # {view_path: [{pattern, suggestion, count}]}
        # dynamic-attr: optional Django settings attr, absent unless the deploying project defines it
        self._threshold = getattr(settings, "HYPERDJANGO_N_PLUS_ONE_THRESHOLD", 5)

    def __call__(self, request):
        # dynamic-attr: DEBUG may be absent on a minimally-configured Django settings object
        if not getattr(settings, "DEBUG", False):
            return self.get_response(request)

        initial = len(connection.queries)

        response = self.get_response(request)

        queries = connection.queries[initial:]
        if not queries:
            return response

        # Analyze for N+1
        analysis = analyze_queries(queries)

        view_key = f"{request.method}:{request.path}"

        if analysis["n_plus_one"]:
            # Store learned patterns
            self._learned[view_key] = analysis["n_plus_one"]

            # Add suggestion headers
            suggestions = []
            for npo in analysis["n_plus_one"]:
                suggestions.append(npo["suggestion"])
            if suggestions:
                response["X-N-Plus-One-Fix"] = "; ".join(suggestions)

        # If we've seen this view before and it had N+1, remind
        elif view_key in self._learned:
            prev = self._learned[view_key]
            response["X-N-Plus-One-Previous"] = "; ".join(p["suggestion"] for p in prev)

        return response

    @property
    def learned_patterns(self):
        """Return all learned N+1 patterns for introspection."""
        return dict(self._learned)
