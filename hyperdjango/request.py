"""
Lightweight HTTP Request object.

No Django dependency. Designed for speed — minimal allocations,
lazy parsing of body/query/cookies.
"""

import contextlib
import os
import tempfile as _tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from hyperdjango._hyperdjango_native import _read_body_chunk
from hyperdjango.client_ip import resolve_client_ip
from hyperdjango.conf import get_setting
from hyperdjango.exceptions import HTTPException
from hyperdjango.types import JSONValue

if TYPE_CHECKING:
    from hyperdjango.app import HyperApp
    from hyperdjango.guard.types import GuardContext
    from hyperdjango.validation.core import BaseModel as _BaseModel


class AsgiScope(TypedDict, total=False):
    """ASGI HTTP connection scope."""

    type: str
    asgi: dict[str, str]
    http_version: str
    method: str
    path: str
    root_path: str
    scheme: str
    query_string: bytes
    headers: list[tuple[bytes, bytes]]
    client: tuple[str, int]
    server: tuple[str, int]
    path_params: dict[str, str]


from hyperdjango._hyperdjango_native import (
    parse_multipart_native as _parse_multipart,
)
from hyperdjango.native import (
    fast_json_loads,
    parse_cookies,
    parse_query_string,
)


def _parse_cookies_lenient(header: str) -> dict[str, str]:
    """Pure-Python, never-crashing fallback cookie parser.

    Used when the native parser rejects a malformed/non-UTF-8 Cookie header.
    Splits on ';' / '=' and returns whatever name=value pairs it can recover —
    a hostile header degrades to a best-effort parse, never an exception.
    """
    out: dict[str, str] = {}
    for part in header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        if name:
            out[name] = value.strip()
    return out


@dataclass
class Request:
    """HTTP request object.

    Attributes:
        method: HTTP method (GET, POST, PUT, PATCH, DELETE, etc.)
        path: URL path (e.g., /users/123)
        headers: Dict of HTTP headers (case-insensitive keys)
        query_string: Raw query string
        body: Raw request body bytes
        path_params: Dict of path parameters extracted by the router
        scope: ASGI scope dict (if available)
    """

    method: str = "GET"
    path: str = "/"
    headers: CaseInsensitiveDict | dict[str, str] = field(default_factory=dict)
    query_string: str = ""
    body: bytes = b""
    path_params: dict[str, str] = field(default_factory=dict)
    scope: AsgiScope | None = None

    # App reference (set at the dispatch boundary, used by shortcuts.render)
    app: HyperApp | None = None

    # Correlation id minted at the dispatch boundary (honors an inbound
    # X-Request-ID / W3C traceparent, else a live telemetry trace id, else a
    # fresh uuid4). It is echoed as the X-Request-ID response header and injected
    # into the framework log context for the request scope. Declared as a real
    # field (never set via setattr) so BOTH dispatch paths and AccessLogMiddleware
    # read the same attribute. See HyperApp dispatch boundary (_resolve_request_id).
    request_id: str | None = None

    # The version of the page that issued this request, parsed by
    # VersionMiddleware from the X-Client-Version header (or the
    # hyper_client_version cookie) when APP_VERSION_CLIENT_BROADCAST is on.
    # "" when the client sent nothing or the broadcast feature is off.
    # Declared as a real field (never set via setattr) so handlers and
    # downstream middleware read the same attribute. See
    # hyperdjango.versioning.client_version() for the standalone parser.
    client_version: str = ""

    # Auth fields (set by SessionAuth: request.user + optional request._perm_checker)
    user: Any = None
    session_id: str | None = None
    # Session data bridge (set by SessionAuth to a dict-backed store proxy).
    # None when no session backend is active; flash messages and other session
    # consumers read/write it and SessionAuth persists mutations post-response.
    session: Any = None
    api_key: str | None = None
    api_key_valid: bool = False
    hyper_validated: _BaseModel | None = None
    hyper_data: JSONValue = None
    oauth2_provider: str | None = None

    # Guard context (set by @guard decorator, see hyperdjango.guard)
    guard: GuardContext | None = None

    # Middleware-injected fields (set at request time, never by constructor)
    _perm_checker: object | None = field(default=None, init=False, repr=False)
    _admin_user: object | None = field(default=None, init=False, repr=False)
    _admin_session_id: str | None = field(default=None, init=False, repr=False)

    # Lazy-parsed caches (private)
    _query_params: dict[str, list[str]] | None = field(
        default=None, init=False, repr=False
    )
    _json: JSONValue = field(default=None, init=False, repr=False)
    _form: dict[str, str] | None = field(default=None, init=False, repr=False)
    _files: dict[str, bytes] | None = field(default=None, init=False, repr=False)
    _cookies: dict[str, str] | None = field(default=None, init=False, repr=False)
    _get_dict: dict[str, str] | None = field(default=None, init=False, repr=False)
    # Pre-parsed multipart parts from Zig server (zero-copy fast path).
    # When set, _parse_multipart() skips the FFI round-trip entirely.
    _multipart_parts: list[tuple[str, str | None, str, bytes]] | None = field(
        default=None, init=False, repr=False
    )
    # Streaming body: total Content-Length for bodies exceeding MAX_BODY_SIZE.
    # When > 0, request.stream() pulls chunks from the Zig socket reader
    # via _read_body_chunk() FFI — true streaming, bounded memory.
    _stream_content_length: int = field(default=0, init=False, repr=False)

    def __post_init__(self):
        self.method = self.method.upper()
        if not isinstance(self.headers, CaseInsensitiveDict):
            self.headers = CaseInsensitiveDict(self.headers or {})
        if isinstance(self.body, str):
            self.body = self.body.encode("utf-8")
        elif self.body is None:
            self.body = b""

    @property
    def query_params(self):
        """Parse query string into dict of lists.

        Uses SIMD-accelerated parser when native extension available.
        Enforces DATA_UPLOAD_MAX_NUMBER_FIELDS on the query string, mirroring
        the cap already applied to form bodies — an unbounded GET query string
        (``?a=1&a=2&…`` repeated) is otherwise a memory/CPU DoS vector.
        """
        if self._query_params is None:
            parsed = parse_query_string(self.query_string)
            max_fields = get_setting("DATA_UPLOAD_MAX_NUMBER_FIELDS")
            if max_fields and parsed:
                total_fields = sum(
                    len(v) if isinstance(v, list) else 1 for v in parsed.values()
                )
                if total_fields > max_fields:
                    raise HTTPException(
                        400,
                        f"Too many query-string fields ({total_fields}). "
                        f"Maximum is {max_fields}.",
                    )
            self._query_params = parsed
        return self._query_params

    def query(self, key, default=None):
        """Get a single query parameter value.

        Usage: request.query("page", "1")
        """
        values = self.query_params.get(key)
        return values[0] if values else default

    @property
    def GET(self):
        """Flat query dict (first value per key).

        Provides dict-like access to query parameters:
            request.GET.get("page", "1")
            request.GET["q"]
        """
        if self._get_dict is None:
            self._get_dict = {
                k: v[0] if v else "" for k, v in self.query_params.items()
            }
        return self._get_dict

    async def text(self) -> str:
        """Get body as text string."""
        return self.body.decode("utf-8", errors="replace") if self.body else ""

    async def bytes(self) -> bytes:
        """Get raw body as bytes."""
        return self.body

    async def stream(self, chunk_size: int = 0) -> AsyncIterator[bytes]:
        """Stream request body in chunks directly from the TCP socket.

        For pass-through proxying to external services (S3, CDN) without
        buffering the full body in memory. The Zig server reads chunks
        from the socket on demand — bounded memory, zero disk::

            @app.post("/proxy-to-s3")
            async def proxy(request):
                async for chunk in request.stream():
                    await s3_client.write(chunk)

        When the body is already buffered (small requests, TestClient,
        ASGI), yields the buffered body in chunks instead.
        """
        if chunk_size == 0:
            chunk_size = int(get_setting("STREAM_BODY_CHUNK_SIZE"))

        # True streaming: pull chunks from Zig via FFI (large bodies)
        if self._stream_content_length > 0:
            while True:
                chunk = _read_body_chunk(chunk_size)
                if not chunk:
                    break
                yield chunk
            return

        # Fallback: yield buffered body in chunks (small bodies, TestClient)
        if self.body:
            data = self.body
            for i in range(0, len(data), chunk_size):
                yield data[i : i + chunk_size]

    @property
    def peer_ip(self) -> str:
        """The socket peer address from the ASGI scope, ignoring all headers.

        This is the only IP an attacker cannot spoof (short of spoofing at the
        TCP layer). ``client_ip`` falls back to this whenever forwarding headers
        are not trusted.
        """
        if self.scope:
            client = self.scope.get("client")
            if client:
                return client[0]
        return "127.0.0.1"

    @property
    def client_ip(self) -> str:
        """Best-effort real client IP, resistant to X-Forwarded-For spoofing.

        SECURITY: X-Forwarded-For / X-Real-IP are attacker-controlled unless the
        request actually passed through a reverse proxy we trust. Trusting them
        unconditionally lets an attacker present a unique IP per request —
        defeating IP-based rate limiting AND growing per-IP rate-limit buckets
        without bound (memory DoS). So forwarding headers are honored ONLY when:

          - ``TRUSTED_PROXY_COUNT`` > 0 (you run N reverse-proxy hops), or
          - the socket peer is listed in ``TRUSTED_PROXIES``.

        With neither configured (the default), the socket peer address is used.
        """
        return resolve_client_ip(
            self.peer_ip,
            self.headers.get("x-forwarded-for"),
            self.headers.get("x-real-ip"),
        )

    @property
    def is_secure(self) -> bool:
        """True if the request was made over HTTPS.

        Checks SECURE_PROXY_SSL_HEADER from conf.py first (e.g. X-Forwarded-Proto),
        then falls back to ASGI scope scheme, then x-forwarded-proto header.
        """
        proxy_header = get_setting("SECURE_PROXY_SSL_HEADER")
        if proxy_header:
            header_val = self.headers.get(proxy_header.lower(), "")
            if header_val == "https":
                return True
            if header_val:
                return False
        if self.scope:
            return self.scope.get("scheme") == "https"
        return self.headers.get("x-forwarded-proto") == "https"

    @property
    def host(self) -> str:
        """Get the request host, respecting USE_X_FORWARDED_HOST setting."""
        if get_setting("USE_X_FORWARDED_HOST"):
            forwarded_host = self.headers.get("x-forwarded-host", "")
            if forwarded_host:
                return forwarded_host.split(",")[0].strip()
        return self.headers.get("host", "")

    @property
    def port(self) -> str:
        """Get the request port, respecting USE_X_FORWARDED_PORT setting."""
        if get_setting("USE_X_FORWARDED_PORT"):
            forwarded_port = self.headers.get("x-forwarded-port", "")
            if forwarded_port:
                return forwarded_port.strip()
        host = self.headers.get("host", "")
        if ":" in host:
            return host.rsplit(":", 1)[1]
        if self.scope:
            server = self.scope.get("server")
            if server:
                return str(server[1])
        return "443" if self.is_secure else "80"

    async def json(self):
        """Parse body as JSON.

        Uses SIMD-accelerated parser when native extension available.
        Raises HTTPException(400) on invalid JSON instead of 500.

        An empty request body is not valid JSON (``json.loads("")`` raises),
        so it also yields a 400 rather than silently returning ``None`` — which
        every caller would then trip over with ``None.get(...)`` → 500.
        """
        if self._json is None:
            if not self.body:
                raise HTTPException(400, "Empty request body (expected JSON)")
            try:
                self._json = fast_json_loads(self.body)
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(400, "Invalid JSON in request body")
        return self._json

    async def form(self):
        """Parse body as form data (urlencoded or multipart).

        Enforces DATA_UPLOAD_MAX_NUMBER_FIELDS from settings.
        """
        if self._form is None:
            content_type = self.headers.get("content-type", "")
            if "application/x-www-form-urlencoded" in content_type:
                self._form = parse_query_string(
                    self.body.decode("utf-8", errors="replace"),
                )
                # Enforce field count limit
                max_fields = get_setting("DATA_UPLOAD_MAX_NUMBER_FIELDS")
                if max_fields and self._form:
                    total_fields = sum(
                        len(v) if isinstance(v, list) else 1
                        for v in self._form.values()
                    )
                    if total_fields > max_fields:
                        raise HTTPException(
                            400,
                            f"Too many form fields ({total_fields}). "
                            f"Maximum is {max_fields}.",
                        )
            elif "multipart/form-data" in content_type:
                self._parse_multipart()
            else:
                self._form = {}
        return self._form

    async def files(self):
        """Get uploaded files from multipart form data.

        Returns dict of {name: UploadedFile} where UploadedFile has
        .filename, .content_type, .data (bytes).
        """
        if self._files is None:
            content_type = self.headers.get("content-type", "")
            if "multipart/form-data" in content_type:
                self._parse_multipart()
            else:
                self._files = {}
        return self._files

    def _parse_multipart(self):
        """Parse multipart/form-data body using native Zig parser.

        Fast path: if ``_multipart_parts`` is set (pre-parsed by Zig server),
        skip the FFI round-trip entirely. This avoids re-copying the body
        and per-part data through the Python→Zig→Python boundary.
        """
        if self._multipart_parts is not None:
            parts = self._multipart_parts
        else:
            # Slow path: parse via FFI (ASGI, TestClient, non-Zig server)
            content_type = self.headers.get("content-type", "")
            boundary = None
            for part in content_type.split(";"):
                part = part.strip()
                if part.startswith("boundary="):
                    boundary = part[9:].strip('"')
                    break

            if not boundary or not self.body:
                self._form = {}
                self._files = {}
                return

            body = (
                self.body
                if isinstance(self.body, bytes)
                else self.body.encode("latin-1")
            )
            parts = _parse_multipart(body, boundary)

        form = {}
        files = {}
        mem_threshold = int(get_setting("FILE_UPLOAD_MAX_MEMORY_SIZE"))
        max_file_size = int(get_setting("FILE_UPLOAD_MAX_SIZE"))
        # Any exit that raises mid-parse (per-file 413, field/file-count limits,
        # os.write ENOSPC, or task cancellation) must NOT orphan the temp files
        # already spilled to disk for earlier parts. mkstemp files are not
        # auto-deleted, so unlink every spilled UploadedFile before propagating.
        try:
            for name, filename, ct, data in parts:
                if filename is not None:
                    if max_file_size and len(data) > max_file_size:
                        raise HTTPException(
                            413,
                            f"File '{filename}' ({len(data)} bytes) exceeds FILE_UPLOAD_MAX_SIZE ({max_file_size}).",
                        )
                    if len(data) > mem_threshold:
                        # Spill to temp file
                        temp_dir = str(get_setting("FILE_UPLOAD_TEMP_DIR", "")) or None
                        fd, temp_path = _tempfile.mkstemp(
                            dir=temp_dir, prefix="hyper_upload_"
                        )
                        try:
                            os.write(fd, data)
                        except BaseException:
                            # ENOSPC / cancellation before the UploadedFile owns
                            # this path — reclaim it here (it is not yet in
                            # ``files``, so the outer handler won't see it).
                            _unlink_quiet(temp_path)
                            raise
                        finally:
                            os.close(fd)
                        files[name] = UploadedFile(
                            filename=filename,
                            content_type=ct,
                            _path=temp_path,
                            _size=len(data),
                        )
                    else:
                        files[name] = UploadedFile(
                            filename=filename,
                            content_type=ct,
                            _data=data,
                        )
                else:
                    form.setdefault(name, []).append(
                        data.decode("utf-8", errors="replace")
                    )

            # Enforce DATA_UPLOAD_MAX_NUMBER_FIELDS
            max_fields = get_setting("DATA_UPLOAD_MAX_NUMBER_FIELDS")
            if max_fields and form:
                total_fields = sum(
                    len(v) if isinstance(v, list) else 1 for v in form.values()
                )
                if total_fields > max_fields:
                    raise HTTPException(
                        400,
                        f"Too many form fields ({total_fields}). Maximum is {max_fields}.",
                    )

            # Enforce DATA_UPLOAD_MAX_NUMBER_FILES
            max_files = get_setting("DATA_UPLOAD_MAX_NUMBER_FILES")
            if max_files and len(files) > max_files:
                raise HTTPException(
                    400,
                    f"Too many uploaded files ({len(files)}). Maximum is {max_files}.",
                )
        except BaseException:
            for spilled in files.values():
                spilled.cleanup()
            raise

        self._form = form
        self._files = files

    @property
    def content_type(self):
        return self.headers.get("content-type", "")

    @property
    def cookies(self):
        """Parse Cookie header via native Zig parser."""
        if self._cookies is None:
            cookie_header = self.headers.get("cookie", "")
            if cookie_header:
                try:
                    self._cookies = parse_cookies(cookie_header)
                except UnicodeDecodeError, ValueError:
                    # A hostile/malformed Cookie header (e.g. non-UTF-8 bytes)
                    # must NOT be able to 500 every request that reads
                    # req.cookies — parse leniently instead of crashing.
                    self._cookies = _parse_cookies_lenient(cookie_header)
            else:
                self._cookies = {}
        return self._cookies

    @property
    def is_json(self):
        return "json" in self.content_type

    def __repr__(self):
        return f"Request({self.method} {self.path})"

    @classmethod
    def from_asgi(cls, scope, body=b""):
        """Create a Request from an ASGI scope."""
        # Drop header names containing '_'. HTTP headers use '-', and most
        # servers map both '-' and '_' to the same env var, letting an attacker
        # spoof X-Forwarded-For via X_Forwarded_For.
        headers = {}
        for key, value in scope.get("headers", []):
            name = key.decode("latin-1")
            if "_" in name:
                continue
            headers[name] = value.decode("latin-1")

        return cls(
            method=scope.get("method", "GET"),
            path=scope.get("path", "/"),
            headers=headers,
            query_string=scope.get("query_string", b"").decode("latin-1"),
            body=body,
            path_params=scope.get("path_params", {}),
            scope=scope,
        )


def _unlink_quiet(path: str | None) -> None:
    """Best-effort unlink of a spilled temp path. Idempotent, never raises."""
    if path is None:
        return
    # Best-effort: temp-file cleanup — the file may already be gone or its dir
    # removed; a failed unlink must never propagate.
    with contextlib.suppress(OSError):
        Path(path).unlink()


@dataclass(slots=True)
class UploadedFile:
    """A file uploaded via multipart/form-data.

    Supports three storage modes transparently:

    1. **Memory** — small files (< ``FILE_UPLOAD_MAX_MEMORY_SIZE``).
       ``.data`` returns bytes directly.
    2. **Disk spill** — larger files written to temp files.
       ``.data`` reads from disk. ``.path`` gives the temp file path.
    3. **Streaming** — for pass-through proxying.
       ``.data`` raises ValueError. Use ``.chunks()`` to iterate.

    All modes support ``.chunks(chunk_size)`` for streaming reads::

        async for chunk in uploaded.chunks():
            await s3.write(chunk)
    """

    filename: str
    content_type: str
    _data: bytes | None = None
    _path: str | None = None
    _size: int = 0

    @property
    def data(self) -> bytes:
        """Full file content. Reads from disk if spilled."""
        if self._data is not None:
            return self._data
        if self._path is not None:
            return Path(self._path).read_bytes()
        raise ValueError(
            "Streaming file — use chunks() or save the stream before accessing .data"
        )

    @property
    def size(self) -> int:
        """File size in bytes."""
        if self._data is not None:
            return len(self._data)
        return self._size

    @property
    def path(self) -> str | None:
        """Temp file path (disk-spill mode), or None for in-memory."""
        return self._path

    @property
    def in_memory(self) -> bool:
        """True if file data is in memory (not spilled to disk)."""
        return self._data is not None

    async def chunks(self, chunk_size: int = 0) -> AsyncIterator[bytes]:
        """Stream file content in chunks. Works for all three modes.

        Default chunk_size reads ``STREAM_BODY_CHUNK_SIZE`` from settings.
        """
        if chunk_size == 0:
            chunk_size = int(get_setting("STREAM_BODY_CHUNK_SIZE"))
        if self._data is not None:
            for i in range(0, len(self._data), chunk_size):
                yield self._data[i : i + chunk_size]
        elif self._path is not None:
            with Path(self._path).open("rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk

    def cleanup(self) -> None:
        """Delete the backing temp file if this upload spilled to disk.

        Idempotent and best-effort: safe to call any number of times and never
        raises. After cleanup the on-disk data is gone, so ``.data``/``.chunks``
        for a spilled file are unreadable.

        Disk-spill temp files are created via ``mkstemp`` and are NOT
        auto-deleted by the OS, so a normally-consumed upload's temp file is
        reclaimed either by an explicit ``cleanup()``/``close()`` call once the
        handler is done with it, or as a best-effort backstop when this
        ``UploadedFile`` is garbage-collected (see ``__del__``).
        """
        path = self._path
        if path is not None:
            # Clear first so a concurrent/re-entrant call is a no-op and .data
            # cannot resurrect a path we are about to unlink.
            self._path = None
            _unlink_quiet(path)

    def close(self) -> None:
        """Alias for :meth:`cleanup` — release the backing temp file."""
        self.cleanup()

    def __del__(self) -> None:
        # Best-effort backstop: reclaim a spilled temp file on GC even if the
        # caller never called cleanup()/close(). Never raises out of __del__.
        self.cleanup()

    def __repr__(self) -> str:
        mode = (
            "memory" if self._data is not None else "disk" if self._path else "stream"
        )
        return f"UploadedFile({self.filename!r}, {self.size} bytes, {mode})"


class CaseInsensitiveDict(dict):
    """Dict with case-insensitive key access."""

    def __init__(self, data=None, **kwargs):
        # Lower-case every key up front, then hand the pairs to dict.__init__
        # (a single C-level bulk insert). This skips N Python-level __setitem__
        # calls — each of which would re-lower an already-lowered key — and is
        # ~40% faster than the per-key loop on a typical 12-header request.
        # Result is identical: last value wins on case-colliding keys.
        if data:
            if isinstance(data, dict):
                super().__init__([(k.lower(), v) for k, v in data.items()])
            else:
                super().__init__([(k.lower(), v) for k, v in data])
        else:
            super().__init__()
        if kwargs:
            for k, v in kwargs.items():
                self[k] = v

    @classmethod
    def _adopt_lowercased(cls, d):
        """Adopt an ALREADY-lowercased mapping as backing — no per-key re-lower.

        Contract: every key in ``d`` is already lowercase. The native Zig server
        lowercases HTTP header names (an ASCII byte op — header field-names are
        ASCII tokens, so this equals ``str.lower()``) in the request arena before
        building the dict it hands to ``_wrap_handler_for_zig``. That makes the
        per-key ``k.lower()`` in ``__init__`` pure waste, and rebuilding the dict
        Python-side (the ``.enhanced`` path did this on every request) redundant.

        This bulk-copies the pairs at the C level (``dict.update`` bypasses the
        overridden ``__setitem__``, so nothing is re-lowered) into a fresh
        instance. ONLY the trusted native path may use this: a caller whose keys
        are not already lowercase must go through ``__init__`` (the default), or
        case-insensitive lookups would miss.
        """
        inst = cls.__new__(cls)
        dict.update(inst, d)
        return inst

    def __setitem__(self, key, value):
        super().__setitem__(key.lower(), value)

    def __getitem__(self, key):
        return super().__getitem__(key.lower())

    def __contains__(self, key):
        return super().__contains__(key.lower())

    def get(self, key, default=None):
        return super().get(key.lower(), default)
