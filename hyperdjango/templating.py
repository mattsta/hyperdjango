"""
HyperDjango template engine — native Zig compilation.

Compiles templates to Zig node trees at load time, renders by walking the tree
and writing directly to a contiguous buffer. Full Jinja2 syntax compatibility.

Usage:
    engine = TemplateEngine("templates")
    html = engine.render("index.html", {"title": "Hello", "items": [1, 2, 3]})
"""

import contextlib
import hashlib
import os
import tempfile
import time as _time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock, local

from hyperdjango._hyperdjango_native import (
    _template_compile,
    _template_deserialize,
    _template_register_filter,
    _template_render,
    _template_serialize,
    _template_set_delimiters,
    _template_set_i18n_callback,
    _template_set_loader,
    _template_set_safety_limits,
    _template_set_sandbox,
    _template_set_undefined_mode,
)

# Engine-level autoescape default. The native setter (`_template_set_autoescape`)
# requires a matching method-table line in main.zig; until that ships the symbol
# is absent from the compiled module. Import defensively so the engine still
# loads, and REJECT `autoescape=False` at construction when the setter is
# missing (see __post_init__) — never silently hand back escaped output.
try:
    from hyperdjango._hyperdjango_native import (
        _template_set_autoescape as _native_set_autoescape,
    )
except ImportError:
    _native_set_autoescape = None
from hyperdjango.native import fast_json_dumps, fast_json_loads
from hyperdjango.telemetry import metrics as _tel_metrics

# ── Native telemetry (zero cost when disabled) ──────────────────────────────
#
# One Counter for total renders (cheap monotonic) plus one Histogram for the
# render-duration distribution. Buckets are tuned for the cached render path
# (median ~40 μs) up through cold-cache compiles (~7 ms upper end). The
# histogram bucket count is the same as the default — only the values differ.
#
# We deliberately don't label by template name: cardinality could explode
# in apps with many partials. Per-template hot-path detail goes through the
# tracer's per-render span (when sampling fires) and the template engine's
# own LRU stats.

_TEMPLATE_RENDER_BUCKETS: tuple[float, ...] = (
    0.00001,  # 10 μs
    0.000025,  # 25 μs
    0.00005,  # 50 μs
    0.0001,  # 100 μs
    0.00025,  # 250 μs
    0.0005,  # 500 μs
    0.001,  # 1 ms
    0.0025,  # 2.5 ms
    0.005,  # 5 ms
    0.01,  # 10 ms
    0.025,  # 25 ms
    0.1,  # 100 ms
)

_template_renders_total = _tel_metrics.Counter(
    "hyperdjango_template_renders_total",
    "Total template renders dispatched through TemplateEngine.",
)
_template_render_duration_seconds = _tel_metrics.Histogram(
    "hyperdjango_template_render_duration_seconds",
    "Template render duration in seconds (cache hit + miss combined).",
    buckets=_TEMPLATE_RENDER_BUCKETS,
)

# The Zig engine keeps render config (undefined mode, sandbox, delimiters,
# limits, loader, i18n callback) in THREAD-LOCAL storage. To skip re-applying it
# on every render we remember the last-applied signature per thread. It is
# module-level (not per-engine) because the underlying Zig state is per-thread
# and shared across engine instances — so a different engine rendering in
# between changes the signature and forces a re-apply.
_render_config_tls = local()


# Per-compile dependency accumulator. The Zig loader callback
# (_load_template_source) runs synchronously on the compiling thread, so the
# in-flight compile's dependency dict lives in thread-local storage instead of
# a shared engine attribute. Two concurrent compiles on the SAME engine would
# otherwise clear()/fill one shared dict interleaved, and the Merkle snapshot
# written to .hztc.meta could omit a template's includes — leaving cached
# bytecode that is never invalidated when that include is edited (a stale
# template served forever, surviving restarts). `.acc` is the current compile's
# dict, or None when no compile is in flight on this thread.
class _CompileDepsLocal(local):
    """Per-thread compile-dependency accumulator. Class-attribute default gives
    every thread a valid `.acc` (None) on first read, so reads need no getattr
    (the threading.local idiom used by app._ThreadLocalLoop)."""

    acc: dict | None = None


_compile_deps_tls = _CompileDepsLocal()


class Namespace:
    """Jinja2-compatible namespace — mutable object for cross-scope state in templates.

    Usage in templates:
        {% set ns = namespace(counter=0, items=[]) %}
        {% for x in data %}
            {% set ns.counter = ns.counter + 1 %}
        {% endfor %}
        Total: {{ ns.counter }}
    """

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            # dynamic-attr: template namespace binds arbitrary runtime-named attributes ({% set ns.x = ... %}); key is not statically knowable
            object.__setattr__(self, key, value)

    def __repr__(self) -> str:
        items = ", ".join(f"{k}={v!r}" for k, v in self.__dict__.items())
        return f"Namespace({items})"


# Characters permitted inside a CSP nonce attribute value. A CSP nonce is a
# base64 (standard or url-safe) token, so this covers every legal byte. Any
# other byte is dropped so a hostile value can never break out of the
# `nonce="..."` attribute (no quote, angle-bracket, or whitespace injection).
_CSP_NONCE_SAFE: frozenset[str] = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=-_"
)


def csp_nonce_attr(nonce: str | None = None) -> str:
    """Render a ` nonce="..."` attribute fragment for a CSP-nonce-protected page.

    Returns a SPACE-prefixed `nonce="<value>"` string when ``nonce`` is a
    non-empty value, or an empty string when it is absent/empty. The leading
    space lets templates write ``<script{{ csp_nonce_attr(csp_nonce)|safe }}>``
    so the output is byte-identical to ``<script>`` when no nonce is supplied —
    existing pages render unchanged unless a ``csp_nonce`` is wired in.

    Designed to be registered as a template global (see
    ``TemplateEngine.__post_init__``) and invoked from explicit inline
    ``<script>``/``<style>`` blocks under a nonce-based Content-Security-Policy.

    The value is sanitized to the CSP nonce character set, so a hostile nonce
    can never inject extra attributes or escape the quoted value.
    """
    if not nonce:
        return ""
    cleaned = "".join(c for c in str(nonce) if c in _CSP_NONCE_SAFE)
    if not cleaned:
        return ""
    return f' nonce="{cleaned}"'


def _fnv1a_64(data: bytes) -> int:
    """FNV-1a 64-bit hash — matches Zig's std.hash.Fnv1a_64."""
    h = 0xCBF29CE484222325
    for b in data:
        h ^= b
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


@dataclass(slots=True)
class _BytecodeMeta:
    """Merkle tree metadata for a cached bytecode file.

    Tracks the content hash of the main template AND every transitive
    dependency (includes, extends, imports). The merkle_hash is a single
    combined hash that changes when ANY file in the dependency tree changes.

    Stored as a JSON sidecar file (.hztc.meta) alongside each .hztc.
    """

    main_hash: int  # FNV-1a of main template source
    dep_hashes: dict[str, int]  # {relative_path: fnv1a(source)} for all transitive deps
    merkle_hash: int  # Combined hash used for .hztc validation

    @staticmethod
    def compute_merkle(main_source: bytes, dep_sources: dict[str, bytes]) -> int:
        """Compute Merkle hash from main source + sorted dependency sources.

        Deterministic: deps sorted by path, each contributes path:hash to the chain.
        Any change in any dependency changes the merkle hash.
        """
        h = _fnv1a_64(main_source)
        for path in sorted(dep_sources):
            # Mix in dependency path and content hash
            path_bytes = path.encode("utf-8")
            dep_hash = _fnv1a_64(dep_sources[path])
            # Chain: h = fnv1a(h || path || dep_hash)
            chain = (
                h.to_bytes(8, "little") + path_bytes + dep_hash.to_bytes(8, "little")
            )
            h = _fnv1a_64(chain)
        return h

    @staticmethod
    def build(main_source: bytes, dep_sources: dict[str, bytes]) -> _BytecodeMeta:
        """Build metadata from main source and collected dependency sources."""
        main_hash = _fnv1a_64(main_source)
        dep_hashes = {path: _fnv1a_64(src) for path, src in dep_sources.items()}
        merkle_hash = _BytecodeMeta.compute_merkle(main_source, dep_sources)
        return _BytecodeMeta(
            main_hash=main_hash,
            dep_hashes=dep_hashes,
            merkle_hash=merkle_hash,
        )

    def to_json(self) -> str:
        """Serialize to JSON for .hztc.meta sidecar file.

        Hashes stored as hex strings — JSON numbers are IEEE 754 doubles
        with only 53 bits of mantissa, so 64-bit FNV-1a hashes would lose
        precision on round-trip if stored as integers.
        """
        return fast_json_dumps(
            {
                "main_hash": format(self.main_hash, "x"),
                "dep_hashes": {k: format(v, "x") for k, v in self.dep_hashes.items()},
                "merkle_hash": format(self.merkle_hash, "x"),
            }
        ).decode()

    @staticmethod
    def from_json(data: str) -> _BytecodeMeta | None:
        """Deserialize from JSON. Returns None on any parse error."""
        try:
            obj = fast_json_loads(data)
            main = obj["main_hash"]
            merkle = obj["merkle_hash"]
            deps = obj["dep_hashes"]
            return _BytecodeMeta(
                main_hash=int(main, 16) if isinstance(main, str) else main,
                dep_hashes={
                    str(k): int(v, 16) if isinstance(v, str) else int(v)
                    for k, v in deps.items()
                },
                merkle_hash=int(merkle, 16) if isinstance(merkle, str) else merkle,
            )
        except KeyError, TypeError, ValueError, RuntimeError:
            return None

    def validate_deps(self, load_source: Callable[[str], bytes | None]) -> bool:
        """Check if all dependencies still match their recorded hashes.

        load_source(dep_path) should return the current bytes of the dependency,
        or None if the file no longer exists.
        """
        for dep_path, recorded_hash in self.dep_hashes.items():
            current_source = load_source(dep_path)
            if current_source is None:
                return False  # Dependency removed
            if _fnv1a_64(current_source) != recorded_hash:
                return False  # Dependency changed
        return True


@dataclass(slots=True)
class CacheStats:
    """Immutable snapshot of template cache statistics.

    Returned by TemplateEngine.cache_stats(). All counters are cumulative
    since engine creation (or last reset_cache_stats() call).
    """

    lru_hits: int
    lru_misses: int
    disk_hits: int
    disk_misses: int
    compiles: int
    evictions: int
    lru_entries: int
    lru_bytes: int
    lru_max_bytes: int

    @property
    def lru_hit_rate(self) -> float:
        """LRU cache hit rate as a fraction [0.0, 1.0]. Returns 0.0 if no lookups."""
        total = self.lru_hits + self.lru_misses
        return self.lru_hits / total if total > 0 else 0.0

    @property
    def disk_hit_rate(self) -> float:
        """Disk bytecode cache hit rate as a fraction [0.0, 1.0]. Returns 0.0 if no lookups."""
        total = self.disk_hits + self.disk_misses
        return self.disk_hits / total if total > 0 else 0.0

    @property
    def total_lookups(self) -> int:
        """Total template lookups (LRU hits + LRU misses)."""
        return self.lru_hits + self.lru_misses


@dataclass(slots=True)
class _LRUCache:
    """Thread-safe LRU cache for compiled templates. Bounded by total memory usage.

    Evicts least-recently-used entries when total cached source bytes exceed
    max_bytes. Each entry tracks the size of the template source that was
    compiled — this is a proxy for the compiled node tree size (which lives
    in Zig heap and isn't directly measurable from Python, but scales linearly
    with source size).

    Tracks hit/miss/eviction counters for cache_stats() diagnostics.
    """

    max_bytes: int = 256 * 1024 * 1024
    _data: OrderedDict = field(default_factory=OrderedDict, init=False, repr=False)
    _sizes: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _mtimes: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _total_bytes: int = field(default=0, init=False, repr=False)
    _hits: int = field(default=0, init=False, repr=False)
    _misses: int = field(default=0, init=False, repr=False)
    _evictions: int = field(default=0, init=False, repr=False)

    def get(self, key: str):
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                self._hits += 1
                return self._data[key]
            self._misses += 1
        return None

    def put(self, key: str, value, source_size: int = 0, mtime: float | None = None):
        with self._lock:
            # If replacing existing entry, subtract its old size
            if key in self._data:
                self._data.move_to_end(key)
                self._total_bytes -= self._sizes.get(key, 0)
            self._data[key] = value
            self._sizes[key] = source_size
            self._total_bytes += source_size
            if mtime is not None:
                self._mtimes[key] = mtime
            # Evict LRU entries until under budget
            while self._total_bytes > self.max_bytes and len(self._data) > 1:
                evicted_key, _ = self._data.popitem(last=False)
                self._total_bytes -= self._sizes.pop(evicted_key, 0)
                self._mtimes.pop(evicted_key, None)
                self._evictions += 1

    def get_mtime(self, key: str) -> float | None:
        with self._lock:
            return self._mtimes.get(key)

    def values(self):
        with self._lock:
            return list(self._data.values())

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._total_bytes

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._data)

    @property
    def hits(self) -> int:
        with self._lock:
            return self._hits

    @property
    def misses(self) -> int:
        with self._lock:
            return self._misses

    @property
    def evictions(self) -> int:
        with self._lock:
            return self._evictions

    def reset_counters(self) -> None:
        """Reset hit/miss/eviction counters to zero."""
        with self._lock:
            self._hits = 0
            self._misses = 0
            self._evictions = 0

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._data

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def clear(self):
        with self._lock:
            self._data.clear()
            self._sizes.clear()
            self._mtimes.clear()
            self._total_bytes = 0


@dataclass(slots=True)
class TemplateEngine:
    """Template engine with native Zig compilation.

    Compiled templates are cached in a thread-safe LRU. The compile step
    (lexer → parser → node tree) happens once per template. The render step
    (walk nodes, resolve variables, write buffer) happens on every call with
    fresh context — no recompilation needed.
    """

    template_dir: str = "templates"
    auto_reload: bool = True
    autoescape: bool = True
    undefined: str = "silent"  # "silent" | "strict" | "debug"
    sandboxed: bool = False  # restrict access to __class__, __globals__, etc.
    max_string_len: int = 0  # max string length in bytecode cache (0 = default 10MB)
    max_array_count: int = (
        0  # max array/node count in bytecode cache (0 = default 100K)
    )
    max_expr_depth: int = 0  # max expression recursion depth (0 = default 500)
    block_start_string: str = "{%"
    block_end_string: str = "%}"
    variable_start_string: str = "{{"
    variable_end_string: str = "}}"
    comment_start_string: str = "{#"
    comment_end_string: str = "#}"
    i18n_callback: Callable | None = (
        None  # Translation function for {% trans %}: str -> str
    )
    cache_max_bytes: int = 256 * 1024 * 1024  # 256 MB default
    bytecode_cache: bool = True  # enable disk bytecode caching (.hztc files)
    bytecode_cache_dir: str | None = None  # default: {template_dir}/__pycache__/hztc

    _compiled_cache: _LRUCache = field(default=None, repr=False)
    _bytecode_dir: Path | None = field(default=None, repr=False)
    _loaded_sources: dict[str, str] = field(default_factory=dict, repr=False)
    _custom_filters: dict[str, Callable] = field(default_factory=dict, repr=False)
    _globals: dict[str, object] = field(default_factory=dict, repr=False)
    _disk_hits: int = field(default=0, repr=False)
    _disk_misses: int = field(default=0, repr=False)
    _compiles: int = field(default=0, repr=False)
    _resolved_path_cache: dict[str, tuple[str, float]] = field(
        default_factory=dict, repr=False
    )  # {template_path: (abs_path, mtime)}
    # Resolved template directory — cached based on the current value of
    # self.template_dir. Path(self.template_dir).resolve() walks every parent
    # directory lstat-ing each component — ~17 syscalls — so calling it per
    # render would be wasteful. We cache the resolved Path along with the source
    # string so reassigning template_dir
    # (which tests do: `engine = TemplateEngine(); engine.template_dir = tmp`)
    # automatically invalidates the cache on next access.
    # See logs/profile_hypernews_report.md for the before/after numbers.
    _resolved_base: Path | None = field(default=None, repr=False)
    _resolved_base_str: str = field(default="", repr=False)
    _resolved_base_source: str = field(default="", repr=False)
    _resolved_template_paths: dict[str, Path] = field(
        default_factory=dict, repr=False
    )  # template_name → resolved full Path (cached across renders)
    # id(source) → (source_ref, compiled-cache key). Avoids MD5-hashing a whole
    # (often large, constant) template string on every render_string() call.
    # The source_ref keeps the string alive so id() can't be recycled onto a
    # different object; identity is re-checked on lookup.
    _string_key_cache: dict[int, tuple[str, str]] = field(
        default_factory=dict, repr=False
    )

    def __post_init__(self):
        # Engine-level autoescape=False is honored via the native setter wired in
        # _apply_render_config. If the native module predates that FFI export the
        # config CANNOT take effect (the Zig default escapes), and silently
        # returning escaped output would be a real config bug — so fail loudly.
        if not self.autoescape and _native_set_autoescape is None:
            raise RuntimeError(
                "TemplateEngine(autoescape=False) cannot be honored: the native "
                "'_template_set_autoescape' FFI export is missing from this build. "
                "Rebuild the native module (main.zig must register "
                "'_template_set_autoescape') to disable HTML autoescaping."
            )
        if self._compiled_cache is None:
            self._compiled_cache = _LRUCache(self.cache_max_bytes)
        # Prime the resolved-base cache with the initial template_dir
        self._refresh_resolved_base()
        # Set up bytecode cache directory
        if self.bytecode_cache:
            if self.bytecode_cache_dir is not None:
                self._bytecode_dir = Path(self.bytecode_cache_dir)
            else:
                self._bytecode_dir = self._resolved_base / "__pycache__" / "hztc"
        # Inject namespace() as a default global
        if "namespace" not in self._globals:
            self._globals["namespace"] = Namespace
        # Inject csp_nonce_attr() as a default global so templates can render a
        # nonce attribute on explicit inline <script>/<style> blocks under a
        # nonce-based Content-Security-Policy. No-op when csp_nonce is absent.
        if "csp_nonce_attr" not in self._globals:
            self._globals["csp_nonce_attr"] = csp_nonce_attr
        # Set the template loader for Zig engine ({% extends %}, {% import %})
        _template_set_loader(self._load_template_source)

    def _refresh_resolved_base(self) -> Path:
        """Return the resolved template_dir, re-resolving only if it changed.

        Fast path (99%): `self.template_dir` matches the cached source → return
        the cached Path with zero filesystem access. Slow path: re-resolve and
        invalidate the per-template cache.
        """
        current = self.template_dir
        if current == self._resolved_base_source and self._resolved_base is not None:
            return self._resolved_base
        # template_dir changed (or first call) — re-resolve and invalidate caches
        self._resolved_base = Path(current).resolve()
        self._resolved_base_str = str(self._resolved_base)
        self._resolved_base_source = current
        self._resolved_template_paths.clear()
        return self._resolved_base

    def _load_template_source(self, template_path: str) -> str:
        """Load template source from disk. Called by Zig engine for extends/import/include.

        Uses a resolved-path cache to skip Path.resolve() and security validation
        on repeated lookups (e.g. dynamic extends/include that re-resolve every render).
        Cache entries are validated via mtime to detect file changes.

        Tracks loaded sources in _loaded_sources for Merkle hash computation.
        """
        # Fast path: check resolved-path cache (avoids Path.resolve + security check)
        cached_entry = self._resolved_path_cache.get(template_path)
        if cached_entry is not None:
            abs_path_str, cached_mtime = cached_entry
            try:
                current_mtime = Path(abs_path_str).stat().st_mtime
                if current_mtime == cached_mtime:
                    source = Path(abs_path_str).read_text(encoding="utf-8")
                    acc = _compile_deps_tls.acc
                    if acc is not None:
                        acc[template_path] = source
                    return source
            except OSError:
                # File removed — fall through to full resolution.
                # pop(...,None): a concurrent loader that already evicted this
                # key must not raise KeyError back out through the Zig callback.
                self._resolved_path_cache.pop(template_path, None)

        # Full resolution with security validation — use cached base
        # (re-resolved lazily if self.template_dir was reassigned)
        base = self._refresh_resolved_base()
        full_path = (base / template_path).resolve()
        # SECURITY: Prevent path traversal via ../
        if (
            not str(full_path).startswith(self._resolved_base_str + "/")
            and full_path != base
        ):
            raise FileNotFoundError(
                f"Template path escapes template directory: {template_path}"
            )
        try:
            stat_result = full_path.stat()
        except OSError:
            raise FileNotFoundError(f"Template not found: {template_path}")
        source = full_path.read_text(encoding="utf-8")
        # Cache the resolved path + mtime for future lookups (capped at 1024)
        self._resolved_path_cache[template_path] = (
            str(full_path),
            stat_result.st_mtime,
        )
        if len(self._resolved_path_cache) > 1024:
            oldest_key = next(iter(self._resolved_path_cache))
            # pop(...,None): two threads can compute the same oldest_key and both
            # try to evict it — the loser must not raise KeyError.
            self._resolved_path_cache.pop(oldest_key, None)
        # Track for Merkle dependency hash (into the current compile's accumulator)
        acc = _compile_deps_tls.acc
        if acc is not None:
            acc[template_path] = source
        return source

    def render(self, template_name: str, context: dict | None = None) -> str:
        """Render a template with the given context via native Zig engine."""
        ctx = dict(self._globals)
        if context:
            ctx.update(context)
        # Native telemetry — fast-path branch is inside the metric methods
        # so when telemetry is disabled this is two LOAD_GLOBAL + branch
        # pairs (~50 ns total). When enabled, two FFI calls.
        if _tel_metrics.is_enabled():
            start_ns = _time.monotonic_ns()
            try:
                return self._render_native(template_name, ctx)
            finally:
                _template_renders_total.inc(1)
                _template_render_duration_seconds.observe(
                    (_time.monotonic_ns() - start_ns) / 1e9
                )
        return self._render_native(template_name, ctx)

    def render_string(self, source: str, context: dict | None = None) -> str:
        """Render a template from a source string via native Zig engine."""
        ctx = dict(self._globals)
        if context:
            ctx.update(context)
        if _tel_metrics.is_enabled():
            start_ns = _time.monotonic_ns()
            try:
                return self._render_string_native(source, ctx)
            finally:
                _template_renders_total.inc(1)
                _template_render_duration_seconds.observe(
                    (_time.monotonic_ns() - start_ns) / 1e9
                )
        return self._render_string_native(source, ctx)

    def add_global(self, name: str, value):
        """Add a global variable available in all templates."""
        # COW swap: build a fresh dict and atomically rebind, so a concurrent
        # render() reading dict(self._globals) always sees a complete, immutable
        # snapshot (never a dict mutated mid-iteration).
        self._globals = {**self._globals, name: value}

    def add_filter(self, name: str, func):
        """Add a custom template filter.

        Stored and wired into all compiled templates (cached + future).
        """
        # COW swap (see add_global): _wire_filters iterates this dict on the
        # render/compile path, so mutating it in place could raise "dict changed
        # size during iteration". Rebind atomically instead.
        self._custom_filters = {**self._custom_filters, name: func}
        # Wire into any already-compiled native templates in the LRU
        for capsule in self._compiled_cache.values():
            if capsule is not None:
                with contextlib.suppress(Exception):
                    _template_register_filter(capsule, name, func)

    def clear_bytecode_cache(self) -> int:
        """Remove all .hztc and .hztc.meta bytecode cache files from disk.

        Returns the number of .hztc files removed.
        """
        if self._bytecode_dir is None or not self._bytecode_dir.exists():
            return 0
        count = 0
        for hztc_file in self._bytecode_dir.rglob("*.hztc"):
            hztc_file.unlink()
            count += 1
        for meta_file in self._bytecode_dir.rglob("*.hztc.meta"):
            meta_file.unlink()
        return count

    def cache_stats(self) -> CacheStats:
        """Return a snapshot of template cache statistics.

        All counters are cumulative since engine creation or last reset_cache_stats().
        Thread-safe — reads LRU counters under lock.
        """
        return CacheStats(
            lru_hits=self._compiled_cache.hits,
            lru_misses=self._compiled_cache.misses,
            disk_hits=self._disk_hits,
            disk_misses=self._disk_misses,
            compiles=self._compiles,
            evictions=self._compiled_cache.evictions,
            lru_entries=self._compiled_cache.count,
            lru_bytes=self._compiled_cache.total_bytes,
            lru_max_bytes=self._compiled_cache.max_bytes,
        )

    def reset_cache_stats(self) -> None:
        """Reset all cache counters to zero. Does NOT clear cached templates."""
        self._compiled_cache.reset_counters()
        self._disk_hits = 0
        self._disk_misses = 0
        self._compiles = 0

    async def render_async(
        self, template_name: str, context: dict | None = None
    ) -> str:
        """Render a template asynchronously. Uses native Zig engine (sync under the hood)."""
        return self.render(template_name, context)

    _UNDEFINED_MODES = {"silent": 0, "strict": 1, "debug": 2}
    _DEFAULT_DELIMS = ("{%", "%}", "{{", "}}", "{#", "#}")

    def _apply_render_config(self) -> None:
        """Apply engine configuration before each render (threadlocal state in Zig).

        These values are per-engine invariants that almost never change between
        renders, so applying them with ~6 native FFI setter calls on every render
        would be wasteful. The Zig state is thread-local, so we memoize the
        last-applied signature
        per thread and skip the setters when this thread already holds it. A
        different engine (or changed config) has a different signature — its
        loader/i18n/delims/limits differ — so it correctly forces a re-apply.
        """
        mode_int = self._UNDEFINED_MODES.get(self.undefined, 0)
        current = (
            self.block_start_string,
            self.block_end_string,
            self.variable_start_string,
            self.variable_end_string,
            self.comment_start_string,
            self.comment_end_string,
        )
        sig = (
            mode_int,
            bool(self.sandboxed),
            bool(self.autoescape),
            self.max_string_len,
            self.max_array_count,
            self.max_expr_depth,
            current,
            self.i18n_callback,
            self._load_template_source,
        )
        # dynamic-attr: threading.local "sig" is set per-thread after the first render config push and absent on a thread's first render
        if getattr(_render_config_tls, "sig", None) == sig:
            return

        _template_set_undefined_mode(mode_int)
        _template_set_sandbox(1 if self.sandboxed else 0)
        # Push the engine-level autoescape default into the native thread-local.
        # The Zig base defaults to escaping; without this a non-default
        # autoescape value never reaches the engine. __post_init__ has already
        # rejected autoescape=False when this setter is unavailable, so when it
        # is None the value is guaranteed True and the Zig default already matches.
        if _native_set_autoescape is not None:
            _native_set_autoescape(1 if self.autoescape else 0)
        _template_set_safety_limits(
            self.max_string_len, self.max_array_count, self.max_expr_depth
        )
        if current != self._DEFAULT_DELIMS:
            _template_set_delimiters(*current)
        else:
            _template_set_delimiters("", "", "", "", "", "")  # reset to defaults
        _template_set_i18n_callback(self.i18n_callback)
        _template_set_loader(self._load_template_source)
        _render_config_tls.sig = sig

    # ── Native Zig rendering ──────────────────────────────────────────────

    def _render_native(self, template_name: str, context: dict) -> str:
        self._apply_render_config()
        capsule = self._get_compiled(template_name)
        result = _template_render(capsule, context)
        return result.decode("utf-8") if isinstance(result, bytes) else result

    def _string_cache_key(self, source: str) -> str:
        """Compiled-cache key for a template string, memoized by object identity.

        Interned/constant template strings (the common case — module-level
        TEMPLATE_* constants rendered repeatedly) are the same object each call,
        so we return the previously computed key without re-hashing the whole
        source. The stored strong ref prevents id() reuse; identity is verified.
        """
        sid = id(source)
        entry = self._string_key_cache.get(sid)
        if entry is not None and entry[0] is source:
            return entry[1]
        key = f"__string__{hashlib.md5(source.encode(), usedforsecurity=False).hexdigest()}"
        if len(self._string_key_cache) >= 1024:
            self._string_key_cache.clear()  # bound memory for dynamic strings
        self._string_key_cache[sid] = (source, key)
        return key

    def _render_string_native(self, source: str, context: dict) -> str:
        self._apply_render_config()
        key = self._string_cache_key(source)
        cached = self._compiled_cache.get(key)
        if cached is None:
            cached = _template_compile(source, "<string>")
            # Wire custom filters into the capsule BEFORE publishing it to the
            # shared cache. Publishing first (the old order) let a concurrent
            # worker `get` the capsule and render it with no custom filters
            # registered, and had `_wire_filters` mutate the native filter table
            # while that worker read it in `_template_render` (native data race).
            # Both sibling paths in `_get_compiled` wire-then-put (:826, :845).
            self._wire_filters(cached)
            self._compiled_cache.put(
                key, cached, source_size=len(source.encode("utf-8"))
            )
        result = _template_render(cached, context)
        return result.decode("utf-8") if isinstance(result, bytes) else result

    def _get_compiled(self, template_name: str):
        """Get or compile a template from the LRU cache.

        Three-tier caching: in-memory LRU → disk bytecode (.hztc) → compile from source.
        The compiled Zig node tree (PyCapsule) is cached and reused for every
        render call with different context dicts. Auto-reload checks file mtime
        when enabled (development mode).

        Bytecode cache uses a Merkle hash over the main template AND all transitive
        dependencies (includes, extends, imports). Any change in any file in the
        dependency tree invalidates the cache.

        Hot-path optimization: the resolved template_dir is computed once in
        __post_init__ and the per-template resolved path is cached in
        _resolved_template_paths to skip `Path.resolve()` (and its lstat walk)
        on every render.
        """
        # Fast path: in-memory LRU hit + no reload check needed.
        # Check the cache FIRST before any filesystem work — this is the 99%
        # case after warmup and must stay allocation-free.
        cached = self._compiled_cache.get(template_name)
        if cached is not None and not self.auto_reload:
            return cached

        # Need the resolved path — check the per-template cache to avoid
        # walking the filesystem on every render. The refresh call is a
        # zero-cost dict comparison when template_dir hasn't changed.
        base = self._refresh_resolved_base()
        template_path = self._resolved_template_paths.get(template_name)
        if template_path is None:
            template_path = (base / template_name).resolve()
            # SECURITY: Prevent path traversal
            tp_str = str(template_path)
            if (
                not tp_str.startswith(self._resolved_base_str + "/")
                and template_path != base
            ):
                raise FileNotFoundError(
                    f"Template path escapes template directory: {template_name}"
                )
            self._resolved_template_paths[template_name] = template_path

        # Auto-reload: check source mtime against cached mtime
        if cached is not None and self.auto_reload:
            cached_mtime = self._compiled_cache.get_mtime(template_name)
            try:
                current_mtime = template_path.stat().st_mtime
                if cached_mtime == current_mtime:
                    return cached
            except OSError:
                return cached

        # LRU miss or stale — read source
        source = template_path.read_text(encoding="utf-8")
        source_bytes = source.encode("utf-8")

        try:
            mtime = template_path.stat().st_mtime
        except OSError:
            mtime = None

        # Tier 2: Try disk bytecode cache with Merkle dependency validation
        if self._bytecode_dir is not None:
            capsule = self._load_bytecode_cache(template_name, source_bytes)
            if capsule is not None:
                self._disk_hits += 1
                self._wire_filters(capsule)
                self._compiled_cache.put(
                    template_name, capsule, source_size=len(source_bytes), mtime=mtime
                )
                return capsule
            self._disk_misses += 1

        # Tier 3: Compile from source — track dependencies for Merkle hash in a
        # per-compile local accumulator, so concurrent compiles on this engine
        # can't clobber each other's dependency snapshot. The Zig loader callback
        # writes into `deps` for the duration of THIS compile only.
        deps: dict[str, str] = {}
        self._compiles += 1
        prev_acc = _compile_deps_tls.acc
        _compile_deps_tls.acc = deps
        try:
            capsule = _template_compile(source, str(template_path))
        finally:
            _compile_deps_tls.acc = prev_acc
        self._wire_filters(capsule)

        # Build Merkle metadata from main source + tracked dependencies
        dep_sources = {path: src.encode("utf-8") for path, src in deps.items()}
        meta = _BytecodeMeta.build(source_bytes, dep_sources)

        # Write bytecode cache + meta to disk (atomic)
        self._save_bytecode_cache(template_name, capsule, source, meta)

        # Store in LRU
        self._compiled_cache.put(
            template_name, capsule, source_size=len(source_bytes), mtime=mtime
        )
        return capsule

    def _wire_filters(self, capsule) -> None:
        """Wire custom filters into a compiled template capsule."""
        for fname, func in self._custom_filters.items():
            _template_register_filter(capsule, fname, func)

    def _bytecode_path(self, template_name: str) -> Path | None:
        """Get the .hztc file path for a template, or None if bytecode cache disabled."""
        if self._bytecode_dir is None:
            return None
        return self._bytecode_dir / (template_name + ".hztc")

    def _meta_path(self, template_name: str) -> Path | None:
        """Get the .hztc.meta file path for a template."""
        if self._bytecode_dir is None:
            return None
        return self._bytecode_dir / (template_name + ".hztc.meta")

    def _load_dep_source(self, dep_path: str) -> bytes | None:
        """Load current source bytes for a dependency. Returns None if missing."""
        base = self._refresh_resolved_base()
        full_path = (base / dep_path).resolve()
        try:
            return full_path.read_bytes()
        except OSError:
            return None

    def _load_bytecode_cache(self, template_name: str, main_source: bytes):
        """Try to load a compiled template from disk bytecode cache.

        Validates the Merkle hash: checks that main source AND all dependencies
        still match their recorded hashes. Returns capsule on hit, None on
        miss/stale/corrupt.
        """
        cache_path = self._bytecode_path(template_name)
        meta_path = self._meta_path(template_name)
        if cache_path is None or not cache_path.exists():
            return None
        if meta_path is None or not meta_path.exists():
            return None

        try:
            # Load and validate Merkle metadata
            meta_json = meta_path.read_text(encoding="utf-8")
            meta = _BytecodeMeta.from_json(meta_json)
            if meta is None:
                return None

            # Check main source hash
            if _fnv1a_64(main_source) != meta.main_hash:
                return None

            # Validate all dependency hashes against current disk state
            if not meta.validate_deps(self._load_dep_source):
                return None

            # All deps valid — deserialize bytecode with Merkle hash
            data = cache_path.read_bytes()
            return _template_deserialize(data, meta.merkle_hash)
        # blind-except: bytecode cache load is a pure optimization; a missing, truncated, or hash-mismatched cache falls back to recompiling the template from source.
        except Exception:
            return None

    def _save_bytecode_cache(
        self, template_name: str, capsule, source: str, meta: _BytecodeMeta
    ) -> None:
        """Serialize compiled template + Merkle metadata to disk.

        Writes both .hztc (bytecode) and .hztc.meta (dependency tree) atomically.
        The .hztc is serialized with the Merkle hash so deserialization validates
        the full dependency tree in a single hash check.
        """
        cache_path = self._bytecode_path(template_name)
        meta_path = self._meta_path(template_name)
        if cache_path is None or meta_path is None:
            return
        try:
            # Serialize with Merkle hash (not just main source hash)
            # We pass source but override the hash via a wrapper source that
            # produces the merkle hash. Since _template_serialize uses the source
            # to compute FNV-1a internally, we need to pass the original source
            # and then patch the hash in the header.
            data = _template_serialize(capsule, source)
            if data is None:
                return

            # Patch the source hash in the serialized data with merkle hash.
            # Header layout: magic(4) + version(2) + reserved(2) + hash(8)
            # So hash is at bytes 8..16, little-endian u64.
            patched = bytearray(data)
            patched[8:16] = meta.merkle_hash.to_bytes(8, "little")
            data = bytes(patched)

            # Ensure directory exists
            cache_path.parent.mkdir(parents=True, exist_ok=True)

            # Write .hztc (atomic)
            self._atomic_write(cache_path, data)

            # Write .hztc.meta (atomic)
            self._atomic_write(meta_path, meta.to_json().encode("utf-8"))
        # blind-except: bytecode cache write is a pure optimization; if serialization or disk IO fails the template still renders, only the on-disk cache is skipped.
        except Exception:
            pass  # Bytecode cache write failure is non-fatal

    def _atomic_write(self, path: Path, data: bytes) -> None:
        """Write data to path atomically via tempfile + os.replace."""
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        closed = False
        try:
            os.write(fd, data)
            os.close(fd)
            closed = True
            Path(tmp_path).replace(str(path))
        except Exception:
            # Clean up the fd and dangling temp file, then re-raise so the caller
            # (not this helper) decides whether the write failure is fatal.
            if not closed:
                with contextlib.suppress(OSError):
                    os.close(fd)
            with contextlib.suppress(OSError):
                Path(tmp_path).unlink()
            raise

    def load_library(self, name: str):
        """Load a template tag library and register its filters/tags.

        Usage:
            engine.load_library("myapp.templatetags.my_tags")
            # or
            engine.load_library("my_tags")  # if registered via Library("my_tags")
        """
        with _library_registry_lock:
            lib = _library_registry.get(name)
        if lib is None:
            # Try importing the module (Django-style templatetags discovery)
            try:
                __import__(name)
                with _library_registry_lock:
                    lib = _library_registry.get(name)
            except ImportError:
                pass
        if lib is None:
            with _library_registry_lock:
                available = list(_library_registry.keys())
            raise ValueError(
                f"Template library '{name}' not found. Available: {available}"
            )

        for fname, func in lib.filters.items():
            self.add_filter(fname, func)
        for tname, func in lib.simple_tags.items():
            self.add_global(tname, func)
        for tname, func in lib.inclusion_tags.items():
            self.add_global(tname, func)


# ─── Template Tag Library System ──────────────────────────────────────────────

# Global registry of template libraries
_library_registry: dict[str, Library] = {}
_library_registry_lock = Lock()


class Library:
    """Template tag/filter library for registering custom template extensions.

    Usage:
        from hyperdjango.templating import Library

        register = Library("my_tags")

        @register.filter
        def currency(value, symbol="$"):
            return f"{symbol}{value:,.2f}"

        @register.filter("shout")
        def make_loud(value):
            return str(value).upper() + "!"

        @register.simple_tag
        def current_time(format="%H:%M"):
            from datetime import datetime
            return datetime.now().strftime(format)

        @register.simple_tag("site_name")
        def get_site_name():
            return "My Site"

    Then in templates:
        {{ price|currency }}
        {{ price|currency("€") }}
        {{ name|shout }}
        {{ current_time() }}
        {{ current_time("%Y-%m-%d") }}
        {{ site_name() }}
    """

    def __init__(self, name: str):
        self.name = name
        self.filters: dict[str, Callable] = {}
        self.simple_tags: dict[str, Callable] = {}
        self.inclusion_tags: dict[str, Callable] = {}
        # Auto-register in global registry
        with _library_registry_lock:
            _library_registry[name] = self

    def filter(self, func_or_name=None):
        """Register a template filter.

        Usage:
            @register.filter
            def my_filter(value):
                return str(value).upper()

            @register.filter("custom_name")
            def my_filter(value):
                return str(value).upper()
        """
        if func_or_name is None or callable(func_or_name):
            # @register.filter or @register.filter without parens
            func = func_or_name
            if func is not None:
                self.filters[func.__name__] = func
                return func

            # @register.filter() with no args
            def decorator(f):
                self.filters[f.__name__] = f
                return f

            return decorator

        # @register.filter("custom_name")
        name = func_or_name

        def decorator(func):
            self.filters[name] = func
            return func

        return decorator

    def simple_tag(self, func_or_name=None):
        """Register a simple template tag (callable in {{ }}).

        The function is added as a global callable in the template context.

        Usage:
            @register.simple_tag
            def now():
                from datetime import datetime
                return datetime.now().isoformat()

            # In template: {{ now() }}

            @register.simple_tag("version")
            def get_version():
                return "1.0.0"

            # In template: {{ version() }}
        """
        if func_or_name is None or callable(func_or_name):
            func = func_or_name
            if func is not None:
                self.simple_tags[func.__name__] = func
                return func

            def decorator(f):
                self.simple_tags[f.__name__] = f
                return f

            return decorator

        name = func_or_name

        def decorator(func):
            self.simple_tags[name] = func
            return func

        return decorator

    def inclusion_tag(self, template_name: str):
        """Register an inclusion tag that renders a sub-template.

        The decorated function returns a context dict used to render
        the specified template. The result is inserted into the parent.

        Usage:
            @register.inclusion_tag("_sidebar.html")
            def sidebar(user):
                return {"items": get_sidebar_items(user)}

            # In template: {{ sidebar(user) }}
        """

        def decorator(func):
            def wrapper(*args, **kwargs):
                ctx = func(*args, **kwargs)
                # Render the inclusion template with the returned context
                # This requires access to the engine — defer to render time
                return _render_inclusion(template_name, ctx)

            wrapper.__name__ = func.__name__
            wrapper.__doc__ = func.__doc__
            self.inclusion_tags[func.__name__] = wrapper
            return wrapper

        return decorator


# Compiled inclusion sub-templates, keyed by name → (mtime, capsule). An edit to
# the source (mtime change) recompiles; otherwise the compiled capsule is reused
# instead of re-reading and recompiling on every inclusion-tag invocation.
_inclusion_capsule_cache: dict[str, tuple[float, object]] = {}
_inclusion_cache_lock = Lock()


def _render_inclusion(template_name: str, context: dict) -> str:
    """Render an inclusion tag template via native Zig engine.

    The compiled template is cached (mtime-validated) so repeated invocations
    skip the read_text + compile. NOTE: the source directory is still the
    process-relative ``templates/`` — inclusion tags predate per-engine dir
    resolution and have no engine handle here.
    """
    try:
        source_path = Path("templates") / template_name
        mtime = source_path.stat().st_mtime  # raises if missing → fallback below
        with _inclusion_cache_lock:
            entry = _inclusion_capsule_cache.get(template_name)
            if entry is not None and entry[0] == mtime:
                capsule = entry[1]
            else:
                source = source_path.read_text(encoding="utf-8")
                capsule = _template_compile(source, template_name)
                _inclusion_capsule_cache[template_name] = (mtime, capsule)
        result = _template_render(capsule, context)
        return result.decode("utf-8") if isinstance(result, bytes) else result
    # blind-except: inclusion-tag rendering is best-effort; a missing or broken sub-template degrades to an HTML-comment placeholder rather than failing the page.
    except Exception:
        pass
    return f"<!-- inclusion_tag: {template_name} -->"


def get_library(name: str) -> Library:
    """Get a registered template library by name."""
    with _library_registry_lock:
        lib = _library_registry.get(name)
    if lib is None:
        raise ValueError(f"Template library '{name}' not found")
    return lib


def get_all_libraries() -> dict[str, Library]:
    """Get all registered template libraries."""
    with _library_registry_lock:
        return dict(_library_registry)
