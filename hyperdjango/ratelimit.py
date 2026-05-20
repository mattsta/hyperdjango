"""
Pluggable rate limiter with multi-server coordination.

Backends:
- InMemoryBackend: per-process dict (single server, fast)
- DatabaseBackend: PostgreSQL UNLOGGED table (multi-server coordination)

Key strategies for multi-tenant support:
- IP-based (default)
- User-based (authenticated users)
- Org-based (multi-tenant organizations)
- Custom key function
- Tier-based (per-group rate limits from RBAC)

Usage:
    # Single server (in-memory)
    app.use(RateLimitMiddleware(max_requests=100, window=60))

    # Multi-server (PostgreSQL UNLOGGED)
    backend = DatabaseRateLimitBackend(db)
    await backend.ensure_table()
    app.use(RateLimitMiddleware(max_requests=100, window=60, backend=backend))

    # Multi-tenant (per-org limits)
    app.use(RateLimitMiddleware(
        max_requests=1000, window=60,
        key_func=org_key,
        backend=backend,
    ))

    # Hierarchical limits
    app.use(RateLimitMiddleware(max_requests=10, window=1, key_func=ip_key))      # 10/sec per IP
    app.use(RateLimitMiddleware(max_requests=100, window=60, key_func=user_key))  # 100/min per user
    app.use(RateLimitMiddleware(max_requests=5000, window=3600, key_func=org_key))# 5K/hr per org

    # Tiered rate limiting (per-group)
    tiers = {
        "free": {"max_requests": 100, "window": 60},
        "pro": {"max_requests": 1000, "window": 60},
        "enterprise": {"max_requests": 10000, "window": 60},
    }
    app.use(TieredRateLimitMiddleware(tiers=tiers, default_tier="free", db=db))
"""

import base64
import contextlib
import fnmatch
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hyperdjango.conf import (
    DEFAULT_RATE_LIMIT_MAX_REQUESTS,
    DEFAULT_RATE_LIMIT_WINDOW,
    get_setting,
)

if TYPE_CHECKING:
    from hyperdjango.database import Database
from hyperdjango.models import Field as ModelField
from hyperdjango.models import Model
from hyperdjango.response import Response
from hyperdjango.security import SecurityEvent as _SecurityEvent
from hyperdjango.security import get_security_log as _get_security_log
from hyperdjango.telemetry import metrics as _tel_metrics

_logger = logging.getLogger("hyperdjango.ratelimit")

# Rate-limit denials, labeled by backend kind (a small, bounded label set — the
# per-key detail lives in SecurityLog, not the metric). One process-wide series.
_rate_limit_hits_total = _tel_metrics.CounterVec(
    "hyperdjango_rate_limit_hits_total",
    "Rate-limit denials by middleware backend.",
    label_names=("backend",),
)

# Sentinel for RateLimitMiddleware fields that were NOT explicitly supplied by
# the caller. A plain int default cannot distinguish "caller passed 100" from
# "used the default", which would make an explicit argument indistinguishable
# from the fallback. With this sentinel __post_init__ can honor the precedence
# explicit arg > setting > module constant.
_UNSET: int = object()  # type: ignore[assignment]

# LOAD_TEST rate-limit bypass is announced exactly once per process (a benign
# double-log under free-threading is acceptable — no correctness impact).
_load_test_bypass_logged = False


def _log_load_test_bypass_once() -> None:
    """Log the LOAD_TEST rate-limit bypass exactly once per process."""
    global _load_test_bypass_logged
    if not _load_test_bypass_logged:
        _load_test_bypass_logged = True
        _logger.warning(
            "LOAD_TEST enabled (LOAD_TEST setting / HYPER_LOAD_TEST env) — "
            "rate limiting is BYPASSED; all requests are allowed."
        )


# ─── IETF RateLimit Headers (draft-ietf-httpapi-ratelimit-headers-10) ─────────
#
# Two Structured Fields headers:
#   RateLimit-Policy: "name";q=quota;w=window     (static policy definition)
#   RateLimit: "name";r=remaining;t=reset          (dynamic per-request status)
#
# Plus RFC 9457 Problem Details on 429 responses.


@dataclass(slots=True)
class QuotaPolicy:
    """A quota policy for the IETF RateLimit-Policy header."""

    name: str
    quota: int
    window: int = 0
    quota_unit: str = "requests"
    partition_key: bytes = b""


@dataclass(slots=True)
class ServiceLimit:
    """Current service limit for the IETF RateLimit header."""

    policy_name: str
    remaining: int
    reset: int = 0
    partition_key: bytes = b""


def _sf_format_string(s: str) -> str:
    """Format an sf-string per RFC 9651: quoted, with \\ and \" escaped.

    RFC 9651 sf-string chars are 0x20-0x7E (visible ASCII + space).
    Control characters (CR, LF, tabs, null bytes) are stripped to prevent
    header injection.
    """
    # Strip non-printable / control characters (only allow 0x20-0x7E)
    cleaned = "".join(c for c in s if 0x20 <= ord(c) <= 0x7E)
    return '"' + cleaned.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _sf_format_byte_sequence(b: bytes) -> str:
    """Format an sf-byte-sequence per RFC 9651: :base64:"""
    return ":" + base64.b64encode(b).decode("ascii") + ":"


def format_ratelimit_policy(policies: list[QuotaPolicy]) -> str:
    """Format the RateLimit-Policy header value.

    Example: "burst";q=100;w=60, "daily";q=1000;w=86400
    """
    parts: list[str] = []
    for p in policies:
        item = _sf_format_string(p.name) + f";q={p.quota}"
        if p.window > 0:
            item += f";w={p.window}"
        if p.quota_unit != "requests":
            item += f";qu={_sf_format_string(p.quota_unit)}"
        if p.partition_key:
            item += f";pk={_sf_format_byte_sequence(p.partition_key)}"
        parts.append(item)
    return ", ".join(parts)


def format_ratelimit(limits: list[ServiceLimit]) -> str:
    """Format the RateLimit header value.

    Example: "burst";r=50;t=30
    """
    parts: list[str] = []
    for lim in limits:
        item = _sf_format_string(lim.policy_name) + f";r={lim.remaining}"
        if lim.reset > 0:
            item += f";t={lim.reset}"
        if lim.partition_key:
            item += f";pk={_sf_format_byte_sequence(lim.partition_key)}"
        parts.append(item)
    return ", ".join(parts)


# RFC 9457 Problem Types for rate limiting
_PROBLEM_TYPE_BASE = "https://iana.org/assignments/http-problem-types#"

PROBLEM_QUOTA_EXCEEDED = f"{_PROBLEM_TYPE_BASE}quota-exceeded"
PROBLEM_TEMPORARY_REDUCED = f"{_PROBLEM_TYPE_BASE}temporary-reduced-capacity"
PROBLEM_ABNORMAL_USAGE = f"{_PROBLEM_TYPE_BASE}abnormal-usage-detected"


def build_problem_detail(
    problem_type: str,
    title: str,
    status: int,
    detail: str,
    violated_policies: list[str],
) -> dict[str, str | int | list[str]]:
    """Build an RFC 9457 Problem Details JSON body."""
    return {
        "type": problem_type,
        "title": title,
        "status": status,
        "detail": detail,
        "violated-policies": violated_policies,
    }


def set_ratelimit_headers(
    response: Response,
    policies: list[QuotaPolicy],
    limits: list[ServiceLimit],
    *,
    include_ietf: bool = True,
    include_legacy: bool = True,
    tier_name: str = "",
    rule_name: str = "",
    cost: int = 0,
) -> None:
    """Set rate limit headers on a response (shared by all middlewares)."""
    if include_ietf:
        response.headers["ratelimit-policy"] = format_ratelimit_policy(policies)
        response.headers["ratelimit"] = format_ratelimit(limits)

    if include_legacy:
        if policies:
            response.headers["x-ratelimit-limit"] = str(policies[0].quota)
        if limits:
            response.headers["x-ratelimit-remaining"] = str(limits[0].remaining)
            if limits[0].reset > 0:
                response.headers["x-ratelimit-reset"] = str(limits[0].reset)
        if tier_name:
            response.headers["x-ratelimit-tier"] = tier_name
        if rule_name:
            response.headers["x-ratelimit-rule"] = rule_name
        if cost > 1:
            response.headers["x-ratelimit-cost"] = str(cost)


def build_429_response(
    policies: list[QuotaPolicy],
    limits: list[ServiceLimit],
    reset: int,
    *,
    include_ietf: bool = True,
    include_legacy: bool = True,
    include_problem_details: bool = True,
    tier_name: str = "",
    rule_name: str = "",
    cost: int = 0,
) -> Response:
    """Build a complete 429 response with IETF and/or legacy headers."""
    policy_names = [p.name for p in policies]

    if include_problem_details and include_ietf:
        body: dict[str, str | int | list[str]] = build_problem_detail(
            problem_type=PROBLEM_QUOTA_EXCEEDED,
            title="Rate limit exceeded",
            status=429,
            detail=f"Quota exceeded for polic{'y' if len(policy_names) == 1 else 'ies'} {', '.join(policy_names)}",
            violated_policies=policy_names,
        )
        body["retry_after"] = reset
        resp = Response.json(body, status=429)
        resp.headers["content-type"] = "application/problem+json"
    else:
        resp = Response.error(429, "Rate limit exceeded")

    resp.headers["retry-after"] = str(reset)
    set_ratelimit_headers(
        resp,
        policies,
        limits,
        include_ietf=include_ietf,
        include_legacy=include_legacy,
        tier_name=tier_name,
        rule_name=rule_name,
        cost=cost,
    )
    return resp


# ─── Key Strategies ────────────────────────────────────────────────────────────


def ip_key(request) -> str:
    """Rate limit by client IP address (default)."""
    return f"ip:{request.client_ip}"


def user_key(request) -> str:
    """Rate limit by authenticated user ID."""
    user = request.user
    if user is None:
        return ip_key(request)
    uid = user.id or user.username or "anon"
    return f"user:{uid}"


def org_key(request) -> str:
    """Rate limit by organization (multi-tenant).

    Expects request.user to have org_id, organization_id, or tenant_id.
    Falls back to user_key if no org found.
    """
    user = request.user
    if user is None:
        return ip_key(request)
    org = user.get("org_id") or user.get("organization_id") or user.get("tenant_id")
    if org:
        return f"org:{org}"
    return user_key(request)


def composite_key(*key_funcs: Callable) -> Callable:
    """Combine multiple key strategies into one.

    Example: composite_key(org_key, user_key) → "org:5:user:42"
    """

    def key_func(request) -> str:
        parts = [fn(request) for fn in key_funcs]
        return ":".join(parts)

    return key_func


# ─── In-Memory Backend ────────────────────────────────────────────────────────


class InMemoryRateLimitBackend:
    """In-process windowed-token-bucket rate limiter.

    Fast, single-process only. State lost on restart.

    Algorithm — **windowed token bucket** (O(1) memory + O(1) time per check).
    Each key stores four numbers: the current token level, the last-refill
    timestamp, the current fixed-window index, and the number of units already
    admitted in that window. Two limits are enforced together:

    1. A hard per-window admission cap: no more than ``max_requests`` units are
       admitted within any single fixed window of ``window`` seconds. The count
       resets when the window rolls.
    2. A continuously-refilling token bucket (rate ``max_requests / window``,
       burst ``max_requests``) that smooths bursts *within* a window.

    A request for ``increment`` units is admitted only if BOTH the remaining
    window allowance (``max_requests - admitted_this_window``) AND the available
    tokens are ``>= increment``. This guarantees at most ``max_requests`` units
    per fixed window — closing the loophole where a plain token bucket admits up
    to ~2x the limit (an initially-full bucket *plus* a window's worth of
    continuous refill) — while keeping O(1) memory. It replaces the old limiter
    that stored EVERY request timestamp per key (O(n) memory + an O(n)
    list-slice per check) behind one global lock.

    Semantics vs. the old exact sliding window: identical per-window ceiling
    (never more than ``max_requests`` admissions per fixed window) and, after a
    full idle window, the allowance and bucket both reset so a caller that goes
    quiet for ``window`` seconds regains its full quota (matching the old
    "window expired" behaviour). The bound is per *fixed* window rather than an
    exact trailing sliding window; a burst straddling a boundary can therefore
    admit up to ``max_requests`` in each of the two adjacent windows, the same
    property as any fixed-window limiter.

    Thread-safe and *sharded*: buckets and locks are split across
    ``_NUM_SHARDS`` partitions keyed by ``hash(key)`` so unrelated keys never
    contend on one global lock.
    """

    _NUM_SHARDS = 16

    def __init__(self, max_buckets: int | None = None):
        # Each shard: {key: [tokens, last_refill, window_idx, admitted_in_window]}
        self._shards: list[dict[str, list[float]]] = [
            {} for _ in range(self._NUM_SHARDS)
        ]
        self._locks: list[threading.Lock] = [
            threading.Lock() for _ in range(self._NUM_SHARDS)
        ]
        self._is_async = False
        # Hard cap on total buckets, split evenly across shards. When a shard is
        # full a new key evicts the oldest one (see check_and_increment), so the
        # dict can never grow past ``_max_buckets * _NUM_SHARDS`` (<= configured
        # total) regardless of how many distinct keys arrive between cleanups.
        if max_buckets is None:
            max_buckets = int(get_setting("RATELIMIT_MAX_BUCKETS") or 0)
        self._max_buckets = (
            max(1, max_buckets // self._NUM_SHARDS) if max_buckets > 0 else 0
        )

    def _shard_for(self, key: str) -> tuple[dict[str, list[float]], threading.Lock]:
        idx = hash(key) % self._NUM_SHARDS
        return self._shards[idx], self._locks[idx]

    def check_and_increment(
        self, key: str, max_requests: int, window: int, increment: int = 1
    ) -> tuple[bool, int, int]:
        """Check rate limit and increment counter atomically.

        increment: how many units this request costs (default 1).
        Returns (allowed, remaining, reset_seconds).
        """
        now = time.monotonic()
        w = float(window) if window > 0 else 1.0
        refill_rate = max_requests / w  # tokens replenished per second
        win_idx = now // w  # current fixed-window index

        buckets, lock = self._shard_for(key)
        with lock:
            bucket = buckets.get(key)
            if bucket is None:
                # New key grows the shard. Enforce a hard per-shard cap so a
                # flood of distinct keys (even legitimate, un-spoofable peer IPs)
                # can never OOM the process between cleanup runs: when the shard
                # is full, evict the least-recently-refilled bucket first (LRU).
                max_buckets = self._max_buckets
                if max_buckets > 0 and len(buckets) >= max_buckets:
                    oldest_key = min(buckets, key=lambda k: buckets[k][1])
                    del buckets[oldest_key]
                tokens = float(max_requests)
                last = now
                cur_win = win_idx
                admitted = 0.0
            else:
                tokens, last, cur_win, admitted = bucket
                elapsed = now - last
                if elapsed > 0:
                    tokens = min(float(max_requests), tokens + elapsed * refill_rate)
                    last = now
                # Rolling into a new fixed window clears the admission counter.
                if win_idx != cur_win:
                    cur_win = win_idx
                    admitted = 0.0

            # Remaining admissions allowed in this fixed window.
            window_allowance = max_requests - admitted
            # Must satisfy BOTH the window cap and the token bucket.
            available = min(window_allowance, tokens)

            if available >= increment:
                tokens -= increment
                admitted += increment
                buckets[key] = [tokens, last, cur_win, admitted]
                remaining = int(min(tokens, max_requests - admitted))
                deficit = max_requests - tokens
                reset = math.ceil(deficit / refill_rate) if deficit > 0 else 0
                return True, remaining, reset

            buckets[key] = [tokens, last, cur_win, admitted]
            remaining = int(max(0.0, min(tokens, window_allowance)))
            if window_allowance < increment:
                # Window cap hit — quota only returns when the window rolls.
                reset = max(1, math.ceil((cur_win + 1) * w - now))
            else:
                # Token-limited — wait for enough tokens to accrue.
                reset = max(1, math.ceil((increment - tokens) / refill_rate))
            return False, remaining, reset

    def reset(self, key: str):
        """Reset rate limit for a key."""
        buckets, lock = self._shard_for(key)
        with lock:
            buckets.pop(key, None)

    def cleanup(self):
        """Drop buckets that have sat idle beyond the retention window.

        A bucket refills fully after `window` seconds of inactivity, so once it
        has been idle past the retention horizon it carries no useful state.
        """
        now = time.monotonic()
        retention = int(get_setting("RATELIMIT_CLEANUP_RETENTION"))
        for buckets, lock in zip(self._shards, self._locks):
            with lock:
                stale = [
                    k
                    for k, b in buckets.items()
                    # b = [tokens, last_refill, window_idx, admitted]
                    if now - b[1] > retention
                ]
                for k in stale:
                    del buckets[k]


# ─── Database Backend ─────────────────────────────────────────────────────────

CREATE_RATELIMIT_TABLE_SQL = """
CREATE UNLOGGED TABLE IF NOT EXISTS hyper_rate_limits (
    key TEXT NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (key, window_start)
)
"""

CREATE_RATELIMIT_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_ratelimits_key ON hyper_rate_limits (key)",
)


class DatabaseRateLimitBackend:
    """PostgreSQL UNLOGGED table rate limiter.

    Multi-server coordination via shared PostgreSQL.
    Uses fixed time windows (not sliding) for efficient SQL aggregation.
    UNLOGGED table = no WAL overhead.

    Each (key, window_start) pair has a count. Window start is truncated
    to the window size (e.g., for 60s window, 12:03:00 → 12:03:00).
    """

    # Floor for cleanup retention. Never delete rows younger than this even if
    # no larger window has been observed yet — matches the historical 1h behaviour.
    _CLEANUP_FLOOR_SECONDS = 3600

    def __init__(self, db):
        self.db = db
        self._is_async = True
        # Largest window (seconds) ever passed to check_and_increment. cleanup()
        # must retain at least this long — a hard-coded 1h would silently delete
        # still-counting buckets for any window > 1h (e.g. a daily quota),
        # degrading it to ~1h and letting callers bypass the limit.
        self._max_window_seconds = self._CLEANUP_FLOOR_SECONDS

    async def ensure_table(self):
        """Create the rate limits UNLOGGED table."""
        try:
            await self.db.execute(CREATE_RATELIMIT_TABLE_SQL)
        # UNLOGGED tables aren't supported on every Postgres deployment (some
        # managed/replicated setups); retry as a regular TABLE. That retry is
        # NOT guarded, so a genuine DDL error still propagates from it.
        # blind-except: UNLOGGED-unsupported fallback to a regular TABLE.
        except Exception:
            await self.db.execute(
                CREATE_RATELIMIT_TABLE_SQL.replace("UNLOGGED TABLE", "TABLE")
            )
        for sql in CREATE_RATELIMIT_INDEX_SQL:
            with contextlib.suppress(Exception):
                await self.db.execute(sql)

    async def check_and_increment(
        self, key: str, max_requests: int, window: int, increment: int = 1
    ) -> tuple[bool, int, int]:
        """Check rate limit and increment counter atomically.

        Uses PostgreSQL's INSERT ON CONFLICT for atomic upsert.
        Fixed time windows for efficient aggregation across servers.
        increment: how many units this request costs (default 1).

        Returns (allowed, remaining, reset_seconds).
        """
        # Track the largest window seen so cleanup() never reaps in-window
        # buckets (benign racy max under free-threading — worst case a later
        # write re-raises it).
        w = int(window)
        if w > self._max_window_seconds:
            self._max_window_seconds = w

        # A naive "SELECT SUM … then INSERT" is a check-then-act race: two
        # concurrent requests can both read a count below the limit and both
        # insert, admitting max+N and defeating the distributed backend.
        # READ COMMITTED alone does not fix it — the buckets live in separate
        # per-second rows, so the row-level lock on ON CONFLICT does not
        # serialize requests that land in different seconds of the same
        # window. Serialize per key with a transaction-scoped advisory lock:
        # all requests for one key run one-at-a-time, and the lock is released
        # automatically at COMMIT/ROLLBACK.
        async with self.db.transaction() as db:
            await db.execute("SELECT pg_advisory_xact_lock(hashtext($1))", key)

            count_row = await db.query_one(
                "SELECT COALESCE(SUM(count), 0) AS total FROM hyper_rate_limits "
                "WHERE key = $1 AND window_start > NOW() - $2 * INTERVAL '1 second'",
                key,
                int(window),
            )
            current_count = 0
            if count_row:
                current_count = (
                    count_row["total"] if isinstance(count_row, dict) else count_row[0]
                )
                current_count = int(current_count) if current_count else 0

            if current_count + increment > max_requests:
                return False, max(0, max_requests - current_count), window

            # Atomic upsert: increment or create
            await db.execute(
                "INSERT INTO hyper_rate_limits (key, window_start, count) "
                "VALUES ($1, date_trunc('second', NOW()), $2) "
                "ON CONFLICT (key, window_start) DO UPDATE SET count = hyper_rate_limits.count + $2",
                key,
                increment,
            )

            remaining = max(0, max_requests - current_count - increment)
            return True, remaining, window

    async def reset(self, key: str):
        """Reset rate limit for a key."""
        await self.db.execute("DELETE FROM hyper_rate_limits WHERE key = $1", key)

    async def cleanup(self):
        """Remove expired rate limit entries. Call periodically.

        Deletes only rows older than the LARGEST configured window (not a
        hard-coded hour) so buckets still inside their counting window — e.g. a
        daily quota — are never reaped out from under check_and_increment.
        """
        await self.db.execute(
            "DELETE FROM hyper_rate_limits "
            "WHERE window_start < NOW() - $1 * INTERVAL '1 second'",
            int(self._max_window_seconds),
        )

    async def get_usage(self, key: str, window: int) -> dict[str, int | None]:
        """Get current usage stats for a key."""
        row = await self.db.query_one(
            "SELECT COALESCE(SUM(count), 0) AS total, "
            "MIN(window_start) AS first_request "
            "FROM hyper_rate_limits "
            "WHERE key = $1 AND window_start > NOW() - $2 * INTERVAL '1 second'",
            key,
            int(window),
        )
        if row:
            return {
                "count": int(row["total"]) if row["total"] else 0,
                "first_request": row.get("first_request"),
            }
        return {"count": 0, "first_request": None}


# ─── Middleware ────────────────────────────────────────────────────────────────


@dataclass
class RateLimitMiddleware:
    """Pluggable rate limiter middleware.

    Supports both in-memory (single server) and database (multi-server) backends.
    Supports multi-tenant key strategies (IP, user, org, custom).

    Usage:
        # Simple IP-based (default)
        app.use(RateLimitMiddleware(max_requests=100, window=60))

        # Per-user with DB backend
        backend = DatabaseRateLimitBackend(db)
        app.use(RateLimitMiddleware(
            max_requests=100, window=60,
            key_func=user_key,
            backend=backend,
        ))

        # Hierarchical: stack multiple middlewares
        app.use(RateLimitMiddleware(max_requests=10, window=1))       # 10/sec per IP
        app.use(RateLimitMiddleware(max_requests=1000, window=3600,
                                    key_func=org_key, backend=db_backend))  # 1K/hr per org
    """

    max_requests: int = _UNSET
    window: int = _UNSET
    key_func: Callable | None = None
    backend: InMemoryRateLimitBackend | DatabaseRateLimitBackend | None = None
    policy_name: str = "default"

    def __post_init__(self):
        # Precedence: explicit constructor arg > setting > module constant.
        # A caller that omits max_requests/window inherits the configured
        # RATE_LIMIT_REQUESTS/RATE_LIMIT_WINDOW settings (previously the module
        # constants were used directly, so RATE_LIMIT_REQUESTS was inert).
        if self.max_requests is _UNSET:
            self.max_requests = int(
                get_setting("RATE_LIMIT_REQUESTS", DEFAULT_RATE_LIMIT_MAX_REQUESTS)
            )
        if self.window is _UNSET:
            self.window = int(
                get_setting("RATE_LIMIT_WINDOW", DEFAULT_RATE_LIMIT_WINDOW)
            )
        if self.key_func is None:
            self.key_func = ip_key
        if self.backend is None:
            self.backend = InMemoryRateLimitBackend()
        self._include_ietf = bool(get_setting("RATELIMIT_IETF_HEADERS"))
        self._include_legacy = bool(get_setting("RATELIMIT_LEGACY_HEADERS"))
        self._include_problem_details = bool(get_setting("RATELIMIT_PROBLEM_DETAILS"))
        # LOAD_TEST disables throttling entirely (documented behaviour that was
        # never wired up). Honors the LOAD_TEST setting / HYPER_LOAD_TEST env.
        self._load_test = bool(get_setting("LOAD_TEST"))
        if self._load_test:
            _log_load_test_bypass_once()

        # Precompute the constant header pieces once — name/quota/window/policy
        # are fixed per instance, so the RateLimit-Policy string, the legacy
        # limit value, and the sf-formatted policy name never need re-serializing
        # (char-by-char) on the hot allowed path. Only remaining/reset vary.
        self._policy_header = format_ratelimit_policy(
            [
                QuotaPolicy(
                    name=self.policy_name, quota=self.max_requests, window=self.window
                )
            ]
        )
        self._limit_str = str(self.max_requests)
        self._ratelimit_name_prefix = _sf_format_string(self.policy_name)
        # Metric label: distinguish the in-memory limiter from the DB-backed one.
        self._backend_label = ("database",) if self.backend._is_async else ("memory",)

    def _set_ratelimit_headers(self, response, remaining, reset):
        """Set the allowed-path headers from precomputed static pieces.

        Equivalent to set_ratelimit_headers() for this middleware's case (no
        partition-key / tier / rule / cost) but avoids rebuilding the
        QuotaPolicy/ServiceLimit lists and re-serializing the constant strings
        on every request — only remaining/reset are formatted per call.
        """
        if self._include_ietf:
            response.headers["ratelimit-policy"] = self._policy_header
            if reset > 0:
                response.headers["ratelimit"] = (
                    f"{self._ratelimit_name_prefix};r={remaining};t={reset}"
                )
            else:
                response.headers["ratelimit"] = (
                    f"{self._ratelimit_name_prefix};r={remaining}"
                )
        if self._include_legacy:
            response.headers["x-ratelimit-limit"] = self._limit_str
            response.headers["x-ratelimit-remaining"] = str(remaining)
            if reset > 0:
                response.headers["x-ratelimit-reset"] = str(reset)

    async def __call__(self, request, call_next):
        if self._load_test:
            return await call_next(request)

        key = self.key_func(request)

        if self.backend._is_async:
            allowed, remaining, reset = await self.backend.check_and_increment(
                key, self.max_requests, self.window
            )
        else:
            allowed, remaining, reset = self.backend.check_and_increment(
                key, self.max_requests, self.window
            )

        if not allowed:
            # Bump the metric before the SecurityLog write so a denial is always
            # counted even if SecurityLog is unconfigured or the audit write raises.
            _rate_limit_hits_total.inc_tuple(self._backend_label)
            sec_log = _get_security_log()
            if sec_log is not None:
                try:
                    await sec_log.log_from_request(
                        _SecurityEvent.RATE_LIMIT_HIT,
                        request,
                        detail=f"{self.max_requests}/{self.window}s exceeded key={key}",
                    )
                # blind-except: SecurityLog audit write is best-effort telemetry; a logging-backend failure is warned about but must not break rate limiting.
                except Exception as e:
                    _logger.warning("SecurityLog.log_from_request failed: %s", e)

            # Error path is cold — build the policy/limit objects here only.
            policies = [
                QuotaPolicy(
                    name=self.policy_name, quota=self.max_requests, window=self.window
                )
            ]
            limits = [
                ServiceLimit(policy_name=self.policy_name, remaining=0, reset=reset)
            ]
            return build_429_response(
                policies,
                limits,
                reset,
                include_ietf=self._include_ietf,
                include_legacy=self._include_legacy,
                include_problem_details=self._include_problem_details,
            )

        response = await call_next(request)
        self._set_ratelimit_headers(response, max(0, remaining), reset)
        return response


# ─── Tiered Rate Limiting ─────────────────────────────────────────────────────

# SQL to add rate_limit_tier column to hyper_groups (safe idempotent migration)
ALTER_GROUPS_TIER_SQL = (
    "ALTER TABLE hyper_groups ADD COLUMN IF NOT EXISTS rate_limit_tier TEXT DEFAULT ''"
)


@dataclass
class TieredRateLimitMiddleware:
    """Per-group tiered rate limiter.

    Each group/role can have a rate_limit_tier (e.g., "free", "pro", "enterprise").
    The middleware resolves the user's highest-priority group tier and applies
    the corresponding rate limit. Falls back to default_tier for anonymous
    or users without a tier.

    Tiers are defined as:
        {"tier_name": {"max_requests": int, "window": int}}

    The user's tier is determined by their highest-priority group that has
    a non-empty rate_limit_tier field.

    Usage:
        tiers = {
            "free": {"max_requests": 100, "window": 60},
            "pro": {"max_requests": 1000, "window": 60},
            "enterprise": {"max_requests": 10000, "window": 60},
        }
        app.use(TieredRateLimitMiddleware(tiers=tiers, default_tier="free", db=db))
    """

    tiers: dict[str, dict[str, int]]
    default_tier: str = "free"
    db: Database | None = None
    backend: InMemoryRateLimitBackend | DatabaseRateLimitBackend | None = None
    key_func: Callable | None = None

    def __post_init__(self):
        if self.key_func is None:
            self.key_func = user_key
        if self.backend is None:
            self.backend = InMemoryRateLimitBackend()
        # Cache: user_id → tier_name (per-process, cleared on restart)
        self._tier_cache: dict[int, str] = {}
        self._cache_lock = threading.Lock()
        self._include_ietf = bool(get_setting("RATELIMIT_IETF_HEADERS"))
        self._include_legacy = bool(get_setting("RATELIMIT_LEGACY_HEADERS"))
        self._include_problem_details = bool(get_setting("RATELIMIT_PROBLEM_DETAILS"))
        self._load_test = bool(get_setting("LOAD_TEST"))
        if self._load_test:
            _log_load_test_bypass_once()

    async def ensure_column(self):
        """Add rate_limit_tier column to hyper_groups if it doesn't exist."""
        if self.db is not None:
            with contextlib.suppress(Exception):
                await self.db.execute(ALTER_GROUPS_TIER_SQL)

    async def get_user_tier(self, request) -> str:
        """Resolve the user's rate limit tier from their highest-priority group."""
        user = request.user
        if user is None:
            return self.default_tier

        user_id = user.id
        if user_id is None:
            return self.default_tier

        # Check cache
        with self._cache_lock:
            if user_id in self._tier_cache:
                return self._tier_cache[user_id]

        if self.db is None:
            return self.default_tier

        # Query: get highest-priority group with a tier assigned
        row = await self.db.query_one(
            "SELECT g.rate_limit_tier FROM hyper_groups g "
            "JOIN hyper_user_groups ug ON g.id = ug.group_id "
            "WHERE ug.user_id = $1 AND g.rate_limit_tier != '' "
            "ORDER BY g.priority DESC LIMIT 1",
            user_id,
        )

        tier = self.default_tier
        if row is not None:
            val = row["rate_limit_tier"] if isinstance(row, dict) else row[0]
            if val and val in self.tiers:
                tier = val

        # Cache it
        with self._cache_lock:
            self._tier_cache[user_id] = tier

        return tier

    def clear_tier_cache(self, user_id: int | None = None):
        """Clear the tier cache for a user or all users."""
        with self._cache_lock:
            if user_id is not None:
                self._tier_cache.pop(user_id, None)
            else:
                self._tier_cache.clear()

    async def __call__(self, request, call_next):
        if self._load_test:
            return await call_next(request)

        tier_name = await self.get_user_tier(request)
        tier = self.tiers.get(tier_name, self.tiers.get(self.default_tier, {}))
        max_requests = tier.get("max_requests", 100)
        window = tier.get("window", 60)

        key = self.key_func(request)

        if self.backend._is_async:
            allowed, remaining, reset = await self.backend.check_and_increment(
                key, max_requests, window
            )
        else:
            allowed, remaining, reset = self.backend.check_and_increment(
                key, max_requests, window
            )

        policies = [QuotaPolicy(name=tier_name, quota=max_requests, window=window)]
        limits = [
            ServiceLimit(
                policy_name=tier_name, remaining=max(0, remaining), reset=reset
            )
        ]

        if not allowed:
            limits[0] = ServiceLimit(policy_name=tier_name, remaining=0, reset=reset)
            return build_429_response(
                policies,
                limits,
                reset,
                include_ietf=self._include_ietf,
                include_legacy=self._include_legacy,
                include_problem_details=self._include_problem_details,
                tier_name=tier_name,
            )

        response = await call_next(request)
        set_ratelimit_headers(
            response,
            policies,
            limits,
            include_ietf=self._include_ietf,
            include_legacy=self._include_legacy,
            tier_name=tier_name,
        )
        return response


# ─── Rule-Based Rate Limiting ─────────────────────────────────────────────────

CREATE_RATELIMIT_RULES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS hyper_rate_limit_rules (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    path_pattern TEXT NOT NULL DEFAULT '*',
    method TEXT NOT NULL DEFAULT '*',
    tier TEXT NOT NULL DEFAULT '*',
    max_requests INTEGER NOT NULL DEFAULT 100,
    window_seconds INTEGER NOT NULL DEFAULT 60,
    cost INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
)
"""

CREATE_RATELIMIT_RULES_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_rl_rules_active ON hyper_rate_limit_rules (is_active, priority DESC)",
)


class RateLimitRule(Model):
    """A rate limit rule that matches requests by path, method, and tier.

    Rules are evaluated in priority order (highest first). The first matching
    rule determines the rate limit. If no rule matches, the tier default applies.

    path_pattern: glob pattern (fnmatch). "*" matches all, "/api/reports*" matches prefix.
    method: HTTP method or "*" for all methods.
    tier: rate_limit_tier name or "*" for all tiers.
    cost: how many units each matching request costs against the quota (default 1).
    """

    class Meta:
        table = "hyper_rate_limit_rules"

    id: int = ModelField(primary_key=True, auto=True)
    name: str = ModelField()
    path_pattern: str = ModelField(default="*")
    method: str = ModelField(default="*")
    tier: str = ModelField(default="*")
    max_requests: int = ModelField(default=100)
    window_seconds: int = ModelField(default=60)
    cost: int = ModelField(default=1)
    priority: int = ModelField(default=0)
    is_active: bool = ModelField(default=True)


def match_rule(rules: list[dict], path: str, method: str, tier: str) -> dict | None:
    """Find the first matching rule for a request.

    Rules must be sorted by priority DESC. Returns None if no match.
    Matching: method and tier use exact match or '*' wildcard.
    Path uses fnmatch glob patterns.
    """
    method_upper = method.upper()
    for rule in rules:
        if not rule.get("is_active", True):
            continue
        # Method match
        rule_method = rule.get("method", "*")
        if rule_method != "*" and rule_method.upper() != method_upper:
            continue
        # Tier match
        rule_tier = rule.get("tier", "*")
        if rule_tier != "*" and rule_tier != tier:
            continue
        # Path match (fnmatch glob)
        rule_path = rule.get("path_pattern", "*")
        if rule_path != "*" and not fnmatch.fnmatch(path, rule_path):
            continue
        return rule
    return None


@dataclass(slots=True)
class _CompiledRule:
    """Pre-compiled rule with cached fnmatch pattern for fast matching."""

    rule: dict[str, int | str | bool]
    path_pattern: str
    is_wildcard_path: bool  # True if path_pattern == "*"
    is_prefix_only: bool  # True if pattern ends with "*" and has no other globs
    prefix: str  # For prefix-only patterns, the fixed prefix (without trailing "*")


@dataclass(slots=True)
class CompiledRuleIndex:
    """Pre-indexed rule set for O(1) method+tier lookup + fast path matching.

    Groups rules by (method, tier) combination so we only check rules that
    could possibly match the request's method and tier. Separates wildcard-path
    rules (match everything) from pattern rules (need fnmatch).

    For rules with simple prefix patterns like "/api/*", uses startswith() instead
    of fnmatch — significantly faster for the common case.
    """

    # Indexed by (method, tier) → list of compiled rules, sorted by priority DESC.
    # Keys use "*" for wildcard method/tier.
    _index: dict[tuple[str, str], list[_CompiledRule]]
    _rule_count: int

    @staticmethod
    def build(rules: list[dict[str, int | str | bool]]) -> CompiledRuleIndex:
        """Compile a list of rule dicts into an indexed lookup structure."""
        index: dict[tuple[str, str], list[_CompiledRule]] = {}

        for rule in rules:
            if not rule.get("is_active", True):
                continue

            method_key = (
                rule.get("method", "*").upper()
                if rule.get("method", "*") != "*"
                else "*"
            )
            tier_key = rule.get("tier", "*")
            path_pattern = rule.get("path_pattern", "*")

            # Detect simple prefix patterns (e.g., "/api/*", "/admin/*")
            is_wildcard = path_pattern == "*"
            is_prefix_only = (
                not is_wildcard
                and path_pattern.endswith("*")
                and "*" not in path_pattern[:-1]
                and "?" not in path_pattern
                and "[" not in path_pattern
            )
            prefix = path_pattern[:-1] if is_prefix_only else ""

            compiled = _CompiledRule(
                rule=rule,
                path_pattern=path_pattern,
                is_wildcard_path=is_wildcard,
                is_prefix_only=is_prefix_only,
                prefix=prefix,
            )

            key = (method_key, tier_key)
            if key not in index:
                index[key] = []
            index[key].append(compiled)

        # Sort each bucket by priority DESC so match() can merge-scan efficiently
        for bucket in index.values():
            bucket.sort(key=lambda c: c.rule.get("priority", 0), reverse=True)

        return CompiledRuleIndex(_index=index, _rule_count=len(rules))

    def match(
        self, path: str, method: str, tier: str
    ) -> dict[str, int | str | bool] | None:
        """Find the first matching rule using the compiled index.

        Iterates buckets in specificity order without allocating a merged list.
        Each bucket is pre-sorted by priority DESC at build time, so we check
        the highest-priority rules first across all applicable buckets.
        """
        method_upper = method.upper()

        # Check buckets in specificity order, merging by priority without allocation.
        # Each bucket is already sorted by priority DESC from build().
        keys = (
            (method_upper, tier),
            (method_upper, "*"),
            ("*", tier),
            ("*", "*"),
        )
        buckets = []
        for key in keys:
            bucket = self._index.get(key)
            if bucket is not None:
                buckets.append(bucket)

        if not buckets:
            return None

        # Single bucket fast path — no merge needed
        if len(buckets) == 1:
            for compiled in buckets[0]:
                if self._path_matches(compiled, path):
                    return compiled.rule
            return None

        # Multi-bucket: merge-scan by priority (each bucket pre-sorted DESC)
        # Use index pointers to walk all buckets simultaneously
        indices = [0] * len(buckets)
        while True:
            best_idx = -1
            best_priority = -1
            for i, bucket in enumerate(buckets):
                if indices[i] < len(bucket):
                    p = bucket[indices[i]].rule.get("priority", 0)
                    if p > best_priority:
                        best_priority = p
                        best_idx = i
            if best_idx == -1:
                break
            compiled = buckets[best_idx][indices[best_idx]]
            indices[best_idx] += 1
            if self._path_matches(compiled, path):
                return compiled.rule

        return None

    @staticmethod
    def _path_matches(compiled: _CompiledRule, path: str) -> bool:
        """Check if a compiled rule's path pattern matches the given path."""
        if compiled.is_wildcard_path:
            return True
        if compiled.is_prefix_only:
            return path.startswith(compiled.prefix)
        return fnmatch.fnmatch(path, compiled.path_pattern)

    @property
    def rule_count(self) -> int:
        return self._rule_count


@dataclass
class RuleBasedRateLimitMiddleware:
    """Multi-dimensional rate limiter with per-path, per-method, per-tier rules.

    Resolves the user's tier from RBAC groups, then matches the request against
    stored rules to find the appropriate limit. Supports cost-based rate limiting
    where expensive endpoints consume more quota units per request.

    Rules are stored in hyper_rate_limit_rules and cached in-process with TTL-based
    refresh. If no rule matches, falls back to the tier's default limits.

    Usage:
        tiers = {
            "free": {"max_requests": 100, "window": 60},
            "pro": {"max_requests": 1000, "window": 60},
            "enterprise": {"max_requests": 10000, "window": 60},
        }
        mw = RuleBasedRateLimitMiddleware(tiers=tiers, default_tier="free", db=db)
        await mw.ensure_tables()

        # Add rules via SQL or admin UI:
        # INSERT INTO hyper_rate_limit_rules (name, path_pattern, method, tier, max_requests, window, cost, priority)
        # VALUES ('expensive-reports-free', '/api/reports*', 'GET', 'free', 20, 60, 5, 100);
        # VALUES ('write-api-free', '/api/*', 'POST', 'free', 50, 60, 1, 50);

        app.use(mw)
    """

    tiers: dict[str, dict[str, int]]
    default_tier: str = "free"
    db: Database | None = None
    backend: InMemoryRateLimitBackend | DatabaseRateLimitBackend | None = None
    key_func: Callable | None = None
    rules_cache_ttl: int = 60  # seconds between rule reloads

    def __post_init__(self):
        if self.key_func is None:
            self.key_func = user_key
        if self.backend is None:
            self.backend = InMemoryRateLimitBackend()
        self._tier_cache: dict[int, str] = {}
        self._cache_lock = threading.Lock()
        # Rules + compiled index + load time are held as ONE tuple reference so
        # a single atomic attribute swap publishes them together. Readers snapshot
        # the reference once and can never observe a torn (rules, index) pair.
        self._rules_state: (
            tuple[list[dict[str, int | str | bool]], CompiledRuleIndex | None, float]
            | None
        ) = None
        # In-flight guard (protected by _cache_lock) so a TTL expiry does not let
        # N concurrent requests each fire the DB reload (thundering herd).
        self._rules_loading: bool = False
        self._include_ietf = bool(get_setting("RATELIMIT_IETF_HEADERS"))
        self._include_legacy = bool(get_setting("RATELIMIT_LEGACY_HEADERS"))
        self._include_problem_details = bool(get_setting("RATELIMIT_PROBLEM_DETAILS"))
        self._load_test = bool(get_setting("LOAD_TEST"))
        if self._load_test:
            _log_load_test_bypass_once()

    async def ensure_tables(self):
        """Create rate limit rules table and ensure groups have tier column."""
        if self.db is not None:
            with contextlib.suppress(Exception):
                await self.db.execute(CREATE_RATELIMIT_RULES_TABLE_SQL)
            for sql in CREATE_RATELIMIT_RULES_INDEX_SQL:
                with contextlib.suppress(Exception):
                    await self.db.execute(sql)
            with contextlib.suppress(Exception):
                await self.db.execute(ALTER_GROUPS_TIER_SQL)

    async def get_user_tier(self, request) -> str:
        """Resolve the user's rate limit tier from their highest-priority group."""
        user = request.user
        if user is None:
            return self.default_tier

        user_id = user.id
        if user_id is None:
            return self.default_tier

        with self._cache_lock:
            if user_id in self._tier_cache:
                return self._tier_cache[user_id]

        if self.db is None:
            return self.default_tier

        row = await self.db.query_one(
            "SELECT g.rate_limit_tier FROM hyper_groups g "
            "JOIN hyper_user_groups ug ON g.id = ug.group_id "
            "WHERE ug.user_id = $1 AND g.rate_limit_tier != '' "
            "ORDER BY g.priority DESC LIMIT 1",
            user_id,
        )

        tier = self.default_tier
        if row is not None:
            val = row["rate_limit_tier"] if isinstance(row, dict) else row[0]
            if val and val in self.tiers:
                tier = val

        with self._cache_lock:
            self._tier_cache[user_id] = tier

        return tier

    def clear_tier_cache(self, user_id: int | None = None):
        """Clear the tier cache for a user or all users."""
        with self._cache_lock:
            if user_id is not None:
                self._tier_cache.pop(user_id, None)
            else:
                self._tier_cache.clear()

    def clear_rules_cache(self):
        """Force rules to reload on next request."""
        # Single atomic swap — no torn intermediate state to observe.
        self._rules_state = None

    async def _ensure_rules_loaded(self):
        """Load/refresh rules from DB with TTL-based caching."""
        now = time.monotonic()
        state = self._rules_state
        if state is not None and (now - state[2]) < self.rules_cache_ttl:
            return

        if self.db is None:
            # Publish the empty state as ONE reference (index built for []).
            self._rules_state = ([], CompiledRuleIndex.build([]), now)
            return

        # Thundering-herd guard. We must not hold the threading.Lock across the
        # DB `await` (that would block the event-loop thread and can deadlock a
        # sibling coroutine on the same loop), so the lock only brackets the
        # fast check-and-claim. If another coroutine is already reloading, serve
        # the current (possibly stale/None) state instead of piling on the DB.
        with self._cache_lock:
            state = self._rules_state
            if (
                state is not None
                and (time.monotonic() - state[2]) < self.rules_cache_ttl
            ):
                return
            if self._rules_loading:
                return
            self._rules_loading = True

        try:
            rows = await self.db.query(
                "SELECT id, name, path_pattern, method, tier, max_requests, window_seconds, cost, priority, is_active "
                "FROM hyper_rate_limit_rules WHERE is_active = TRUE ORDER BY priority DESC, id ASC"
            )
            cols = [
                "id",
                "name",
                "path_pattern",
                "method",
                "tier",
                "max_requests",
                "window_seconds",
                "cost",
                "priority",
                "is_active",
            ]
            result = []
            for r in rows:
                d = dict(zip(cols, r)) if not isinstance(r, dict) else r
                result.append(d)
            # Build the index BEFORE publishing, then swap the whole
            # (rules, index, time) triple in one atomic assignment so readers
            # never see the new rules paired with a stale/None index.
            index = CompiledRuleIndex.build(result)
            self._rules_state = (result, index, time.monotonic())
        finally:
            with self._cache_lock:
                self._rules_loading = False

    async def __call__(self, request, call_next):
        if self._load_test:
            return await call_next(request)

        # Resolve user tier
        tier_name = await self.get_user_tier(request)

        # Load rules
        await self._ensure_rules_loaded()

        # Find matching rule using compiled index (fast) or linear scan (fallback).
        # Snapshot the state reference ONCE so rules and index stay consistent
        # even if another coroutine swaps in a fresh reload concurrently.
        path = request.path
        method = request.method
        state = self._rules_state
        rules = state[0] if state is not None else []
        index = state[1] if state is not None else None
        if index is not None:
            matched = index.match(path, method, tier_name)
        else:
            matched = match_rule(rules, path, method, tier_name)

        if matched:
            max_requests = int(matched.get("max_requests", 100))
            window = int(matched.get("window_seconds", 60))
            cost = int(matched.get("cost", 1))
            rule_id = matched.get("id", 0)
            rule_name = matched.get("name", "")
        else:
            # Fallback to tier defaults
            tier_config = self.tiers.get(
                tier_name, self.tiers.get(self.default_tier, {})
            )
            max_requests = tier_config.get("max_requests", 100)
            window = tier_config.get("window", 60)
            cost = 1
            rule_id = 0
            rule_name = ""

        # Build key: separate counters per rule
        identity = self.key_func(request)
        if rule_id:
            key = f"{identity}:rule:{rule_id}"
        else:
            key = f"{identity}:tier:{tier_name}"

        # Check and increment with cost
        if self.backend._is_async:
            allowed, remaining, reset = await self.backend.check_and_increment(
                key, max_requests, window, cost
            )
        else:
            allowed, remaining, reset = self.backend.check_and_increment(
                key, max_requests, window, cost
            )

        policy_name = rule_name or f"tier:{tier_name}"
        policies = [QuotaPolicy(name=policy_name, quota=max_requests, window=window)]
        limits = [
            ServiceLimit(
                policy_name=policy_name, remaining=max(0, remaining), reset=reset
            )
        ]

        if not allowed:
            limits[0] = ServiceLimit(policy_name=policy_name, remaining=0, reset=reset)
            return build_429_response(
                policies,
                limits,
                reset,
                include_ietf=self._include_ietf,
                include_legacy=self._include_legacy,
                include_problem_details=self._include_problem_details,
                tier_name=tier_name,
                rule_name=rule_name,
                cost=cost,
            )

        response = await call_next(request)
        set_ratelimit_headers(
            response,
            policies,
            limits,
            include_ietf=self._include_ietf,
            include_legacy=self._include_legacy,
            tier_name=tier_name,
            rule_name=rule_name,
            cost=cost,
        )
        return response
