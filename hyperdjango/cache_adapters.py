"""
Distributed cache adapter system.

Extends the cache framework with production-ready distributed caching patterns:

- CacheAdapter: Base protocol for pluggable cache backends
- ConsistentHashRing: Distribute keys across multiple cache nodes
- StampedeProtection: Probabilistic early expiration (XFetch algorithm)
- TwoTierCache: L1 (in-process LocMemCache) + L2 (shared DatabaseCache)
- CacheMiddleware: Full-page response caching

uses LocMemCache (L1) + DatabaseCache/PostgreSQL UNLOGGED (L2).
Custom adapters can be registered for any backend.

Usage:
    from hyperdjango.cache_adapters import (
        TwoTierCache, StampedeProtection, ConsistentHashRing,
        CacheMiddleware, register_adapter, get_adapter,
    )

    # Two-tier: fast local + shared database
    cache = TwoTierCache(
        l1=LocMemCache(max_size=1000),
        l2=DatabaseCache(db),
        l1_ttl=10,  # Local cache for 10s
    )
    await cache.set("key", value, ttl=300)
    result = await cache.get("key")  # Tries L1, falls back to L2

    # Stampede protection
    cache = StampedeProtection(backend=LocMemCache(), beta=1.0)
    # Early probabilistic expiry prevents thundering herd on popular keys

    # Consistent hashing across multiple cache nodes
    ring = ConsistentHashRing(nodes={"node1": cache1, "node2": cache2})
    node = ring.get_node("user:42")  # Deterministic routing

    # Full-page caching middleware
    app.use(CacheMiddleware(cache, ttl=60, exclude=["/admin", "/api/auth"]))

    # Custom adapter registration
    register_adapter("custom", CustomAdapter)
    adapter = get_adapter("custom")
"""

import asyncio
import contextlib
import hashlib
import math
import random
import threading
import time
from collections.abc import Callable
from typing import Protocol

from hyperdjango._hyperdjango_native import (
    _hashring_add_node,
    _hashring_build,
    _hashring_free,
    _hashring_get_node,
    _hashring_get_node_instance,
    _hashring_get_stats,
    _hashring_hash_key,
    _hashring_new,
    _hashring_remove_node,
)

# Native hash ring functions (Zig compiled, ketama-compatible)
from hyperdjango.conf import get_setting as _get_setting
from hyperdjango.keybuilder import injective_join
from hyperdjango.logging import logger as _logger
from hyperdjango.response import Response as _Response
from hyperdjango.types import JSONValue

# ---------------------------------------------------------------------------
# Cache Adapter Protocol
# ---------------------------------------------------------------------------


class CacheAdapter(Protocol):
    """Protocol for pluggable cache backends.

    All cache backends must implement this interface.
    Methods may be sync or async depending on the backend.
    """

    def get(self, key: str, default: JSONValue = None) -> JSONValue: ...
    def set(self, key: str, value: JSONValue, ttl: int | None = None): ...
    def delete(self, key: str) -> bool: ...
    def clear(self): ...
    def has(self, key: str) -> bool: ...


# ---------------------------------------------------------------------------
# Consistent Hash Ring
# ---------------------------------------------------------------------------


class ConsistentHashRing:
    """Distribute cache keys across multiple nodes using consistent hashing.

    Uses native Zig implementation with ketama-compatible MD5 hashing.
    Batch sort (O(N log N)) instead of insort per point (O(N²)).
    Contiguous sorted array for cache-friendly binary search lookups.

    Inspired by uhashring but reimplemented in Zig for 10-50x faster lookups.

    Usage:
        ring = ConsistentHashRing(nodes={"shard1": cache1, "shard2": cache2})
        cache = ring.get_node("user:42")  # Returns cache1 or cache2 deterministically
        await cache.set("user:42", data)
    """

    def __init__(
        self,
        nodes: dict[str, CacheAdapter] | None = None,
        replicas: int = 4,
        vnodes: int = 40,
        weight_fn: Callable[[str], int] | None = None,
    ):
        self._nodes: dict[str, CacheAdapter] = {}
        self._weights: dict[str, int] = {}
        self._weight_fn = weight_fn
        self._handle = _hashring_new(replicas, vnodes)
        # Guards the multi-step ring mutations (two Python dicts + the native
        # add/build/remove sequence) so a reader never lands mid-build on a
        # half-resorted array and two mutators never interleave.
        self._lock = threading.Lock()

        if nodes:
            for name, backend in nodes.items():
                # weight=None → add_node applies self._weight_fn (or 1).
                self.add_node(name, backend)

    def __del__(self):
        if self._handle is not None:
            with contextlib.suppress(Exception):
                _hashring_free(self._handle)

    def add_node(self, name: str, backend: CacheAdapter, weight: int | None = None):
        """Add a cache node to the ring.

        When ``weight`` is not given, the ring's ``weight_fn`` (if any) is
        consulted so a dynamically added shard receives the same weight-scaled
        vnode count it would have gotten at construction time; without a
        ``weight_fn`` the weight defaults to 1.
        """
        if weight is None:
            weight = self._weight_fn(name) if self._weight_fn is not None else 1
        # The dict updates + native add/build must be one indivisible step so a
        # concurrent get_node never observes the node in a dict but absent from
        # a not-yet-rebuilt native array (or vice versa).
        with self._lock:
            self._nodes[name] = backend
            self._weights[name] = weight
            _hashring_add_node(self._handle, name, weight, 0, backend)
            _hashring_build(self._handle)

    def remove_node(self, name: str):
        """Remove a cache node from the ring."""
        with self._lock:
            self._nodes.pop(name, None)
            self._weights.pop(name, None)
            _hashring_remove_node(self._handle, name)

    def get_node(self, key: str) -> CacheAdapter | None:
        """Get the cache backend responsible for the given key."""
        # Hold the lock across the native handle read so a lookup cannot land
        # mid-build while add_node/remove_node is mutating the ring.
        with self._lock:
            if not self._nodes:
                raise RuntimeError("No nodes in consistent hash ring")

            instance = _hashring_get_node_instance(self._handle, key)
            if instance is not None:
                return instance
            # Fallback to name lookup if instance wasn't stored
            name = _hashring_get_node(self._handle, key)
            if name is not None:
                return self._nodes.get(name)
            return None

    def get_node_name(self, key: str) -> str:
        """Get the name of the node responsible for the given key."""
        with self._lock:
            if not self._nodes:
                raise RuntimeError("No nodes in consistent hash ring")

            name = _hashring_get_node(self._handle, key)
            return name if name is not None else ""

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def node_names(self) -> list[str]:
        with self._lock:
            return list(self._nodes.keys())

    def get_stats(self) -> dict[str, int | dict[str, int]]:
        """Get ring statistics including per-node point distribution."""
        with self._lock:
            return _hashring_get_stats(self._handle)

    @staticmethod
    def hash_key(key: str) -> int:
        """Hash a key to a ketama-compatible 32-bit integer."""
        return _hashring_hash_key(key)


# ---------------------------------------------------------------------------
# Stampede Protection (XFetch algorithm)
# ---------------------------------------------------------------------------


class StampedeProtection:
    """Cache with probabilistic early expiration to prevent thundering herd.

    Uses the XFetch algorithm: as a cached value approaches expiry, each
    request has an increasing probability of recomputing it. This spreads
    cache regeneration across multiple requests instead of all hitting at once.

    Usage:
        cache = StampedeProtection(backend=LocMemCache(), beta=1.0)
        cache.set("key", value, ttl=300, compute_time_ms=50)
        result = cache.get("key")  # May return None early to trigger recompute
    """

    def __init__(self, backend: CacheAdapter, beta: float = 1.0):
        self._backend = backend
        self.beta = beta  # Higher = more aggressive early expiry

    def get(self, key: str, default: JSONValue = None) -> JSONValue:
        """Get a cached value, with probabilistic early expiry.

        Returns None before actual expiry if XFetch decides this request
        should recompute the value (preventing stampede).
        """
        entry = self._backend.get(f"_xf:{key}")
        if entry is None:
            return default

        value, expires_at, compute_time = entry
        now = time.time()

        if now >= expires_at:
            return default  # Actually expired

        # XFetch: probabilistic early expiry
        # P(recompute) = beta * compute_time * ln(random) + expires_at <= now
        remaining = expires_at - now
        if compute_time > 0 and remaining > 0:
            threshold = compute_time * self.beta * math.log(random.random() + 1e-10)
            if now - threshold >= expires_at:
                return default  # Trigger early recompute

        return value

    def set(
        self, key: str, value: JSONValue, ttl: int = 300, compute_time_ms: float = 0
    ):
        """Cache a value with stampede protection metadata.

        Args:
            compute_time_ms: How long it took to compute this value (in ms).
                Used by XFetch to scale early expiry probability.
        """
        expires_at = time.time() + ttl
        compute_time = compute_time_ms / 1000.0  # Convert to seconds
        # Store as (value, expiry, compute_time) — the backend handles TTL
        self._backend.set(f"_xf:{key}", (value, expires_at, compute_time), ttl + 60)

    def delete(self, key: str) -> bool:
        return self._backend.delete(f"_xf:{key}")

    def clear(self):
        self._backend.clear()

    def has(self, key: str) -> bool:
        return self.get(key) is not None


# ---------------------------------------------------------------------------
# Two-Tier Cache (L1 local + L2 shared)
# ---------------------------------------------------------------------------


class TwoTierCache:
    """Two-tier cache: fast L1 (in-process) + shared L2 (database).

    - L1: LocMemCache — sub-microsecond, process-local
    - L2: DatabaseCache — shared across servers, survives restarts

    On get: check L1 first, fall back to L2, promote to L1 on hit.
    On set: write to both L1 and L2.
    On delete: remove from both.

    L1 has a shorter TTL to limit staleness across processes.
    """

    def __init__(
        self,
        l1: CacheAdapter,
        l2: CacheAdapter,
        l1_ttl: int = 10,
        fail_silently: bool = False,
    ):
        self.l1 = l1  # Fast local cache (LocMemCache)
        self.l2 = l2  # Shared cache (DatabaseCache or any CacheAdapter)
        self.l1_ttl = l1_ttl  # L1 TTL (shorter = more consistent, longer = faster)
        self.fail_silently = (
            fail_silently  # When True, L2 exceptions log a warning and fall through
        )
        self._l1_hits = 0
        self._l2_hits = 0
        self._misses = 0
        self._l2_errors = 0

    def get(self, key: str, default: JSONValue = None) -> JSONValue:
        """Get from L1, fall back to L2, promote to L1 on L2 hit.

        If fail_silently=True and L2 raises, the exception is logged as a
        warning and the result is treated as a cache miss (returns default).
        Otherwise L2 exceptions propagate to the caller.
        """
        # Try L1
        result = self.l1.get(key)
        if result is not None:
            self._l1_hits += 1
            return result

        # Try L2 — optionally catch errors
        try:
            result = self.l2.get(key)
        # blind-except: L2 is a pluggable secondary tier; fail_silently intentionally degrades any L2 backend error to a cache miss, and re-raises when the flag is off.
        except Exception as e:
            self._l2_errors += 1
            if not self.fail_silently:
                raise
            _logger.warning(
                "TwoTierCache: L2 get({key!r}) failed, returning default: {err}",
                key=key,
                err=e,
            )
            self._misses += 1
            return default

        if asyncio.iscoroutine(result):
            raise RuntimeError(
                "TwoTierCache.get() called with async L2 backend. "
                "Use await cache.aget() instead."
            )
        if result is not None:
            self._l2_hits += 1
            # Promote to L1 (L1 errors should be rare, still not catastrophic)
            try:
                self.l1.set(key, result, self.l1_ttl)
            # blind-except: promoting an L2 hit into L1 is a best-effort optimization; a promotion failure must not fail a get that already holds a valid L2 result.
            except Exception as e:
                _logger.warning("TwoTierCache: L1 promote failed: {err}", err=e)
            return result

        self._misses += 1
        return default

    async def aget(self, key: str, default: JSONValue = None) -> JSONValue:
        """Async get — L1 is sync, L2 may be async.

        If fail_silently=True and L2 raises, logs a warning and falls through
        to default.
        """
        # Try L1 (always sync)
        result = self.l1.get(key)
        if result is not None:
            self._l1_hits += 1
            return result

        # Try L2 (may be async for DatabaseCache)
        try:
            if self.l2._is_async:
                result = await self.l2.get(key)
            else:
                result = self.l2.get(key)
        # blind-except: L2 is a pluggable secondary tier; fail_silently intentionally degrades any L2 backend error to a cache miss, and re-raises when the flag is off.
        except Exception as e:
            self._l2_errors += 1
            if not self.fail_silently:
                raise
            _logger.warning(
                "TwoTierCache: L2 aget({key!r}) failed, returning default: {err}",
                key=key,
                err=e,
            )
            self._misses += 1
            return default

        if result is not None:
            self._l2_hits += 1
            try:
                self.l1.set(key, result, self.l1_ttl)
            # blind-except: promoting an L2 hit into L1 is a best-effort optimization; a promotion failure must not fail a get that already holds a valid L2 result.
            except Exception as e:
                _logger.warning("TwoTierCache: L1 promote failed: {err}", err=e)
            return result

        self._misses += 1
        return default

    def set(self, key: str, value: JSONValue, ttl: int | None = None):
        """Write to both L1 and L2.

        With fail_silently=True, L2 write errors are logged but do not propagate —
        L1 remains consistent and the write is treated as a best-effort L2 push.
        """
        self.l1.set(key, value, self.l1_ttl)
        try:
            self.l2.set(key, value, ttl)
        # blind-except: L2 is a pluggable secondary tier; fail_silently intentionally degrades any L2 backend write error to best-effort (L1 stays consistent), and re-raises when the flag is off.
        except Exception as e:
            self._l2_errors += 1
            if not self.fail_silently:
                raise
            _logger.warning(
                "TwoTierCache: L2 set({key!r}) failed: {err}", key=key, err=e
            )

    async def aset(self, key: str, value: JSONValue, ttl: int | None = None):
        """Async set — write to both L1 and L2.

        Same fail_silently semantics as set().
        """
        self.l1.set(key, value, self.l1_ttl)
        try:
            if self.l2._is_async:
                await self.l2.set(key, value, ttl)
            else:
                self.l2.set(key, value, ttl)
        # blind-except: L2 is a pluggable secondary tier; fail_silently intentionally degrades any L2 backend write error to best-effort (L1 stays consistent), and re-raises when the flag is off.
        except Exception as e:
            self._l2_errors += 1
            if not self.fail_silently:
                raise
            _logger.warning(
                "TwoTierCache: L2 aset({key!r}) failed: {err}", key=key, err=e
            )

    def delete(self, key: str) -> bool:
        """Remove from both L1 and L2.

        With fail_silently=True, L2 delete errors are logged but the method still
        returns based on the L1 result (L1 is the source of truth locally).
        """
        r1 = self.l1.delete(key)
        try:
            r2 = self.l2.delete(key)
        # blind-except: L2 is a pluggable secondary tier; fail_silently intentionally degrades any L2 backend delete error to best-effort (L1 is the local source of truth), and re-raises when the flag is off.
        except Exception as e:
            self._l2_errors += 1
            if not self.fail_silently:
                raise
            _logger.warning(
                "TwoTierCache: L2 delete({key!r}) failed: {err}", key=key, err=e
            )
            r2 = False
        return r1 or r2

    def clear(self):
        """Clear both tiers."""
        self.l1.clear()
        try:
            self.l2.clear()
        # blind-except: L2 is a pluggable secondary tier; fail_silently intentionally degrades any L2 backend clear error to best-effort, and re-raises when the flag is off.
        except Exception as e:
            self._l2_errors += 1
            if not self.fail_silently:
                raise
            _logger.warning("TwoTierCache: L2 clear failed: {err}", err=e)
        self._l1_hits = 0
        self._l2_hits = 0
        self._misses = 0
        self._l2_errors = 0

    def has(self, key: str) -> bool:
        if self.l1.has(key):
            return True
        try:
            return self.l2.has(key)
        # blind-except: L2 is a pluggable secondary tier; fail_silently intentionally degrades any L2 backend has() error to a negative result, and re-raises when the flag is off.
        except Exception as e:
            if not self.fail_silently:
                raise
            _logger.warning(
                "TwoTierCache: L2 has({key!r}) failed: {err}", key=key, err=e
            )
            return False

    def get_stats(self) -> dict[str, int | float]:
        """Get two-tier cache statistics."""
        total = self._l1_hits + self._l2_hits + self._misses
        return {
            "l1_hits": self._l1_hits,
            "l2_hits": self._l2_hits,
            "misses": self._misses,
            "l2_errors": self._l2_errors,
            "total_requests": total,
            "l1_hit_rate": self._l1_hits / total if total else 0.0,
            "l2_hit_rate": self._l2_hits / total if total else 0.0,
            "overall_hit_rate": (self._l1_hits + self._l2_hits) / total
            if total
            else 0.0,
        }


# ---------------------------------------------------------------------------
# Cache Middleware (full-page response caching)
# ---------------------------------------------------------------------------


class CacheMiddleware:
    """Full-page response caching middleware.

    Caches GET responses for configured TTL. Skips non-GET methods,
    authenticated users (if configured), and excluded paths.

    Usage:
        app.use(CacheMiddleware(cache, ttl=60, exclude=["/admin"]))
    """

    def __init__(
        self,
        cache: CacheAdapter,
        ttl: int = 60,
        exclude: list[str] | None = None,
        cache_authenticated: bool = False,
        vary_headers: list[str] | None = None,
    ):
        self.cache = cache
        self.ttl = ttl
        self.exclude = exclude or []
        self.cache_authenticated = cache_authenticated
        self.vary_headers = vary_headers or []

    async def __call__(self, request, call_next):
        # Only cache GET requests
        if request.method != "GET":
            return await call_next(request)

        # Skip excluded paths
        for prefix in self.exclude:
            if request.path.startswith(prefix):
                return await call_next(request)

        # Skip authenticated users unless explicitly allowed
        user = request.user
        if not self.cache_authenticated:
            if user is not None and user.is_authenticated:
                return await call_next(request)

        # Auth-state fail-safe (independent of middleware ordering): if the
        # request carries a session cookie but request.user is unresolved
        # (None) — e.g. CacheMiddleware is ordered OUTSIDE the auth middleware,
        # so auth has not run yet — we cannot prove the request is anonymous
        # (nor build a correct per-user key). Serving or storing a path-only
        # anonymous entry here would leak a logged-in user's personalized page
        # to everyone. Treat the request as uncacheable.
        if user is None and self._has_session_cookie(request):
            return await call_next(request)

        # Build cache key
        cache_key = self._make_key(request)

        # Check cache
        if self.cache._is_async:
            cached = await self.cache.get(cache_key)
        else:
            cached = self.cache.get(cache_key)

        if cached is not None:
            body, status, content_type = cached
            resp = (
                _Response.html(body, status=status)
                if "html" in content_type
                else _Response.text(body, status=status)
            )
            resp.headers["X-Cache"] = "HIT"
            return resp

        # Execute request
        response = await call_next(request)

        # Cache the response
        # dynamic-attr: response is the middleware chain's return value — a hyperdjango Response (.status) or a Django HttpResponse (.status_code); the status attribute name is not statically pinned
        status = getattr(response, "status", getattr(response, "status_code", 500))

        # Parse Cache-Control directives case-insensitively and
        # whitespace-tolerantly. Never cache a response the origin marked
        # private / no-cache / no-store.
        cc_value = response.headers.get("cache-control", "") or response.headers.get(
            "Cache-Control", ""
        )
        cc_directives = {
            tok.strip().lower().split("=", 1)[0]
            for tok in cc_value.split(",")
            if tok.strip()
        }
        is_public = "public" in cc_directives
        no_store = bool(cc_directives & {"private", "no-cache", "no-store"})

        # Refuse to cache responses with `Vary: *` — the response is unique per
        # request, so caching it can leak content across users. Tokens are
        # stripped before comparison so " * " is still recognized.
        vary_value = response.headers.get("vary", "") or response.headers.get(
            "Vary", ""
        )
        vary_tokens = [tok.strip() for tok in vary_value.split(",") if tok.strip()]
        vary_star = any(tok == "*" for tok in vary_tokens)

        # The cache key (`_make_key`) only discriminates on `self.vary_headers`.
        # If the response declares it varies on some OTHER request header
        # (e.g. Accept-Language, Cookie, Accept-Encoding), two requests that
        # differ only in that header collide on one key — we'd serve the wrong
        # variant. Refuse to cache unless every declared Vary field is one we
        # already fold into the key. Authorization is always folded into the
        # key by `_make_key` (hashed) whenever present, so a response that
        # varies on Authorization is safely keyed per-token — hence it is a
        # covered field and never treated as uncovered.
        _covered = {h.lower() for h in self.vary_headers}
        _covered.add("authorization")
        vary_uncovered = any(tok.lower() not in _covered for tok in vary_tokens)

        # A response to a request that carried an Authorization header may be
        # user-specific. Unless it is explicitly public, it must vary on
        # Authorization (so a shared cache never serves one user's response to
        # another) and we do not store it in our own page cache.
        has_authorization = bool(
            request.headers.get("authorization") or request.headers.get("Authorization")
        )
        authz_sensitive = has_authorization and not is_public
        if authz_sensitive and not any(
            tok.lower() == "authorization" for tok in vary_tokens
        ):
            vary_tokens.append("Authorization")
            response.headers["Vary"] = ", ".join(vary_tokens)

        cacheable = (
            200 <= status < 300
            and not vary_star
            and not vary_uncovered
            and not no_store
            and not authz_sensitive
        )
        if cacheable:
            # dynamic-attr: response is the middleware chain's return value — a hyperdjango Response exposes .body; other response-like objects may not
            body = getattr(response, "body", b"")
            if isinstance(body, bytes):
                body = body.decode("utf-8", errors="replace")
            content_type = response.headers.get("content-type", "text/html")
            cache_data = (body, status, content_type)

            if self.cache._is_async:
                await self.cache.set(cache_key, cache_data, self.ttl)
            else:
                self.cache.set(cache_key, cache_data, self.ttl)

            response.headers["X-Cache"] = "MISS"

        return response

    def _make_key(self, request) -> str:
        """Build a cache key from request path + query string + vary headers + user.

        Includes user identity when cache_authenticated=True to prevent
        serving one user's cached page to another user.
        """
        parts = [request.path]
        qs = request.query_string
        if qs:
            parts.append(qs)
        for header in self.vary_headers:
            val = request.headers.get(header, "")
            if val:
                parts.append(f"{header}={val}")
        # Fold the Authorization request value into the key (hashed — never the
        # plaintext token) whenever present. This makes per-token caching of a
        # `Cache-Control: public` + `Vary: Authorization` response safe: each
        # bearer gets a distinct key, so one user's response can never be
        # cross-served to a different token under a path-only key.
        authz = request.headers.get("authorization") or request.headers.get(
            "Authorization"
        )
        if authz:
            ah = hashlib.md5(authz.encode(), usedforsecurity=False).hexdigest()
            parts.append(f"authz={ah}")
        # Include user identity for authenticated caching
        if self.cache_authenticated:
            user = request.user
            if user is not None:
                uid = user.id
                if uid is not None:
                    parts.append(f"user={uid}")
        # INJECTIVE join (single authority): the untrusted components (path,
        # query_string, vary-header values) can all contain the separator, so a
        # plain "|".join let a crafted request FORGE the trust-bearing suffix —
        # an unauthenticated "/p?x=1|user=5" produced the SAME key as
        # authenticated user 5's "/p?x=1", cross-serving/poisoning their page.
        # injective_join length-prefixes each component so an embedded separator
        # is data, never a boundary.
        raw = injective_join(parts)
        if len(raw) > 200:
            h = hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()
            return f"page:{h}"
        return f"page:{raw}"

    def _has_session_cookie(self, request) -> bool:
        """True if the request carries the configured session cookie.

        Signals a client that may be authenticated even though this middleware
        cannot see its resolved identity (request.user is None). Parsed from the
        raw Cookie header (matching the whole cookie name as a token, to avoid
        substring false positives) so it does not depend on any request.cookies
        property being present.
        """
        cookie_header = request.headers.get("cookie") or request.headers.get(
            "Cookie", ""
        )
        if not cookie_header:
            return False
        session_name = _get_setting("SESSION_COOKIE_NAME")
        for pair in cookie_header.split(";"):
            name = pair.strip().split("=", 1)[0].strip()
            if name == session_name:
                return True
        return False


# ---------------------------------------------------------------------------
# Adapter Registry
# ---------------------------------------------------------------------------

_adapter_registry: dict[str, type] = {}
_adapter_registry_lock = threading.Lock()


def register_adapter(name: str, adapter_class: type):
    """Register a custom cache adapter by name.

    Usage:
        register_adapter("custom", CustomAdapter)
    """
    with _adapter_registry_lock:
        _adapter_registry[name] = adapter_class


def get_adapter(name: str) -> type | None:
    """Get a registered cache adapter class by name."""
    with _adapter_registry_lock:
        return _adapter_registry.get(name)


def list_adapters() -> list[str]:
    """List all registered adapter names."""
    with _adapter_registry_lock:
        return list(_adapter_registry.keys())
