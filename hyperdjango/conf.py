"""
HyperDjango settings with defaults, validation, and environment loading.

All settings are namespaced under HYPERDJANGO_* in Django's settings.
Environment variables use HYPER_* prefix (e.g. HYPER_SECRET_KEY, HYPER_DEBUG).

Usage in Django settings.py:
    HYPERDJANGO_POOL_SIZE = 0          # 0 = auto-tune from THREAD_POOL_SIZE (+ headroom)
    HYPERDJANGO_PREPARED_STATEMENTS = True
    HYPERDJANGO_POOL_MAX_QUERIES = 10000  # rotate connections after N queries
    HYPERDJANGO_POOL_MAX_LIFETIME = 3600  # max connection age in seconds
    HYPERDJANGO_STATEMENT_CACHE_SIZE = 256  # LRU prepared statement cache
    HYPERDJANGO_CONNECT_TIMEOUT = 10000    # ms — TCP connect timeout (non-blocking + poll)
    HYPERDJANGO_QUERY_TIMEOUT = 0          # ms — PostgreSQL statement_timeout (0 = unlimited)

Usage via environment:
    HYPER_SECRET_KEY=mysecret HYPER_DEBUG=true uv run hyper run

Usage via .env file (project root):
    SECRET_KEY=mysecret
    DEBUG=true
"""

import contextlib
import logging
import os
import pathlib
import secrets
import zoneinfo
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

_logger = logging.getLogger("hyperdjango.conf")

# This module is ONE of the two sanctioned environment boundaries (the other is
# site_config.py). It is the single authority that turns HYPER_*/PG*/DATABASE_URL
# environment into settings; every other framework module reads values through
# get_setting()/resolve_database_url() and never touches os.environ directly.
# scripts/check_no_os_environ.py enforces this.

# ── Shared platform constants ──────────────────────────────────────────────────
# Used across multiple modules. Import from here to avoid magic number duplication.

# Time constants (seconds)
ONE_MINUTE = 60
ONE_HOUR = 3600
ONE_DAY = 86400

# Pagination
DEFAULT_PAGE_SIZE = 25
DEFAULT_MAX_PAGE_SIZE = 100

# Rate limiting
DEFAULT_RATE_LIMIT_MAX_REQUESTS = 100
DEFAULT_RATE_LIMIT_WINDOW = 60  # 1 minute

# Caching
DEFAULT_CACHE_TTL = 300  # 5 minutes
DEFAULT_MAX_CACHE_BYTES = 256 * 1024 * 1024  # 256 MB

# Performance monitoring
DEFAULT_SLOW_QUERY_THRESHOLD_MS = 100.0

# Content types
CONTENT_TYPE_JSON = "application/json"
CONTENT_TYPE_FORM = "application/x-www-form-urlencoded"
CONTENT_TYPE_MULTIPART = "multipart/form-data"

# Headers
HEADER_CONTENT_TYPE = "content-type"

# Search
MAX_SEARCH_LENGTH = 200
MAX_REGEX_LENGTH = 100

# Shared membership sets (avoid recreating per-call)
# "on" is included because HTML checkboxes submit `name=on` when checked.
TRUTHY_STRINGS = frozenset({"1", "true", "yes", "on"})
FALSY_STRINGS = frozenset({"0", "false", "no", ""})


def parse_bool(value: object) -> bool:
    """Coerce a value to a bool — the single source of truth for string→bool.

    Used by forms, admin coercion, serializers, config, and doctor checks so the
    rules never drift. Semantics:

    - An actual ``bool`` passes through unchanged.
    - A ``str`` is matched **case-insensitively** (after stripping surrounding
      whitespace) against :data:`TRUTHY_STRINGS` (``1/true/yes/on``); everything
      else — ``""``, ``"0"``, ``"false"``, ``"off"`` — is ``False``. Case matters:
      ``"TRUE"``, ``"Yes"``, ``"On"`` are all truthy (a bug the old per-module
      sets had, since several compared without lowercasing).
    - Any other type falls back to Python truthiness (``bool(value)``).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in TRUTHY_STRINGS
    return bool(value)


WRITE_METHODS = frozenset({"POST", "PUT", "PATCH"})
METERING_BUCKETS = ("hourly", "daily", "monthly")

# Server thread pool
DEFAULT_THREAD_POOL_SIZE = 24
MAX_THREAD_POOL_SIZE = 128

# Static files
STATIC_FILE_MAX_AGE = ONE_HOUR  # Cache-Control: max-age for static files
STATIC_FILE_IMMUTABLE_MAX_AGE = 365 * ONE_DAY  # 1 year for hashed/immutable files

# Time units
ONE_WEEK = 7 * ONE_DAY

# Test runner
TEST_TIMEOUT_SECONDS = 300

# ── Default settings ──────────────────────────────────────────────────────────

DEFAULTS: dict[str, object] = {
    # ── Database ──
    "POOL_SIZE": 0,  # 0 = auto-tune: max(THREAD_POOL_SIZE + headroom + offload_workers, floor)
    "PREPARED_STATEMENTS": True,
    "POOL_MAX_QUERIES": 10000,
    "POOL_MAX_LIFETIME": 3600,
    "STATEMENT_CACHE_SIZE": 256,
    "CONNECT_TIMEOUT": 10000,  # ms — TCP connect + auth timeout (100ms..300s)
    "QUERY_TIMEOUT": 0,  # ms — PostgreSQL statement_timeout (0 = unlimited)
    "DATABASE_URL": "",  # postgresql://user:pass@host/db
    # Max concurrent DB round-trips offloaded off a MULTIPLEXING event loop
    # (the shared WebSocket pool / HTTP reactor) so a query can't stall the
    # other connections that loop drives. Thread-per-request loops (HTTP,
    # tests, scripts) run queries inline and never touch this. 0 = auto =
    # min(cpu_count, 8). Its slots are folded into the pool connection budget.
    "DB_OFFLOAD_WORKERS": 0,
    # ── Security ──
    "SECRET_KEY": "",  # REQUIRED in production — sessions, CSRF, signing
    "DEBUG": False,
    "ALLOWED_HOSTS": [],  # list of allowed hostnames
    "SECURE_SSL_REDIRECT": False,
    "SECURE_HSTS_SECONDS": 0,  # 0 = disabled
    "SECURE_HSTS_INCLUDE_SUBDOMAINS": False,
    "SECURE_HSTS_PRELOAD": False,
    "SECURE_CONTENT_TYPE_NOSNIFF": True,
    "SECURE_REFERRER_POLICY": "same-origin",
    "SECURE_PROXY_SSL_HEADER": "",  # header name, e.g. "X-Forwarded-Proto"
    "SECURE_REDIRECT_EXEMPT": [],  # regex patterns exempt from SSL redirect
    "SECURE_SSL_HOST": "",  # host to redirect SSL to (empty = same host)
    "SECURE_CROSS_ORIGIN_OPENER_POLICY": "same-origin",
    "SECURE_CSP": {},  # Content Security Policy directives (empty = no CSP)
    "X_FRAME_OPTIONS": "DENY",  # "DENY" | "SAMEORIGIN" | ""
    # ── CSRF ──
    "CSRF_COOKIE_SECURE": False,
    "CSRF_COOKIE_HTTPONLY": True,
    "CSRF_COOKIE_SAMESITE": "Lax",
    "CSRF_TRUSTED_ORIGINS": [],  # list of trusted origins for CSRF
    "CSRF_COOKIE_DOMAIN": "",  # empty = current domain only
    "CSRF_COOKIE_NAME": "csrftoken",
    "CSRF_COOKIE_PATH": "/",
    "CSRF_COOKIE_AGE": 31449600,  # 1 year in seconds
    "CSRF_HEADER_NAME": "X-CSRFToken",
    # ── Cache ──
    "CACHE_BACKEND": "memory",  # "memory" | "database"
    "CACHE_TTL": 300,
    "CACHE_MAX_BYTES": 256 * 1024 * 1024,
    "CACHE_KEY_PREFIX": "",  # prefix for all cache keys
    "CACHE_VERSION": 1,  # default cache version number
    # ── Auth ──
    "PASSWORD_HASHER": "argon2id",  # only argon2id supported
    "LOGIN_URL": "/login/",
    "LOGIN_REDIRECT_URL": "/",
    "LOGOUT_REDIRECT_URL": "/",
    "PASSWORD_RESET_TIMEOUT": 259200,  # 3 days in seconds
    "AUTH_PASSWORD_VALIDATORS": [],  # list of validator class paths
    "SESSION_COOKIE_AGE": 1209600,  # 2 weeks in seconds
    "SESSION_COOKIE_SECURE": False,
    "SESSION_COOKIE_HTTPONLY": True,
    "SESSION_COOKIE_SAMESITE": "Lax",
    "SESSION_COOKIE_NAME": "sessionid",
    "SESSION_EXPIRE_AT_BROWSER_CLOSE": False,
    "SESSION_COOKIE_DOMAIN": "",  # empty = current domain only
    "SESSION_COOKIE_PATH": "/",
    "SESSION_SAVE_EVERY_REQUEST": False,
    "PASSWORD_MIN_LENGTH": 8,
    # ── App Secrets ──
    # Generated fresh per process. Override in production via HYPER_* env vars
    # or Django settings (HYPERDJANGO_CSRF_SECRET, etc.). Per-session random
    # defaults are safe for development but DO NOT persist across restarts.
    # Production: set HYPER_CSRF_SECRET, HYPER_SESSION_SECRET, etc. in env.
    "CSRF_SECRET": secrets.token_urlsafe(32),
    "SESSION_SECRET": secrets.token_urlsafe(32),
    "SESSION_SIGNING_KEY": secrets.token_urlsafe(
        32
    ),  # RESERVED — TokenConfig uses explicit keys; sessions use SESSION_SECRET
    "ADMIN_SECRET": secrets.token_urlsafe(32),
    "API_KEY": secrets.token_urlsafe(32),
    # Cursor-pagination signing secret. Empty falls back to SECRET_KEY (then an
    # ephemeral per-process key). Set in production so cursors survive restarts.
    "CURSOR_SECRET": "",
    # ── Seed credentials ──
    "SEED_PASSWORD": "",  # Empty = random per user, printed at seed time
    "ADMIN_PASSWORD": "",  # Empty = random, printed at seed time
    # Non-interactive `createsuperuser --noinput` password (env-only in practice).
    "SUPERUSER_PASSWORD": "",
    # Comma-separated extra command-module paths for `hyper` command discovery
    # (in addition to the default "commands" package).
    "COMMANDS": "",
    # ── Server ──
    "PORT": 8000,  # Server listen port
    "HOST": "127.0.0.1",  # Server listen host
    "CORS_ORIGINS": [],  # CORS allowed origins (empty = no cross-origin requests allowed)
    # Cross-origin WebSocket allowlist (CSWSH defense). Same-origin connections
    # are always allowed; empty means ONLY same-origin. "*" allows any origin.
    "WS_ALLOWED_ORIGINS": [],
    # ── Multi-Tenancy ──
    # Fail CLOSED: a TenantMixin model queried with NO active tenant context (and
    # not .unscoped()) returns ZERO rows instead of every tenant's data. Cross-
    # tenant access (CLI/migrations/admin) must be explicit via .unscoped().
    "TENANT_STRICT": True,
    # ── Rate Limiting ──
    "RATE_LIMIT_REQUESTS": 120,  # Requests per window
    "RATE_LIMIT_WINDOW": 60,  # Window in seconds
    "RATELIMIT_IETF_HEADERS": True,  # IETF RateLimit + RateLimit-Policy headers
    "RATELIMIT_LEGACY_HEADERS": True,  # Legacy x-ratelimit-* headers
    "RATELIMIT_PROBLEM_DETAILS": True,  # RFC 9457 Problem Details on 429
    # ── Embeddings (semantic search) ──
    "EMBEDDINGS_API_URL": "",  # OpenAI-compatible embeddings endpoint
    "EMBEDDINGS_API_KEY": "",  # Embeddings API key
    "EMBEDDINGS_MODEL": "text-embedding-3-small",  # Embedding model name
    "EMBEDDINGS_VECTOR_DIM": 1536,  # Embedding vector dimension
    # ── Feature Flags ──
    "LOAD_TEST": False,  # Disable rate limiting for load tests
    # ── Email ──
    "EMAIL_HOST": "localhost",
    "EMAIL_PORT": 25,
    "EMAIL_HOST_USER": "",
    "EMAIL_HOST_PASSWORD": "",
    "EMAIL_USE_TLS": False,
    "EMAIL_USE_SSL": False,
    "EMAIL_BACKEND": "smtp",  # "smtp" | "console" | "memory"
    "DEFAULT_FROM_EMAIL": "webmaster@localhost",
    "EMAIL_SUBJECT_PREFIX": "[HyperDjango] ",
    "EMAIL_TIMEOUT": 30,  # seconds
    "EMAIL_SSL_CERTFILE": "",
    "EMAIL_SSL_KEYFILE": "",
    "SERVER_EMAIL": "root@localhost",
    # ── Static Files ──
    "STATIC_URL": "/static/",
    "STATIC_ROOT": "",
    "STATIC_MAX_AGE": 3600,
    "STATICFILES_DIRS": [],  # additional directories for static file finders
    # ── Media ──
    "MEDIA_URL": "/media/",
    "MEDIA_ROOT": "",  # filesystem path for user-uploaded files
    # ── Upload ──
    "MAX_UPLOAD_SIZE": 10 * 1024 * 1024,  # 10MB
    "ALLOWED_UPLOAD_EXTENSIONS": [],  # empty = all allowed
    "DATA_UPLOAD_MAX_NUMBER_FIELDS": 1000,
    "DATA_UPLOAD_MAX_NUMBER_FILES": 100,
    "FILE_UPLOAD_TEMP_DIR": "",  # empty = system temp
    "FILE_UPLOAD_PERMISSIONS": 0o644,
    "FILE_UPLOAD_DIRECTORY_PERMISSIONS": 0o755,
    "FILE_UPLOAD_MAX_MEMORY_SIZE": 2621440,  # 2.5MB — per-file threshold: memory vs disk spill
    "FILE_UPLOAD_MAX_SIZE": 0,  # 0 = unlimited per-file max during streaming
    "STREAM_BODY_CHUNK_SIZE": 262144,  # 256KB — chunk size for request.stream()
    # ── Server ──
    "HTTP_SERVER": "auto",  # "auto" | "zig" | "asgi"
    "THREAD_POOL_SIZE": 24,
    # Native HTTP connection model. "reactor" (default): idle keep-alive
    # connections wait in a kqueue/epoll reactor (no worker thread); a worker
    # serves one request then returns the connection to the reactor — live
    # connections bounded by fds/memory, not the pool. "threaded": one worker
    # thread pinned per connection for its lifetime — max live = THREAD_POOL_SIZE.
    # reactor is
    # the SAFE default: it degrades gracefully under many connections (holds them,
    # latency rises) instead of starving connections past the pool size the way
    # 'threaded' does. 'threaded' is an opt-in max-throughput mode for known-bounded
    # connection counts (see docs/design/http-connection-reactor.md and the README
    # 'HTTP server modes' section for the full trade-off + the threaded ceiling).
    "HTTP_SERVER_MODEL": "reactor",
    # Threaded-mode load-shedding: once the accept backlog exceeds this many
    # pending connections (workers all pinned), new connections get a fast 503
    # instead of being accepted and starved. 0 = disable (unbounded queue).
    # Ignored in reactor mode (which holds all connections). 0 here = "use the
    # native default" (THREAD_POOL_SIZE × 8); set a positive int to override.
    "HTTP_MAX_PENDING": 0,
    "MAX_BODY_SIZE": 10 * 1024 * 1024,  # 10MB — max body for in-memory buffering
    # ── WebSocket concurrency model ──
    # "shared" (default): connections are multiplexed over a small pool of
    #   event loops (one per core). Concurrent-connection count is bounded by
    #   fds/memory, not the thread pool — the right default for real WebSocket
    #   workloads (many mostly-idle clients) with full multi-core throughput.
    #   Handlers must be cooperative (never park a thread per connection).
    # "thread": one-OS-thread-per-connection. Max live connections =
    #   THREAD_POOL_SIZE. Only needed for handlers that do heavy synchronous
    #   CPU work per message and can't be made cooperative.
    "WEBSOCKET_CONCURRENCY": "shared",
    "WEBSOCKET_LOOP_COUNT": 0,  # 0 = auto (min(cpu_count, 8))
    # Native listen() backlog (accept queue depth). Kernel silently clamps to
    # somaxconn. Read by the Zig server at startup; doctor cross-checks it.
    "LISTEN_BACKLOG": 4096,
    # Slow-client socket send timeout (ms). 0 = unbounded (a stuck client can
    # pin a worker's send). Read by the Zig server at startup; doctor sanity-checks.
    "SEND_TIMEOUT_MS": 30000,
    # ── Logging ──
    "LOG_LEVEL": "INFO",
    "LOG_FORMAT": "text",  # "text" | "json"
    # ── Proxy ──
    "USE_X_FORWARDED_HOST": False,
    "USE_X_FORWARDED_PORT": False,
    "TRUSTED_PROXY_COUNT": 0,
    "TRUSTED_PROXIES": [],
    "MTLS_PROXY_SECRET": "",
    # ── URL ──
    "APPEND_SLASH": True,
    "PREPEND_WWW": False,
    # ── Messages ──
    "MESSAGE_LEVEL": 20,  # INFO level
    "MESSAGE_TAGS": {},  # empty = use defaults
    # ── Other ──
    "ADMINS": [],  # list of (name, email) tuples for error notifications
    "MANAGERS": [],  # list of (name, email) tuples
    "DISALLOWED_USER_AGENTS": [],  # list of user-agent regex strings
    # ── Internationalization ──
    "LANGUAGE_CODE": "en",
    "TIME_ZONE": "UTC",
    "USE_TZ": True,
    "DATE_FORMAT": "N j, Y",  # "March 28, 2026"
    "DATETIME_FORMAT": "N j, Y, P",  # "March 28, 2026, 3:45 p.m."
    "TIME_FORMAT": "P",  # "3:45 p.m."
    "SHORT_DATE_FORMAT": "m/d/Y",  # "03/28/2026"
    "SHORT_DATETIME_FORMAT": "m/d/Y P",
    "DECIMAL_SEPARATOR": ".",
    "THOUSAND_SEPARATOR": ",",
    "USE_THOUSAND_SEPARATOR": False,
    "NUMBER_GROUPING": 3,
    "FIRST_DAY_OF_WEEK": 0,  # 0=Sunday, 1=Monday
    "DATE_INPUT_FORMATS": ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"],
    "DATETIME_INPUT_FORMATS": [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%m/%d/%Y %H:%M:%S",
    ],
    # ── Tasks ──
    "TASK_WORKERS": 4,
    "TASK_MAX_QUEUE_SIZE": 10000,
    "TASK_DLQ_MAX_SIZE": 10000,
    # ── i18n Paths ──
    "LOCALE_PATHS": [],
    # ── Features ──
    "FILE_ROUTING": False,
    "FILE_ROUTING_DIR": "views",
    "STATIC_CACHE": False,
    "HOT_RELOAD": False,
    "PRE_VALIDATION": False,
    "VALIDATION_BACKEND": "dhi",  # "dhi" | "django"
    # ── Versioning ──
    "APP_VERSION": "",  # explicit version (git SHA, semver); empty = auto-compute from manifest
    "APP_VERSION_HEADER": True,  # emit X-App-Version on responses
    "APP_VERSION_MISMATCH": "prompt",  # "prompt" | "reload" | "warn" | "ignore"
    "APP_VERSION_CLIENT_BROADCAST": True,  # client sends X-Client-Version on requests
    "APP_BUILD_COMMIT": "",  # git commit of the release; /version metadata only
    "VERSION_ENDPOINT": True,  # mount /version endpoint when mount_version() called
    "STATIC_DEV_VERSION_QUERY": True,  # append ?v=hash to static URLs in dev mode
    # ── Static Files Tuning ──
    "STATICFILES_GZIP_MIN_SIZE": 1024,  # bytes; compress files larger than this
    "STATICFILES_HASH_LENGTH": 12,  # hex chars in content-hash filenames
    "STATICFILES_MAX_POST_PROCESS_PASSES": 5,  # CSS url() rewrite iterations
    "STATICFILES_DEV_HASH_CACHE_MAX": 4096,  # max entries in dev-mode hash cache
    # ── Task Queue Tuning ──
    "TASK_MAX_COMPLETED_RESULTS": 10000,  # result retention before eviction
    "TASK_CLEANUP_INTERVAL": 100,  # check eviction every N completions
    "TASK_SHUTDOWN_TIMEOUT": 5,  # seconds to wait for workers on shutdown
    "TASK_MAX_PENDING_PER_USER": 0,  # 0 = unlimited
    "TASK_CIRCUIT_FAILURE_THRESHOLD": 5,  # failures before circuit opens
    "TASK_CIRCUIT_RECOVERY_TIMEOUT": 30.0,  # seconds before recovery probe
    "TASK_CIRCUIT_WINDOW": 300.0,  # rolling failure window (seconds)
    # ── Performance / Diagnostics ──
    "PERFORMANCE_HISTORY_SIZE": 1000,  # request history ring buffer size
    "PERFORMANCE_N_PLUS_ONE_THRESHOLD": 5,  # repeated query count to flag N+1
    "SLOW_QUERY_SQL_LENGTH": 2000,  # SQL text truncation in slow query log
    "SLOW_QUERY_PARAMS_LENGTH": 500,  # params truncation in slow query log
    "SLOW_QUERY_RETENTION_DAYS": 7,  # days to keep slow query log entries
    # ── Rate Limiting Tuning ──
    "RATELIMIT_CLEANUP_RETENTION": 3600,  # seconds to keep old rate limit entries
    "RATELIMIT_MAX_BUCKETS": 100000,  # hard cap on in-memory buckets (LRU-evicted)
    # ── Hot Reload ──
    "HOT_RELOAD_POLL_INTERVAL": 0.3,  # RESERVED — native watcher has no poll fallback; unused
    "HOT_RELOAD_SSE_HEARTBEAT": 30,  # seconds between SSE keepalive pings
    # ── Telemetry (v0.15.0+) ──
    "TELEMETRY_ENABLED": False,  # master switch; zero cost when False
    "TELEMETRY_SERVICE_NAME": "hyperdjango",  # attached to every root span
    "TELEMETRY_SAMPLE_RATIO": 0.01,  # head sampling rate 0.0-1.0
    "TELEMETRY_DRAIN_INTERVAL": 1.0,  # seconds between background drains
    "TELEMETRY_EXTRACT_TRACEPARENT": True,  # honor inbound W3C trace-context
    "TELEMETRY_SINKS": ["prometheus"],  # "prometheus" | "stdout" | "memory"
    "TELEMETRY_SPAN_RING_CAPACITY": 16384,  # power of 2 in [256, 16777216]
    "TELEMETRY_AUTO_LOG_CORRELATION": True,  # auto-inject trace_id/span_id into log records
}


# ── Setting definition with validation metadata ──────────────────────────────


@dataclass(slots=True)
class SettingDefinition:
    """Validation metadata for a single setting."""

    name: str
    type: type  # int, str, bool, list, etc.
    default: object
    required: bool = False  # if True, must be set explicitly (in production)
    min_value: int | float | None = None
    max_value: int | float | None = None
    choices: frozenset[str] | None = None  # valid string values
    # Optional extra validator. Receives the resolved value and raises
    # ValueError with a clear message if the value is invalid. Used for
    # checks that can't be expressed as a simple range/choice (e.g. a
    # TIME_ZONE that must resolve via zoneinfo).
    validator: Callable[[object], None] | None = None
    description: str = ""


# ── Custom setting validators ─────────────────────────────────────────────────


def _validate_time_zone(value: object) -> None:
    """Validate TIME_ZONE resolves to a real IANA zone via zoneinfo.

    Raises ValueError naming the bad zone if it can't be resolved, instead of
    silently accepting a misconfigured zone.
    """
    try:
        zoneinfo.ZoneInfo(value)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"TIME_ZONE: {value!r} is not a valid time zone "
            f"(zoneinfo could not resolve it)"
        ) from exc


# ── Setting definitions registry ──────────────────────────────────────────────

SETTING_DEFINITIONS: dict[str, SettingDefinition] = {
    # ── Database ──
    "POOL_SIZE": SettingDefinition(
        name="POOL_SIZE",
        type=int,
        default=0,
        min_value=0,
        max_value=1024,
        description="Connection pool size (0 = auto-tune)",
    ),
    "PREPARED_STATEMENTS": SettingDefinition(
        name="PREPARED_STATEMENTS",
        type=bool,
        default=True,
        description="RESERVED — the native pg pool ABI does not yet accept this; setting it has no effect.",
    ),
    "POOL_MAX_QUERIES": SettingDefinition(
        name="POOL_MAX_QUERIES",
        type=int,
        default=10000,
        min_value=0,
        description="Rotate connections after N queries",
    ),
    "POOL_MAX_LIFETIME": SettingDefinition(
        name="POOL_MAX_LIFETIME",
        type=int,
        default=3600,
        min_value=0,
        description="Max connection age in seconds",
    ),
    "STATEMENT_CACHE_SIZE": SettingDefinition(
        name="STATEMENT_CACHE_SIZE",
        type=int,
        default=256,
        min_value=0,
        max_value=65536,
        description="RESERVED — the native pg pool ABI does not yet accept this; setting it has no effect.",
    ),
    "CONNECT_TIMEOUT": SettingDefinition(
        name="CONNECT_TIMEOUT",
        type=int,
        default=10000,
        min_value=100,
        max_value=300000,
        description="TCP connect + auth timeout in ms",
    ),
    "QUERY_TIMEOUT": SettingDefinition(
        name="QUERY_TIMEOUT",
        type=int,
        default=0,
        min_value=0,
        description="PostgreSQL statement_timeout in ms (0 = unlimited)",
    ),
    "DATABASE_URL": SettingDefinition(
        name="DATABASE_URL",
        type=str,
        default="",
        description="PostgreSQL connection URL",
    ),
    "DB_OFFLOAD_WORKERS": SettingDefinition(
        name="DB_OFFLOAD_WORKERS",
        type=int,
        default=0,
        min_value=0,
        max_value=128,
        description=(
            "Max concurrent DB round-trips offloaded off a multiplexing event "
            "loop (shared WebSocket pool / HTTP reactor) so one query can't "
            "stall the loop's other connections. 0 = auto (min(cpu, 8)). "
            "Folded into the pool connection budget."
        ),
    ),
    # ── Security ──
    "SECRET_KEY": SettingDefinition(
        name="SECRET_KEY",
        type=str,
        default="",
        required=True,
        description="Secret key for sessions, CSRF, signing",
    ),
    "DEBUG": SettingDefinition(
        name="DEBUG",
        type=bool,
        default=False,
        description="Enable debug mode",
    ),
    "ALLOWED_HOSTS": SettingDefinition(
        name="ALLOWED_HOSTS",
        type=list,
        default=[],
        description="List of allowed hostnames",
    ),
    "SECURE_SSL_REDIRECT": SettingDefinition(
        name="SECURE_SSL_REDIRECT",
        type=bool,
        default=False,
        description="Redirect HTTP to HTTPS",
    ),
    "SECURE_HSTS_SECONDS": SettingDefinition(
        name="SECURE_HSTS_SECONDS",
        type=int,
        default=0,
        min_value=0,
        description="HSTS max-age in seconds (0 = disabled)",
    ),
    "SECURE_HSTS_INCLUDE_SUBDOMAINS": SettingDefinition(
        name="SECURE_HSTS_INCLUDE_SUBDOMAINS",
        type=bool,
        default=False,
        description="Include subdomains in HSTS",
    ),
    "SECURE_HSTS_PRELOAD": SettingDefinition(
        name="SECURE_HSTS_PRELOAD",
        type=bool,
        default=False,
        description="Enable HSTS preload",
    ),
    "SECURE_CONTENT_TYPE_NOSNIFF": SettingDefinition(
        name="SECURE_CONTENT_TYPE_NOSNIFF",
        type=bool,
        default=True,
        description="Set X-Content-Type-Options: nosniff",
    ),
    "SECURE_REFERRER_POLICY": SettingDefinition(
        name="SECURE_REFERRER_POLICY",
        type=str,
        default="same-origin",
        choices=frozenset(
            {
                "no-referrer",
                "no-referrer-when-downgrade",
                "origin",
                "origin-when-cross-origin",
                "same-origin",
                "strict-origin",
                "strict-origin-when-cross-origin",
                "unsafe-url",
            }
        ),
        description="Referrer-Policy header value",
    ),
    "SECURE_PROXY_SSL_HEADER": SettingDefinition(
        name="SECURE_PROXY_SSL_HEADER",
        type=str,
        default="",
        description="Header name indicating HTTPS behind proxy (e.g. X-Forwarded-Proto)",
    ),
    "SECURE_REDIRECT_EXEMPT": SettingDefinition(
        name="SECURE_REDIRECT_EXEMPT",
        type=list,
        default=[],
        description="Regex patterns exempt from SSL redirect",
    ),
    "SECURE_SSL_HOST": SettingDefinition(
        name="SECURE_SSL_HOST",
        type=str,
        default="",
        description="Host to redirect SSL to (empty = same host)",
    ),
    "SECURE_CROSS_ORIGIN_OPENER_POLICY": SettingDefinition(
        name="SECURE_CROSS_ORIGIN_OPENER_POLICY",
        type=str,
        default="same-origin",
        choices=frozenset({"same-origin", "same-origin-allow-popups", "unsafe-none"}),
        description="Cross-Origin-Opener-Policy header value",
    ),
    "SECURE_CSP": SettingDefinition(
        name="SECURE_CSP",
        type=dict,
        default={},
        description="Content Security Policy directives (empty = no CSP)",
    ),
    "X_FRAME_OPTIONS": SettingDefinition(
        name="X_FRAME_OPTIONS",
        type=str,
        default="DENY",
        choices=frozenset({"DENY", "SAMEORIGIN", ""}),
        description="X-Frame-Options header value",
    ),
    # ── CSRF ──
    "CSRF_COOKIE_SECURE": SettingDefinition(
        name="CSRF_COOKIE_SECURE",
        type=bool,
        default=False,
        description="Set Secure flag on CSRF cookie",
    ),
    "CSRF_COOKIE_HTTPONLY": SettingDefinition(
        name="CSRF_COOKIE_HTTPONLY",
        type=bool,
        default=True,
        description="Set HttpOnly flag on CSRF cookie",
    ),
    "CSRF_COOKIE_SAMESITE": SettingDefinition(
        name="CSRF_COOKIE_SAMESITE",
        type=str,
        default="Lax",
        choices=frozenset({"Strict", "Lax", "None"}),
        description="SameSite attribute for CSRF cookie",
    ),
    "CSRF_TRUSTED_ORIGINS": SettingDefinition(
        name="CSRF_TRUSTED_ORIGINS",
        type=list,
        default=[],
        description="List of trusted origins for CSRF validation",
    ),
    "CSRF_COOKIE_DOMAIN": SettingDefinition(
        name="CSRF_COOKIE_DOMAIN",
        type=str,
        default="",
        description="Domain for CSRF cookie (empty = current domain only)",
    ),
    "CSRF_COOKIE_NAME": SettingDefinition(
        name="CSRF_COOKIE_NAME",
        type=str,
        default="csrftoken",
        description="Name of the CSRF cookie",
    ),
    "CSRF_COOKIE_PATH": SettingDefinition(
        name="CSRF_COOKIE_PATH",
        type=str,
        default="/",
        description="Path for CSRF cookie",
    ),
    "CSRF_COOKIE_AGE": SettingDefinition(
        name="CSRF_COOKIE_AGE",
        type=int,
        default=31449600,
        min_value=0,
        description="CSRF cookie max age in seconds (default 1 year)",
    ),
    "CSRF_HEADER_NAME": SettingDefinition(
        name="CSRF_HEADER_NAME",
        type=str,
        default="X-CSRFToken",
        description="HTTP header name for CSRF token",
    ),
    # ── Cache ──
    "CACHE_BACKEND": SettingDefinition(
        name="CACHE_BACKEND",
        type=str,
        default="memory",
        choices=frozenset({"memory", "database"}),
        description="Cache backend type",
    ),
    "CACHE_TTL": SettingDefinition(
        name="CACHE_TTL",
        type=int,
        default=300,
        min_value=0,
        description="Default cache TTL in seconds",
    ),
    "CACHE_MAX_BYTES": SettingDefinition(
        name="CACHE_MAX_BYTES",
        type=int,
        default=256 * 1024 * 1024,
        min_value=0,
        description="Maximum cache size in bytes",
    ),
    "CACHE_KEY_PREFIX": SettingDefinition(
        name="CACHE_KEY_PREFIX",
        type=str,
        default="",
        description="Prefix for all cache keys",
    ),
    "CACHE_VERSION": SettingDefinition(
        name="CACHE_VERSION",
        type=int,
        default=1,
        min_value=1,
        description="Default cache version number",
    ),
    # ── Auth ──
    "PASSWORD_HASHER": SettingDefinition(
        name="PASSWORD_HASHER",
        type=str,
        default="argon2id",
        choices=frozenset({"argon2id"}),
        description="Password hashing algorithm",
    ),
    "LOGIN_URL": SettingDefinition(
        name="LOGIN_URL",
        type=str,
        default="/login/",
        description="URL to redirect unauthenticated users to",
    ),
    "LOGIN_REDIRECT_URL": SettingDefinition(
        name="LOGIN_REDIRECT_URL",
        type=str,
        default="/",
        description="URL to redirect to after successful login",
    ),
    "LOGOUT_REDIRECT_URL": SettingDefinition(
        name="LOGOUT_REDIRECT_URL",
        type=str,
        default="/",
        description="URL to redirect to after logout",
    ),
    "PASSWORD_RESET_TIMEOUT": SettingDefinition(
        name="PASSWORD_RESET_TIMEOUT",
        type=int,
        default=259200,
        min_value=1,
        description="Password reset token validity in seconds (default 3 days)",
    ),
    "AUTH_PASSWORD_VALIDATORS": SettingDefinition(
        name="AUTH_PASSWORD_VALIDATORS",
        type=list,
        default=[],
        description="List of password validator class paths",
    ),
    "SESSION_COOKIE_AGE": SettingDefinition(
        name="SESSION_COOKIE_AGE",
        type=int,
        default=1209600,
        min_value=0,
        description="Session cookie max age in seconds",
    ),
    "SESSION_COOKIE_SECURE": SettingDefinition(
        name="SESSION_COOKIE_SECURE",
        type=bool,
        default=False,
        description="Set Secure flag on session cookie",
    ),
    "SESSION_COOKIE_HTTPONLY": SettingDefinition(
        name="SESSION_COOKIE_HTTPONLY",
        type=bool,
        default=True,
        description="Set HttpOnly flag on session cookie",
    ),
    "SESSION_COOKIE_SAMESITE": SettingDefinition(
        name="SESSION_COOKIE_SAMESITE",
        type=str,
        default="Lax",
        choices=frozenset({"Strict", "Lax", "None"}),
        description="SameSite attribute for session cookie",
    ),
    "SESSION_COOKIE_NAME": SettingDefinition(
        name="SESSION_COOKIE_NAME",
        type=str,
        default="sessionid",
        description="Session cookie name",
    ),
    "SESSION_EXPIRE_AT_BROWSER_CLOSE": SettingDefinition(
        name="SESSION_EXPIRE_AT_BROWSER_CLOSE",
        type=bool,
        default=False,
        description="Expire session when browser closes",
    ),
    "SESSION_COOKIE_DOMAIN": SettingDefinition(
        name="SESSION_COOKIE_DOMAIN",
        type=str,
        default="",
        description="Domain for session cookie (empty = current domain only)",
    ),
    "SESSION_COOKIE_PATH": SettingDefinition(
        name="SESSION_COOKIE_PATH",
        type=str,
        default="/",
        description="Path for session cookie",
    ),
    "SESSION_SAVE_EVERY_REQUEST": SettingDefinition(
        name="SESSION_SAVE_EVERY_REQUEST",
        type=bool,
        default=False,
        description="Save session to DB on every request (not just when modified)",
    ),
    "PASSWORD_MIN_LENGTH": SettingDefinition(
        name="PASSWORD_MIN_LENGTH",
        type=int,
        default=8,
        min_value=1,
        max_value=128,
        description="Minimum password length",
    ),
    # ── App Secrets ──
    "CSRF_SECRET": SettingDefinition(
        name="CSRF_SECRET",
        type=str,
        default="<random per session>",
        description="CSRF middleware signing secret (auto-generated if not set)",
    ),
    "SESSION_SECRET": SettingDefinition(
        name="SESSION_SECRET",
        type=str,
        default="<random per session>",
        description="SessionAuth secret (auto-generated if not set)",
    ),
    "SESSION_SIGNING_KEY": SettingDefinition(
        name="SESSION_SIGNING_KEY",
        type=str,
        default="<random per session>",
        description="TokenEngine signing key (auto-generated if not set)",
    ),
    "ADMIN_SECRET": SettingDefinition(
        name="ADMIN_SECRET",
        type=str,
        default="<random per session>",
        description="HyperAdmin panel secret (auto-generated if not set)",
    ),
    "API_KEY": SettingDefinition(
        name="API_KEY",
        type=str,
        default="<random per session>",
        description="API key (auto-generated if not set)",
    ),
    "CURSOR_SECRET": SettingDefinition(
        name="CURSOR_SECRET",
        type=str,
        default="",
        description=(
            "Cursor-pagination signing secret. Empty falls back to SECRET_KEY, "
            "then an ephemeral per-process key (cursors won't survive restarts)."
        ),
    ),
    # ── Seed credentials ──
    "SEED_PASSWORD": SettingDefinition(
        name="SEED_PASSWORD",
        type=str,
        default="",
        description="Global password for all seed users (empty = random per user)",
    ),
    "ADMIN_PASSWORD": SettingDefinition(
        name="ADMIN_PASSWORD",
        type=str,
        default="",
        description="Password for hyper_users admin (empty = random, printed at seed time)",
    ),
    "SUPERUSER_PASSWORD": SettingDefinition(
        name="SUPERUSER_PASSWORD",
        type=str,
        default="",
        description="Password for `createsuperuser --noinput` (env-only in practice)",
    ),
    "COMMANDS": SettingDefinition(
        name="COMMANDS",
        type=str,
        default="",
        description=(
            "Comma-separated extra command-module paths for `hyper` command "
            "discovery (in addition to the default 'commands' package)."
        ),
    ),
    # ── Server ──
    "PORT": SettingDefinition(
        name="PORT",
        type=int,
        default=8000,
        min_value=1,
        max_value=65535,
        description="Server listen port",
    ),
    "HOST": SettingDefinition(
        name="HOST",
        type=str,
        default="127.0.0.1",
        description="Server listen host",
    ),
    "CORS_ORIGINS": SettingDefinition(
        name="CORS_ORIGINS",
        type=list,
        default=[],
        description="CORS allowed origins list (empty = no cross-origin requests)",
    ),
    "WS_ALLOWED_ORIGINS": SettingDefinition(
        name="WS_ALLOWED_ORIGINS",
        type=list,
        default=[],
        description=(
            "Cross-origin WebSocket allowlist (CSWSH defense). Same-origin "
            "connections are always allowed; empty = same-origin only; '*' = any."
        ),
    ),
    "TENANT_STRICT": SettingDefinition(
        name="TENANT_STRICT",
        type=bool,
        default=True,
        description=(
            "Fail closed: a TenantMixin model queried with no active tenant "
            "context (and not .unscoped()) returns zero rows instead of all "
            "tenants' data. Set False only for non-security multi-tenant apps."
        ),
    ),
    # ── Rate Limiting ──
    "RATE_LIMIT_REQUESTS": SettingDefinition(
        name="RATE_LIMIT_REQUESTS",
        type=int,
        default=120,
        min_value=1,
        description="Rate limit: requests per window",
    ),
    "RATE_LIMIT_WINDOW": SettingDefinition(
        name="RATE_LIMIT_WINDOW",
        type=int,
        default=60,
        min_value=1,
        description="Rate limit: window in seconds",
    ),
    # ── Embeddings ──
    "EMBEDDINGS_API_URL": SettingDefinition(
        name="EMBEDDINGS_API_URL",
        type=str,
        default="",
        description="OpenAI-compatible embeddings API endpoint",
    ),
    "EMBEDDINGS_API_KEY": SettingDefinition(
        name="EMBEDDINGS_API_KEY",
        type=str,
        default="",
        description="Embeddings API key",
    ),
    "EMBEDDINGS_MODEL": SettingDefinition(
        name="EMBEDDINGS_MODEL",
        type=str,
        default="text-embedding-3-small",
        description="Embedding model name",
    ),
    "EMBEDDINGS_VECTOR_DIM": SettingDefinition(
        name="EMBEDDINGS_VECTOR_DIM",
        type=int,
        default=1536,
        min_value=1,
        description="Embedding vector dimension",
    ),
    # ── Feature Flags ──
    "LOAD_TEST": SettingDefinition(
        name="LOAD_TEST",
        type=bool,
        default=False,
        description="Disable rate limiting for load tests",
    ),
    # ── Email ──
    "EMAIL_HOST": SettingDefinition(
        name="EMAIL_HOST",
        type=str,
        default="localhost",
        description="SMTP server hostname",
    ),
    "EMAIL_PORT": SettingDefinition(
        name="EMAIL_PORT",
        type=int,
        default=25,
        min_value=1,
        max_value=65535,
        description="SMTP server port",
    ),
    "EMAIL_HOST_USER": SettingDefinition(
        name="EMAIL_HOST_USER",
        type=str,
        default="",
        description="SMTP authentication username",
    ),
    "EMAIL_HOST_PASSWORD": SettingDefinition(
        name="EMAIL_HOST_PASSWORD",
        type=str,
        default="",
        description="SMTP authentication password",
    ),
    "EMAIL_USE_TLS": SettingDefinition(
        name="EMAIL_USE_TLS",
        type=bool,
        default=False,
        description="Use STARTTLS for SMTP",
    ),
    "EMAIL_USE_SSL": SettingDefinition(
        name="EMAIL_USE_SSL",
        type=bool,
        default=False,
        description="Use implicit TLS/SSL for SMTP",
    ),
    "EMAIL_BACKEND": SettingDefinition(
        name="EMAIL_BACKEND",
        type=str,
        default="smtp",
        choices=frozenset({"smtp", "console", "memory"}),
        description="Email sending backend",
    ),
    "DEFAULT_FROM_EMAIL": SettingDefinition(
        name="DEFAULT_FROM_EMAIL",
        type=str,
        default="webmaster@localhost",
        description="Default From: email address",
    ),
    "EMAIL_SUBJECT_PREFIX": SettingDefinition(
        name="EMAIL_SUBJECT_PREFIX",
        type=str,
        default="[HyperDjango] ",
        description="Subject prefix for outgoing emails",
    ),
    "EMAIL_TIMEOUT": SettingDefinition(
        name="EMAIL_TIMEOUT",
        type=int,
        default=30,
        min_value=1,
        max_value=300,
        description="SMTP connection timeout in seconds",
    ),
    "EMAIL_SSL_CERTFILE": SettingDefinition(
        name="EMAIL_SSL_CERTFILE",
        type=str,
        default="",
        description="Path to SSL certificate file for SMTP",
    ),
    "EMAIL_SSL_KEYFILE": SettingDefinition(
        name="EMAIL_SSL_KEYFILE",
        type=str,
        default="",
        description="Path to SSL key file for SMTP",
    ),
    "SERVER_EMAIL": SettingDefinition(
        name="SERVER_EMAIL",
        type=str,
        default="root@localhost",
        description="Email address for error notification messages",
    ),
    # ── Static Files ──
    "STATIC_URL": SettingDefinition(
        name="STATIC_URL",
        type=str,
        default="/static/",
        description="URL prefix for static files",
    ),
    "STATIC_ROOT": SettingDefinition(
        name="STATIC_ROOT",
        type=str,
        default="",
        description="Filesystem path for collected static files",
    ),
    "STATIC_MAX_AGE": SettingDefinition(
        name="STATIC_MAX_AGE",
        type=int,
        default=3600,
        min_value=0,
        description="Cache-Control max-age for static files",
    ),
    "STATICFILES_DIRS": SettingDefinition(
        name="STATICFILES_DIRS",
        type=list,
        default=[],
        description="Additional directories for static file finders",
    ),
    # ── Media ──
    "MEDIA_URL": SettingDefinition(
        name="MEDIA_URL",
        type=str,
        default="/media/",
        description="URL prefix for user-uploaded media files",
    ),
    "MEDIA_ROOT": SettingDefinition(
        name="MEDIA_ROOT",
        type=str,
        default="",
        description="Filesystem path for user-uploaded files",
    ),
    # ── Upload ──
    "MAX_UPLOAD_SIZE": SettingDefinition(
        name="MAX_UPLOAD_SIZE",
        type=int,
        default=10 * 1024 * 1024,
        min_value=0,
        description="Maximum upload size in bytes",
    ),
    "ALLOWED_UPLOAD_EXTENSIONS": SettingDefinition(
        name="ALLOWED_UPLOAD_EXTENSIONS",
        type=list,
        default=[],
        description="Allowed file extensions for uploads (empty = all)",
    ),
    "DATA_UPLOAD_MAX_NUMBER_FIELDS": SettingDefinition(
        name="DATA_UPLOAD_MAX_NUMBER_FIELDS",
        type=int,
        default=1000,
        min_value=1,
        description="Maximum number of form fields per request",
    ),
    "DATA_UPLOAD_MAX_NUMBER_FILES": SettingDefinition(
        name="DATA_UPLOAD_MAX_NUMBER_FILES",
        type=int,
        default=100,
        min_value=1,
        description="Maximum number of files per upload request",
    ),
    "FILE_UPLOAD_TEMP_DIR": SettingDefinition(
        name="FILE_UPLOAD_TEMP_DIR",
        type=str,
        default="",
        description="Temporary directory for file uploads (empty = system temp)",
    ),
    "FILE_UPLOAD_PERMISSIONS": SettingDefinition(
        name="FILE_UPLOAD_PERMISSIONS",
        type=int,
        default=0o644,
        min_value=0,
        description="File permissions for uploaded files (octal)",
    ),
    "FILE_UPLOAD_DIRECTORY_PERMISSIONS": SettingDefinition(
        name="FILE_UPLOAD_DIRECTORY_PERMISSIONS",
        type=int,
        default=0o755,
        min_value=0,
        description="Directory permissions for upload directories (octal)",
    ),
    "FILE_UPLOAD_MAX_MEMORY_SIZE": SettingDefinition(
        name="FILE_UPLOAD_MAX_MEMORY_SIZE",
        type=int,
        default=2621440,
        min_value=0,
        description="Per-file size threshold (bytes): files smaller stay in memory, larger spill to disk.",
    ),
    "FILE_UPLOAD_MAX_SIZE": SettingDefinition(
        name="FILE_UPLOAD_MAX_SIZE",
        type=int,
        default=0,
        min_value=0,
        description="Maximum size per uploaded file (bytes). 0 = unlimited.",
    ),
    "STREAM_BODY_CHUNK_SIZE": SettingDefinition(
        name="STREAM_BODY_CHUNK_SIZE",
        type=int,
        default=262144,
        min_value=4096,
        max_value=16777216,
        description="Chunk size (bytes) for request.stream() and UploadedFile.chunks().",
    ),
    # ── Server ──
    "HTTP_SERVER": SettingDefinition(
        name="HTTP_SERVER",
        type=str,
        default="auto",
        choices=frozenset({"auto", "zig", "asgi"}),
        description="HTTP server backend",
    ),
    "HTTP_SERVER_MODEL": SettingDefinition(
        name="HTTP_SERVER_MODEL",
        type=str,
        default="reactor",
        choices=frozenset({"threaded", "reactor"}),
        description=(
            "Native HTTP connection model. 'reactor' (default, SAFE): idle "
            "keep-alive connections wait in a kqueue/epoll reactor, so the server "
            "holds thousands of concurrent connections (bounded by fds/memory, not "
            "the thread pool) and degrades gracefully under load — the right "
            "default for public/general web serving. 'threaded': one worker thread "
            "per connection (max live ≈ THREAD_POOL_SIZE) — ~10% higher peak "
            "throughput and lower latency, but STARVES connections past the pool "
            "size, so only use it when connection count is known-bounded (internal "
            "high-RPS APIs behind a connection-pooling proxy)."
        ),
    ),
    "HTTP_MAX_PENDING": SettingDefinition(
        name="HTTP_MAX_PENDING",
        type=int,
        default=0,
        min_value=0,
        description=(
            "Threaded-mode load-shedding cap. Once the accept backlog exceeds this "
            "many pending connections (all workers pinned), new connections get a "
            "fast 503 Service Unavailable instead of being accepted and starved — "
            "so threaded mode degrades gracefully at its margin. 0 = framework "
            "default (THREAD_POOL_SIZE × 8); a positive int overrides. Ignored in "
            "reactor mode. To fully disable shedding set HYPER_HTTP_MAX_PENDING=0 "
            "in the environment."
        ),
    ),
    "THREAD_POOL_SIZE": SettingDefinition(
        name="THREAD_POOL_SIZE",
        type=int,
        default=24,
        min_value=1,
        max_value=128,
        description="Server thread pool size",
    ),
    "WEBSOCKET_CONCURRENCY": SettingDefinition(
        name="WEBSOCKET_CONCURRENCY",
        type=str,
        default="shared",
        choices=frozenset({"shared", "thread"}),
        description=(
            "WebSocket concurrency model. 'shared' (default): multiplex "
            "connections over a small event-loop pool — no thread-pool "
            "connection ceiling, multi-core throughput, flat memory. "
            "'thread': one OS thread per connection (max = THREAD_POOL_SIZE)."
        ),
    ),
    "WEBSOCKET_LOOP_COUNT": SettingDefinition(
        name="WEBSOCKET_LOOP_COUNT",
        type=int,
        default=0,
        min_value=0,
        max_value=128,
        description="Event loops in the shared WebSocket pool (0 = auto, min(cpu, 8)).",
    ),
    "LISTEN_BACKLOG": SettingDefinition(
        name="LISTEN_BACKLOG",
        type=int,
        default=4096,
        min_value=0,
        description=(
            "Native listen() backlog (accept queue depth). The kernel silently "
            "clamps this to somaxconn. Needs headroom ABOVE the largest burst "
            "of simultaneous connects: the queue must hold the whole burst "
            "while the acceptor drains it, and an overflowed connection is "
            "dropped with no error at either end."
        ),
    ),
    "SEND_TIMEOUT_MS": SettingDefinition(
        name="SEND_TIMEOUT_MS",
        type=int,
        default=30000,
        min_value=0,
        description=(
            "Slow-client socket send timeout in ms. 0 = unbounded (a stuck "
            "client can pin a worker's send indefinitely)."
        ),
    ),
    "MAX_BODY_SIZE": SettingDefinition(
        name="MAX_BODY_SIZE",
        type=int,
        default=10 * 1024 * 1024,
        min_value=0,
        description="Maximum request body size in bytes",
    ),
    # ── Logging ──
    "LOG_LEVEL": SettingDefinition(
        name="LOG_LEVEL",
        type=str,
        default="INFO",
        choices=frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}),
        description="Logging level",
    ),
    "LOG_FORMAT": SettingDefinition(
        name="LOG_FORMAT",
        type=str,
        default="text",
        choices=frozenset({"text", "json"}),
        description="Log output format",
    ),
    # ── Proxy ──
    "USE_X_FORWARDED_HOST": SettingDefinition(
        name="USE_X_FORWARDED_HOST",
        type=bool,
        default=False,
        description="Use X-Forwarded-Host header for request.get_host()",
    ),
    "USE_X_FORWARDED_PORT": SettingDefinition(
        name="USE_X_FORWARDED_PORT",
        type=bool,
        default=False,
        description="Use X-Forwarded-Port header for request port",
    ),
    "TRUSTED_PROXY_COUNT": SettingDefinition(
        name="TRUSTED_PROXY_COUNT",
        type=int,
        default=0,
        min_value=0,
        description=(
            "Number of trusted reverse-proxy hops in front of the app. "
            "0 (default) means X-Forwarded-For / X-Real-IP are NOT trusted and "
            "request.client_ip comes from the socket peer — preventing IP "
            "spoofing (and the rate-limit-key spoofing / bucket-OOM it enables). "
            "Set to the number of proxies you actually run to honor XFF."
        ),
    ),
    "TRUSTED_PROXIES": SettingDefinition(
        name="TRUSTED_PROXIES",
        type=list,
        default=[],
        description=(
            "List of trusted proxy IP addresses. When the socket peer is one of "
            "these, X-Forwarded-For / X-Real-IP are honored regardless of "
            "TRUSTED_PROXY_COUNT. Empty by default."
        ),
    ),
    "MTLS_PROXY_SECRET": SettingDefinition(
        name="MTLS_PROXY_SECRET",
        type=str,
        default="",
        description=(
            "Shared attestation secret for the external-proxy mTLS topology. "
            "When a TLS-terminating proxy in front of the app verifies client "
            "certificates, it must send this value in X-Hyper-MTLS-Attest "
            "alongside the verified-identity headers; hyperdjango.mtls honors "
            "those headers only when the attestation matches (constant-time). "
            "Empty (default) disables the external-proxy path entirely — the "
            "in-process MTLSTerminator uses a per-process random secret and "
            "does not need this."
        ),
    ),
    # ── URL ──
    "APPEND_SLASH": SettingDefinition(
        name="APPEND_SLASH",
        type=bool,
        default=True,
        description="Append trailing slash to URLs that lack one",
    ),
    "PREPEND_WWW": SettingDefinition(
        name="PREPEND_WWW",
        type=bool,
        default=False,
        description="Prepend www. to URLs that lack it",
    ),
    # ── Messages ──
    "MESSAGE_LEVEL": SettingDefinition(
        name="MESSAGE_LEVEL",
        type=int,
        default=20,
        min_value=0,
        description="Minimum message level to display (default 20 = INFO)",
    ),
    "MESSAGE_TAGS": SettingDefinition(
        name="MESSAGE_TAGS",
        type=dict,
        default={},
        description="Custom message level to CSS class tag mapping",
    ),
    # ── Other ──
    "ADMINS": SettingDefinition(
        name="ADMINS",
        type=list,
        default=[],
        description="List of (name, email) tuples for error notification recipients",
    ),
    "MANAGERS": SettingDefinition(
        name="MANAGERS",
        type=list,
        default=[],
        description="List of (name, email) tuples for broken link notifications",
    ),
    "DISALLOWED_USER_AGENTS": SettingDefinition(
        name="DISALLOWED_USER_AGENTS",
        type=list,
        default=[],
        description="List of user-agent regex strings to block",
    ),
    # ── Internationalization ──
    "LANGUAGE_CODE": SettingDefinition(
        name="LANGUAGE_CODE",
        type=str,
        default="en",
        description="Default language code (BCP 47)",
    ),
    "TIME_ZONE": SettingDefinition(
        name="TIME_ZONE",
        type=str,
        default="UTC",
        validator=_validate_time_zone,
        description="Default time zone (must resolve via zoneinfo)",
    ),
    "USE_TZ": SettingDefinition(
        name="USE_TZ",
        type=bool,
        default=True,
        description="Store datetimes as UTC in the database",
    ),
    "DATE_FORMAT": SettingDefinition(
        name="DATE_FORMAT",
        type=str,
        default="N j, Y",
        description="Default date display format",
    ),
    "DATETIME_FORMAT": SettingDefinition(
        name="DATETIME_FORMAT",
        type=str,
        default="N j, Y, P",
        description="Default datetime display format",
    ),
    "TIME_FORMAT": SettingDefinition(
        name="TIME_FORMAT",
        type=str,
        default="P",
        description="Default time display format",
    ),
    "SHORT_DATE_FORMAT": SettingDefinition(
        name="SHORT_DATE_FORMAT",
        type=str,
        default="m/d/Y",
        description="Short date display format",
    ),
    "SHORT_DATETIME_FORMAT": SettingDefinition(
        name="SHORT_DATETIME_FORMAT",
        type=str,
        default="m/d/Y P",
        description="Short datetime display format",
    ),
    "DECIMAL_SEPARATOR": SettingDefinition(
        name="DECIMAL_SEPARATOR",
        type=str,
        default=".",
        description="Decimal separator character",
    ),
    "THOUSAND_SEPARATOR": SettingDefinition(
        name="THOUSAND_SEPARATOR",
        type=str,
        default=",",
        description="Thousands grouping separator character",
    ),
    "USE_THOUSAND_SEPARATOR": SettingDefinition(
        name="USE_THOUSAND_SEPARATOR",
        type=bool,
        default=False,
        description="Enable thousands separator in number formatting",
    ),
    "NUMBER_GROUPING": SettingDefinition(
        name="NUMBER_GROUPING",
        type=int,
        default=3,
        min_value=0,
        max_value=10,
        description="Number of digits per group for thousands separator",
    ),
    "FIRST_DAY_OF_WEEK": SettingDefinition(
        name="FIRST_DAY_OF_WEEK",
        type=int,
        default=0,
        min_value=0,
        max_value=6,
        description="First day of the week (0=Sunday, 1=Monday)",
    ),
    "DATE_INPUT_FORMATS": SettingDefinition(
        name="DATE_INPUT_FORMATS",
        type=list,
        default=["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"],
        description="Accepted date input formats (strptime)",
    ),
    "DATETIME_INPUT_FORMATS": SettingDefinition(
        name="DATETIME_INPUT_FORMATS",
        type=list,
        default=["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m/%d/%Y %H:%M:%S"],
        description="Accepted datetime input formats (strptime)",
    ),
    # ── Features ──
    "FILE_ROUTING": SettingDefinition(
        name="FILE_ROUTING",
        type=bool,
        default=False,
        description="Enable file-based routing",
    ),
    "FILE_ROUTING_DIR": SettingDefinition(
        name="FILE_ROUTING_DIR",
        type=str,
        default="views",
        description="Directory for file-based route discovery",
    ),
    "STATIC_CACHE": SettingDefinition(
        name="STATIC_CACHE",
        type=bool,
        default=False,
        description="Enable in-memory static file cache",
    ),
    "HOT_RELOAD": SettingDefinition(
        name="HOT_RELOAD",
        type=bool,
        default=False,
        description="Enable hot reload on file changes",
    ),
    "PRE_VALIDATION": SettingDefinition(
        name="PRE_VALIDATION",
        type=bool,
        default=False,
        description="Enable pre-GIL request validation",
    ),
    "VALIDATION_BACKEND": SettingDefinition(
        name="VALIDATION_BACKEND",
        type=str,
        default="dhi",
        choices=frozenset({"dhi", "django"}),
        description="Validation engine backend",
    ),
    # ── Telemetry (v0.15.0+) ──
    "TELEMETRY_ENABLED": SettingDefinition(
        name="TELEMETRY_ENABLED",
        type=bool,
        default=False,
        description="Master switch for metrics + span recording. Zero cost when False.",
    ),
    "TELEMETRY_SERVICE_NAME": SettingDefinition(
        name="TELEMETRY_SERVICE_NAME",
        type=str,
        default="hyperdjango",
        description="Service name attached as the default tracer identity",
    ),
    "TELEMETRY_SAMPLE_RATIO": SettingDefinition(
        name="TELEMETRY_SAMPLE_RATIO",
        type=float,
        default=0.01,
        min_value=0.0,
        max_value=1.0,
        description="Head sampling ratio — 0.0 = off, 1.0 = record every root span",
    ),
    "TELEMETRY_DRAIN_INTERVAL": SettingDefinition(
        name="TELEMETRY_DRAIN_INTERVAL",
        type=float,
        default=1.0,
        min_value=0.01,
        max_value=300.0,
        description="Seconds between background span/metric drain ticks",
    ),
    "TELEMETRY_EXTRACT_TRACEPARENT": SettingDefinition(
        name="TELEMETRY_EXTRACT_TRACEPARENT",
        type=bool,
        default=True,
        description="Parse incoming W3C `traceparent` header as the parent context",
    ),
    "TELEMETRY_SINKS": SettingDefinition(
        name="TELEMETRY_SINKS",
        type=list,
        default=["prometheus"],
        description="List of built-in sink names to attach: prometheus|stdout|memory",
    ),
    "TELEMETRY_SPAN_RING_CAPACITY": SettingDefinition(
        name="TELEMETRY_SPAN_RING_CAPACITY",
        type=int,
        default=16384,
        min_value=256,
        max_value=16777216,
        description=(
            "Number of slots in the native span ring buffer. MUST be a "
            "power of 2. Default 16384 = 4 MB. Larger = more burst "
            "headroom; smaller = lower memory footprint."
        ),
    ),
    "TELEMETRY_AUTO_LOG_CORRELATION": SettingDefinition(
        name="TELEMETRY_AUTO_LOG_CORRELATION",
        type=bool,
        default=True,
        description=(
            "When telemetry is enabled, automatically inject trace_id, "
            "span_id, and trace_flags into every log record's extra dict "
            "via a logger patcher. The JSON sink auto-promotes those "
            "fields to top level so log aggregators join logs to traces "
            "with zero extra mapping. "
            "Set False to opt out — useful for hot-loop logging where "
            "the contextvar lookup is unwanted overhead, or when wiring "
            "a custom patcher that handles correlation differently."
        ),
    ),
    # ── Versioning ──
    "APP_VERSION": SettingDefinition(
        name="APP_VERSION",
        type=str,
        default="",
        description=(
            "Explicit app version string (git SHA, semver, build number). "
            "When non-empty, takes priority over auto-computed versions "
            "from the static manifest or registered components."
        ),
    ),
    "APP_VERSION_HEADER": SettingDefinition(
        name="APP_VERSION_HEADER",
        type=bool,
        default=True,
        description="Emit X-App-Version header on all HTTP responses.",
    ),
    "APP_VERSION_MISMATCH": SettingDefinition(
        name="APP_VERSION_MISMATCH",
        type=str,
        default="prompt",
        choices=frozenset({"prompt", "reload", "warn", "ignore"}),
        description=(
            "Operator policy advertised as X-App-Version-Action when a "
            "client's page version differs from the serving version: "
            "'prompt' (default) shows a dismissible 'new version available' "
            "banner with a Reload button — user-initiated, so no in-flight "
            "state is destroyed; 'reload' reloads at the next navigation "
            "boundary (never mid-interaction) and degrades to 'prompt' if "
            "one reload did not clear the skew; 'warn' logs to console; "
            "'ignore' injects no mismatch script and, as a response header, "
            "tells already-instrumented older clients to stand down."
        ),
    ),
    "APP_VERSION_CLIENT_BROADCAST": SettingDefinition(
        name="APP_VERSION_CLIENT_BROADCAST",
        type=bool,
        default=True,
        description=(
            "Broadcast each page's own version back to the server so a load "
            "balancer can pin a client to a matching backend through a "
            "rolling deploy. Gates the whole feature: the injected script's "
            "X-Client-Version header + hyper_client_version cookie, the "
            "middleware's inbound parse onto request.client_version, and the "
            "hyperdjango_version_skew_requests_total metric."
        ),
    ),
    "APP_BUILD_COMMIT": SettingDefinition(
        name="APP_BUILD_COMMIT",
        type=str,
        default="",
        description=(
            "Git commit the running build was released from, surfaced in "
            "/version metadata. Kept OUT of the version string itself (release "
            "stamps stay clean digit runs; package indexes reject local "
            "version suffixes). `hyper release stamp` prints the value to "
            "wire into the deploy environment."
        ),
    ),
    "VERSION_ENDPOINT": SettingDefinition(
        name="VERSION_ENDPOINT",
        type=bool,
        default=True,
        description="Mount /version endpoint when mount_version() is called.",
    ),
    "STATIC_DEV_VERSION_QUERY": SettingDefinition(
        name="STATIC_DEV_VERSION_QUERY",
        type=bool,
        default=True,
        description=(
            "In dev mode (no collectstatic), append ?v=<content_hash> to "
            "static URLs generated by the static_url() template helper."
        ),
    ),
    # ── Static Files Tuning ──
    "STATICFILES_GZIP_MIN_SIZE": SettingDefinition(
        name="STATICFILES_GZIP_MIN_SIZE",
        type=int,
        default=1024,
        min_value=0,
        description="Minimum file size in bytes before gzip compression kicks in.",
    ),
    "STATICFILES_HASH_LENGTH": SettingDefinition(
        name="STATICFILES_HASH_LENGTH",
        type=int,
        default=12,
        min_value=4,
        max_value=32,
        description="Hex chars in content-hash filenames (e.g., styles.a1b2c3d4e5f6.css).",
    ),
    "STATICFILES_MAX_POST_PROCESS_PASSES": SettingDefinition(
        name="STATICFILES_MAX_POST_PROCESS_PASSES",
        type=int,
        default=5,
        min_value=1,
        max_value=20,
        description="Maximum CSS url() rewrite iterations during collectstatic.",
    ),
    "STATICFILES_DEV_HASH_CACHE_MAX": SettingDefinition(
        name="STATICFILES_DEV_HASH_CACHE_MAX",
        type=int,
        default=4096,
        min_value=64,
        description="Max entries in dev-mode static file hash cache.",
    ),
    # ── Task Queue Tuning ──
    "TASK_MAX_COMPLETED_RESULTS": SettingDefinition(
        name="TASK_MAX_COMPLETED_RESULTS",
        type=int,
        default=10000,
        min_value=100,
        description="Maximum completed task results retained before eviction.",
    ),
    "TASK_CLEANUP_INTERVAL": SettingDefinition(
        name="TASK_CLEANUP_INTERVAL",
        type=int,
        default=100,
        min_value=1,
        description="Check result eviction every N task completions.",
    ),
    "TASK_SHUTDOWN_TIMEOUT": SettingDefinition(
        name="TASK_SHUTDOWN_TIMEOUT",
        type=int,
        default=5,
        min_value=1,
        max_value=60,
        description="Seconds to wait for task workers to finish on shutdown.",
    ),
    # ── Performance / Diagnostics ──
    "PERFORMANCE_HISTORY_SIZE": SettingDefinition(
        name="PERFORMANCE_HISTORY_SIZE",
        type=int,
        default=1000,
        min_value=10,
        description="Request history ring buffer size for PerformanceMiddleware.",
    ),
    "PERFORMANCE_N_PLUS_ONE_THRESHOLD": SettingDefinition(
        name="PERFORMANCE_N_PLUS_ONE_THRESHOLD",
        type=int,
        default=5,
        min_value=2,
        description="Repeated query count that triggers N+1 detection warning.",
    ),
    "SLOW_QUERY_SQL_LENGTH": SettingDefinition(
        name="SLOW_QUERY_SQL_LENGTH",
        type=int,
        default=2000,
        min_value=100,
        description="SQL text truncation length in slow query log.",
    ),
    "SLOW_QUERY_PARAMS_LENGTH": SettingDefinition(
        name="SLOW_QUERY_PARAMS_LENGTH",
        type=int,
        default=500,
        min_value=50,
        description="Parameter text truncation length in slow query log.",
    ),
    "SLOW_QUERY_RETENTION_DAYS": SettingDefinition(
        name="SLOW_QUERY_RETENTION_DAYS",
        type=int,
        default=7,
        min_value=1,
        description="Days to retain slow query log entries before cleanup.",
    ),
    # ── Rate Limiting Tuning ──
    "RATELIMIT_CLEANUP_RETENTION": SettingDefinition(
        name="RATELIMIT_CLEANUP_RETENTION",
        type=int,
        default=3600,
        min_value=60,
        description="Seconds to retain rate limit entries before cleanup.",
    ),
    "RATELIMIT_MAX_BUCKETS": SettingDefinition(
        name="RATELIMIT_MAX_BUCKETS",
        type=int,
        default=100000,
        min_value=1,
        description=(
            "Hard cap on the number of in-memory rate-limit buckets. When a "
            "shard is full the oldest (least-recently-refilled) bucket is "
            "evicted, bounding memory even under a flood of distinct keys."
        ),
    ),
    "RATELIMIT_IETF_HEADERS": SettingDefinition(
        name="RATELIMIT_IETF_HEADERS",
        type=bool,
        default=True,
        description="Emit IETF RateLimit + RateLimit-Policy headers (draft-ietf-httpapi-ratelimit-headers-10).",
    ),
    "RATELIMIT_LEGACY_HEADERS": SettingDefinition(
        name="RATELIMIT_LEGACY_HEADERS",
        type=bool,
        default=True,
        description="Emit legacy x-ratelimit-* headers alongside IETF headers.",
    ),
    "RATELIMIT_PROBLEM_DETAILS": SettingDefinition(
        name="RATELIMIT_PROBLEM_DETAILS",
        type=bool,
        default=True,
        description="Use RFC 9457 Problem Details JSON format for 429 responses.",
    ),
    # ── Hot Reload ──
    "HOT_RELOAD_POLL_INTERVAL": SettingDefinition(
        name="HOT_RELOAD_POLL_INTERVAL",
        type=float,
        default=0.3,
        min_value=0.05,
        description="Seconds between file polls in fallback mode (no native watcher).",
    ),
    "HOT_RELOAD_SSE_HEARTBEAT": SettingDefinition(
        name="HOT_RELOAD_SSE_HEARTBEAT",
        type=int,
        default=30,
        min_value=5,
        description="Seconds between SSE keepalive pings for hot reload clients.",
    ),
    # ── Tasks ──
    "TASK_WORKERS": SettingDefinition(
        name="TASK_WORKERS",
        type=int,
        default=4,
        min_value=1,
        max_value=64,
        description="Number of background task worker threads",
    ),
    "TASK_MAX_QUEUE_SIZE": SettingDefinition(
        name="TASK_MAX_QUEUE_SIZE",
        type=int,
        default=10000,
        min_value=1,
        max_value=1000000,
        description="Maximum number of pending tasks in the queue",
    ),
    "TASK_DLQ_MAX_SIZE": SettingDefinition(
        name="TASK_DLQ_MAX_SIZE",
        type=int,
        default=10000,
        min_value=1,
        max_value=1000000,
        description="Maximum number of dead letter queue entries",
    ),
    "TASK_MAX_PENDING_PER_USER": SettingDefinition(
        name="TASK_MAX_PENDING_PER_USER",
        type=int,
        default=0,
        min_value=0,
        description="Max pending tasks per user (0 = unlimited)",
    ),
    "TASK_CIRCUIT_FAILURE_THRESHOLD": SettingDefinition(
        name="TASK_CIRCUIT_FAILURE_THRESHOLD",
        type=int,
        default=5,
        min_value=1,
        description="Consecutive failures before a task's circuit breaker opens",
    ),
    "TASK_CIRCUIT_RECOVERY_TIMEOUT": SettingDefinition(
        name="TASK_CIRCUIT_RECOVERY_TIMEOUT",
        type=float,
        default=30.0,
        min_value=0.0,
        description="Seconds an open circuit waits before probing recovery",
    ),
    "TASK_CIRCUIT_WINDOW": SettingDefinition(
        name="TASK_CIRCUIT_WINDOW",
        type=float,
        default=300.0,
        min_value=0.0,
        description="Rolling window (seconds) for circuit-breaker failure counting",
    ),
    # ── i18n Paths ──
    "LOCALE_PATHS": SettingDefinition(
        name="LOCALE_PATHS",
        type=list,
        default=[],
        description="Directories to scan for .po translation files",
    ),
}


# ── Settings validation ──────────────────────────────────────────────────────


def validate_settings(settings: dict[str, object] | None = None) -> list[str]:
    """Validate settings against SETTING_DEFINITIONS.

    If *settings* is None, uses get_all_settings() to gather current values.
    Returns a list of error strings (empty list = all valid).
    """
    if settings is None:
        settings = get_all_settings()

    errors: list[str] = []

    for name, defn in SETTING_DEFINITIONS.items():
        value = settings.get(name, defn.default)

        # ── Required check (SECRET_KEY must be non-empty in production) ──
        if defn.required:
            debug = settings.get("DEBUG", False)
            if not value:
                if debug:
                    value = secrets.token_hex(32)
                    settings[name] = value
                    _logger.warning(
                        "%s not set — using auto-generated key (development only)",
                        name,
                    )
                else:
                    errors.append(f"{name}: required setting is not set")
                    continue

        # ── Skip validation on empty non-required strings ──
        if value == "" and defn.type is str and not defn.required:
            continue

        # ── Type check ──
        if not isinstance(value, defn.type):
            errors.append(
                f"{name}: expected type {defn.type.__name__}, "
                f"got {type(value).__name__} ({value!r})"
            )
            continue

        # ── Range checks (int/float only) ──
        if defn.min_value is not None and isinstance(value, (int, float)):
            if value < defn.min_value:
                errors.append(
                    f"{name}: value {value} is below minimum {defn.min_value}"
                )

        if defn.max_value is not None and isinstance(value, (int, float)):
            if value > defn.max_value:
                errors.append(
                    f"{name}: value {value} is above maximum {defn.max_value}"
                )

        # ── Choice check (str only) ──
        if defn.choices is not None and isinstance(value, str):
            if value not in defn.choices:
                errors.append(
                    f"{name}: value {value!r} is not one of {sorted(defn.choices)}"
                )

        # ── Custom validator hook (raises ValueError on invalid value) ──
        if defn.validator is not None:
            try:
                defn.validator(value)
            except ValueError as exc:
                errors.append(str(exc))

    return errors


# ── Environment loading ──────────────────────────────────────────────────────


def _coerce_value(raw: str, target_type: type) -> object:
    """Coerce a raw string from environment to the target type."""
    if target_type is bool:
        return parse_bool(raw)
    if target_type is int:
        return int(raw)
    if target_type is float:
        return float(raw)
    if target_type is list:
        # Comma-separated values, strip whitespace
        if not raw.strip():
            return []
        return [item.strip() for item in raw.split(",")]
    # str — return as-is
    return raw


def _parse_env_file(path: pathlib.Path) -> dict[str, str]:
    """Parse a simple .env file (KEY=VALUE per line, # comments, blank lines)."""
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip optional surrounding quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            result[key] = value
    return result


def load_env_settings(
    env: dict[str, str] | None = None,
    dotenv_path: pathlib.Path | None = None,
) -> dict[str, object]:
    """Load settings from HYPER_* environment variables and optional .env file.

    Reads HYPER_<NAME> from environment and maps to <NAME> in settings.
    Also reads a .env file from project root (if it exists) as a lower-priority
    source. Environment variables override .env values.

    Returns a dict of setting name -> coerced value (only settings that were
    found in the environment / .env file).
    """
    if env is None:
        env = dict(os.environ)

    # ── Load .env file (lower priority) ──
    file_vars: dict[str, str] = {}
    if dotenv_path is not None:
        file_vars = _parse_env_file(dotenv_path)
    else:
        # Auto-detect .env in current working directory
        default_env = pathlib.Path.cwd() / ".env"
        file_vars = _parse_env_file(default_env)

    result: dict[str, object] = {}

    for name, defn in SETTING_DEFINITIONS.items():
        env_key = f"HYPER_{name}"

        # Environment variable takes priority over .env file
        raw: str | None = env.get(env_key)
        if raw is None:
            # Check .env file (with HYPER_ prefix first, then bare name)
            raw = file_vars.get(env_key)
            if raw is None:
                raw = file_vars.get(name)
        if raw is None:
            continue

        # Coerce per-key: a single malformed value (e.g. HYPER_POOL_SIZE=abc,
        # which int() rejects) must NOT abort the whole load and silently discard
        # every other override — including security secrets that would then fall
        # back to an insecure auto-default. Log only the offending KEY and its
        # expected type; NEVER the value or the exception message (int()/float()
        # ValueErrors echo the bad literal, which may be a secret). Keep loading.
        try:
            result[name] = _coerce_value(raw, defn.type)
        except ValueError, TypeError:
            _logger.error(
                "Ignoring malformed env setting %s: could not coerce to %s",
                env_key,
                defn.type.__name__,
            )

    return result


# ── Settings access ──────────────────────────────────────────────────────────


# ── Settings resolution cache ────────────────────────────────────────────
#
# get_setting() is on the hot path — called ~6 times per request to look up
# values like CACHE_KEY_PREFIX, CACHE_VERSION, APPEND_SLASH, etc. In the v0.14.4
# hypernews profile it showed up at 396ms self-time / 324K calls on the cached
# `/` endpoint alone. The old implementation did a Python-level
# `from django.conf import settings` inside the function body on EVERY call
# plus an f-string format plus a getattr against Django's LazySettings proxy.
#
# Strategy: split the two sources. Django overrides are cached in
# _DJANGO_OVERRIDES (populated lazily on first call, auto-invalidated via
# the ``setting_changed`` signal). DEFAULTS is read fresh on every call
# because dict.get is ~40 ns — not a hot path cost, and because test code
# uses ``unittest.mock.patch.dict(DEFAULTS, ...)`` to override defaults
# locally and expects those overrides to take effect immediately.

_SETTING_MISSING = object()  # sentinel: name not found in any source

# Cached Django settings proxy. Resolved lazily on first call. None means
# "not yet probed"; False means "Django not available (standalone mode)".
_django_settings_ref: object = None


class _UnpopulatedOverrides(dict):
    """Sentinel marking the Django-override cache as not-yet-scanned.

    An empty ``dict`` whose *type* signals "unpopulated". ``get_setting``
    performs a SINGLE atomic read of the ``_DJANGO_OVERRIDES`` global and
    decides purely from the value it read: if it is one of these sentinels,
    (re)populate; otherwise use it directly. Encoding the populated-state in
    the reference itself (instead of a separate boolean flag) removes the
    read-flag-then-read-dict TOCTOU that let a concurrent invalidation slip
    an emptied dict in between — the root of the "wrong default returned"
    race under free-threading. Being a real (empty) dict also preserves the
    ``in`` / ``len`` / ``.get`` introspection some scripts perform,
    and keeping it distinct from a genuinely-empty populated dict avoids
    re-scanning Django on every call when a project sets zero overrides.
    """

    # Subclasses the builtin ``dict`` (a C type) so the sentinel is itself a
    # real empty dict.
    # slots-required: a @dataclass cannot model a ``dict`` subclass.
    __slots__ = ()


# Cache of names that Django settings override (resolved values only).
# Populated lazily on first ``get_setting`` call. Auto-invalidated via the
# ``setting_changed`` signal wired in ``_subscribe_to_setting_changed``.
# Published only via whole-object reference swap — never mutated in place.
_DJANGO_OVERRIDES: dict[str, object] = _UnpopulatedOverrides()

# Cache of env-var overrides loaded from HYPER_* environment variables and
# the .env file. Populated lazily on first ``get_setting`` call. Persists
# for the process lifetime — env vars don't change at runtime.
_ENV_OVERRIDES: dict[str, object] = {}
_ENV_OVERRIDES_POPULATED = False


def _resolve_django_settings() -> object:
    """Return the Django settings LazySettings proxy, or False if unavailable.

    "Unavailable" covers both the case where Django isn't installed AND
    the case where Django is importable but settings aren't configured
    (no DJANGO_SETTINGS_MODULE, no settings.configure() call). Cached
    after first probe so we don't pay the import + configured check on
    every get_setting() call.

    When Django IS configured, also subscribes to the ``setting_changed``
    signal so runtime mutations (e.g. from ``@override_settings`` in tests,
    or direct assignment) auto-invalidate the get_setting() cache.
    """
    global _django_settings_ref
    # Resolve into a LOCAL and return the LOCAL — never re-read the global at
    # the return. A concurrent clear_settings_cache() sets the global back to
    # None; if we returned the global here it could read as None mid-call, and
    # _populate_django_overrides() would then treat None as a live settings
    # object (None is not False), getattr its way to an empty override dict,
    # and publish it — every reader then sees the DEFAULTS fallback instead of
    # the configured value. Resolving into `ref` closes that window: the
    # returned value is always the proxy or False, never None.
    ref = _django_settings_ref
    if ref is None:
        try:
            from django.conf import settings as django_settings

            # LazySettings.configured is a property that returns True if
            # settings have been loaded. False means access would raise
            # ImproperlyConfigured — treat the whole proxy as unavailable.
            if django_settings.configured:
                ref = django_settings
                _django_settings_ref = ref
                _subscribe_to_setting_changed()
            else:
                ref = False
                _django_settings_ref = False
        # blind-except: probing an unconfigured/half-initialized Django settings proxy can raise ImproperlyConfigured (or import errors); treat settings as unavailable and fall back to built-in defaults rather than crashing every get_setting() call.
        except Exception:
            ref = False
            _django_settings_ref = False
    return ref


# Guard to avoid double-registering the setting_changed receiver.
_setting_changed_subscribed = False


def _subscribe_to_setting_changed() -> None:
    """Auto-invalidate ``_DJANGO_OVERRIDES`` when Django settings change.

    Django fires ``django.test.signals.setting_changed`` whenever a setting
    is set via ``@override_settings`` or ``settings.configure()`` or any
    direct assignment through ``LazySettings.__setattr__``. Hook into it
    so tests that mutate HYPERDJANGO_* settings at runtime see the new
    values on the next get_setting() call without needing to remember
    to call clear_settings_cache() manually.
    """
    global _setting_changed_subscribed
    if _setting_changed_subscribed:
        return
    try:
        from django.test.signals import setting_changed

        def _on_setting_changed(sender, setting, value, enter, **kwargs):
            # Only invalidate for HYPERDJANGO_* settings — ignore unrelated ones.
            if isinstance(setting, str) and setting.startswith("HYPERDJANGO_"):
                # Invalidate the whole Django-override snapshot: next
                # get_setting() re-probes Django and rebuilds. Lower the
                # POPULATED flag FIRST, then swap in a fresh empty dict so a
                # concurrent reader never sees a half-cleared dict (which
                # would race pop -> KeyError under free-threading). Single
                # atomic reference swap; never mutate the live dict in place.
                global _DJANGO_OVERRIDES
                _DJANGO_OVERRIDES = _UnpopulatedOverrides()
                # Let derived caches (e.g. cache.py namespace memo) refresh.
                _notify_settings_changed()

        # Hold a strong reference to prevent GC (signal uses weakrefs by default).
        global _setting_changed_receiver
        _setting_changed_receiver = _on_setting_changed
        setting_changed.connect(_setting_changed_receiver, weak=False)
        _setting_changed_subscribed = True
    # blind-except: setting_changed auto-invalidation is a test-time convenience; django.test.signals may be unavailable in production, in which case we simply skip subscribing (callers can still clear_settings_cache() manually).
    except Exception:
        pass


# Strong ref to the signal receiver, so it isn't garbage collected.
_setting_changed_receiver = None


# Observers notified whenever the settings cache is invalidated. Lets downstream
# modules (e.g. cache.py's namespace memo) drop derived caches without polling
# get_setting() on every hot-path call.
_settings_changed_observers: list[Callable[[], None]] = []


def register_settings_changed_hook(fn: Callable[[], None]) -> None:
    """Register a callback fired when the settings cache is invalidated."""
    _settings_changed_observers.append(fn)


def _notify_settings_changed() -> None:
    for fn in _settings_changed_observers:
        with contextlib.suppress(Exception):
            fn()


def clear_settings_cache() -> None:
    """Invalidate the get_setting() caches (Django + env) and proxy ref.

    Call this from tests after mutating Django settings or env vars at
    runtime so subsequent get_setting() calls re-probe the sources of
    truth. Typically not needed for Django settings because the
    ``setting_changed`` signal auto-invalidates them.
    """
    global _django_settings_ref, _DJANGO_OVERRIDES
    global _ENV_OVERRIDES_POPULATED
    # Single atomic reference swap back to the unpopulated sentinel — the next
    # get_setting() rebuilds. Readers do one atomic read of _DJANGO_OVERRIDES,
    # so they see either the old fully-populated snapshot or this sentinel,
    # never a half-cleared dict.
    _DJANGO_OVERRIDES = _UnpopulatedOverrides()
    _ENV_OVERRIDES_POPULATED = False
    _ENV_OVERRIDES.clear()
    _django_settings_ref = None
    _notify_settings_changed()


def _populate_env_overrides() -> None:
    """Populate ``_ENV_OVERRIDES`` from HYPER_* env vars + .env file.

    Called lazily from ``get_setting`` on first use, like the Django
    overrides path. Env vars are static at runtime so this only runs
    once per process.
    """
    global _ENV_OVERRIDES_POPULATED
    with contextlib.suppress(Exception):
        _ENV_OVERRIDES.update(load_env_settings())
    _ENV_OVERRIDES_POPULATED = True


def _populate_django_overrides() -> dict[str, object]:
    """Populate ``_DJANGO_OVERRIDES`` by scanning Django settings once.

    Iterates every key in DEFAULTS and probes Django settings for the
    corresponding HYPERDJANGO_* attribute. Any attribute that Django
    defines (overrides a default or provides a new value) is cached.
    Called lazily from ``get_setting`` on first use.

    Thread-safety: builds a FRESH dict and publishes it with a single atomic
    reference swap — the live dict readers hold is never mutated in place.
    Returns the freshly published dict so the caller holds a consistent
    snapshot even if a concurrent invalidation resets the cache right after.
    """
    global _DJANGO_OVERRIDES
    django_settings = _resolve_django_settings()
    if django_settings is False:
        fresh: dict[str, object] = {}
        _DJANGO_OVERRIDES = fresh
        return fresh
    fresh = {}
    _sentinel = object()
    for name in DEFAULTS:
        full_name = "HYPERDJANGO_" + name
        try:
            # NOTE: getattr required here because django_settings is a LazySettings
            # proxy object — attributes are loaded dynamically from the settings
            # module and cannot be accessed via direct attribute syntax
            # dynamic-attr: django_settings is a Django LazySettings proxy; full_name is a runtime-composed setting name resolved dynamically from the user's settings module
            val = getattr(django_settings, full_name, _sentinel)  # noqa: B009 — Django LazySettings proxy requires getattr
        # blind-except: resolving one HYPERDJANGO_* setting off the LazySettings proxy is best-effort; a lazy-import/config error for that key leaves it at the sentinel so the built-in default applies, without aborting the whole settings snapshot.
        except Exception:
            val = _sentinel
        if val is not _sentinel:
            fresh[name] = val
    # Publish the fully-built dict with a single atomic reference swap. A reader
    # that observes a non-sentinel _DJANGO_OVERRIDES is therefore guaranteed to
    # read this complete dict — never one being filled in place. Return the fresh
    # dict so the caller has a consistent snapshot regardless of any concurrent
    # invalidation.
    _DJANGO_OVERRIDES = fresh
    return fresh


# ── Database URL resolution (single authority) ────────────────────────────────


def _assemble_pg_url(env: dict[str, str]) -> str:
    """Assemble a connection URL from the standard libpq ``PG*`` variables.

    Returns ``""`` unless ``PGDATABASE`` (the one part with no sane default) is
    present. Host / port / user fall back to the libpq conventions
    (``localhost`` / ``5432`` / OS user); the password is included only when set.
    """
    database = (env.get("PGDATABASE") or "").strip()
    if not database:
        return ""
    host = (env.get("PGHOST") or "").strip() or "localhost"
    port = (env.get("PGPORT") or "").strip() or "5432"
    user = (
        env.get("PGUSER") or env.get("USER") or env.get("USERNAME") or ""
    ).strip() or "postgres"
    password = env.get("PGPASSWORD") or ""
    auth = f"{user}:{password}@" if password else f"{user}@"
    return f"postgresql://{auth}{host}:{port}/{database}"


def fill_url_auth(url: str) -> str:
    """Fill any username / password / host / port a DB URL leaves out.

    This is the SINGLE place the framework reads the libpq ``PG*`` (and the
    ``USER``/``USERNAME`` OS-user fallbacks) to complete a bare URL such as
    ``postgres://host/db`` or ``postgres:///db``. The connection layer delegates
    here rather than reading ``PG*`` itself, so conf.py is the sole DB-env
    boundary. Empty input passes through unchanged (nothing to complete); a URL
    that already carries both user and host is returned as-is. The database name
    is never invented — a URL naming no database stays invalid and is rejected
    at connection time (``database._ensure_url_user``).
    """
    if not url:
        return url
    parsed = urlparse(url)
    if parsed.username and parsed.hostname:
        return url
    env = os.environ
    user = parsed.username or (
        env.get("PGUSER") or env.get("USER") or env.get("USERNAME") or "postgres"
    )
    password = parsed.password or env.get("PGPASSWORD", "")
    host = parsed.hostname or env.get("PGHOST") or "localhost"
    auth = f"{user}:{password}@" if password else f"{user}@"
    netloc = f"{auth}{host}"
    port = parsed.port or (env.get("PGPORT") or None)
    if port:
        netloc += f":{port}"
    return urlunparse(parsed._replace(netloc=netloc))


def resolve_database_url() -> str:
    """Resolve the effective PostgreSQL connection URL — the ONE authority.

    Every component that needs "which database" — the server / ``get_db()``, the
    production-config guard, and every ``hyper`` CLI command (setup, seed,
    migrate, makemigrations, shell, dbshell, inspectdb, …) — resolves through
    this function, either directly or via ``get_setting("DATABASE_URL")`` which
    delegates here. So a user sets ONE variable in ANY accepted convention and
    the whole framework agrees on the same database.

    Precedence (first non-empty wins):
      a. **Explicit override** — a Django ``HYPERDJANGO_DATABASE_URL`` setting or
         the ``HyperApp(database=...)`` constructor bridge (which writes
         ``DEFAULTS["DATABASE_URL"]``). An explicit URL beats every env var.
      b. ``HYPER_DATABASE_URL`` env var / ``.env`` entry (framework convention).
      c. ``DATABASE_URL`` env var / ``.env`` entry (12-factor / libpq URI).
      d. Assembled from the libpq ``PG*`` set when ``PGDATABASE`` is present
         (``postgresql://PGUSER:PGPASSWORD@PGHOST:PGPORT/PGDATABASE``).
      e. ``""`` — not configured; callers raise their own clear error.

    The connection layer (:func:`hyperdjango.database._ensure_url_user`) then
    fills any user / password / host / port the resolved URL still omits via
    :func:`fill_url_auth` — the SINGLE place the framework reads the libpq
    ``PG*`` / OS defaults — so a bare ``postgres://host/db`` also works and no
    other module needs to touch ``PG*``.
    """
    # (a) Explicit override: Django setting, then the constructor bridge.
    django_overrides = _DJANGO_OVERRIDES
    if isinstance(django_overrides, _UnpopulatedOverrides):
        django_overrides = _populate_django_overrides()
    django_val = django_overrides.get("DATABASE_URL")
    if isinstance(django_val, str) and django_val:
        return django_val
    defaults_val = DEFAULTS.get("DATABASE_URL")
    if isinstance(defaults_val, str) and defaults_val:
        return defaults_val
    # (b) / (c) env var, then .env file — the env var wins per key.
    file_vars = _parse_env_file(pathlib.Path.cwd() / ".env")
    for key in ("HYPER_DATABASE_URL", "DATABASE_URL"):
        val = os.environ.get(key) or file_vars.get(key)
        if val:
            return val
    # (d) Assemble from the libpq PG* set; (e) "" when nothing is configured.
    return _assemble_pg_url(os.environ)


def get_all_settings() -> dict[str, object]:
    """Return all current settings with defaults applied.

    Merges: DEFAULTS <- env/dotenv <- Django settings (highest priority).
    """
    # Start with defaults
    result: dict[str, object] = dict(DEFAULTS)

    # Layer on environment settings
    env_settings = load_env_settings()
    result.update(env_settings)

    # Layer on Django settings (highest priority)
    django_settings = _resolve_django_settings()
    if django_settings is not False:
        _sentinel = object()
        for name in DEFAULTS:
            full_name = "HYPERDJANGO_" + name
            # NOTE: getattr required here because django_settings is a LazySettings
            # proxy object — attributes are loaded dynamically from the settings
            # module and cannot be accessed via direct attribute syntax
            # dynamic-attr: django_settings is a Django LazySettings proxy; full_name is a runtime-composed setting name resolved dynamically from the user's settings module
            val = getattr(django_settings, full_name, _sentinel)  # noqa: B009 — Django LazySettings proxy requires getattr
            if val is not _sentinel:
                result[name] = val

    # DATABASE_URL is resolved by the single authority (also honors bare
    # DATABASE_URL / the PG* set, which the layered scan above does not) so the
    # reported value matches exactly what get_db()/the CLI will connect to.
    result["DATABASE_URL"] = resolve_database_url()

    return result


# Security-sensitive settings whose DEFAULT is a fresh random value per process.
# When one of these resolves from DEFAULTS (no env/Django override), warn once:
# every restart would otherwise invalidate the sessions, CSRF tokens and signed
# values minted under the previous one. Public because deployment tooling needs
# the same list — `hyper service install` mints and PERSISTS every one of these
# so a systemd-restarted service keeps signing identically.
AUTO_RANDOM_SECRET_SETTINGS = frozenset(
    {
        "CSRF_SECRET",
        "SESSION_SECRET",
        "SESSION_SIGNING_KEY",
        "ADMIN_SECRET",
        "API_KEY",
        "SECRET_KEY",
    }
)
_warned_auto_secrets: set[str] = set()


def get_setting(name: str, default: object = None) -> object:
    """Get a HyperDjango setting with fallback to default.

    Resolution order (highest precedence first):
      1. Django settings (HYPERDJANGO_<NAME>)
      2. Environment variables (HYPER_<NAME>) and .env file
      3. DEFAULTS dict
      4. Caller-provided ``default``

    Works even when Django is not configured (standalone mode).

    Hot path:
      - Django overrides are cached in ``_DJANGO_OVERRIDES`` (populated
        once on first call; auto-invalidated via the ``setting_changed``
        signal when tests mutate settings).
      - Env-var overrides are cached in ``_ENV_OVERRIDES`` (populated
        once; env vars don't change at runtime).
      - DEFAULTS is read fresh on every call because tests frequently
        use ``unittest.mock.patch.dict(DEFAULTS, ...)`` to inject local
        overrides, and ``dict.get`` is ~40 ns — not a hot-path cost.

    ``DATABASE_URL`` is special-cased to the single connection-URL authority
    :func:`resolve_database_url`, so every reader of the setting (server,
    ``get_db``, the prod-config guard) honors the full precedence — the
    constructor bridge / Django setting, then ``HYPER_DATABASE_URL``, then bare
    ``DATABASE_URL``, then the libpq ``PG*`` set — from one source of truth.
    """
    if name == "DATABASE_URL":
        return resolve_database_url()
    # Django overrides: ONE atomic read of the published snapshot, then a
    # single .get. The populated-state is encoded in the reference itself
    # (an _UnpopulatedOverrides sentinel means "not yet scanned"), so a
    # single global read decides everything — no read-flag-then-read-dict
    # window for a concurrent invalidation to slip an emptied dict through,
    # and never `name in dict` then `dict[name]` (that TOCTOU races the
    # invalidators and raises KeyError under free-threading).
    # _populate_django_overrides() returns the fresh dict it publishes, so
    # this caller always ends up holding a complete, consistent snapshot.
    django_overrides = _DJANGO_OVERRIDES
    if isinstance(django_overrides, _UnpopulatedOverrides):
        django_overrides = _populate_django_overrides()
    val = django_overrides.get(name, _SETTING_MISSING)
    if val is not _SETTING_MISSING:
        return val
    if not _ENV_OVERRIDES_POPULATED:
        _populate_env_overrides()
    env_val = _ENV_OVERRIDES.get(name, _SETTING_MISSING)
    if env_val is not _SETTING_MISSING:
        return env_val
    # One dict lookup, not two (`name in DEFAULTS` then `DEFAULTS[name]`).
    # DEFAULTS only ever holds real config values, never the private sentinel,
    # so a sentinel result unambiguously means "not present".
    val = DEFAULTS.get(name, _SETTING_MISSING)
    if val is not _SETTING_MISSING:
        # Warn once per process when a security secret falls through to the
        # auto-generated default (no env var or Django override provided).
        if name in AUTO_RANDOM_SECRET_SETTINGS and name not in _warned_auto_secrets:
            _warned_auto_secrets.add(name)
            if val:
                _logger.warning(
                    "Using auto-generated %s (random per session, won't persist across restarts). "
                    "Set HYPER_%s env var or HYPERDJANGO_%s in Django settings for production.",
                    name,
                    name,
                    name,
                )
        return val
    return default


class SettingNotConfigured(RuntimeError):
    """A setting that must be explicitly configured was left at its default.

    Raised by :func:`require_setting` for security-critical settings that must
    never run on an auto-generated or empty value.
    """


def is_explicitly_set(name: str) -> bool:
    """Return True if ``name`` was provided by the environment or Django.

    "Explicitly set" means a ``HYPER_<name>`` environment variable, a ``.env``
    entry, or a ``HYPERDJANGO_<name>`` Django setting supplied the value — i.e.
    resolution did NOT fall through to the built-in ``DEFAULTS`` (which, for a
    security secret, is a random per-process value). Use it to tell a
    configured value apart from an auto-generated one.

    Example::

        if not is_explicitly_set("SESSION_SIGNING_KEY"):
            logger.warning("Signing key is ephemeral — set it for production")
    """
    django_overrides = _DJANGO_OVERRIDES
    if isinstance(django_overrides, _UnpopulatedOverrides):
        django_overrides = _populate_django_overrides()
    if name in django_overrides:
        return True
    if not _ENV_OVERRIDES_POPULATED:
        _populate_env_overrides()
    return name in _ENV_OVERRIDES


def require_setting(
    name: str,
    *,
    min_length: int = 0,
    validator: Callable[[object], None] | None = None,
) -> object:
    """Return a setting that MUST be explicitly and validly configured, or fail.

    Resolves ``name`` like :func:`get_setting`, then rejects, by raising
    :class:`SettingNotConfigured`, any value that is not safe to use:

    - **not explicitly provided** (env / ``.env`` / Django) — it would fall
      through to the built-in default (for a security secret, a random
      per-process value);
    - **explicitly set but empty/blank** (``HYPER_X=""``) — being "set" is not
      enough, an empty signing key is still forgeable-trivial;
    - **shorter than ``min_length``** characters, when given — e.g. reject a
      2-character signing key;
    - **rejected by ``validator``** — an optional callable that raises on a
      value failing an app-specific constraint (charset, prefix, entropy…).

    Use it for security-critical settings — signing keys, credentials — that
    must never silently run on an auto-generated, empty, or too-weak value: an
    app that resolves one during import/startup then refuses to start until it
    is configured properly, rather than booting on an insecure default.

    This is the opt-in, per-setting complement to the ``required=True`` flag on
    a :class:`SettingDefinition` (which is only enforced by ``validate_settings``
    / ``hyper check``): ``require_setting`` fails at the point of use, for any
    *registered* setting an app depends on without wiring a separate check.

    The setting MUST be registered — present in ``SETTING_DEFINITIONS`` (and
    ``DEFAULTS``). ``is_explicitly_set`` only sees registered names:
    ``load_env_settings`` iterates ``SETTING_DEFINITIONS`` and the Django scan
    iterates ``DEFAULTS``, so an unregistered app-custom name is invisible to
    both. ``require_setting("MY_APP_SECRET")`` therefore raises even with
    ``HYPER_MY_APP_SECRET`` exported — register it first (add a
    ``SettingDefinition`` / ``DEFAULTS`` entry).

    Example — sign tokens only with an explicitly configured, ≥32-char key::

        from hyperdjango.conf import require_setting
        from hyperdjango.signing import SigningKey

        class TokenConfig:
            keys = [SigningKey(secret=require_setting(
                "SESSION_SIGNING_KEY", min_length=32))]
    """
    if not is_explicitly_set(name):
        raise SettingNotConfigured(
            f"{name} must be explicitly configured (set HYPER_{name}, a .env "
            f"entry, or Django HYPERDJANGO_{name}); it has no safe default. "
            f'Generate one, e.g. python -c "import secrets; '
            f'print(secrets.token_urlsafe(48))".'
        )
    value = get_setting(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise SettingNotConfigured(
            f"{name} is set but empty — provide a real value, not a blank."
        )
    # Measure the trimmed length: surrounding whitespace is not entropy, so
    # "  ab  " must not satisfy min_length=6. Blank-only values are already
    # rejected above.
    if min_length and isinstance(value, str) and len(value.strip()) < min_length:
        raise SettingNotConfigured(
            f"{name} is too short ({len(value.strip())} chars); needs at least "
            f"{min_length}. Generate a stronger value."
        )
    if validator is not None:
        validator(value)  # raises (SettingNotConfigured / ValueError) if invalid
    return value
