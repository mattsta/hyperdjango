"""
Simple async middleware protocol.

Middleware is an async callable: (request, next) -> response.
No Django dependency.

Usage:
    @app.middleware
    async def timing(request, call_next):
        start = time.monotonic()
        response = await call_next(request)
        elapsed = time.monotonic() - start
        response.headers["x-response-time"] = f"{elapsed:.4f}s"
        return response
"""

import abc
import contextvars
import gzip as _gzip
import hmac
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from hyperdjango.conf import (
    ONE_DAY,
    get_setting,
)
from hyperdjango.cors import CorsPolicy
from hyperdjango.logging import logger as _logger
from hyperdjango.native._crypto import (
    hmac_sha256_hex_truncated,
    hmac_sha256_verify_truncated,
)
from hyperdjango.response import Response, _sanitize_header
from hyperdjango.security import SecurityEvent as _SecurityEvent
from hyperdjango.security import get_security_log as _get_security_log
from hyperdjango.security_headers import build_security_headers
from hyperdjango.telemetry import metrics as _tel_metrics
from hyperdjango.versioning import (
    APP_VERSION_ACTION_HEADER_NAME,
    APP_VERSION_HEADER_NAME,
    CLIENT_VERSION_COOKIE_NAME,
    CLIENT_VERSION_HEADER_NAME,
    VERSION_ACTIONS,
    _escape_version_for_js,
    get_app_version,
    get_client_script,
)
from hyperdjango.versioning import (
    client_version as _client_version,
)

# One-shot flag for the "response is already compressed, skipping HTML
# injection" debug line. Injecting a <script> into gzipped bytes silently
# produces a corrupt body, so the skip is explicit — but it would otherwise
# log on EVERY HTML response, so it is emitted once per process.
_version_inject_skip_logged = False

# ── Native telemetry metrics (zero cost when disabled) ──────────────────────
#
# Registered at module load time — one FFI call each, once per process.
# When telemetry is disabled (production default) every .inc() call is one
# LOAD_GLOBAL + branch (~25 ns). When enabled it bumps a native atomic
# counter via a single FFI call.
#
# Counter cardinality is bounded — we deliberately label by *reason*
# (a small enum) rather than per-key/per-path so the time series stays
# manageable. Per-key data lives in `SecurityLog` for the audit trail.

_csrf_violations_total = _tel_metrics.CounterVec(
    "hyperdjango_csrf_violations_total",
    "CSRF token validation failures.",
    label_names=("reason",),
)

# Version cohort observability. Exactly three relations — app versions are
# opaque hashes with no ordering, so there is no stale/newer split to make.
_version_skew_requests_total = _tel_metrics.CounterVec(
    "hyperdjango_version_skew_requests_total",
    "Requests by client/server app-version relation.",
    label_names=("relation",),
)


@dataclass(slots=True)
class MiddlewareSpan:
    """Timing data for a single middleware in the execution chain.

    Captures both wall-clock duration and the middleware's identity.
    """

    name: str
    start_ns: int
    end_ns: int

    @property
    def duration_ns(self) -> int:
        """Duration in nanoseconds."""
        return self.end_ns - self.start_ns

    @property
    def duration_ms(self) -> float:
        """Duration in milliseconds."""
        return self.duration_ns / 1_000_000

    @property
    def duration_us(self) -> float:
        """Duration in microseconds."""
        return self.duration_ns / 1_000


@dataclass(slots=True)
class MiddlewareTimeline:
    """Complete execution timeline for a middleware chain.

    Collects per-middleware timing spans during a request. Provides
    total duration, breakdown per middleware, and identification of
    the slowest middleware.
    """

    spans: list[MiddlewareSpan] = field(default_factory=list)
    request_start_ns: int = 0
    request_end_ns: int = 0

    @property
    def total_ns(self) -> int:
        """Total request processing time in nanoseconds."""
        return self.request_end_ns - self.request_start_ns

    @property
    def total_ms(self) -> float:
        """Total request processing time in milliseconds."""
        return self.total_ns / 1_000_000

    @property
    def slowest(self) -> MiddlewareSpan | None:
        """The slowest middleware span, or None if no spans recorded."""
        if not self.spans:
            return None
        return max(self.spans, key=lambda s: s.duration_ns)

    def summary(self) -> list[dict[str, str | float]]:
        """Return a list of {name, duration_ms, percent} dicts for each middleware."""
        total = self.total_ns
        result: list[dict[str, str | float]] = []
        for span in self.spans:
            pct = (span.duration_ns / total * 100) if total > 0 else 0.0
            result.append(
                {
                    "name": span.name,
                    "duration_ms": round(span.duration_ms, 3),
                    "percent": round(pct, 1),
                }
            )
        return result


# Per-request timeline storage.
#
# The ContextVar is the reactor-safe channel: request-scoped state isolated per
# asyncio Task, so under a multiplexing reactor (or when instrumentation runs on
# the shared WS loop) many requests sharing one OS thread never bleed task A's
# spans into task B's timeline. This mirrors the ContextVar pattern i18n.py /
# tenancy.py use for their request-scoped state.
#
# `_timeline_local` is retained as a thread-local mirror so the documented
# "access the timeline after request processing" behavior is preserved exactly:
# a ContextVar set inside the request Task is not visible to the outer thread
# context once the Task finishes, whereas a thread-local mirror persists. Reads
# prefer the ContextVar (correct while concurrent requests are in flight on one
# thread) and fall back to the mirror (post-request / non-instrumented reads).
_current_timeline: contextvars.ContextVar[MiddlewareTimeline | None] = (
    contextvars.ContextVar("hyperdjango_current_timeline", default=None)
)


@dataclass(slots=True)
class _TimelineMirror:
    """Thread-owned mirror of the active middleware timeline (see
    profiling._ProfileMirror for why writes must not land on the
    threading.local itself under free-threading)."""

    timeline: MiddlewareTimeline | None = None


class _TimelineLocal(threading.local):
    def __init__(self) -> None:
        self.state = _TimelineMirror()


_timeline_local = _TimelineLocal()


def get_current_timeline() -> MiddlewareTimeline | None:
    """Get the middleware timeline for the current request, or None if not instrumented."""
    tl = _current_timeline.get()
    if tl is not None:
        return tl
    return _timeline_local.state.timeline


@dataclass(slots=True, eq=False)
class BodyEncoder:
    """Capability marker: this middleware rewrites the response body into an
    encoded form.

    Gzip compression is the case that exists today; anything that re-encodes
    the bytes (brotli, encryption, a framing layer) belongs here too. Subclass
    it so middlewares that must operate on the DECODED body — HTML injectors,
    body-hashing ETag writers, anything that does a byte scan — can state the
    ordering requirement against the CAPABILITY instead of against a specific
    plugin class.

    Ordering, given how :class:`MiddlewareStack` composes: the first-registered
    middleware is the OUTERMOST, so on the way out responses unwind
    innermost-first and the LAST-registered middleware touches the body FIRST.
    A ``BodyEncoder`` must therefore be registered BEFORE (outside) anything
    that needs the decoded body.

    Empty by design: it declares a property of the middleware, not an interface
    to call. ``eq=False`` keeps identity equality: a capability marker has no
    value to compare, and the stack validator locates itself by identity.
    """


@dataclass(slots=True, eq=False)
class StackValidator(abc.ABC):
    """Capability marker: this middleware can reject the composed stack at boot.

    A middleware whose correctness depends on WHERE it sits relative to other
    middlewares implements :meth:`validate_stack`. The platform calls it once
    at startup with the full ordered stack (see :meth:`MiddlewareStack.validate`)
    and lets whatever it raises abort the boot.

    This is the entire contract — the platform never knows which middlewares
    care, what they check, or what they are called.
    """

    @abc.abstractmethod
    def validate_stack(self, middlewares: list[Callable]) -> None:
        """Validate this middleware's position within the composed stack.

        ``middlewares`` is the full ordered stack (outermost first), including
        this instance. Raise to abort startup; return ``None`` to accept.
        Must be side-effect free — it may run more than once.
        """


class MiddlewareStack:
    """Ordered stack of middleware that wraps a handler.

    When instrument=True, wraps each middleware with nanosecond timing.
    Access the timeline via get_current_timeline() during or after request processing,
    or via the X-Middleware-Timeline response header.
    """

    def __init__(self, instrument: bool = False):
        self._middleware: list[Callable] = []
        self._instrument = instrument

    def add(self, middleware_func):
        """Add middleware to the stack. First added = outermost."""
        self._middleware.append(middleware_func)

    def validate(self) -> None:
        """Let every :class:`StackValidator` in the stack vet the composition.

        Called once at startup, before the server binds. The stack owns the
        ordered list, so it owns the dispatch; it stays entirely generic —
        it dispatches on the marker base and never names a middleware.
        Validators receive a snapshot, so one cannot mutate the stack out
        from under the others.
        """
        middlewares = list(self._middleware)
        for mw in middlewares:
            if isinstance(mw, StackValidator):
                mw.validate_stack(middlewares)

    def wrap(self, handler):
        """Wrap a handler with all middleware, returning a single callable."""
        if self._instrument:
            return self._wrap_instrumented(handler)
        wrapped = handler
        for mw in reversed(self._middleware):
            wrapped = self._make_next(mw, wrapped)
        return wrapped

    def _wrap_instrumented(self, handler):
        """Build an instrumented middleware chain with per-middleware timing."""

        # Wrap the innermost handler to record its timing as "handler"
        async def timed_handler(request):
            start = time.perf_counter_ns()
            result = await handler(request)
            end = time.perf_counter_ns()
            timeline = get_current_timeline()
            if timeline is not None:
                timeline.spans.append(
                    MiddlewareSpan(
                        name="handler",
                        start_ns=start,
                        end_ns=end,
                    )
                )
            return result

        wrapped = timed_handler
        for mw in reversed(self._middleware):
            wrapped = self._make_timed_next(mw, wrapped)

        async def instrumented_entry(request):
            timeline = MiddlewareTimeline()
            timeline.request_start_ns = time.perf_counter_ns()
            # ContextVar for per-Task isolation; thread-local mirror so the
            # timeline stays readable after the request (preserved behavior).
            _current_timeline.set(timeline)
            _timeline_local.state.timeline = timeline
            try:
                response = await wrapped(request)
                return response
            finally:
                timeline.request_end_ns = time.perf_counter_ns()

        return instrumented_entry

    @staticmethod
    def _make_next(middleware, inner):
        async def wrapped(request):
            return await middleware(request, inner)

        return wrapped

    @staticmethod
    def _make_timed_next(middleware, inner):
        mw_name = _middleware_name(middleware)

        async def wrapped(request):
            start = time.perf_counter_ns()
            result = await middleware(request, inner)
            end = time.perf_counter_ns()
            timeline = get_current_timeline()
            if timeline is not None:
                timeline.spans.append(
                    MiddlewareSpan(
                        name=mw_name,
                        start_ns=start,
                        end_ns=end,
                    )
                )
            return result

        return wrapped


def _middleware_name(mw) -> str:
    """Extract a readable name from a middleware callable."""
    if hasattr(mw, "__name__"):
        return mw.__name__
    cls = type(mw)
    if cls.__name__ != "function":
        return cls.__name__
    return repr(mw)


# --- Built-in middleware ---


@dataclass(slots=True)
class CORSMiddleware:
    """Cross-Origin Resource Sharing middleware."""

    origins: list[str] = field(default_factory=lambda: ["*"])
    methods: list[str] = field(
        default_factory=lambda: ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    )
    headers: list[str] = field(default_factory=lambda: ["*"])
    expose_headers: list[str] = field(default_factory=list)
    allow_credentials: bool = False
    max_age: int = ONE_DAY
    # Cached joined-header strings — built once at __post_init__ so we don't
    # rebuild ", ".join(...) on every response.
    _methods_joined: str = field(init=False, repr=False, default="")
    _headers_joined: str = field(init=False, repr=False, default="")
    _expose_headers_str: str = field(init=False, repr=False, default="")
    _max_age_str: str = field(init=False, repr=False, default="")
    _policy: CorsPolicy = field(init=False, repr=False, default=None)

    def __post_init__(self) -> None:
        self._methods_joined = ", ".join(self.methods)
        self._headers_joined = ", ".join(self.headers)
        self._expose_headers_str = ", ".join(self.expose_headers)
        self._max_age_str = str(self.max_age)
        # The one CORS decision authority (also used by the Django adapter).
        # It rejects the wildcard+credentials account-takeover combination here.
        self._policy = CorsPolicy(self.origins, self.allow_credentials)

    async def __call__(self, request, call_next):
        origin = request.headers.get("origin", "")
        if request.method == "OPTIONS":
            return self._preflight_response(origin)
        response = await call_next(request)
        self._add_cors_headers(response, origin, preflight=False)
        return response

    def _preflight_response(self, origin):
        resp = Response.empty(status=204)
        self._add_cors_headers(resp, origin, preflight=True)
        resp.headers["access-control-max-age"] = self._max_age_str
        return resp

    def _append_vary_origin(self, response):
        """Add `Origin` to the response's Vary header.

        Any response whose Access-Control-Allow-Origin echoes the request
        Origin is origin-dependent; a shared cache that ignored Vary would
        serve one origin's ACAO to another (cross-origin cache poisoning).
        """
        existing = response.headers.get("vary") or response.headers.get("Vary") or ""
        tokens = [tok.strip() for tok in existing.split(",") if tok.strip()]
        if not any(tok.lower() == "origin" for tok in tokens):
            tokens.append("Origin")
        value = ", ".join(tokens)
        # Preserve the caller's existing header casing; default to lower to
        # match the rest of the CORS headers written here.
        if "Vary" in response.headers and "vary" not in response.headers:
            response.headers["Vary"] = value
        else:
            response.headers["vary"] = value

    def _add_cors_headers(self, response, origin, preflight=False):
        decision = self._policy.resolve(origin)
        if decision is None:
            return
        if decision.vary_origin:
            # We are echoing a specific origin back — the response varies by
            # Origin and must be marked so caches never serve one origin's
            # Access-Control-Allow-Origin to another.
            self._append_vary_origin(response)
        response.headers["access-control-allow-origin"] = decision.allow_origin
        response.headers["access-control-allow-methods"] = self._methods_joined
        response.headers["access-control-allow-headers"] = self._headers_joined
        # Access-Control-Expose-Headers only applies to actual responses, not
        # preflight — it tells the browser which response headers JS may read.
        if not preflight and self._expose_headers_str:
            response.headers["access-control-expose-headers"] = self._expose_headers_str
        if decision.allow_credentials:
            response.headers["access-control-allow-credentials"] = "true"


@dataclass(slots=True)
class LoggingMiddleware:
    """Request/response logging."""

    async def __call__(self, request, call_next):
        start = time.monotonic()
        response = await call_next(request)
        elapsed = time.monotonic() - start
        _logger.info(
            "{method} {path} → {status} ({elapsed:.3f}s)",
            method=request.method,
            path=request.path,
            status=response.status,
            elapsed=elapsed,
        )
        return response


class TimingMiddleware:
    """Adds X-Response-Time header. Stateless — no fields needed."""

    async def __call__(self, request, call_next):
        start = time.monotonic()
        response = await call_next(request)
        elapsed = time.monotonic() - start
        response.headers["x-response-time"] = f"{elapsed:.6f}s"
        return response


@dataclass(slots=True)
class SecurityHeadersMiddleware:
    """Add common security headers to all responses.

    Reads defaults from conf.py settings (X_FRAME_OPTIONS, SECURE_CROSS_ORIGIN_OPENER_POLICY,
    SECURE_CSP, SECURE_CONTENT_TYPE_NOSNIFF, SECURE_REFERRER_POLICY, SECURE_SSL_REDIRECT,
    SECURE_REDIRECT_EXEMPT, SECURE_SSL_HOST, SECURE_PROXY_SSL_HEADER).
    Constructor parameters override conf.py values.

    Usage:
        app.use(SecurityHeadersMiddleware())
        app.use(SecurityHeadersMiddleware(hsts=True))
    """

    content_type_nosniff: bool | None = None
    frame_options: str | None = None
    hsts: bool = False
    hsts_max_age: int = 31536000
    csp: str | dict[str, str] | None = None
    referrer_policy: str | None = None
    permissions_policy: str | None = None
    cross_origin_opener_policy: str | None = None
    ssl_redirect: bool | None = None
    ssl_host: str | None = None
    proxy_ssl_header: str | None = None
    redirect_exempt: list[str] | None = None
    _headers: dict[str, str] = field(init=False, default_factory=dict)
    _redirect_exempt_patterns: list[re.Pattern[str]] = field(
        init=False, default_factory=list
    )
    _disallowed_agent_patterns: list[re.Pattern[str]] = field(
        init=False, default_factory=list
    )
    _ssl_redirect: bool = field(init=False, default=False)
    _ssl_host: str = field(init=False, default="")
    _proxy_ssl_header: str = field(init=False, default="")
    _prepend_www: bool = field(init=False, default=False)
    _allowed_hosts: tuple[str, ...] = field(init=False, default=())

    def __post_init__(self):
        # Resolve from conf.py settings, constructor params override
        nosniff = (
            self.content_type_nosniff
            if self.content_type_nosniff is not None
            else get_setting("SECURE_CONTENT_TYPE_NOSNIFF")
        )
        frame_opt = (
            self.frame_options
            if self.frame_options is not None
            else get_setting("X_FRAME_OPTIONS")
        )
        coop = (
            self.cross_origin_opener_policy
            if self.cross_origin_opener_policy is not None
            else get_setting("SECURE_CROSS_ORIGIN_OPENER_POLICY")
        )
        ref_policy = (
            self.referrer_policy
            if self.referrer_policy is not None
            else get_setting("SECURE_REFERRER_POLICY")
        )
        csp_value = self.csp if self.csp is not None else get_setting("SECURE_CSP")
        self._ssl_redirect = (
            self.ssl_redirect
            if self.ssl_redirect is not None
            else get_setting("SECURE_SSL_REDIRECT")
        )
        self._ssl_host = (
            self.ssl_host
            if self.ssl_host is not None
            else get_setting("SECURE_SSL_HOST")
        )
        self._proxy_ssl_header = (
            self.proxy_ssl_header
            if self.proxy_ssl_header is not None
            else get_setting("SECURE_PROXY_SSL_HEADER")
        )

        self._prepend_www = get_setting("PREPEND_WWW")
        # Snapshot ALLOWED_HOSTS so the SSL / PREPEND_WWW redirects can
        # validate the client-controlled Host header before building a
        # Location from it — otherwise `Host: evil.com` yields an
        # open-redirect (https://evil.com/...).
        self._allowed_hosts = tuple(get_setting("ALLOWED_HOSTS") or ())

        exempt = (
            self.redirect_exempt
            if self.redirect_exempt is not None
            else get_setting("SECURE_REDIRECT_EXEMPT")
        )
        self._redirect_exempt_patterns = [re.compile(p) for p in exempt]

        # DISALLOWED_USER_AGENTS: compile regex patterns for blocking
        disallowed_agents = get_setting("DISALLOWED_USER_AGENTS")
        self._disallowed_agent_patterns = [re.compile(p) for p in disallowed_agents]

        hsts_seconds = (
            self.hsts_max_age if self.hsts else get_setting("SECURE_HSTS_SECONDS")
        )
        # The one security-header set (also used by the Django adapter).
        self._headers = build_security_headers(
            nosniff=nosniff,
            frame_options=frame_opt,
            hsts_seconds=hsts_seconds,
            hsts_include_subdomains=get_setting("SECURE_HSTS_INCLUDE_SUBDOMAINS"),
            hsts_preload=get_setting("SECURE_HSTS_PRELOAD"),
            csp=csp_value,
            referrer_policy=ref_policy,
            permissions_policy=self.permissions_policy,
            cross_origin_opener_policy=coop,
        )

    def _is_secure(self, request) -> bool:
        """Check if request is secure, considering proxy SSL header."""
        if self._proxy_ssl_header:
            header_val = request.headers.get(self._proxy_ssl_header.lower(), "")
            if header_val == "https":
                return True
        if request.scope:
            return request.scope.get("scheme") == "https"
        return request.headers.get("x-forwarded-proto") == "https"

    def _is_exempt(self, path: str) -> bool:
        """Check if path is exempt from SSL redirect."""
        for pattern in self._redirect_exempt_patterns:
            if pattern.search(path):
                return True
        return False

    def _host_allowed(self, host: str) -> bool:
        """Validate a client-supplied Host header against ALLOWED_HOSTS.

        Django-style matching: an exact hostname, a leading-dot pattern
        (".example.com" matches example.com and any subdomain), or "*"
        (matches anything). The port and any IPv6 brackets are stripped
        before comparison. Used to gate host-derived redirects so a
        forged Host header cannot drive an open redirect.
        """
        if not host:
            return False
        # No explicit allowlist configured (dev default): permit the host. A
        # same-host scheme/www redirect is not an open redirect, and gating it
        # here would break the redirect entirely when ALLOWED_HOSTS is unset.
        # A CONFIGURED allowlist still enforces (a forged Host is rejected).
        if not self._allowed_hosts:
            return True
        # Strip port. For bracketed IPv6 (`[::1]:8000`) only the trailing
        # `:port` after the closing bracket is a port separator.
        if host.startswith("["):
            bare = host.partition("]")[0].lstrip("[")
        else:
            bare = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        bare = bare.lower().rstrip(".")
        for pattern in self._allowed_hosts:
            pat = pattern.lower()
            if pat == "*":
                return True
            if pat.startswith("."):
                # ".example.com" matches example.com and *.example.com
                if bare == pat[1:] or bare.endswith(pat):
                    return True
            elif bare == pat:
                return True
        return False

    async def __call__(self, request, call_next):
        # Host-header validation. When an ALLOWED_HOSTS allowlist is configured,
        # reject a request whose effective Host is not on it BEFORE any app code
        # runs. request.host (which honors USE_X_FORWARDED_HOST) is what views use
        # to build absolute URLs — password-reset / email-verification links,
        # redirects — and what response caches key on, so an unvalidated forged
        # Host is a header-injection / web-cache-poisoning / account-takeover
        # vector. Django rejects the identical case with 400; we now match. Dev
        # (empty allowlist) intentionally stays open so localhost just works.
        if self._allowed_hosts and not self._host_allowed(request.host):
            return Response.error(400, "Bad Request (invalid Host header)")

        # DISALLOWED_USER_AGENTS check
        if self._disallowed_agent_patterns:
            user_agent = request.headers.get("user-agent", "")
            for pattern in self._disallowed_agent_patterns:
                if pattern.search(user_agent):
                    return Response.error(403, "Forbidden")

        # PREPEND_WWW redirect check. The Host header is client-controlled,
        # so only build a Location from it when it passes ALLOWED_HOSTS —
        # otherwise a forged Host drives an open redirect.
        if self._prepend_www:
            host = request.headers.get("host", "")
            if host and not host.startswith("www.") and self._host_allowed(host):
                scheme = "https" if self._is_secure(request) else "http"
                redirect_url = f"{scheme}://www.{host}{request.path}"
                return Response.redirect(redirect_url, status=301)

        # SSL redirect check. Prefer the configured canonical SSL host; only
        # fall back to the request Host header when it is an allowed host.
        if self._ssl_redirect and not self._is_secure(request):
            if not self._is_exempt(request.path):
                host = self._ssl_host
                if not host:
                    req_host = request.headers.get("host", "")
                    host = req_host if self._host_allowed(req_host) else ""
                if host:
                    redirect_url = f"https://{host}{request.path}"
                    return Response.redirect(redirect_url, status=301)

        response = await call_next(request)
        for k, v in self._headers.items():
            response.headers.setdefault(k, v)
        return response


@dataclass(slots=True)
class CompressionMiddleware(BodyEncoder):
    """Gzip compression for responses above a minimum size.

    Checks Accept-Encoding header and compresses response body with gzip.
    Skips already-compressed content types (images, video, etc.).

    Declares the :class:`BodyEncoder` capability: it rewrites the response
    body into an encoded form, so anything that needs the decoded bytes must
    be registered AFTER it (inside it).

    Usage:
        app.use(CompressionMiddleware(min_size=500))
    """

    min_size: int = 500
    level: int = 6
    _skip_types: frozenset = field(
        init=False,
        default=frozenset(
            {
                "image/",
                "video/",
                "audio/",
                "application/zip",
                "application/gzip",
                "application/x-bzip2",
            }
        ),
    )

    @staticmethod
    def _gzip_acceptable(accept_encoding):
        """Decide whether gzip may be used per an Accept-Encoding header.

        Parses the header into (token, q) pairs (RFC 9110 quality values)
        and honors explicit refusal — unlike a bare ``"gzip" in header``
        substring test, which would wrongly compress for ``gzip;q=0``,
        ignore a ``*`` wildcard, and mis-handle ``identity;q=0``. gzip is
        acceptable when it appears with q>0; failing that, a ``*`` with q>0
        permits it (unless gzip was explicitly listed at q=0). Tokens are
        matched case-insensitively so ``GZIP`` is honored.
        """
        gzip_q = None
        star_q = None
        for part in accept_encoding.split(","):
            part = part.strip()
            if not part:
                continue
            token, _, params = part.partition(";")
            token = token.strip().lower()
            q = 1.0
            if params:
                for param in params.split(";"):
                    param = param.strip()
                    if param[:2].lower() == "q=":
                        try:
                            q = float(param[2:])
                        except ValueError:
                            q = 1.0
                        break
            if token == "gzip":
                gzip_q = q
            elif token == "*":
                star_q = q
        if gzip_q is not None:
            return gzip_q > 0
        if star_q is not None:
            return star_q > 0
        return False

    @staticmethod
    def _has_header(response, name):
        """Case-insensitive presence check over the response header dict."""
        return any(k.lower() == name for k in response.headers)

    def _merge_vary_accept_encoding(self, response):
        """Merge ``Accept-Encoding`` into the response's Vary header.

        Mirrors ``_append_vary_origin``: a plain ``setdefault("vary", ...)``
        is a no-op when an upstream already set e.g. ``Vary: Cookie``, so
        ``Accept-Encoding`` would never be added and a shared cache could
        hand a gzipped body to a client that sent no (or a q=0) gzip. Read
        the existing value and append ``Accept-Encoding`` only if absent
        (case-insensitively), preserving the caller's header casing.
        """
        existing = response.headers.get("vary") or response.headers.get("Vary") or ""
        tokens = [tok.strip() for tok in existing.split(",") if tok.strip()]
        if not any(tok.lower() == "accept-encoding" for tok in tokens):
            tokens.append("Accept-Encoding")
        value = ", ".join(tokens)
        if "Vary" in response.headers and "vary" not in response.headers:
            response.headers["Vary"] = value
        else:
            response.headers["vary"] = value

    def _breach_unsafe(self, response):
        """BREACH mitigation: refuse to compress responses that likely
        reflect a per-user secret alongside attacker-influenced input.

        Compressing such a body turns its size into an oracle that leaks
        the secret (CSRF token, session id) a byte at a time — the BREACH
        attack (CVE-2013-3587). We cannot tell which bodies are vulnerable,
        so we skip on two conservative signals:

          * an RFC 9111 ``no-transform`` Cache-Control directive, which by
            spec forbids any content-coding change (must always be honored);
          * a ``Set-Cookie`` header, a strong marker of authenticated /
            per-user content.

        Ordinary cacheable public responses (no cookie, no ``no-transform``)
        are still compressed.
        """
        # Set-Cookie: structured list (set_cookie) or a directly-assigned
        # header value, either case.
        if response._cookies or self._has_header(response, "set-cookie"):
            return True
        cache_control = (
            response.headers.get("cache-control")
            or response.headers.get("Cache-Control")
            or ""
        )
        for directive in cache_control.split(","):
            if directive.strip().lower() == "no-transform":
                return True
        return False

    async def __call__(self, request, call_next):
        accept = request.headers.get("accept-encoding", "")
        if not self._gzip_acceptable(accept):
            return await call_next(request)

        response = await call_next(request)

        # Status guard: only transform ordinary full-body 2xx responses.
        # A 206 Partial Content carries a Content-Range describing offsets
        # into the UNCOMPRESSED body; gzipping it changes the byte length
        # and invalidates that range (produces a corrupt ranged download).
        # 204 No Content has no body to compress. Anything outside 2xx
        # (redirects, 304 Not Modified, errors) is left untouched too.
        if not (200 <= response.status < 300) or response.status in (204, 206):
            return response
        if self._has_header(response, "content-range"):
            return response

        ct = response.headers.get("content-type", "")
        if any(ct.startswith(skip) for skip in self._skip_types):
            return response
        if "content-encoding" in response.headers:
            return response
        # BREACH: never compress responses likely to carry a per-user secret,
        # and always honor a no-transform directive. See _breach_unsafe.
        if self._breach_unsafe(response):
            return response

        if response.is_streaming:
            # Compress streaming responses chunk-by-chunk so we don't buffer
            # the entire body in memory before sending — important for SSE,
            # large file downloads, and any open-ended iterator. Drop the
            # Content-Length header since the compressed size is unknown.
            original_iter = response._stream_iter
            level = self.level

            async def _gzip_stream():
                buf = _BytesAccumulator()
                compressor = _gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=level)
                async for chunk in _ensure_async_iter(original_iter):
                    if isinstance(chunk, str):
                        chunk = chunk.encode("utf-8")
                    compressor.write(chunk)
                    compressor.flush(_gzip.zlib.Z_SYNC_FLUSH)
                    if buf.data:
                        yield bytes(buf.data)
                        buf.data.clear()
                compressor.close()  # emits gzip trailer
                if buf.data:
                    yield bytes(buf.data)

            response._stream_iter = _gzip_stream()
            response.headers["content-encoding"] = "gzip"
            response.headers.pop("content-length", None)
            self._merge_vary_accept_encoding(response)
            return response

        if len(response.body) < self.min_size:
            return response

        compressed = _gzip.compress(response.body, compresslevel=self.level)
        if len(compressed) < len(response.body):
            response.body = compressed
            response.headers["content-encoding"] = "gzip"
            response.headers["content-length"] = str(len(compressed))
            self._merge_vary_accept_encoding(response)

        return response


@dataclass(slots=True)
class _BytesAccumulator:
    """Minimal write-only buffer for GzipFile streaming output."""

    data: bytearray = field(default_factory=bytearray)

    def write(self, b: bytes) -> int:
        self.data.extend(b)
        return len(b)

    def flush(self) -> None:
        pass


async def _ensure_async_iter(it):
    """Yield from either a sync or async iterable transparently."""
    if hasattr(it, "__aiter__"):
        async for chunk in it:
            yield chunk
        return
    for chunk in it:
        yield chunk


@dataclass(slots=True)
class CSRFMiddleware:
    """CSRF protection using double-submit cookie pattern.

    Safe methods (GET, HEAD, OPTIONS) pass through.
    Unsafe methods require valid CSRF token in header or form field.

    Reads defaults from conf.py settings (CSRF_COOKIE_NAME, CSRF_COOKIE_DOMAIN,
    CSRF_COOKIE_PATH, CSRF_COOKIE_AGE, CSRF_HEADER_NAME, CSRF_TRUSTED_ORIGINS,
    CSRF_COOKIE_SECURE, CSRF_COOKIE_HTTPONLY, CSRF_COOKIE_SAMESITE).
    Constructor parameters override conf.py values.

    Usage:
        app.use(CSRFMiddleware(secret="your-secret"))
    """

    SAFE_METHODS: frozenset = field(
        init=False, default=frozenset({"GET", "HEAD", "OPTIONS"})
    )

    # No hardcoded default: a CSRF secret baked into this (public, open-source)
    # file would be known to every attacker who reads the repo, letting them mint
    # valid CSRF tokens and defeat the protection entirely. Left empty here and
    # resolved from configured secrets in __post_init__ (fail-closed on a
    # placeholder/empty result). See _FORBIDDEN_SECRETS.
    secret: str = ""
    cookie_name: str | None = None
    header_name: str | None = None
    field_name: str = "_csrf_token"
    cookie_domain: str | None = None
    cookie_path: str | None = None
    cookie_age: int | None = None
    cookie_secure: bool | None = None
    cookie_httponly: bool | None = None
    cookie_samesite: str | None = None
    trusted_origins: list[str] | None = None
    exempt_paths: set[str] = field(default_factory=set)
    exempt_prefixes: set[str] = field(default_factory=set)
    _exempt_prefix_tuple: tuple[str, ...] = field(init=False, default=())
    _cookie_name: str = field(init=False, default="")
    _header_name: str = field(init=False, default="")
    _cookie_domain: str = field(init=False, default="")
    _cookie_path: str = field(init=False, default="/")
    _cookie_age: int = field(init=False, default=31449600)
    _cookie_secure: bool = field(init=False, default=False)
    _cookie_httponly: bool = field(init=False, default=True)
    _cookie_samesite: str = field(init=False, default="Lax")
    _trusted_origins: list[str] = field(init=False, default_factory=list)
    # Pre-parsed origin patterns for O(1) exact match + fast wildcard check
    _exact_origins: frozenset[str] = field(init=False, default_factory=frozenset)
    _wildcard_origins: list[tuple[str, str]] = field(
        init=False, default_factory=list
    )  # (scheme, domain)

    # Placeholder/empty secrets that must never sign CSRF tokens — a token signed
    # with a well-known or empty key is trivially forgeable by anyone who reads
    # the (public) source, defeating CSRF protection. Mirrors the same guard in
    # PasswordResetTokenGenerator.
    _FORBIDDEN_SECRETS: frozenset = field(
        init=False,
        default=frozenset(
            {"", "csrf-secret-change-me", "change-me", "changeme", "secret", "default"}
        ),
    )

    def __post_init__(self):
        # Resolve the signing secret: an explicit strong secret wins; otherwise
        # fall back to configured CSRF_SECRET/SESSION_SECRET/SECRET_KEY (which are
        # a per-process random value at worst — never a source-known constant).
        # Fail closed if nothing usable is configured.
        resolved = self.secret
        if not resolved or resolved.strip().lower() in self._FORBIDDEN_SECRETS:
            resolved = (
                get_setting("CSRF_SECRET")
                or get_setting("SESSION_SECRET")
                or get_setting("SECRET_KEY")
                or ""
            )
        if not resolved or resolved.strip().lower() in self._FORBIDDEN_SECRETS:
            raise ValueError(
                "CSRFMiddleware requires a real secret. Pass secret=<strong random> "
                "or set SECRET_KEY / CSRF_SECRET; the built-in placeholder is refused "
                "because a secret in public source makes CSRF tokens forgeable."
            )
        self.secret = resolved

        self._cookie_name = (
            self.cookie_name
            if self.cookie_name is not None
            else get_setting("CSRF_COOKIE_NAME")
        )
        self._cookie_domain = (
            self.cookie_domain
            if self.cookie_domain is not None
            else get_setting("CSRF_COOKIE_DOMAIN")
        )
        self._cookie_path = (
            self.cookie_path
            if self.cookie_path is not None
            else get_setting("CSRF_COOKIE_PATH")
        )
        self._cookie_age = (
            self.cookie_age
            if self.cookie_age is not None
            else get_setting("CSRF_COOKIE_AGE")
        )
        self._cookie_secure = (
            self.cookie_secure
            if self.cookie_secure is not None
            else get_setting("CSRF_COOKIE_SECURE")
        )
        self._cookie_httponly = (
            self.cookie_httponly
            if self.cookie_httponly is not None
            else get_setting("CSRF_COOKIE_HTTPONLY")
        )
        self._cookie_samesite = (
            self.cookie_samesite
            if self.cookie_samesite is not None
            else get_setting("CSRF_COOKIE_SAMESITE")
        )
        self._trusted_origins = (
            self.trusted_origins
            if self.trusted_origins is not None
            else get_setting("CSRF_TRUSTED_ORIGINS")
        )
        # Normalize header name: conf stores as "X-CSRFToken", we need lowercase for header lookup
        raw_header = (
            self.header_name
            if self.header_name is not None
            else get_setting("CSRF_HEADER_NAME")
        )
        self._header_name = raw_header.lower()
        self._exempt_prefix_tuple = (
            tuple(self.exempt_prefixes) if self.exempt_prefixes else ()
        )
        # Pre-parse trusted origins: split into exact set + wildcard list
        exact = set()
        wildcards = []
        for origin in self._trusted_origins:
            if origin.startswith("https://*.") or origin.startswith("http://*."):
                scheme_end = origin.index("://")
                scheme = origin[:scheme_end]
                domain = origin[scheme_end + 5 :]  # skip "://*."
                wildcards.append((scheme, domain))
            else:
                exact.add(origin)
        self._exact_origins = frozenset(exact)
        self._wildcard_origins = wildcards

    def _generate_token(self):
        token = secrets.token_urlsafe(32)
        sig = hmac_sha256_hex_truncated(self.secret.encode(), token.encode(), 16)
        return f"{token}.{sig}"

    def _validate_token(self, token):
        if not token or "." not in token:
            return False
        data, sig = token.rsplit(".", 1)
        if not sig.isascii():
            return False  # Valid signatures are always hex (ASCII)
        return hmac_sha256_verify_truncated(
            self.secret.encode(), data.encode("utf-8", errors="replace"), sig, 16
        )

    def _origin_is_trusted(self, request) -> bool:
        """Check if the request origin is in CSRF_TRUSTED_ORIGINS.

        Uses pre-parsed frozenset for O(1) exact match, then checks
        wildcard patterns only if no exact match found.
        """
        if not self._trusted_origins:
            return False
        origin = request.headers.get("origin", "")
        if not origin:
            return False
        # O(1) exact match
        if origin in self._exact_origins:
            return True
        # Wildcard subdomain matching (pre-parsed scheme + domain)
        if self._wildcard_origins and "://" in origin:
            scheme_end = origin.index("://")
            origin_scheme = origin[:scheme_end]
            origin_host = origin[scheme_end + 3 :]
            for trusted_scheme, trusted_domain in self._wildcard_origins:
                if origin_scheme == trusted_scheme and (
                    origin_host == trusted_domain
                    or origin_host.endswith("." + trusted_domain)
                ):
                    return True
        return False

    async def __call__(self, request, call_next):
        existing_token = request.cookies.get(self._cookie_name)
        if not existing_token:
            existing_token = self._generate_token()

        if request.method in self.SAFE_METHODS:
            response = await call_next(request)
            if not request.cookies.get(self._cookie_name):
                response.set_cookie(
                    self._cookie_name,
                    existing_token,
                    httponly=self._cookie_httponly,
                    samesite=self._cookie_samesite,
                    secure=self._cookie_secure,
                    max_age=self._cookie_age,
                    domain=self._cookie_domain or None,
                    path=self._cookie_path,
                )
            return response

        if request.path in self.exempt_paths:
            return await call_next(request)

        if self._exempt_prefix_tuple and request.path.startswith(
            self._exempt_prefix_tuple
        ):
            return await call_next(request)

        if request.api_key_valid:
            return await call_next(request)

        # Trusted origins skip CSRF token check
        if self._origin_is_trusted(request):
            return await call_next(request)

        token = request.headers.get(self._header_name)
        if not token:
            # Parse form body to extract CSRF token from form fields
            form_data = await request.form()
            if form_data:
                form_tokens = form_data.get(self.field_name, [])
                token = form_tokens[0] if form_tokens else None

        cookie_token = request.cookies.get(self._cookie_name)

        # Double-submit pattern: token must match cookie AND be validly signed
        if not token or not cookie_token:
            _csrf_violations_total.inc_tuple(("missing",))
            sec_log = _get_security_log()
            if sec_log is not None:
                try:
                    await sec_log.log_from_request(
                        _SecurityEvent.CSRF_VIOLATION,
                        request,
                        detail="token missing",
                    )
                # blind-except: SecurityLog audit write is best-effort telemetry; a logging-backend failure is warned about but must not break CSRF handling.
                except Exception as e:
                    _logger.warning("SecurityLog.log_from_request failed: {err}", err=e)
            return Response.error(403, "CSRF token missing")
        if not hmac.compare_digest(token, cookie_token):
            _csrf_violations_total.inc_tuple(("mismatch",))
            sec_log = _get_security_log()
            if sec_log is not None:
                try:
                    await sec_log.log_from_request(
                        _SecurityEvent.CSRF_VIOLATION,
                        request,
                        detail="token mismatch",
                    )
                # blind-except: SecurityLog audit write is best-effort telemetry; a logging-backend failure is warned about but must not break CSRF handling.
                except Exception as e:
                    _logger.warning("SecurityLog.log_from_request failed: {err}", err=e)
            return Response.error(403, "CSRF token mismatch")

        # Verify the server-minted HMAC signature on BOTH the cookie token and
        # the submitted token. Equality alone (plain double-submit) is
        # bypassable: an attacker who can plant a cookie can echo the same value
        # in the header/form. Requiring a valid signature means only tokens this
        # server issued (via _generate_token) are accepted.
        if not self._validate_token(cookie_token) or not self._validate_token(token):
            _csrf_violations_total.inc_tuple(("bad_signature",))
            sec_log = _get_security_log()
            if sec_log is not None:
                try:
                    await sec_log.log_from_request(
                        _SecurityEvent.CSRF_VIOLATION,
                        request,
                        detail="token signature invalid",
                    )
                # blind-except: SecurityLog audit write is best-effort telemetry; a logging-backend failure is warned about but must not break CSRF handling.
                except Exception as e:
                    _logger.warning("SecurityLog.log_from_request failed: {err}", err=e)
            return Response.error(403, "CSRF token invalid")

        return await call_next(request)


# ---------------------------------------------------------------------------
# Version middleware — X-App-Version header + HTMX mismatch detection
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class VersionMiddleware(StackValidator):
    """Version cohort headers + operator-owned stale-client policy.

    Response side, on every response:

    - ``X-App-Version`` — the version that produced this response.
    - ``X-App-Version-Action`` — the OPERATOR's policy for what a stale
      client should do (``prompt`` | ``reload`` | ``warn`` | ``ignore``),
      resolved from ``APP_VERSION_MISMATCH``. Emitted even for ``ignore``,
      because that value is itself a directive telling already-instrumented
      older clients to stand down.

    HTML responses additionally get ``window.__hyperAppVersion`` plus the
    ``window.hyperVersion`` API before ``</head>``, and the mismatch /
    cohort-broadcast script before ``</body>``.

    Request side (when ``APP_VERSION_CLIENT_BROADCAST`` is on): parses the
    client's own version from ``X-Client-Version`` / ``hyper_client_version``
    onto ``request.client_version`` and bumps
    ``hyperdjango_version_skew_requests_total{relation}``.

    Ordering: HTML injection needs the DECODED body, so this middleware must
    see the response before any :class:`BodyEncoder` encodes it — i.e. it must
    be the INNER one (registered with ``app.use()`` AFTER every encoder).
    A response that already carries ``Content-Encoding`` is skipped at
    runtime, and :meth:`validate_stack` rejects the wrong order at startup.

    Performance: the version string and pre-built HTML injection snippet are
    cached. Only a single string comparison detects staleness. The per-request
    hot path for JSON/API responses is two dict assignments.

    Settings:
        ``APP_VERSION_HEADER`` (bool, default True) — emit headers on all responses.
        ``APP_VERSION_MISMATCH`` (str, default "prompt") — client action on mismatch:
            "prompt" shows a user-initiated reload banner, "reload" reloads at
            the next navigation boundary, "warn" logs to console, "ignore"
            disables mismatch script injection entirely.
        ``APP_VERSION_CLIENT_BROADCAST`` (bool, default True) — gates the whole
            cohort-broadcast feature (client header + cookie, inbound parse,
            skew metric).
    """

    _enabled: bool = field(default=True, init=False, repr=False)
    _inject_script: bool = field(default=True, init=False, repr=False)
    _inject_html: bool = field(default=True, init=False, repr=False)
    _broadcast: bool = field(default=True, init=False, repr=False)
    _mismatch_action: str = field(default="prompt", init=False, repr=False)
    _action_header: str = field(default="prompt", init=False, repr=False)
    # Body script for the resolved (action, broadcast) policy, pre-encoded once
    # at init — it does not vary with the version, only the head tag does.
    _body_script: bytes = field(default=b"", init=False, repr=False)
    # Pre-sanitized version string + pre-built injection bytes, held as ONE
    # immutable (raw_version, header, inject_bytes) tuple and swapped atomically.
    # The guard (raw_version) travels WITH its data, so a concurrent request can
    # never see an updated guard paired with a stale/empty header or inject.
    _cache_snapshot: tuple[str, str, bytes] = field(
        default=("", "", b""), init=False, repr=False
    )

    def __post_init__(self) -> None:
        self._enabled = bool(get_setting("APP_VERSION_HEADER", True))
        self._mismatch_action = str(get_setting("APP_VERSION_MISMATCH", "prompt"))
        # Fail at construction, not at first request: an action no client
        # understands would silently degrade to a policy the operator did not
        # choose. This middleware owns the setting, so it owns the check.
        if self._mismatch_action not in VERSION_ACTIONS:
            allowed = ", ".join(sorted(VERSION_ACTIONS))
            raise RuntimeError(
                f"APP_VERSION_MISMATCH is {self._mismatch_action!r} — "
                f"allowed values are: {allowed}."
            )
        self._broadcast = bool(get_setting("APP_VERSION_CLIENT_BROADCAST", True))
        # "ignore" keeps its historical meaning: no mismatch script at all.
        self._inject_script = self._mismatch_action != "ignore"
        # …but the cohort broadcast is a separate feature with its own switch,
        # so HTML is still touched when only broadcast is on.
        self._inject_html = self._inject_script or self._broadcast
        self._action_header = _sanitize_header(self._mismatch_action)
        script = get_client_script(self._mismatch_action, self._broadcast)
        self._body_script = script.body.encode("utf-8")

    def validate_stack(self, middlewares: list[Callable]) -> None:
        """Reject a stack where the body is encoded before we can inject.

        :class:`MiddlewareStack` runs the first-registered middleware as the
        OUTERMOST one, so responses unwind innermost-first: whichever
        middleware was registered LAST touches the body FIRST. HTML injection
        needs the decoded bytes, so every :class:`BodyEncoder` has to be
        registered BEFORE this middleware. Any encoder appearing AFTER us in
        the ordered stack would compress first and leave the injection with
        gzipped bytes it can only skip.

        The check is capability-based: any ``BodyEncoder`` trips it, not just
        the compressor that ships today.
        """
        own_idx = -1
        offender: Callable | None = None
        for mw in middlewares:
            if mw is self:
                own_idx = 0
            elif own_idx == 0 and offender is None and isinstance(mw, BodyEncoder):
                offender = mw
        if offender is None:
            return
        name = type(offender).__name__
        raise RuntimeError(
            f"Middleware order: VersionMiddleware is registered before the "
            f"BodyEncoder middleware {name}, so the encoder runs first on the "
            f"way out and VersionMiddleware would inject its <script> into "
            f"already-encoded bytes (a corrupt body — the injection is skipped "
            f"instead). Register every BodyEncoder middleware FIRST: "
            f"app.use({name}(...)) then app.use(VersionMiddleware())."
        )

    def _refresh_cache(self, raw_version: str) -> tuple[str, str, bytes]:
        """Rebuild the cache snapshot. Sanitization runs once per version change.

        Builds the full (raw, header, inject) tuple first, then publishes it with
        a single atomic reference swap — never a field-by-field partial update.
        """
        header = _sanitize_header(raw_version)
        inject = b""
        if self._inject_html:
            # Pre-build the head injection once so the hot path never re-runs
            # json.dumps / sanitize on an HTML response. The client API script
            # reads window.__hyperAppVersion, so it must follow the version tag.
            safe_js = _escape_version_for_js(header)
            head_script = get_client_script(self._mismatch_action, self._broadcast).head
            inject = (
                f"<script>window.__hyperAppVersion={safe_js};</script>{head_script}"
            ).encode()
        snapshot = (raw_version, header, inject)
        self._cache_snapshot = snapshot  # atomic reference swap (guard + data)
        return snapshot

    @staticmethod
    def _is_encoded(response) -> bool:
        """True when the response body is already content-encoded.

        Response headers are a plain dict, so the lookup is case-insensitive
        by scan (same discipline as ``CompressionMiddleware._has_header``).
        """
        for k, v in response.headers.items():
            if k.lower() == "content-encoding" and v:
                return True
        return False

    def _record_client_version(self, request, raw_version: str) -> None:
        """Parse the client's own version and record the cohort relation.

        Runs before the handler so handlers and downstream middleware can read
        ``request.client_version``. Zero telemetry cost when disabled — the
        CounterVec short-circuits on a module-level flag.
        """
        client = _client_version(request)
        request.client_version = client
        if not client:
            relation = "unversioned"
        elif client == raw_version:
            relation = "match"
        else:
            relation = "skew"
        _version_skew_requests_total.inc_tuple((relation,))

    async def __call__(self, request, call_next):
        # The inbound cohort parse has ONE switch (APP_VERSION_CLIENT_BROADCAST)
        # and is independent of APP_VERSION_HEADER: an operator may route on the
        # client's version without echoing the server's on every response.
        if self._broadcast:
            self._record_client_version(request, get_app_version().version)

        response = await call_next(request)
        if not self._enabled:
            return response

        raw_version = get_app_version().version
        # Bind the snapshot once; the guard (snap[0]) is paired with its data.
        snap = self._cache_snapshot
        if raw_version != snap[0]:
            snap = self._refresh_cache(raw_version)
        _raw_cached, header, inject = snap

        response.headers[APP_VERSION_HEADER_NAME] = header
        response.headers[APP_VERSION_ACTION_HEADER_NAME] = self._action_header

        # HTML injection for cohort broadcast + mismatch detection.
        # Uses pre-built bytes from _refresh_cache — no json.dumps or
        # sanitization per request. Only byte-level rfind + concatenation.
        if self._inject_html and inject:
            ct = response.content_type or ""
            if "text/html" in ct and response.body:
                if self._is_encoded(response):
                    # Splicing a <script> into gzipped bytes yields a corrupt
                    # body. Historically this failed silently; make it explicit.
                    global _version_inject_skip_logged
                    if not _version_inject_skip_logged:
                        _version_inject_skip_logged = True
                        _logger.debug(
                            "VersionMiddleware: skipping HTML injection on a "
                            "content-encoded response. Register "
                            "CompressionMiddleware BEFORE VersionMiddleware so "
                            "injection sees uncompressed bytes."
                        )
                    return response
                body = response.body
                # Scan cost discipline: </body> sits at the END of a document,
                # so rfind touches only trailing bytes; </head> sits near the
                # START, so a forward find touches only leading bytes. The
                # previous `b"</body>" in body` gate + rfind(b"</head>") pair
                # scanned the FULL body twice per response — measured at
                # 7.5 us per scan on rendered pages (2x 896 ms across a 60K-
                # request profile window).
                body_idx = body.rfind(b"</body>")
                if body_idx != -1:
                    # Inject version meta + client API before </head>
                    head_idx = body.find(b"</head>", 0, body_idx)
                    if head_idx != -1:
                        body = body[:head_idx] + inject + body[head_idx:]
                        body_idx += len(inject)
                    # Inject mismatch / broadcast script before </body>
                    if self._body_script:
                        body = body[:body_idx] + self._body_script + body[body_idx:]
                    response.body = body
                    response.headers["content-length"] = str(len(body))

        return response


# ---------------------------------------------------------------------------
# Version routing middleware — blue/green, canary, split testing
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class VersionRouterMiddleware:
    """Route requests based on the requesting page's own app version.

    Reads ``X-Client-Version`` from the request (or the
    ``hyper_client_version`` cookie the injected head script writes). If the
    version is in ``version_map``, sets ``x-backend-target`` on the response
    for upstream proxy routing (nginx, Envoy, etc.). Unknown versions return
    409 Conflict.

    This is the app-aware signaling layer for proxies that want it. Because
    the client now broadcasts its version on every request, the primary load
    balancer pattern is a plain REQUEST-side map on ``X-Client-Version`` /
    ``hyper_client_version`` — no response post-processing needed.

    Usage::

        app.use(VersionRouterMiddleware(
            version_map={"v1": "backend-v1", "v2": "backend-v2"},
            default_version="v2",
        ))
    """

    version_map: dict[str, str] = field(default_factory=dict)
    default_version: str = ""
    request_header: str = CLIENT_VERSION_HEADER_NAME
    response_header: str = "x-app-served-version"
    routing_header: str = "x-backend-target"
    # Pre-sanitized version_map values — built once at init, not per-request
    _safe_map: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self._safe_map = {k: _sanitize_header(v) for k, v in self.version_map.items()}
        # A default_version outside the map would 409 EVERY request carrying
        # no client version — the exact traffic the default exists to serve.
        # A silent full outage becomes a boot error instead.
        if (
            self.default_version
            and self.version_map
            and self.default_version not in self.version_map
        ):
            available = ", ".join(sorted(self.version_map))
            raise ValueError(
                f"VersionRouterMiddleware default_version "
                f"{self.default_version!r} is not in version_map "
                f"(available: {available})"
            )

    async def __call__(self, request, call_next):
        # Shared parse: header first, cookie fallback, CRLF-sanitized and
        # length-capped (the value is attacker-controlled).
        requested = _client_version(
            request, self.request_header, CLIENT_VERSION_COOKIE_NAME
        )

        # Use default if no version requested
        if not requested:
            requested = self.default_version

        # Route known versions
        if requested and self.version_map:
            backend = self._safe_map.get(requested)
            if backend is not None:
                response = await call_next(request)
                response.headers[self.routing_header] = backend
                response.headers[self.response_header] = requested
                return response
            # Unknown version requested — 409 Conflict.
            # Unified error contract: {"detail", "status"}. The requested version
            # and the sorted list of known versions are embedded in `detail` so
            # the client keeps that hint without breaking the uniform shape.
            available = ", ".join(sorted(self.version_map))
            return Response.error(
                409,
                f"Unknown app version '{requested}'. Available versions: {available}",
            )

        response = await call_next(request)
        response.headers[self.response_header] = get_app_version().version
        return response
