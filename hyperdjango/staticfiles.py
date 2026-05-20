"""
Static file serving, finding, collecting, and manifest storage.

Production-ready static file pipeline inspired by Django's staticfiles,
modernized for HyperApp. Supports:

- **StaticFilesFinder**: Locate files across multiple source directories
- **StaticFilesMiddleware**: Serve with ETag, gzip, Cache-Control, If-Modified-Since
- **ManifestStaticFilesStorage**: Content-hash filenames + staticfiles.json manifest
- **collectstatic**: Collect, hash, compress, generate manifest

Usage:
    from hyperdjango.staticfiles import (
        StaticFilesMiddleware, StaticFilesFinder,
        ManifestStaticFilesStorage, get_static_url,
    )

    # Dev: serve static files with caching headers
    app.use(StaticFilesMiddleware(
        static_dirs=["static", "node_modules"],
        prefix="/static/",
    ))

    # Production: collect with hashed filenames
    storage = ManifestStaticFilesStorage(
        static_dirs=["static"],
        static_root="collected_static",
    )
    storage.collectstatic()

    # Template: resolve hashed URL
    url = get_static_url("css/styles.css")  # "/static/css/styles.a1b2c3d4e5f6.css"
"""

import contextlib
import gzip
import hashlib
import mimetypes
import os
import re
import shutil
import threading
import time
import urllib.parse
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path

# Native Zig static file helpers (optional — Python fallback if not built)
from hyperdjango._hyperdjango_native import (
    _file_read_with_hash as _native_read_with_hash,
)
from hyperdjango.conf import STATIC_FILE_MAX_AGE, get_setting

# Membership sets for hot-path checks
_GET_HEAD_METHODS = frozenset({"GET", "HEAD"})
_SUPPORTED_MANIFEST_VERSIONS = frozenset({"1.0", "1.1"})
from hyperdjango.native import fast_json_dumps, fast_json_loads
from hyperdjango.response import Response

# ---------------------------------------------------------------------------
# Content types that benefit from gzip compression
# ---------------------------------------------------------------------------

_COMPRESSIBLE_TYPES = frozenset(
    {
        "text/html",
        "text/css",
        "text/javascript",
        "text/plain",
        "text/xml",
        "application/json",
        "application/javascript",
        "application/xml",
        "application/xhtml+xml",
        "image/svg+xml",
        "application/manifest+json",
        "application/wasm",
    }
)

# File extensions for CSS/JS that may contain URL references
_CSS_EXTENSIONS = frozenset({".css"})
_JS_EXTENSIONS = frozenset({".js", ".mjs"})

# Regex patterns for URL references in CSS
_CSS_URL_PATTERN = re.compile(
    r"""url\(\s*(['"]?)(.+?)\1\s*\)""",
    re.IGNORECASE,
)
_CSS_IMPORT_PATTERN = re.compile(
    r"""@import\s+(['"])(.+?)\1""",
    re.IGNORECASE,
)

# Matches regions of CSS where url()/@import must NOT be rewritten:
# block comments, double-quoted strings, single-quoted strings, and CSS
# escape sequences. Avoids corrupting url()/@import tokens that appear inside
# string literals or comments (e.g. content: "url(fake.png)").
_CSS_IGNORED_BLOCK_PATTERN = re.compile(
    r"""(/\*.*?\*/)|("(?:\\.|[^"\\])*")|('(?:\\.|[^'\\])*')|(\\.)""",
    re.DOTALL,
)

# ---------------------------------------------------------------------------
# Static Files Finder
# ---------------------------------------------------------------------------


@dataclass
class StaticFilesFinder:
    """Locate static files across multiple source directories.

    Each directory is searched in order. First match wins.
    Supports optional prefix per directory: ("vendor", "/path/to/vendor/static")
    maps files like "vendor/jquery.js" → /path/to/vendor/static/jquery.js.
    """

    dirs: list[str | tuple[str, str]] = field(default_factory=list)
    _locations: list[tuple[str, str]] = field(default=None, init=False, repr=False)

    def __post_init__(self):
        self._locations = []
        for entry in self.dirs:
            if isinstance(entry, (list, tuple)) and len(entry) == 2:
                prefix, path = entry
                prefix = prefix.strip("/")
            else:
                prefix = ""
                path = str(entry)
            abs_path = str(Path(path).resolve())
            if Path(abs_path).is_dir():
                self._locations.append((prefix, abs_path))

    def find(self, path: str) -> str | None:
        """Find the absolute filesystem path for a static file.

        Returns None if not found.
        """
        path = path.lstrip("/")
        for prefix, root in self._locations:
            if prefix:
                if not path.startswith(prefix + "/"):
                    continue
                rel_path = path[len(prefix) + 1 :]
            else:
                rel_path = path

            # Prevent path traversal
            rel_path = os.path.normpath(rel_path)
            if rel_path.startswith("..") or Path(rel_path).is_absolute():
                continue

            # Deny dotfiles/dotdirs and the manifest — mirror list_all()'s skip
            # so that /static/.env, /static/.git/config, and staticfiles.json are
            # never disclosed even when present on disk under a static root.
            segments = rel_path.replace(os.sep, "/").split("/")
            if any(seg.startswith(".") for seg in segments):
                continue
            if segments[-1] == "staticfiles.json":
                continue

            full = str(Path(root) / rel_path)
            # Ensure resolved path is within root
            try:
                real_full = str(Path(full).resolve())
                real_root = str(Path(root).resolve())
                if (
                    not real_full.startswith(real_root + os.sep)
                    and real_full != real_root
                ):
                    continue
            except OSError, ValueError:
                continue

            if Path(full).is_file():
                return full
        return None

    def list_all(
        self, ignore_patterns: list[str] | None = None
    ) -> list[tuple[str, str]]:
        """List all static files as (relative_path, absolute_path) pairs.

        Deduplicates: first occurrence of a relative path wins.
        """
        seen = set()
        results = []
        ignore = set(ignore_patterns or [])

        for prefix, root in self._locations:
            for dirpath, dirnames, filenames in os.walk(root):
                # Skip hidden directories
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]

                for filename in filenames:
                    if filename.startswith("."):
                        continue
                    if any(filename.endswith(pat.lstrip("*")) for pat in ignore):
                        continue

                    abs_path = str(Path(dirpath) / filename)
                    rel_from_root = os.path.relpath(abs_path, root)

                    rel_path = f"{prefix}/{rel_from_root}" if prefix else rel_from_root

                    # Normalize separators
                    rel_path = rel_path.replace(os.sep, "/")

                    if rel_path not in seen:
                        seen.add(rel_path)
                        results.append((rel_path, abs_path))

        return sorted(results, key=lambda x: x[0])


# ---------------------------------------------------------------------------
# Static Files Middleware (development + production serving)
# ---------------------------------------------------------------------------


@dataclass
class StaticFilesMiddleware:
    """Serve static files with proper caching headers.

    Features:
    - ETag (MD5 of content) with If-None-Match → 304
    - If-Modified-Since → 304 based on file mtime
    - Cache-Control headers (configurable max_age)
    - Gzip compression for compressible content types
    - Immutable caching for content-hashed filenames
    - Content-type auto-detection
    - Path traversal prevention

    Usage:
        app.use(StaticFilesMiddleware(
            static_dirs=["static"],
            prefix="/static/",
            max_age=STATIC_FILE_MAX_AGE,  # 1 hour for dev
            gzip_min_size=1024,    # Compress files > 1KB
        ))

        # Production with manifest:
        app.use(StaticFilesMiddleware(
            static_root="collected_static",
            prefix="/static/",
            max_age=STATIC_FILE_IMMUTABLE_MAX_AGE,  # 1 year for hashed files
            immutable=True,
        ))
    """

    # Source directories for finding files (dev mode)
    static_dirs: list[str | tuple[str, str]] = field(default_factory=list)
    # Collected static root (production mode)
    static_root: str | None = None
    # URL prefix to intercept
    prefix: str = "/static/"
    # Cache-Control max-age in seconds
    max_age: int = STATIC_FILE_MAX_AGE
    # Add immutable directive for content-hashed files
    immutable: bool = False
    # Minimum size for gzip compression (0 = disable)
    gzip_min_size: int = 1024
    # Enable in-memory file cache for repeated serves
    use_cache: bool = True
    # Maximum cache size in bytes (0 = unlimited)
    max_cache_bytes: int = 256 * 1024 * 1024  # 256MB default

    # Internal state
    _finder: StaticFilesFinder | None = field(default=None, init=False, repr=False)
    _file_cache: OrderedDict[str, tuple[bytes, bytes | None, str, str, float]] = field(
        default_factory=OrderedDict, init=False, repr=False
    )
    _cache_bytes: int = field(default=0, init=False, repr=False)
    _cache_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def __post_init__(self):
        # Wire settings: STATIC_URL, STATIC_ROOT, STATIC_MAX_AGE, STATICFILES_DIRS
        setting_prefix = get_setting("STATIC_URL")
        if self.prefix == "/static/" and setting_prefix != "/static/":
            self.prefix = setting_prefix

        setting_root = get_setting("STATIC_ROOT")
        if self.static_root is None and setting_root:
            self.static_root = setting_root

        setting_max_age = get_setting("STATIC_MAX_AGE")
        if (
            self.max_age == STATIC_FILE_MAX_AGE
            and setting_max_age != STATIC_FILE_MAX_AGE
        ):
            self.max_age = setting_max_age

        setting_dirs = get_setting("STATICFILES_DIRS")
        if not self.static_dirs and setting_dirs:
            self.static_dirs = list(setting_dirs)

        # Wire gzip_min_size from settings if still at default
        setting_gzip = get_setting("STATICFILES_GZIP_MIN_SIZE")
        if self.gzip_min_size == 1024 and setting_gzip != 1024:
            self.gzip_min_size = int(setting_gzip)

        # Normalize prefix
        self.prefix = "/" + self.prefix.strip("/") + "/"

        # Set up finder from static_dirs or static_root
        dirs = list(self.static_dirs)
        if self.static_root and Path(self.static_root).is_dir():
            dirs.append(self.static_root)
        if dirs:
            self._finder = StaticFilesFinder(dirs=dirs)
            # Share the finder for dev-mode versioned URL resolution
            set_dev_finder(self._finder)

    async def __call__(self, request, call_next):
        """Middleware entry point."""
        path = request.path

        # Only intercept requests matching our prefix
        if not path.startswith(self.prefix):
            return await call_next(request)

        # Only serve GET/HEAD
        if request.method not in _GET_HEAD_METHODS:
            return await call_next(request)

        # Strip prefix to get relative file path
        rel_path = path[len(self.prefix) :]
        if not rel_path:
            return await call_next(request)

        # Find the file
        response = self._serve_file(rel_path, request)
        if response is not None:
            return response

        return await call_next(request)

    def _serve_file(self, rel_path: str, request) -> Response | None:
        """Serve a static file with caching headers."""
        # Percent-decode for defense in depth (e.g., %2e%2e → ..)
        rel_path = urllib.parse.unquote(rel_path)

        # Prevent path traversal
        clean = os.path.normpath(rel_path)
        if clean.startswith("..") or Path(clean).is_absolute():
            return None

        # Resolve on disk first (cheap: path join + is_file). We need the path
        # to stat-validate any cache hit against the file's current mtime.
        abs_path = None
        if self._finder:
            abs_path = self._finder.find(clean)
        if abs_path is None:
            return None

        # Current on-disk mtime — used both to validate cache entries and for
        # conditional (If-Modified-Since) responses.
        try:
            current_mtime = Path(abs_path).stat().st_mtime
        except OSError:
            current_mtime = time.time()

        # Check the file cache, but only trust it if the file hasn't changed.
        # Without this, an edited file is served stale until restart — and
        # get_static_url_versioned's ?v=<hash> cache-busts straight into the
        # SAME stale bytes because the middleware never re-reads them.
        if self.use_cache:
            with self._cache_lock:
                cached = self._file_cache.get(clean)
                if cached is not None:
                    if cached[4] == current_mtime:
                        # Fresh — move to end for LRU ordering.
                        self._file_cache.move_to_end(clean)
                    else:
                        # Stale — drop the entry and re-read below.
                        stale_size = len(cached[0]) + (
                            len(cached[1]) if cached[1] else 0
                        )
                        del self._file_cache[clean]
                        self._cache_bytes -= stale_size
                        cached = None
            if cached is not None:
                content, gzipped, content_type, etag, mtime = cached
                return self._build_response(
                    request,
                    content,
                    gzipped,
                    content_type,
                    etag,
                    mtime,
                    clean,
                )

        # Read file + compute hash in one native pass
        try:
            content, full_hash = _native_read_with_hash(abs_path)
            etag = full_hash[:12]
        except OSError, PermissionError:
            return None

        # Content type
        content_type = mimetypes.guess_type(abs_path)[0] or "application/octet-stream"

        mtime = current_mtime

        # Pre-compress for compressible types when caching (compress once, serve many)
        # When not caching, compression happens in _build_response on-demand
        gzipped = None
        if self.use_cache and self.gzip_min_size and len(content) >= self.gzip_min_size:
            base_type = content_type.split(";")[0].strip()
            if base_type in _COMPRESSIBLE_TYPES:
                gzipped = gzip.compress(content, compresslevel=6)

        # Cache for future requests (thread-safe LRU with eviction)
        if self.use_cache:
            entry_size = len(content) + (len(gzipped) if gzipped else 0)
            # A single file bigger than the whole cache budget must NOT evict
            # every other entry and then insert itself anyway (that thrashes
            # the cache to hold one oversized file). Skip caching it entirely
            # and serve it directly.
            oversized = self.max_cache_bytes > 0 and entry_size > self.max_cache_bytes
            if not oversized:
                with self._cache_lock:
                    if clean not in self._file_cache:
                        # Evict LRU entries until there's room
                        while (
                            self.max_cache_bytes > 0
                            and self._cache_bytes + entry_size > self.max_cache_bytes
                            and self._file_cache
                        ):
                            evicted_key, evicted_entry = self._file_cache.popitem(
                                last=False
                            )
                            evicted_size = len(evicted_entry[0]) + (
                                len(evicted_entry[1]) if evicted_entry[1] else 0
                            )
                            self._cache_bytes -= evicted_size

                        self._file_cache[clean] = (
                            content,
                            gzipped,
                            content_type,
                            etag,
                            mtime,
                        )
                        self._cache_bytes += entry_size

        return self._build_response(
            request, content, gzipped, content_type, etag, mtime, clean
        )

    def _build_response(
        self,
        request,
        content: bytes,
        gzipped: bytes | None,
        content_type: str,
        etag: str,
        mtime: float,
        path: str,
    ) -> Response:
        """Build response with caching headers, conditional responses, and compression."""
        quoted_etag = f'"{etag}"'

        # Check If-None-Match → 304
        if_none_match = request.headers.get("if-none-match", "")
        if quoted_etag in if_none_match or if_none_match == "*":
            return Response(
                body=b"",
                status=304,
                headers={
                    "etag": quoted_etag,
                    "cache-control": self._cache_control(path),
                    "x-content-type-options": "nosniff",
                },
            )

        # Check If-Modified-Since → 304
        if_modified_since = request.headers.get("if-modified-since", "")
        if if_modified_since:
            try:
                ims_dt = parsedate_to_datetime(if_modified_since)
                if mtime <= ims_dt.timestamp():
                    return Response(
                        body=b"",
                        status=304,
                        headers={
                            "etag": quoted_etag,
                            "cache-control": self._cache_control(path),
                            "x-content-type-options": "nosniff",
                        },
                    )
            except ValueError, TypeError:
                pass

        # Build headers
        headers = {
            "content-type": content_type,
            "etag": quoted_etag,
            "cache-control": self._cache_control(path),
            "content-length": str(len(content)),
            "vary": "Accept-Encoding",
            # Advertise byte-range support so clients (video/audio players,
            # resumable downloaders) know they may issue Range requests.
            "accept-ranges": "bytes",
            # MIME-sniffing hardening: stop browsers from re-interpreting a
            # generic/octet-stream asset as executable HTML/JS. Inherited by the
            # 206 range branch below (range_headers = dict(headers)).
            "x-content-type-options": "nosniff",
        }

        # Last-Modified
        with contextlib.suppress(ValueError, OSError):
            headers["last-modified"] = formatdate(mtime, usegmt=True)

        # Byte-Range handling (single range only). Ranges apply to the identity
        # representation, so a satisfiable Range short-circuits compression and
        # returns 206 with a slice; an unsatisfiable one returns 416.
        range_header = request.headers.get("range", "")
        # If-Range (RFC 7233 §3.2): when a client resumes a download it may send
        # `If-Range: <validator>` alongside `Range:`. If the validator still
        # matches the current representation, honor the Range (206); if the
        # representation CHANGED, the Range must be IGNORED and the full 200 body
        # returned (never a 206 slice of the new file). Only a strong comparison
        # is valid for If-Range, so weak etags (W/"...") never match.
        if range_header and request.headers.get("if-range", ""):
            if_range = request.headers.get("if-range", "").strip()
            if if_range.startswith('"'):
                # Entity-tag validator — strong comparison against current ETag.
                range_applicable = if_range == quoted_etag
            elif if_range.startswith("W/"):
                # Weak validator is not usable with If-Range → ignore Range.
                range_applicable = False
            else:
                # HTTP-date validator — compare against Last-Modified (1s res).
                try:
                    ir_dt = parsedate_to_datetime(if_range)
                    range_applicable = int(mtime) == int(ir_dt.timestamp())
                except ValueError, TypeError:
                    range_applicable = False
            if not range_applicable:
                # Representation changed — drop the Range, serve full 200 below.
                range_header = ""
        if range_header:
            parsed = self._parse_byte_range(range_header, len(content))
            if parsed == "invalid":
                return Response(
                    body=b"",
                    status=416,
                    headers={
                        "content-range": f"bytes */{len(content)}",
                        "content-type": content_type,
                        "accept-ranges": "bytes",
                        "etag": quoted_etag,
                        "cache-control": self._cache_control(path),
                        "x-content-type-options": "nosniff",
                    },
                )
            if parsed is not None:
                start, end = parsed
                slice_body = content[start : end + 1]
                range_headers = dict(headers)
                range_headers["content-length"] = str(len(slice_body))
                range_headers["content-range"] = f"bytes {start}-{end}/{len(content)}"
                if request.method == "HEAD":
                    slice_body = b""
                return Response(body=slice_body, status=206, headers=range_headers)

        # Serve compressed content if client accepts gzip
        body = content
        accept_encoding = request.headers.get("accept-encoding", "")
        if self._accepts_gzip(accept_encoding):
            if gzipped is not None:
                # Use pre-compressed version (from cache)
                body = gzipped
                headers["content-encoding"] = "gzip"
                headers["content-length"] = str(len(body))
            elif self.gzip_min_size and len(content) >= self.gzip_min_size:
                # Compress on-demand (use_cache=False path)
                base_type = content_type.split(";")[0].strip()
                if base_type in _COMPRESSIBLE_TYPES:
                    body = gzip.compress(content, compresslevel=6)
                    headers["content-encoding"] = "gzip"
                    headers["content-length"] = str(len(body))

        # HEAD request: return headers only
        if request.method == "HEAD":
            body = b""

        return Response(body=body, status=200, headers=headers)

    @staticmethod
    def _accepts_gzip(accept_encoding: str) -> bool:
        """Return True if the client accepts gzip per RFC 7231 §5.3.4.

        A plain substring test (``"gzip" in header``) is wrong: it treats
        ``gzip;q=0`` — an *explicit refusal* — as acceptance. Parse each
        ``token;q=value`` pair (token match case-insensitive) and honor
        q-values: gzip is acceptable when it is listed with q>0, or when no
        explicit gzip entry exists but a wildcard ``*`` has q>0.
        """
        if not accept_encoding:
            return False
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
                            q = float(param[2:].strip())
                        except ValueError:
                            q = 0.0
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
    def _parse_byte_range(range_header: str, size: int):
        """Parse a single HTTP byte range against a content length.

        Returns ``(start, end)`` inclusive for a satisfiable range, the string
        ``"invalid"`` for a syntactically-valid-but-unsatisfiable range (→ 416),
        or ``None`` when the header isn't a single-range request we handle (the
        caller then serves the full body). Multi-range requests are declined
        (None) so the whole file is served — always correct, just not optimal.
        """
        if not range_header.startswith("bytes="):
            return None
        spec = range_header[len("bytes=") :].strip()
        if "," in spec or "-" not in spec:
            return None
        start_s, _, end_s = spec.partition("-")
        try:
            if start_s == "":
                # Suffix range: final N bytes.
                n = int(end_s)
                if n <= 0:
                    return "invalid"
                start = max(0, size - n)
                end = size - 1
            else:
                start = int(start_s)
                end = int(end_s) if end_s else size - 1
        except ValueError:
            return None
        if size == 0 or start >= size or start > end:
            return "invalid"
        end = min(end, size - 1)
        return (start, end)

    def _cache_control(self, path: str) -> str:
        """Generate Cache-Control header value."""
        parts = [f"max-age={self.max_age}", "public"]
        # Content-hashed filenames are immutable
        if self.immutable or self._is_hashed_filename(path):
            parts.append("immutable")
        return ", ".join(parts)

    @staticmethod
    def _is_hashed_filename(path: str) -> bool:
        """Check if filename contains a content hash (e.g., styles.a1b2c3d4e5f6.css).

        Requires exactly STATICFILES_HASH_LENGTH hex chars (matching
        ManifestStaticFilesStorage.hash_length — the generation side) to
        minimize false positives on filenames like chart.d3.js. Recognition
        must track generation: if the setting is changed to 8 or 16, hashed
        assets must still be recognized so the ``immutable`` Cache-Control is
        applied.
        """
        hash_len = int(get_setting("STATICFILES_HASH_LENGTH", 12))
        name = Path(path).name
        parts = name.rsplit(".", 2)
        if len(parts) >= 3:
            potential_hash = parts[-2]
            if len(potential_hash) == hash_len and all(
                c in "0123456789abcdef" for c in potential_hash
            ):
                return True
        return False

    def clear_cache(self):
        """Clear the in-memory file cache."""
        with self._cache_lock:
            self._file_cache.clear()
            self._cache_bytes = 0


# ---------------------------------------------------------------------------
# Manifest Static Files Storage
# ---------------------------------------------------------------------------


@dataclass
class ManifestStaticFilesStorage:
    """Collect static files with content-hash filenames and a JSON manifest.

    Generates filenames like `css/styles.a1b2c3d4e5f6.css` where the hash
    is the first 12 hex chars of the MD5 digest. Writes a `staticfiles.json`
    manifest mapping original names to hashed names.

    CSS files are post-processed to rewrite url() references to their
    hashed equivalents.

    Usage:
        storage = ManifestStaticFilesStorage(
            static_dirs=["static", "node_modules/bootstrap/dist"],
            static_root="collected_static",
        )

        # Collect all files
        result = storage.collectstatic()
        print(f"Collected {result['copied']} files")

        # Look up hashed URL
        url = storage.url("css/styles.css")
        # → "css/styles.a1b2c3d4e5f6.css"
    """

    static_dirs: list[str | tuple[str, str]] = field(default_factory=list)
    static_root: str = "staticfiles"
    manifest_name: str = "staticfiles.json"
    hash_length: int = 12
    max_post_process_passes: int = 5

    # Internal state
    _finder: StaticFilesFinder | None = field(default=None, init=False, repr=False)
    _manifest: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _manifest_loaded: bool = field(default=False, init=False, repr=False)

    def __post_init__(self):
        self._finder = StaticFilesFinder(dirs=self.static_dirs)
        # Wire settings if fields are at defaults
        setting_hash_len = get_setting("STATICFILES_HASH_LENGTH")
        if self.hash_length == 12 and setting_hash_len != 12:
            self.hash_length = int(setting_hash_len)
        setting_passes = get_setting("STATICFILES_MAX_POST_PROCESS_PASSES")
        if self.max_post_process_passes == 5 and setting_passes != 5:
            self.max_post_process_passes = int(setting_passes)

    def collectstatic(
        self, clear: bool = False, dry_run: bool = False
    ) -> dict[str, int | list[str]]:
        """Collect all static files into static_root with hashed filenames.

        Returns dict with stats: {copied, skipped, post_processed, errors}.
        """
        root = str(Path(self.static_root).resolve())

        if clear and not dry_run and Path(root).exists():
            shutil.rmtree(root)

        if not dry_run:
            Path(root).mkdir(parents=True, exist_ok=True)

        # Find all source files
        all_files = self._finder.list_all()

        stats = {"copied": 0, "skipped": 0, "post_processed": 0, "errors": []}
        hashed_files = {}

        # Phase 1: Copy and hash all non-CSS/JS files
        adjustable = []
        for rel_path, abs_path in all_files:
            ext = Path(rel_path).suffix.lower()
            if ext in _CSS_EXTENSIONS or ext in _JS_EXTENSIONS:
                adjustable.append((rel_path, abs_path))
            else:
                try:
                    content = Path(abs_path).read_bytes()
                    hashed_name = self._hashed_name(rel_path, content)
                    if not dry_run:
                        self._save_file(root, hashed_name, content)
                        self._save_file(root, rel_path, content)
                    hashed_files[rel_path] = hashed_name
                    stats["copied"] += 1
                # blind-except: collectstatic records a per-file failure in stats and continues collecting the remaining files rather than aborting.
                except Exception as e:
                    stats["errors"].append(f"{rel_path}: {e}")

        # Phase 2: Process CSS/JS files (may reference other files)
        # Multiple passes to resolve transitive references
        for pass_num in range(self.max_post_process_passes):
            changed = False
            for rel_path, abs_path in adjustable:
                try:
                    content = Path(abs_path).read_bytes()

                    ext = Path(rel_path).suffix.lower()
                    if ext in _CSS_EXTENSIONS:
                        # Only CSS is rewritten. Decode strictly — a non-UTF-8
                        # stylesheet (e.g. legacy latin-1) must NOT be mangled by
                        # errors="replace" (that would corrupt bytes into U+FFFD
                        # and change the file). If it isn't valid UTF-8 we can't
                        # safely rewrite url() refs, so copy the bytes verbatim.
                        try:
                            text = content.decode("utf-8")
                        except UnicodeDecodeError:
                            new_content = content
                        else:
                            text = self._rewrite_css_urls(text, rel_path, hashed_files)
                            new_content = text.encode("utf-8")
                    else:
                        # JS and other adjustable assets are copied byte-for-byte.
                        new_content = content

                    new_hashed = self._hashed_name(rel_path, new_content)

                    old_hashed = hashed_files.get(rel_path)
                    if old_hashed != new_hashed:
                        changed = True

                    hashed_files[rel_path] = new_hashed
                    if not dry_run:
                        self._save_file(root, new_hashed, new_content)
                        self._save_file(root, rel_path, new_content)

                    if pass_num == 0:
                        stats["copied"] += 1
                # blind-except: collectstatic records a per-file post-processing failure in stats and continues with the remaining assets.
                except Exception as e:
                    stats["errors"].append(f"{rel_path}: {e}")

            stats["post_processed"] += 1
            if not changed:
                break

        # Phase 3: Write manifest
        self._manifest = hashed_files
        self._manifest_loaded = True
        if not dry_run:
            self._save_manifest(root)

        return stats

    def _hashed_name(self, rel_path: str, content: bytes) -> str:
        """Generate a hashed filename: name.hash.ext."""
        digest = hashlib.md5(content, usedforsecurity=False).hexdigest()[
            : self.hash_length
        ]
        p = Path(rel_path)
        return str(p.with_name(f"{p.stem}.{digest}{p.suffix}"))

    def _save_file(self, root: str, rel_path: str, content: bytes):
        """Save a file to the static root atomically (write temp, then rename).

        collectstatic makes several passes and re-writes the same destinations;
        a plain open("wb") leaves a truncated/partial file visible if the
        process is interrupted mid-write. Writing to a unique temp file in the
        same directory and atomically renaming means readers only ever see the
        complete previous or complete new file.
        """
        dest = Path(root) / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.parent / f".{dest.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
        try:
            with tmp.open("wb") as f:
                f.write(content)
            tmp.replace(dest)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    @staticmethod
    def get_ignored_blocks(content: str) -> list[tuple[int, int]]:
        """Return (start, end) spans of CSS regions that must not be rewritten.

        Covers block comments, double/single-quoted string literals, and CSS
        escape sequences. url()/@import tokens inside these regions are part of
        string data or comments and must be left verbatim.
        """
        return [m.span() for m in _CSS_IGNORED_BLOCK_PATTERN.finditer(content)]

    @staticmethod
    def is_in_ignored_block(pos: int, blocks: list[tuple[int, int]]) -> bool:
        """Return True if pos falls inside any ignored (start, end) span."""
        for start, end in blocks:
            if start <= pos < end:
                return True
            if start > pos:
                # Spans are sorted ascending; no later span can contain pos.
                break
        return False

    def _rewrite_css_urls(
        self, css_text: str, css_path: str, hashed_files: dict[str, str]
    ) -> str:
        """Rewrite url() and @import references in CSS to hashed filenames.

        url()/@import tokens inside string literals or comments are skipped so
        that e.g. content: "url(fake.png)" is left verbatim.
        """
        css_dir = str(Path(css_path).parent)
        ignored_blocks = self.get_ignored_blocks(css_text)

        def replace_url(match):
            if self.is_in_ignored_block(match.start(), ignored_blocks):
                return match.group(0)
            quote = match.group(1)
            url = match.group(2)
            resolved = self._resolve_css_url(url, css_dir, hashed_files)
            if resolved != url:
                return f"url({quote}{resolved}{quote})"
            return match.group(0)

        def replace_import(match):
            if self.is_in_ignored_block(match.start(), ignored_blocks):
                return match.group(0)
            quote = match.group(1)
            url = match.group(2)
            resolved = self._resolve_css_url(url, css_dir, hashed_files)
            if resolved != url:
                return f"@import {quote}{resolved}{quote}"
            return match.group(0)

        css_text = _CSS_URL_PATTERN.sub(replace_url, css_text)
        css_text = _CSS_IMPORT_PATTERN.sub(replace_import, css_text)
        return css_text

    @staticmethod
    def _resolve_css_url(url: str, css_dir: str, hashed_files: dict[str, str]) -> str:
        """Resolve a CSS url() to its hashed equivalent."""
        # Skip absolute URLs, data URIs, protocol-relative, fragments
        if url.startswith(("http://", "https://", "//", "data:", "#")):
            return url

        # Strip query string and fragment for lookup
        clean_url = url.split("?")[0].split("#")[0]

        # Resolve relative to CSS file's directory
        if not clean_url.startswith("/"):
            resolved = os.path.normpath(str(Path(css_dir) / clean_url))
        else:
            resolved = clean_url.lstrip("/")

        # Normalize separators
        resolved = resolved.replace(os.sep, "/")

        # Look up in hashed files
        hashed = hashed_files.get(resolved)
        if hashed is None:
            return url

        # Reconstruct relative path from CSS dir to hashed file
        if not url.startswith("/"):
            try:
                hashed_rel = os.path.relpath(hashed, css_dir).replace(os.sep, "/")
            except ValueError:
                hashed_rel = hashed
        else:
            hashed_rel = "/" + hashed

        # Re-append query string and/or fragment
        suffix = ""
        qs_idx = url.find("?")
        hash_idx = url.find("#")
        if qs_idx != -1:
            suffix = url[qs_idx:]
        elif hash_idx != -1:
            suffix = url[hash_idx:]

        return hashed_rel + suffix

    def _save_manifest(self, root: str):
        """Write staticfiles.json manifest."""
        sorted_paths = dict(sorted(self._manifest.items()))

        # Compute manifest-level hash
        pairs_json = fast_json_dumps(list(sorted_paths.items()))
        if isinstance(pairs_json, bytes):
            pairs_json = pairs_json.decode("utf-8")
        manifest_hash = hashlib.md5(
            pairs_json.encode("utf-8"), usedforsecurity=False
        ).hexdigest()[: self.hash_length]

        manifest = {
            "version": "1.1",
            "paths": sorted_paths,
            "hash": manifest_hash,
        }

        manifest_path = Path(root) / self.manifest_name
        manifest_json = fast_json_dumps(manifest)
        if isinstance(manifest_json, bytes):
            manifest_json = manifest_json.decode("utf-8")

        with manifest_path.open("w", encoding="utf-8") as f:
            f.write(manifest_json)

    def load_manifest(self) -> dict[str, str]:
        """Load the manifest from disk."""
        if self._manifest_loaded:
            return self._manifest

        manifest_path = Path(self.static_root).resolve() / self.manifest_name
        if not manifest_path.exists():
            self._manifest = {}
            self._manifest_loaded = True
            return self._manifest

        with manifest_path.open(encoding="utf-8") as f:
            raw = f.read()

        data = fast_json_loads(raw)
        version = data.get("version", "1.0")
        if version not in _SUPPORTED_MANIFEST_VERSIONS:
            raise ValueError(f"Unsupported manifest version: {version}")

        self._manifest = data.get("paths", {})
        self._manifest_loaded = True
        return self._manifest

    def url(self, name: str) -> str:
        """Look up the hashed filename for a static file.

        Returns the hashed name if found in manifest, otherwise the original name.
        """
        manifest = self.load_manifest()
        return manifest.get(name, name)

    def stored_name(self, name: str, strict: bool = True) -> str:
        """Look up hashed name. Raises ValueError if strict and not found."""
        manifest = self.load_manifest()
        hashed = manifest.get(name)
        if hashed is None and strict:
            raise ValueError(f"Missing staticfiles manifest entry for '{name}'")
        return hashed or name


# ---------------------------------------------------------------------------
# Global manifest instance + template helper
# ---------------------------------------------------------------------------

_manifest_storage: ManifestStaticFilesStorage | None = None


def get_manifest_storage() -> ManifestStaticFilesStorage | None:
    """Get the global manifest storage instance."""
    return _manifest_storage


def set_manifest_storage(storage: ManifestStaticFilesStorage):
    """Set the global manifest storage instance."""
    global _manifest_storage
    _manifest_storage = storage


def get_static_url(name: str, prefix: str = "/static/") -> str:
    """Resolve a static file name to its URL (with hash if manifest loaded).

    This is the function behind {% static 'path' %} in templates.

    Usage:
        url = get_static_url("css/styles.css")
        # → "/static/css/styles.a1b2c3d4e5f6.css" (production with manifest)
        # → "/static/css/styles.css" (dev without manifest)
    """
    storage = _manifest_storage
    resolved = storage.url(name) if storage is not None else name

    prefix = prefix.rstrip("/") + "/"
    return f"{prefix}{resolved}"


# ---------------------------------------------------------------------------
# Dev-mode versioned URL helper (appends ?v=<content_hash>)
# ---------------------------------------------------------------------------

_dev_finder: StaticFilesFinder | None = None
# Bounded cache: max 4096 entries to prevent memory leak in dev servers
# with many static files. Uses OrderedDict for O(1) LRU eviction.
_dev_hash_cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
_dev_hash_lock = threading.Lock()
_DEV_HASH_CACHE_MAX: int = 0  # lazy-initialized on first use


def get_dev_finder() -> StaticFilesFinder | None:
    """Get the global dev-mode file finder."""
    return _dev_finder


def set_dev_finder(finder: StaticFilesFinder) -> None:
    """Set the global dev-mode file finder (called by StaticFilesMiddleware)."""
    global _dev_finder
    _dev_finder = finder


def get_static_url_versioned(name: str, prefix: str = "/static/") -> str:
    """Resolve a static file URL with cache-busting version parameter.

    This is the function behind ``{{ static_url('path') }}`` in templates.

    In production (manifest loaded): delegates to ``get_static_url()``
    which returns content-hash filenames (already cache-safe).

    In dev mode (no manifest): appends ``?v=<content_hash[:12]>`` to the
    URL, computed from the file's actual content with mtime-based cache
    invalidation to avoid re-hashing on every render.

    Usage::

        url = get_static_url_versioned("css/styles.css")
        # Production: "/static/css/styles.a1b2c3d4e5f6.css"
        # Dev mode:   "/static/css/styles.css?v=a1b2c3d4e5f6"
    """
    # Production: manifest available — use hashed filenames
    storage = _manifest_storage
    if storage is not None:
        return get_static_url(name, prefix)

    # Dev mode: compute ?v=hash from file content
    prefix = prefix.rstrip("/") + "/"
    base_url = f"{prefix}{name}"

    if not get_setting("STATIC_DEV_VERSION_QUERY", True):
        return base_url

    finder = _dev_finder
    if finder is None:
        return base_url

    abs_path = finder.find(name)
    if abs_path is None:
        return base_url

    # Mtime-based cache: only re-hash when file changes
    try:
        mtime = Path(abs_path).stat().st_mtime
    except OSError:
        return base_url

    with _dev_hash_lock:
        cached = _dev_hash_cache.get(abs_path)
        if cached is not None and cached[1] == mtime:
            return f"{base_url}?v={cached[0]}"

    # Compute content hash
    try:
        content, full_hash = _native_read_with_hash(abs_path)
    # blind-except: if the native read+hash path fails for any reason, fall back to the pure-Python read+hash below, which is identical.
    except Exception:
        # Fallback: read and hash in Python
        try:
            with Path(abs_path).open("rb") as f:
                content = f.read()
            full_hash = hashlib.md5(content, usedforsecurity=False).hexdigest()
        except OSError:
            return base_url

    short_hash = full_hash[:12]

    with _dev_hash_lock:
        global _DEV_HASH_CACHE_MAX
        if _DEV_HASH_CACHE_MAX == 0:
            _DEV_HASH_CACHE_MAX = int(get_setting("STATICFILES_DEV_HASH_CACHE_MAX"))
        _dev_hash_cache[abs_path] = (short_hash, mtime)
        # LRU eviction: remove oldest entry when cache exceeds max
        while len(_dev_hash_cache) > _DEV_HASH_CACHE_MAX:
            _dev_hash_cache.popitem(last=False)

    return f"{base_url}?v={short_hash}"
