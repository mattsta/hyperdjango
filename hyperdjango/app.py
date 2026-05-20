"""
HyperApp — the core application class.

Combines routing, middleware, database lifecycle, templating,
and server management into a single ergonomic API.

Usage:
    from hyperdjango import HyperApp

    app = HyperApp(title="My App")

    @app.get("/")
    async def index(request):
        return Response.json({"hello": "world"})

    if __name__ == "__main__":
        app.run()
"""

import asyncio
import contextlib
import faulthandler
import html as _html
import inspect
import mimetypes
import os
import re
import secrets
import stat as _stat
import sys
import threading
import traceback
import uuid
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

from hyperdjango._hyperdjango_native import (
    HyperServer,
    _db_listen,
    _server_add_ws_route,
)
from hyperdjango.conf import DEFAULTS, get_setting
from hyperdjango.logging import logger
from hyperdjango.logging._core import _core as _logger_core
from hyperdjango.logging._core import log_context
from hyperdjango.native._coro import (
    close_all_thread_loops,
    get_thread_event_loop,
    run_coro_on_loop,
)
from hyperdjango.openapi import OpenAPISpecCache
from hyperdjango.request import CaseInsensitiveDict, Request
from hyperdjango.response import Response, _sanitize_header

# Telemetry context accessor, resolved ONCE at import. Telemetry stays
# zero-cost when disabled (module-level metric registration is the documented
# cheap pattern); the try/except keeps HyperApp importable even in a build
# where the telemetry package is absent entirely.
try:
    from hyperdjango.telemetry.context import current as _telemetry_current
except ImportError:  # pragma: no cover — telemetry package always ships today
    _telemetry_current = None
from hyperdjango.router import _APPEND_SLASH_REDIRECT, Router
from hyperdjango.site_config import SiteConfig
from hyperdjango.standalone_middleware import MiddlewareStack
from hyperdjango.staticfiles import get_static_url, get_static_url_versioned
from hyperdjango.templating import TemplateEngine
from hyperdjango.versioning import (
    get_app_version,
    mount_version_endpoints,
)
from hyperdjango.websocket import WebSocket, WebSocketDisconnect

_SKIP_INJECTION_PARAMS = frozenset({"return", "request"})


def _get_app_version_string() -> str:
    """Template global: returns the current app version string."""
    return get_app_version().version


def _etag_matches(if_none_match: str, etag: str) -> bool:
    """RFC 9110 If-None-Match: '*' or a (possibly weak) list containing etag."""
    inm = if_none_match.strip()
    if inm == "*":
        return True
    for token in inm.split(","):
        token = token.strip()
        if token.startswith("W/"):
            token = token[2:].strip()
        if token == etag:
            return True
    return False


def _zig_exception_to_response(exc: Exception) -> Response:
    """Normalize an exception from a Zig-dispatched handler into a Response.

    Returning a Response (rather than letting the exception unwind past the
    middleware chain) is what lets response-decorating middleware — security
    headers, CORS, rate-limit headers, session cookie re-save — still run on
    error responses. The innermost dispatch calls this so every outer
    middleware sees a Response, never a raised exception.

    Delegates to the single framework-wide mapper (``exception_to_response``)
    so the Zig path emits the exact same unified body shape and forwards the
    same headers as the ASGI dispatch and REST viewset boundaries.
    """
    return exception_to_response(exc)


def _response_to_zig_tuple(result: Response) -> tuple:
    """Serialize a ``Response`` into the Zig enhanced-response tuple.

    ``(status:int, content_type:str, body:bytes, extra_headers:str|None)``,
    mirroring ``sendFullResponse``. Body stays bytes (no decode/re-encode).
    Used for BOTH the success path and the error safety-net so that error
    responses forward their headers (e.g. ``Retry-After`` on a 429, or any
    ``HTTPException.headers``) exactly like success responses.
    """
    body = result.body  # always bytes (Response normalizes in __init__)
    ct = result.headers.get("content-type", "application/json")

    extra_headers = _serialize_extra_headers(result)
    return (result.status, ct, body, extra_headers)


def _serialize_extra_headers(
    result: Response, *, exclude_framing: bool = False
) -> str | None:
    """Serialize a ``Response``'s headers into Zig's ``"\\r\\nKey: Value"`` block.

    Set-Cookie is emitted as one line PER cookie (from ``Response._cookie_lines``)
    rather than a single value with embedded CRLF, so 2+ cookies are distinct
    header lines on the wire. ``content-type``/``content-length`` are always
    dropped (Zig owns Content-Type via the tuple field and frames Content-Length
    from the body). ``exclude_framing`` additionally drops
    ``transfer-encoding``/``connection`` for the chunked-streaming path, where Zig
    emits those framing headers itself.
    """
    extra_parts: list[str] = []
    for hk, hv in result.headers.items():
        hk_lower = hk.lower()
        if hk_lower == "content-type":
            continue  # Already handled via content_type field
        if hk_lower == "content-length":
            # Zig always frames Content-Length from the actual body bytes.
            # Forwarding a header-set value too (VersionMiddleware/
            # CompressionMiddleware set it after rewriting the body) would emit a
            # DUPLICATE Content-Length — a response-smuggling vector. Drop it.
            continue
        if hk_lower == "set-cookie":
            # Emitted per-cookie below (the joined header value carries an
            # embedded CRLF — never forward it as a single header value).
            continue
        if exclude_framing and hk_lower in ("transfer-encoding", "connection"):
            continue
        # Sanitize per-VALUE (and name) here — the native path assembles the
        # header block from these, and a value assigned AFTER Response construction
        # (e.g. a handler returning (body, status, {"X-Foo": user_value}) or
        # middleware setting resp.headers[k]=user) never passed through the
        # construction-time guard. A single embedded CR/LF would otherwise inject a
        # spurious response header (the native block guard only catches a full
        # blank line). Same policy the ASGI send path uses.
        extra_parts.append(f"\r\n{_sanitize_header(hk)}: {_sanitize_header(hv)}")
    for cookie in result._cookie_lines():
        extra_parts.append(f"\r\nset-cookie: {cookie}")
    return "".join(extra_parts) if extra_parts else None


# --- Shared dispatch helpers (used by BOTH the ASGI and native paths) ---------
# These exist so the two dispatch paths (`HyperApp._dispatch` under ASGI and
# `_wrap_handler_for_zig._inner_dispatch` under the native Zig server) route
# handler results, exceptions, request-ids, and streaming bodies through ONE
# code path and can never silently drift (the round-7 `_inject_services`
# extraction, generalized).


def coerce_response(result) -> Response:
    """Normalize a handler's return value into a ``Response`` — ONE contract for
    both dispatch paths.

    Contract (chosen to match the historical ASGI behavior and avoid an XSS
    surprise — a bare ``str`` is NOT auto-treated as trusted HTML):

      - ``Response``                → returned unchanged
      - ``str``                     → ``Response.text`` (``text/plain; charset=utf-8``)
      - ``dict`` / ``list``         → ``Response.json`` (200)
      - ``(body, status)`` /
        ``(body, status, headers)`` → the body is coerced recursively, then the
        given integer status (and optional headers mapping) are applied. Only a
        2/3-tuple whose second element is a real ``int`` (not ``bool``) is treated
        as a status tuple; any other tuple is serialized as a JSON array.
      - anything else (``int``, ``float``, ``None``, dataclass, …)
                                    → ``Response.json(value)`` (200)

    The native and ASGI paths share this one contract: ``str`` is ``text/plain``
    and any unrecognized scalar is serialized as JSON, never a 500.
    """
    if isinstance(result, Response):
        return result
    # A handler may RETURN an HTTPException (as well as raise one) — map it to
    # the unified {"detail","status"} body. Both forms are ergonomic:
    #   raise HTTPException(404, "x")   |   return HTTPException(404, "x")
    if isinstance(result, HTTPException):
        return exception_to_response(result)
    if isinstance(result, str):
        return Response.text(result)
    if isinstance(result, (dict, list)):
        return Response.json(result)
    if isinstance(result, tuple):
        n = len(result)
        # (body, status): 2-tuple whose 2nd element is a real int (not bool).
        if n == 2 and isinstance(result[1], int) and not isinstance(result[1], bool):
            resp = coerce_response(result[0])
            resp.status = result[1]
            return resp
        # (body, status, headers): 3-tuple with an int status AND a dict of
        # headers. Anything else (e.g. an all-int (1, 2, 3)) is a JSON array.
        if (
            n == 3
            and isinstance(result[1], int)
            and not isinstance(result[1], bool)
            and isinstance(result[2], dict)
        ):
            resp = coerce_response(result[0])
            resp.status = result[1]
            for hk, hv in result[2].items():
                resp.headers[hk] = hv
            return resp
    return Response.json(result)


def _current_trace_id() -> str | None:
    """Return the active telemetry trace id (32-char hex) if a real span is live.

    The context accessor is imported at module top (`_telemetry_current`) —
    NEVER inside this function. A previous revision ran
    `from hyperdjango.telemetry.context import current` here, per request:
    the import system's locking across 64 free-threaded workers formed a
    convoy in SOME server instances from birth, pinning the whole process in
    a stable ~155k rps regime (vs ~550k) — the "bistable instance" mystery.
    In-process frame sampling put 89% of all samples on that import line.
    Import-in-function on a hot path is not lazy loading; it is a per-call
    trip through sys.modules + import locks.
    """
    if _telemetry_current is None:
        return None
    span = _telemetry_current()
    if span is not None and span.is_valid:
        return span.trace_id_hex
    return None


# A trusted-adopt allowlist for an inbound X-Request-ID: printable token chars
# only, bounded length. Anything else is treated as hostile and a fresh id is
# minted instead (see _resolve_request_id).
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,200}\Z")


def _resolve_request_id(req: Request) -> str:
    """Mint (or adopt) the correlation id for a request — identical on both paths.

    Precedence: an inbound ``X-Request-ID`` header, else the trace id from a W3C
    ``traceparent`` header, else a live telemetry span's trace id, else a fresh
    ``uuid4`` hex. The value is bounded so a hostile inbound header cannot bloat
    logs / the echoed response header.

    An inbound ``X-Request-ID`` is only ADOPTED when it is a bounded, safe token
    (``[A-Za-z0-9._-]``, 1..200 chars). A malformed / oversized inbound id is
    NOT trusted as the canonical correlation id — it is ignored and a fresh id
    minted below — so a client cannot pin an arbitrary string across requests or
    inject unexpected content into the correlation field / echoed header.
    """
    inbound = req.headers.get("x-request-id")
    if inbound:
        candidate = inbound.strip()
        if _REQUEST_ID_RE.match(candidate):
            return candidate
        # Fall through to mint our own — do not trust an unsafe client id.
    traceparent = req.headers.get("traceparent")
    if traceparent:
        # W3C trace-context: "version-traceid-spanid-flags"; field 1 is the trace id.
        parts = traceparent.split("-")
        if len(parts) >= 2 and parts[1]:
            return parts[1][:200]
    trace_id = _current_trace_id()
    if trace_id:
        return trace_id
    return uuid.uuid4().hex


def _request_log_context(request_id: str) -> dict:
    """Build the per-request log context injected for the request scope.

    Kept tiny and identical on both paths; AccessLogMiddleware later merges its
    own richer context (path/method/client_ip/user) on top of the same
    ``request_id`` because it reads ``request.request_id`` set here.
    """
    return {"request_id": request_id}


def _make_native_stream_pull(stream_iter, loop):
    """Build the sync pull callable Zig invokes to drive a streaming response.

    The native chunked-send loop (server.zig ``sendChunkedResponse``) calls this
    ONCE PER CHUNK: each call advances the async iterator by exactly one step on
    the worker's thread-local event loop (via ``_run_dispatch``, the same
    single-step driver the request path uses) and returns:

      - ``bytes``  — the next chunk to frame (an empty ``b""`` is skipped, not a
        stream terminator),
      - ``None``   — the stream is exhausted (``StopAsyncIteration``).

    Driving one chunk at a time — instead of materializing the whole iterator —
    is what makes an infinite ``Response.sse`` heartbeat stream memory-bounded
    and incremental (first bytes reach the client before the generator finishes),
    fixing the thread-pool DoS / OOM of the old ``_materialize_stream``. A genuine
    error mid-stream propagates out (Zig aborts the response and closes).
    """
    anext_ = stream_iter.__anext__

    def pull():
        try:
            chunk = _run_dispatch(loop, anext_())
        except StopAsyncIteration:
            return None
        except BaseException:
            # A genuine error mid-stream propagates out to the Zig chunked-send
            # loop, which aborts the response. Release the async generator (and
            # any fd its `with open(...)` holds) before it does, otherwise the
            # suspended generator lingers until GC. aclose() exists only on
            # async generators (Response.stream also accepts a plain async
            # iterator), so probe for it; its own teardown must not mask the
            # original error.
            # dynamic-attr: aclose is an optional async-generator capability on the AsyncIterator protocol, not guaranteed on every stream iterator
            aclose = getattr(stream_iter, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    _run_dispatch(loop, aclose())
            raise
        if isinstance(chunk, str):
            return chunk.encode("utf-8")
        if isinstance(chunk, (bytes, bytearray, memoryview)):
            return bytes(chunk)
        return str(chunk).encode("utf-8")

    return pull


def _response_to_zig_stream_tuple(result: Response, loop) -> tuple:
    """Serialize a STREAMING ``Response`` into the Zig chunked-response tuple.

    ``(status:int, content_type:str, b"", extra_headers:str|None, pull:callable)``
    — a 5-tuple whose trailing callable is the streaming sentinel: the presence of
    a callable 5th slot is how the Zig side distinguishes a chunked response from
    the ordinary 4-tuple ``(status, ct, body, extra_headers)``. Zig writes the
    status line + headers with ``Transfer-Encoding: chunked`` (owning that framing
    header + ``Connection`` itself, hence ``exclude_framing``), then repeatedly
    calls ``pull`` to stream chunk frames until it returns ``None``.
    """
    ct = result.headers.get("content-type", "application/octet-stream")
    extra_headers = _serialize_extra_headers(result, exclude_framing=True)
    pull = _make_native_stream_pull(result._stream_iter, loop)
    return (result.status, ct, b"", extra_headers, pull)


def _build_native_scope(kwargs: dict) -> dict | None:
    """Assemble a minimal ASGI-like scope for the native path so ``peer_ip`` /
    ``client_ip`` / ``is_secure`` resolve through the SAME ``Request`` code as
    the ASGI path. Without a scope, ``peer_ip`` collapses every production client
    to ``127.0.0.1`` (bucketing them together in RateLimitMiddleware and
    mis-attributing SecurityLog), so the peer address is threaded through here.

    The Zig server does NOT yet thread the accepted socket's peer address into
    the wrapper kwargs (see ``callPythonHandler`` in zig/src/server.zig — it
    builds method/path/body/query_string/headers/path_params only). When it does,
    it must populate ``_peer = (ip:str, port:int)`` — the NATIVE_PEER hook read
    here — and ``client_ip`` becomes byte-for-byte identical to ASGI.

    Until then ``client`` is deliberately left UNSET (``peer_ip`` falls back
    to ``127.0.0.1``): we must NOT fabricate the peer from
    ``X-Forwarded-For``/``X-Real-IP``, because ``peer_ip`` is contractually the
    unspoofable socket address that ``client_ip``'s trust logic relies on —
    seeding it from an attacker-controlled header would defeat the whole guard.

    TODO(native-wave): thread getpeername() from the accepting worker into
    ``_peer`` so native client_ip == ASGI client_ip. Flagged for the native side.
    """
    scope: dict = {}
    peer = kwargs.get("_peer")  # NATIVE_PEER hook: (ip, port) or None (not passed yet)
    if peer is not None:
        scope["client"] = (peer[0], int(peer[1]))
    return scope or None


async def _finalize_native(app, req: Request, chain) -> Response:
    """Shared native-path dispatch boundary: request-id observability.

    Mints/echoes the request id and installs the request-scope log context around
    the middleware→handler chain (identical policy to the ASGI ``handle`` path).
    A streaming ``Response`` is returned with its iterator INTACT — the native
    wrapper detects it and drives it incrementally through the Zig chunked-send
    path (see ``_response_to_zig_stream_tuple``); it is NOT materialized here.
    Materializing would buffer the whole stream, hanging a worker on an infinite
    SSE heartbeat and doubling peak memory on a large stream.
    """
    request_id = _resolve_request_id(req)
    req.request_id = request_id
    token = log_context.set(_request_log_context(request_id))
    try:
        response = await chain
    finally:
        log_context.reset(token)
    if isinstance(response, Response):
        response.headers.setdefault("x-request-id", request_id)
    return response


# Thread-local event loop for Zig worker threads: ONE loop per worker thread,
# created on first use, reused for every request, tracked for clean shutdown.
# The registry lives in hyperdjango.native._coro so EVERY native-path caller
# (this wrapper dispatch, the Zig direct-coroutine fallback, streaming
# drivers) shares the same per-thread loop — DB-offload policy is keyed by
# loop identity (database.mark_loop_multiplexing) and shutdown closes loops
# through the shared list.
_get_thread_event_loop = get_thread_event_loop


def _run_dispatch(loop, coro):
    """Run a per-request dispatch coroutine to completion.

    Delegates to the shared eager-Task runner (`hyperdjango.native._coro`):
    a handler that never suspends on real I/O (the common case — DB
    round-trips are native/blocking, not asyncio futures) completes inside
    the eagerly-started Task with zero event-loop iterations (~0.5 µs); a
    genuinely-suspending handler finishes on this worker's persistent loop.

    History: an earlier hand-rolled version stepped the raw coroutine with a
    custom send/throw driver at the same fast-path cost — but it never
    registered a ``current_task()``, so any handler using
    ``asyncio.wait_for`` / ``asyncio.timeout`` / ``TaskGroup`` on the native
    path failed with ``RuntimeError("Timeout should be used inside a task")``.
    The eager Task keeps the fast path AND the full Task semantics.
    """
    return run_coro_on_loop(loop, coro)


_close_all_thread_loops = close_all_thread_loops


# Map Python converter types to Zig ParamType names.
# Used to build param_types_json for Zig-native type coercion.
_CONVERTER_TO_ZIG_TYPE: dict[type, str] = {
    int: "int",
    float: "float",
    str: "str",
    bool: "bool",
}


def _build_param_types_json(
    param_names: list[str],
    param_converters: list[type],
) -> str:
    """Build Zig param_types_json from route converter metadata.

    Format: "name:type|name:type|..." where type is int/float/str/bool.
    Empty string if no params.

    Examples:
        ["id"], [int]              → "id:int"
        ["id", "slug"], [int, str] → "id:int|slug:str"
        [], []                     → ""
    """
    if not param_names:
        return ""
    parts: list[str] = []
    for name, conv in zip(param_names, param_converters):
        zig_type = _CONVERTER_TO_ZIG_TYPE.get(conv, "str")
        parts.append(f"{name}:{zig_type}")
    return "|".join(parts)


def _export_native_config() -> None:
    """The single, sanctioned settings→native-env bridge.

    The native Zig HTTP server reads a handful of its knobs from the process
    environment at startup (it has no other channel into the running Python
    settings system). This is the ONE place that reads those settings via
    get_setting and publishes them as the `HYPER_*` env vars the Zig side
    expects, so a value set in Django settings / DEFAULTS — not just an env var —
    takes effect. Eliminating the env round-trip entirely needs a native-API
    change; until then every write lives here (never scattered), so the bridge is
    one auditable surface rather than ad-hoc `os.environ[...] = ...` writes spread
    through the server startup path.
    """
    # The Zig server selects its connection model from HYPER_HTTP_SERVER_MODEL.
    http_server_model = get_setting("HTTP_SERVER_MODEL")
    if http_server_model:
        os.environ["HYPER_HTTP_SERVER_MODEL"] = str(http_server_model)

    # Threaded-mode load-shedding cap (0 = use the native default). Publish only a
    # positive override; leave the env untouched at 0 so the Zig default
    # (THREAD_POOL_SIZE × 8) applies.
    max_pending = int(get_setting("HTTP_MAX_PENDING") or 0)
    if max_pending > 0:
        os.environ["HYPER_HTTP_MAX_PENDING"] = str(max_pending)

    # Keep the native router's APPEND_SLASH 301 in agreement with the ASGI path:
    # the Zig side defaults to enabled, so export the flag either way.
    os.environ["HYPER_APPEND_SLASH"] = "1" if get_setting("APPEND_SLASH") else "0"


class HyperApp:
    """The hyperdjango application.

    A Django-inspired, performance-first web framework.
    """

    def __init__(
        self,
        title="HyperDjango",
        database=None,
        templates=None,
        static=None,
        views=None,
        debug=None,
        max_body_size=None,
        secret_key=None,
        allowed_hosts=None,
        site_config: SiteConfig | None = None,
    ):
        self.site_config = site_config
        self.title = site_config.name if site_config is not None else title
        self.templates_dir = templates

        # database_url is PER-INSTANCE (this app's explicit DB, or None) — it
        # drives the "No database configured" raise and must NOT inherit a URL a
        # different app set globally. Push an explicit URL into settings so
        # get_db() can auto-create. The prod guard reads get_setting (below), not
        # this attr, so it stays truthful about what get_db actually uses.
        if database:
            DEFAULTS["DATABASE_URL"] = database
        self.database_url = database
        self.static_dir = static
        self.views_dir = views
        # Bounded in-memory cache for the fallback static server (_try_static).
        # Each entry: (body, content_type, etag, mtime_ns, size). Serves cached
        # bytes after a single mtime stat-revalidation — turning the 7-syscall
        # (stat+open+fstat+read+read+close+guess) hot path into one stat on a
        # cache hit — and adds ETag / If-None-Match 304 support. Capped by total
        # bytes with LRU eviction. Only used when an app runs with static=... and
        # no StaticFilesMiddleware in front.
        self._static_cache: OrderedDict[str, tuple] = OrderedDict()
        self._static_cache_lock = threading.Lock()
        self._static_cache_bytes = 0
        self._static_cache_max_bytes = 8 * 1024 * 1024  # 8 MiB
        # debug is PER-INSTANCE (constructor arg wins, else the DEBUG setting).
        # It is deliberately NOT written into the global DEFAULTS: doing so would
        # leak one app's debug flag into every later-constructed app in the same
        # process (and into get_setting). Constructor arg wins.
        self.debug = debug if debug is not None else get_setting("DEBUG")
        self.max_body_size = (
            max_body_size if max_body_size is not None else get_setting("MAX_BODY_SIZE")
        )
        # Bridge secret_key into settings (mirrors the DATABASE_URL bridge above).
        # Every signer — sessions, CSRF, password-reset, versioning HMAC — reads
        # get_setting("SECRET_KEY"), never self.secret_key, so a constructor key
        # that was only stored on the instance left them effectively keyless. Read
        # it back through get_setting so self.secret_key reflects exactly what the
        # signers see, keeping the prod guard (which checks self.secret_key) honest.
        if secret_key is not None:
            DEFAULTS["SECRET_KEY"] = secret_key
        self.secret_key = get_setting("SECRET_KEY")
        # Same bridge for allowed_hosts: SecurityHeadersMiddleware validates the
        # Host header via get_setting("ALLOWED_HOSTS"), never self.allowed_hosts,
        # so a constructor value that was only stored on the instance left host
        # validation disabled while the prod warning falsely passed.
        if allowed_hosts is not None:
            DEFAULTS["ALLOWED_HOSTS"] = allowed_hosts
        self.allowed_hosts = get_setting("ALLOWED_HOSTS")

        # The plaintext port the native server actually binds, published before
        # the startup hooks run so a hook (e.g. an in-process mTLS terminator)
        # can forward to the app's real port instead of a hand-copied constant
        # that silently drifts whenever the app's port moves. None until run().
        self.bound_port: int | None = None

        self.router = Router()
        self._middleware = MiddlewareStack()
        self._on_startup: list[Callable] = []
        self._on_shutdown: list[Callable] = []
        self._ws_handlers: dict[str, Callable] = {}
        self._exception_handlers: dict[type, Callable] = {}
        self._health_checks: dict[str, Callable] = {}
        self._services: dict[type, object] = {}
        # Native auto-CRUD routes served entirely in Zig (see add_db_route).
        self._db_routes: list[tuple[str, str, str, str, str, str, str]] = []

        # Database (lazy init). Guarded by _db_lock so a racing cold start on
        # free-threaded Python can't build two Database objects (two pools).
        self._db = None
        self._db_lock = threading.Lock()

        # LISTEN/NOTIFY dedup registry: channel -> callback. Guards against a
        # hot-reload re-running @app.listen (or a double call) accumulating
        # native listener threads that are never released — mirrors the
        # double-checked registry PgChannelLayer._start_listener uses.
        self._listeners: dict[str, Callable] = {}
        self._listeners_lock = threading.Lock()

        # Template engine (lazy init). Guarded by _template_lock so a racing
        # first render can't publish a half-configured engine.
        self._template_engine = None
        self._template_lock = threading.Lock()

        # Cached handler chain (lazy init)
        self._cached_handler = None

        # OpenAPI spec caches, one OpenAPISpecCache per mount_docs()
        # call. invalidate_openapi_cache(app) walks this list to
        # reset all caches — useful for tests that add routes after
        # mount_docs() and want the new routes to show up, or for
        # hot-reload scenarios where the route set changes at runtime.
        self._openapi_caches: list[OpenAPISpecCache] = []

    # --- Route decorators (delegate to router) ---

    def get(self, pattern, name=None):
        return self.router.get(pattern, name)

    def post(self, pattern, name=None):
        return self.router.post(pattern, name)

    def put(self, pattern, name=None):
        return self.router.put(pattern, name)

    def patch(self, pattern, name=None):
        return self.router.patch(pattern, name)

    def delete(self, pattern, name=None):
        return self.router.delete(pattern, name)

    def route(self, pattern, methods=None, name=None):
        return self.router.route(pattern, methods, name)

    def add_db_route(
        self,
        method,
        pattern,
        *,
        table,
        op="select_one",
        pk_column="id",
        pk_param="id",
        columns="",
    ):
        """Register a NATIVE auto-CRUD route served entirely in Zig — the request
        is answered straight from the PostgreSQL wire protocol, without ever
        entering Python.

        ⚠️  SECURITY — read before using. This path **bypasses the entire Python
        request cycle**: no middleware runs, which means **no authentication, no
        SessionAuth, no tenancy scoping, no rate limiting, and no per-object
        permission checks**. A `select_one` route is a raw ``SELECT … WHERE
        {pk} = $1`` — anyone who can reach the URL can read (or, for insert/delete
        ops, write) any row by primary key. Use it **only** for data that is
        already fully public and non-tenant-scoped (e.g. a published article by
        slug, a public product catalog). For anything user-owned, tenant-scoped,
        or access-controlled, use a normal ``@app.get`` view or a ``ModelViewSet``
        so auth/tenancy middleware applies. See docs/database.md#native-auto-crud-routes.

        Args:
            method: HTTP method (e.g. "GET", "POST", "DELETE").
            pattern: Route pattern with the pk as a path param, e.g. "/articles/{id}".
            table: Table name to query. Not user-controlled — never interpolate
                request input here.
            op: One of select_one, select_list, insert, delete, custom_query,
                custom_query_single.
            pk_column: Primary-key column used in the WHERE clause.
            pk_param: Path-param name carrying the pk value (must match `pattern`).
            columns: Comma-separated column list (op-specific; "" = all).

        The response is the same lossless JSON the ``db.query_json`` fast path
        produces — see docs/database.md#result-serialization-to-json (NUMERIC and
        non-finite floats come back as strings, etc.).
        """
        self._db_routes.append(
            (
                method.upper(),
                pattern,
                op,
                table,
                pk_column or "",
                pk_param or "",
                columns or "",
            )
        )

    def websocket(self, pattern):
        """Register a WebSocket handler.

        Usage:
            @app.websocket("/ws/chat")
            async def chat(ws):
                await ws.accept()
                async for msg in ws.iter_text():
                    await ws.send_text(f"Echo: {msg}")
        """

        def decorator(func):
            self._ws_handlers[pattern] = func
            return func

        return decorator

    def channel(self, pattern: str, channel_name: str | None = None, **handler_kwargs):
        """Register a WebSocket handler bridged to a Channel.

        Combines @app.websocket with websocket_channel_handler for ergonomic
        real-time features. Supports room-based patterns via path params.

        Usage:
            @app.channel("/ws/chat/{room}")
            async def on_message(text, channel, ws):
                await channel.publish({"user": "alice", "text": text})

            # Or without a custom handler (auto-publishes received text as JSON):
            @app.channel("/ws/notifications", channel_name="notifications")
            async def on_connect(ws, channel):
                print(f"Client connected to {channel.name}")
        """

        app = self

        def decorator(func):
            async def ws_handler(ws):
                await ws.accept()
                # Resolve channel name from path params or explicit name
                params = ws.path_params if hasattr(ws, "path_params") else {}
                ch_name = channel_name or pattern.split("/")[-1]
                # Substitute path params: /ws/chat/{room} → chat:room_value
                for param_key, param_val in params.items():
                    ch_name_resolved = ch_name.replace(
                        f"{{{param_key}}}", str(param_val)
                    )
                    if ch_name_resolved != ch_name:
                        ch_name = ch_name_resolved

                # Lazy: the channels subsystem (and its deps) load only when an
                # app actually wires up a channel-backed websocket, not at import.
                from hyperdjango.channels import (
                    get_channel_layer,
                    websocket_channel_handler,
                )

                layer = get_channel_layer()
                ch = layer.channel(ch_name)

                # Determine callback role: on_message or on_connect
                if "text" in (func.__code__.co_varnames[: func.__code__.co_argcount]):
                    await websocket_channel_handler(
                        ws, ch, on_message=func, **handler_kwargs
                    )
                else:
                    await websocket_channel_handler(
                        ws, ch, on_connect=func, **handler_kwargs
                    )

            app._ws_handlers[pattern] = ws_handler
            return func

        return decorator

    # --- Health checks ---

    def add_health_check(self, name: str, check):
        """Register a custom health check.

        check: async def check() -> bool, or sync def check() -> bool
        Returns True if healthy, False if unhealthy.

        Usage:
            async def check_cache():
                return cache.ping()
            app.add_health_check("cache", check_cache)
        """
        self._health_checks[name] = check

    def mount_health(self, liveness_path="/health", readiness_path="/ready"):
        """Mount health check endpoints.

        GET /health — liveness probe (always 200 if process is running)
        GET /ready — readiness probe (checks DB, custom checks)

        Usage:
            app.mount_health()  # defaults
            app.mount_health("/healthz", "/readyz")  # custom paths
        """
        app = self

        async def liveness(request):
            return Response.json({"status": "ok"})

        async def readiness(request):
            checks = {}
            healthy = True

            # Built-in: database check
            db = app._db
            if db is None and app.database_url:
                from hyperdjango.database import get_db

                db = get_db()
            if db is not None:
                try:
                    await db.query("SELECT 1")
                    checks["database"] = "ok"
                # blind-except: readiness probe must report a DB failure as unhealthy status, not crash the health endpoint
                except Exception as e:
                    # Never serialize the raw DB exception into the (unauthenticated)
                    # readiness body — it can leak host/port/DSN fragments/SQLSTATE.
                    # Report a generic status and log the detail server-side.
                    logger.opt(exception=e).warning("Readiness DB check failed")
                    checks["database"] = "error"
                    healthy = False

            # Custom checks
            for name, check_fn in app._health_checks.items():
                try:
                    if inspect.iscoroutinefunction(check_fn):
                        result = await check_fn()
                    else:
                        result = check_fn()
                    checks[name] = "ok" if result else "unhealthy"
                    if not result:
                        healthy = False
                # blind-except: readiness probe must report a custom check's failure as unhealthy, not crash the health endpoint
                except Exception as e:
                    # Same info-leak guard as the DB check: generic status in the
                    # body, full detail only in the server-side log.
                    logger.opt(exception=e).warning(
                        "Readiness check {name} failed", name=name
                    )
                    checks[name] = "error"
                    healthy = False

            status = 200 if healthy else 503
            return Response.json(
                {"status": "ok" if healthy else "unhealthy", "checks": checks},
                status=status,
            )

        self.router.add("GET", liveness_path, liveness)
        self.router.add("GET", readiness_path, readiness)
        return self

    # --- Versioning ---

    def mount_version(
        self,
        version_path: str = "/version",
        bust_path: str = "/cache/bust",
    ) -> HyperApp:
        """Mount version metadata and cache bust endpoints.

        ``GET /version`` — returns app version, source, and component info.
        ``POST /cache/bust`` — manual cache invalidation (requires auth token).

        Usage::

            app.mount_version()  # defaults
            app.mount_version("/api/version", "/api/cache/bust")  # custom paths
        """
        mount_version_endpoints(self, version_path, bust_path)
        return self

    def register_version_component(self, name: str, paths: list[str]) -> HyperApp:
        """Register non-static files contributing to the app version hash.

        Use for templates, config files, or other assets not in the static
        pipeline but that affect the user experience::

            app.register_version_component("templates", [
                "templates/base.html",
                "templates/nav.html",
            ])
        """
        get_app_version().register_component(name, paths)
        return self

    def compute_app_version(self) -> str:
        """Compute the app version from manifest + registered components.

        Returns the resolved version string.
        """
        av = get_app_version()
        av.load_from_manifest()
        av.compute_from_components()
        return av.version

    # --- Exception handlers ---

    def exception_handler(self, exc_class: type):
        """Register a custom exception handler.

        Usage (return the unified error body via HTTPException):
            @app.exception_handler(ValueError)
            async def handle_value_error(request, exc):
                return Response.error(400, str(exc))

            @app.exception_handler(PermissionError)
            async def handle_permission(request, exc):
                return Response.error(403, "Forbidden")
        """

        def decorator(func):
            self._exception_handlers[exc_class] = func
            return func

        return decorator

    def add_exception_handler(self, exc_class: type, handler):
        """Register an exception handler programmatically.

        handler signature: async def handler(request, exc) -> Response
        Sync handlers are also supported.
        """
        self._exception_handlers[exc_class] = handler

    def _find_exception_handler(self, exc: Exception):
        """Find the best matching handler for an exception using MRO.

        Walks the exception's class hierarchy (most specific first)
        to find a registered handler.

        HTTPException precedence: an ``HTTPException`` is an *intentional* HTTP
        result — the handler raised it to produce a specific status/detail — so
        it carries its own built-in mapping (see ``_resolve_exception`` /
        ``_zig_exception_to_response``). Only a handler registered specifically
        for ``HTTPException`` or a subclass of it may override that mapping. A
        generic ``Exception`` (or ``BaseException``) catch-all — which every
        HTTPException also matches by MRO — must NOT swallow it into a 500.
        So when the exception is an HTTPException we skip any matched handler
        whose registered class is a strict superclass of HTTPException.
        """
        is_http = isinstance(exc, HTTPException)
        for cls in type(exc).__mro__:
            handler = self._exception_handlers.get(cls)
            if handler is not None:
                if is_http and not issubclass(cls, HTTPException):
                    continue
                return handler
        return None

    # --- Dependency Injection ---

    def provide(self, service_type: type, instance):
        """Register a service instance for dependency injection.

        Handlers can receive injected services via type-annotated parameters.

        Usage:
            app.provide(Database, db)
            app.provide(PermissionChecker, checker)

            @app.get("/users")
            async def list_users(request, db: Database):
                return Response.json(await db.query("SELECT * FROM users"))
        """
        self._services[service_type] = instance

    def get_service(self, service_type: type):
        """Retrieve a registered service by type. Returns None if not registered."""
        return self._services.get(service_type)

    # --- LISTEN/NOTIFY ---

    def listen(self, channel, callback=None):
        """Listen for PostgreSQL NOTIFY events on a channel.

        Spawns a background thread that calls callback(channel, payload)
        for each notification received.

        Usage:
            @app.listen("new_orders")
            def on_new_order(channel, payload):
                print(f"New order: {payload}")

            # Or:
            app.listen("events", my_callback)
        """
        if callback is not None:
            self._start_listener(channel, callback)
            return callback

        def decorator(func):
            self._start_listener(channel, func)
            return func

        return decorator

    def _start_listener(self, channel, callback):
        if not self.database_url:
            raise RuntimeError("database URL required for LISTEN/NOTIFY")
        # Register under the lock, double-checking inside: a re-registration of
        # the SAME channel (hot reload re-running @app.listen, or a double call)
        # must be a no-op — there is no native unlisten primitive, so issuing a
        # second _db_listen would leak an extra native listener thread that fires
        # every NOTIFY twice. First registration wins.
        with self._listeners_lock:
            if channel in self._listeners:
                return
            _db_listen(self.database_url, channel, callback)
            self._listeners[channel] = callback

    def _native_shutdown_databases(self):
        """Databases to disconnect during native shutdown (dedup by identity).

        The native ``run()`` path pre-initializes the module-level ``get_db()``
        singleton and hands its pool to Zig, while the ``db`` property may have
        created ``self._db`` for ORM access. Either (or both, or neither) may
        exist; disconnect each distinct one exactly once. Peeking the singleton
        via the module global avoids CREATING a pool during shutdown.
        """
        seen: list = []
        if self._db is not None:
            seen.append(self._db)
        if self.database_url:
            from hyperdjango import database as _database_module

            singleton = _database_module._db
            if singleton is not None and not any(singleton is s for s in seen):
                seen.append(singleton)
        return seen

    # --- Background tasks ---

    def task(self, func=None, **kwargs):
        """Register a background task.

        Usage:
            @app.task
            async def send_email(to, subject):
                ...

            send_email.delay("user@example.com", "Hello")
        """
        # Lazy: the tasks subsystem pulls in the scheduler + typeguard, which
        # only matter once an app registers a background task.
        from hyperdjango.tasks import task as _task_decorator

        return _task_decorator(func, **kwargs)

    # --- OAuth2 ---

    def oauth2(self, providers, secret=None, **kwargs):
        """Configure OAuth2 authentication with one or more providers.

        Usage:
            from hyperdjango.auth.oauth2 import google, github
            app.oauth2([
                google(client_id="...", client_secret="..."),
                github(client_id="...", client_secret="..."),
            ], secret="your-secret")
        """
        # Circular: auth.oauth2 imports HTTPException from app
        from hyperdjango.auth.oauth2 import OAuth2

        # Deferred: hyperdjango.auth.sessions transitively pulls in the
        # Django forms/ORM/template compat layer (see hyperdjango.validation),
        # a real cost (~100ms import time) that shouldn't be paid by every
        # app just for importing HyperApp — only apps that actually call
        # .oauth2() (or otherwise touch SessionAuth) need it.
        from hyperdjango.auth.sessions import SessionAuth

        # Never sign session cookies with a hardcoded, source-known constant.
        # The old placeholder default was non-empty, so it slipped past the
        # prod-config validator while letting anyone who reads the source forge
        # session cookies. Resolve an explicit secret, then fall
        # back to configured settings; if still unset, fail closed in production
        # and (mirroring HyperAdmin) auto-generate a random dev key + warn.
        resolved_secret = (
            secret or get_setting("SESSION_SECRET") or get_setting("SECRET_KEY")
        )
        if not resolved_secret:
            if not self.debug:
                raise ValueError(
                    "app.oauth2() requires a secret in production. Pass "
                    "secret=... or set SESSION_SECRET / SECRET_KEY — refusing "
                    "to sign session cookies with an insecure default."
                )
            resolved_secret = secrets.token_urlsafe(32)
            logger.warning(
                "app.oauth2() using an auto-generated session secret "
                "(sessions won't survive restart). Set secret= or "
                "SESSION_SECRET / SECRET_KEY explicitly for production."
            )

        oauth = OAuth2(secret=resolved_secret, **kwargs)
        for provider in providers:
            oauth.add_provider(provider)

        # Auto-connect to session auth if available in middleware stack
        mw_list = self._middleware._middleware or []
        for mw in mw_list:
            if isinstance(mw, SessionAuth):
                oauth.set_session_auth(mw)
                break
        else:
            # Create default session auth
            sa = SessionAuth(secret=oauth.secret)
            self.use(sa)
            oauth.set_session_auth(sa)

        self.use(oauth)
        return oauth

    # --- Middleware ---

    def use(self, middleware):
        """Add middleware to the stack.

        Middleware is an async callable: (request, call_next) -> response.
        Or an instance with __call__(request, call_next).

        Usage:
            app.use(CORSMiddleware(origins=["*"]))

            @app.use
            async def timing(request, call_next):
                ...
        """
        if callable(middleware):
            self._middleware.add(middleware)
            self._cached_handler = None  # Invalidate cached handler chain
        return middleware

    def middleware(self, func):
        """Decorator to add middleware."""
        self._middleware.add(func)
        # Invalidate the cached handler chain (parity with use()). Without this,
        # middleware registered after the chain was first built — hot reload, or
        # any post-first-request registration — is silently dropped on the ASGI path.
        self._cached_handler = None
        return func

    # --- Lifecycle hooks ---

    def on_startup(self, func):
        """Register a startup hook."""
        self._on_startup.append(func)
        return func

    def on_shutdown(self, func):
        """Register a shutdown hook."""
        self._on_shutdown.append(func)
        return func

    # --- Database ---

    @property
    def db(self):
        """Access the database connection pool."""
        db = self._db
        if db is not None:
            return db
        if not self.database_url:
            raise RuntimeError("No database configured. Pass database= to HyperApp().")
        # Double-checked locking: build the Database fully, then publish it
        # under the lock so two concurrent cold-start callers can't each spin
        # up a Database (and its connection pool).
        with self._db_lock:
            if self._db is None:
                from hyperdjango.database import Database

                self._db = Database(self.database_url)
            return self._db

    # --- Templates ---

    def render(self, template_name, context=None, status=200):
        """Render a Jinja2 template and return an HTML Response."""
        engine = self._template_engine
        if engine is None:
            # Double-checked locking. Build and FULLY configure the engine in a
            # local, then publish it as the single last assignment. Publishing
            # self._template_engine before registering the static/app_version/
            # site globals would let a concurrent renderer grab a
            # half-configured engine (missing {{ static(...) }} etc.).
            with self._template_lock:
                engine = self._template_engine
                if engine is None:
                    engine = TemplateEngine(
                        self.templates_dir or "templates",
                        auto_reload=self.debug,
                    )
                    # Register static file helpers in templates
                    engine.add_global("static", get_static_url)
                    engine.add_filter("static", get_static_url)
                    # Versioned URL helper (dev: ?v=hash, prod: delegates to static)
                    engine.add_global("static_url", get_static_url_versioned)
                    engine.add_filter("static_url", get_static_url_versioned)
                    # App version string for manual injection in templates
                    engine.add_global("app_version", _get_app_version_string)
                    # Site config for white-label branding ({{ site.name }}, {{ site_css }})
                    if self.site_config is not None:
                        engine.add_global("site", self.site_config)
                        engine.add_global("site_css", self.site_config.to_css_vars())
                    # Publish only after full configuration (single assignment).
                    self._template_engine = engine

        html = engine.render(template_name, context or {})
        return Response.html(html, status=status)

    # --- Request handling ---

    async def handle(self, request):
        """Handle a request through the middleware stack and router.

        Exception resolution order:
        1. Custom exception handlers (registered via @app.exception_handler)
        2. HTTPException → JSON {"detail": ...} with status code
        3. Other exceptions → debug HTML page or generic 500 JSON
        """
        # Build the handler chain: middleware → guarded router dispatch (cached).
        # _guarded_dispatch normalizes handler/router exceptions to a Response at
        # the innermost point, so every response-decorating middleware still runs
        # on error responses (security headers, CORS, rate-limit, cookie re-save).
        if self._cached_handler is None:
            self._cached_handler = self._middleware.wrap(self._guarded_dispatch)
        handler = self._cached_handler

        # Parity with the native path: expose the owning app to middleware BEFORE
        # the chain runs (the native wrapper sets req.app at construction), and
        # mint the request-id + install the request-scope log context up front so
        # every middleware and handler sees the same request.app / request.request_id.
        request.app = self
        request_id = _resolve_request_id(request)
        request.request_id = request_id
        token = log_context.set(_request_log_context(request_id))
        try:
            try:
                response = await handler(request)
            # blind-except: request error boundary — a middleware exception is normalized into a Response via _resolve_exception; handled, not swallowed
            except Exception as exc:
                # Safety net: reaching here means a middleware itself raised
                # (outside the inner boundary). Normalize it so we never leak a
                # raw exception.
                response = await self._resolve_exception(request, exc)
        finally:
            log_context.reset(token)
        # Echo the correlation id (both dispatch paths do this identically); the
        # ASGI path keeps true chunked streaming, so bodies are NOT materialized.
        if isinstance(response, Response):
            response.headers.setdefault("x-request-id", request_id)
        return response

    async def _resolve_exception(self, request, exc):
        """Normalize an exception into a Response.

        Resolution order: custom exception handler (walks MRO) → HTTPException
        JSON → debug traceback page → generic 500. Kept as a method so both the
        innermost dispatch boundary and the outer safety net share one policy.
        """
        custom_handler = self._find_exception_handler(exc)
        if custom_handler is not None:
            if inspect.iscoroutinefunction(custom_handler):
                return await custom_handler(request, exc)
            return custom_handler(request, exc)
        # Intentional HTTP results (HTTPException + every REST APIException) go
        # through the single mapper — one body shape, headers forwarded.
        if isinstance(exc, HTTPException):
            return exception_to_response(exc)
        if self.debug:
            tb = _html.escape(traceback.format_exc())
            exc_name = _html.escape(type(exc).__name__)
            exc_msg = _html.escape(str(exc))
            req_info = _html.escape(f"{request.method} {request.path}")
            qs = _html.escape(request.query_string or "")
            return Response.html(
                f"<html><body style='font-family:system-ui;margin:2em;'>"
                f"<h1 style='color:#c00'>{exc_name}</h1>"
                f"<p style='font-size:1.2em'>{exc_msg}</p>"
                f"<h3>Request</h3>"
                f"<p><code>{req_info}</code></p>"
                f"{'<p>Query: <code>' + qs + '</code></p>' if qs else ''}"
                f"<h3>Traceback</h3>"
                f"<pre style='background:#f5f5f5;padding:1em;overflow:auto'>{tb}</pre>"
                f"</body></html>",
                status=500,
            )
        # Generic 500 — same unified {"detail","status"} body as everywhere else.
        return exception_to_response(exc)

    async def _guarded_dispatch(self, request):
        """Innermost boundary: route + normalize exceptions to a Response.

        Wrapping ``_dispatch`` here (rather than catching outside the middleware
        chain) means the middleware stack sees a Response for 4xx/5xx too, so
        response middleware decorates error responses just like success ones.
        """
        try:
            return await self._dispatch(request)
        # blind-except: innermost dispatch boundary — any handler exception is normalized into a Response so response middleware sees it; handled, not swallowed
        except Exception as exc:
            return await self._resolve_exception(request, exc)

    async def _dispatch(self, request):
        """Route a request to its handler."""
        route, params = self.router.resolve(request.method, request.path)

        # APPEND_SLASH: redirect to URL with trailing slash (301)
        if route is _APPEND_SLASH_REDIRECT:
            redirect_to = params.get("redirect_to", request.path + "/")
            qs = request.query_string
            if qs:
                redirect_to = f"{redirect_to}?{qs}"
            return Response.redirect(redirect_to, status=301)

        if route is None:
            # Try serving static files
            if self.static_dir and request.method == "GET":
                static_response = self._try_static(request.path, request)
                if static_response:
                    return static_response
            raise HTTPException(404, "Not Found")

        request.path_params = params
        # request.app is set at the dispatch boundary (handle()) before the
        # middleware chain runs, so middleware sees it too; re-assert here as a
        # harmless idempotent fallback for any direct _dispatch() caller/test.
        request.app = self

        # Call the handler, injecting registered services
        handler = route.handler
        kwargs = dict(params)

        # Auto-inject services by type annotation (shared with the native path)
        self._inject_services(handler, kwargs)

        if route.is_async:
            response = await handler(request, **kwargs)
        else:
            response = handler(request, **kwargs)

        # Convert handler return to Response via the ONE shared contract so the
        # native path (_inner_dispatch) and ASGI path can never drift.
        return coerce_response(response)

    def _inject_services(self, handler, kwargs):
        """Auto-inject registered services into ``kwargs`` by type annotation.

        Shared by the ASGI (``_dispatch``) and native (``_wrap_handler_for_zig``)
        dispatch paths so service DI behaves identically regardless of which
        server is running — the two paths must never drift. Mutates ``kwargs``
        in place. The per-handler annotation scan is memoized on the handler.
        """
        if not self._services:
            return
        # dynamic-attr: memo attribute we attach to an arbitrary user handler callable; absent on first call
        injectable = getattr(
            handler, "_injectable_params", None
        )  # cached annotation lookup
        if injectable is None:
            try:
                hints = handler.__annotations__
            except AttributeError:
                hints = {}
            injectable = {
                k: v for k, v in hints.items() if k not in _SKIP_INJECTION_PARAMS
            }
            handler._injectable_params = injectable
        for param_name, param_type in injectable.items():
            if param_name in kwargs:
                continue  # path param takes precedence
            service = self._services.get(param_type)
            if service is not None:
                kwargs[param_name] = service

    def _try_static(self, path, request=None):
        """Try to serve a static file (cached, with ETag / 304 support).

        On a cache hit the file is served straight from memory after a single
        ``os.stat`` mtime revalidation — no open/read. ``request`` (when passed)
        enables ``If-None-Match`` conditional requests → ``304 Not Modified``.
        """
        if not self.static_dir:
            return None

        # Strip /static/ prefix if present (path is the full request path)
        clean = path.lstrip("/")
        clean = clean.removeprefix("static/")

        # Security: prevent path traversal
        clean = os.path.normpath(clean)
        if clean.startswith(".."):
            return None

        static_path = Path(self.static_dir)
        file_path = static_path / clean

        # One stat: existence + regular-file check + mtime/size for revalidation.
        try:
            st = file_path.stat()
        except OSError, ValueError:
            return None
        if not _stat.S_ISREG(st.st_mode) or static_path not in file_path.parents:
            return None

        key = str(file_path)
        mtime_ns = st.st_mtime_ns
        size = st.st_size

        entry = None
        with self._static_cache_lock:
            cached = self._static_cache.get(key)
            if cached is not None and cached[3] == mtime_ns and cached[4] == size:
                # Fresh: promote to MRU and reuse the cached bytes (no read).
                self._static_cache.move_to_end(key)
                entry = cached

        if entry is None:
            # Miss or stale (mtime/size changed): read once and (re)populate.
            try:
                body = file_path.read_bytes()
            except OSError:
                return None
            content_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
            etag = f'"{size:x}-{mtime_ns:x}"'
            entry = (body, content_type, etag, mtime_ns, size)
            self._static_cache_put(key, entry, size)

        body, content_type, etag = entry[0], entry[1], entry[2]

        # Conditional GET: honor If-None-Match, skipping the body on a match.
        if request is not None:
            inm = request.headers.get("if-none-match")
            if inm and _etag_matches(inm, etag):
                not_modified = Response(body=b"", status=304)
                not_modified.headers["etag"] = etag
                return not_modified

        resp = Response(body=body, content_type=content_type)
        resp.headers["etag"] = etag
        # Never MIME-sniff a served static asset (a user-uploaded .svg/.html
        # under a static dir must not execute inline) — parity with
        # Response.file() and the native serveFile path.
        resp.headers["x-content-type-options"] = "nosniff"
        return resp

    def _static_cache_put(self, key, entry, size):
        """Insert a static entry, evicting LRU entries to stay within budget."""
        with self._static_cache_lock:
            old = self._static_cache.pop(key, None)
            if old is not None:
                self._static_cache_bytes -= old[4]
            # A file larger than the whole budget is served but not cached.
            if size > self._static_cache_max_bytes:
                return
            self._static_cache[key] = entry
            self._static_cache_bytes += size
            while (
                self._static_cache_bytes > self._static_cache_max_bytes
                and len(self._static_cache) > 1
            ):
                _, evicted = self._static_cache.popitem(last=False)
                self._static_cache_bytes -= evicted[4]

    # --- File-based route discovery ---

    def discover_routes(self, views_dir=None):
        """Discover and register file-based routes."""
        # Deferred: hyperdjango.routing.file_router pulls in django.http
        # (~34ms import time) — only apps that actually call this method
        # need it, not every HyperApp.
        from hyperdjango.routing.file_router import discover_routes as _discover_routes

        routes = _discover_routes(
            views_dir or self.views_dir or get_setting("FILE_ROUTING_DIR")
        )
        for method, pattern, handler, name in routes:
            self.router.add(method, pattern, handler, name)

    # --- ASGI interface ---

    async def __call__(self, scope, receive, send):
        """ASGI application interface.

        Works with uvicorn, hypercorn, granian, etc.
        """
        if scope["type"] == "lifespan":
            await self._handle_lifespan(scope, receive, send)
            return

        if scope["type"] == "websocket":
            await self._handle_websocket(scope, receive, send)
            return

        if scope["type"] != "http":
            return

        # Read request body (with size limit)
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if self.max_body_size and len(body) > self.max_body_size:
                response = Response.error(413, "Request body too large")
                await response.send(send)
                return
            if not message.get("more_body", False):
                break

        request = Request.from_asgi(scope, body)
        response = await self.handle(request)
        await response.send(send)

    async def _handle_websocket(self, scope, receive, send):
        """Handle WebSocket connections."""
        path = scope.get("path", "/")
        handler = self._ws_handlers.get(path)
        if handler is None:
            await send({"type": "websocket.close", "code": 4004})
            return

        ws = WebSocket(scope, receive, send)
        # Wait for connect message
        msg = await receive()
        if msg["type"] == "websocket.connect":
            try:
                await handler(ws)
            except WebSocketDisconnect:
                pass
            # blind-except: websocket handler failure must close the socket with 1011, not propagate into the ASGI server
            except Exception:
                with contextlib.suppress(Exception):
                    await ws.close(1011, "Internal error")

    async def _handle_lifespan(self, scope, receive, send):
        """Handle ASGI lifespan events (startup/shutdown)."""
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    await self._startup()
                    await send({"type": "lifespan.startup.complete"})
                # blind-except: ASGI lifespan protocol requires reporting startup failure via lifespan.startup.failed, not raising into the server
                except Exception as e:
                    await send({"type": "lifespan.startup.failed", "message": str(e)})
                    return
            elif message["type"] == "lifespan.shutdown":
                await self._shutdown()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _startup(self):
        """Run startup hooks."""
        # Discover file-based routes
        if self.views_dir:
            self.discover_routes()

        # Connect database
        if self.database_url and self._db is not None:
            await self._db.connect()

        for hook in self._on_startup:
            if inspect.iscoroutinefunction(hook):
                await hook()
            else:
                hook()

    async def _shutdown(self):
        """Run shutdown hooks."""
        for hook in self._on_shutdown:
            if inspect.iscoroutinefunction(hook):
                await hook()
            else:
                hook()

        # Disconnect database
        if self._db is not None:
            await self._db.disconnect()

    # --- Development server ---

    def run(self, host=None, port=None, prod: bool = False):
        """Start the server.

        Bind address resolution: explicit argument > HOST/PORT setting
        (env HYPER_HOST/HYPER_PORT or Django settings) > literal default. Passing
        host=None/port=None (the default) lets the setting take effect; the
        DEFAULTS carry the 127.0.0.1:8000 literal fallback.

        prod=True enables production mode:
        - Forces debug=False
        - Validates configuration (warns on default secrets, missing DATABASE_URL)
        - Disables hot reload
        - Installs SIGTERM/SIGINT graceful shutdown handler
        """
        if host is None:
            host = get_setting("HOST")
        if port is None:
            port = get_setting("PORT")

        if prod:
            self.debug = False

        # Let every middleware that cares vet the composed stack (ordering
        # constraints, incompatible pairings). Generic dispatch — the platform
        # names no middleware; see MiddlewareStack.validate / StackValidator.
        # Runs unconditionally (debug included): a stack that silently
        # misbehaves is a misconfiguration, not a prod-only risk.
        self._middleware.validate()

        # Production validation
        if prod or not self.debug:
            self._validate_production_config(prod=prod)

        # Discover file-based routes before anything
        if self.views_dir:
            self.discover_routes()

        # Register static file route for Zig native server
        if self.static_dir:
            static_app = self

            async def _static_handler(request, **kwargs):
                resp = static_app._try_static(request.path, request)
                if resp:
                    return resp
                # Honor the unified {"detail","status"} error contract (routed
                # through exception_to_response by the dispatch boundary) instead
                # of a bespoke {"error": ...} shape.
                raise HTTPException(404, "Not Found")

            self.router.add("GET", "/static/{filepath:path}", _static_handler)

        # Optimize router: compress paths, sort children
        self.router.finalize()

        # Signal handlers are installed in the Zig server (self-pipe + atomic flag).
        # SIGTERM/SIGINT cause server.run() to return, then Python-side cleanup runs.

        # Write PID file for management commands (hyper stop/status)
        pid_file = Path(f".hyper.{port}.pid")
        pid_file.write_text(str(os.getpid()))

        # Crash observability: dump every thread's Python traceback on a
        # native fatal signal (SIGSEGV/SIGBUS/SIGFPE/SIGABRT from the Zig
        # extension). ZERO hot-path cost — faulthandler only installs signal
        # handlers here; nothing runs until a fault fires. Without this, a
        # native crash in production is a silent dead process; with it, the
        # crash file names the thread and the exact native call. The file
        # sits next to the PID file so operators find it where they look
        # first, and it is only created on first fault (opened lazily-ish:
        # created empty at startup, populated only by a crash).
        crash_file = pid_file.with_suffix(".crash.log")
        try:
            self._crash_fh = crash_file.open("a")
            faulthandler.enable(file=self._crash_fh, all_threads=True)
        except OSError:
            # Unwritable CWD (read-only deploy) — run without the crash file
            # rather than refusing to start; stderr still gets the default.
            faulthandler.enable(all_threads=True)

        try:
            self._run_native(host, port)
        # blind-except: top-level native-server boundary — any startup/run failure is logged and the process exits non-zero via SystemExit
        except Exception:
            # exception(), not error(): the traceback is the ONLY diagnostic a
            # supervisor/test harness gets from an exit-1 startup failure.
            logger.exception("Zig server failed")
            sys.exit(1)
        finally:
            pid_file.unlink(missing_ok=True)
            # Clean shutdown: don't litter empty crash files. A populated one
            # (a real crash happened earlier in this process's life) stays.
            with contextlib.suppress(OSError, ValueError, AttributeError):
                # dynamic-attr: _crash_fh only exists when the crash file opened successfully above; AttributeError is the no-file branch, not a probe of unknown objects
                faulthandler.disable()
                self._crash_fh.close()
                if crash_file.stat().st_size == 0:
                    crash_file.unlink()

    def _validate_production_config(self, prod: bool = False):
        """Validate production configuration.

        When prod=True, a missing/empty SECRET_KEY is fatal: sessions, CSRF and
        signing all depend on it, so booting a production server with an empty
        key is an insecure default we refuse to start silently (fail closed).
        Everything else is a loud warning.
        """
        warnings: list[str] = []

        if self.debug:
            warnings.append(
                "debug=True in production — stack traces will leak to clients"
            )

        # Read the RESOLVED setting (constructor / HYPER_DATABASE_URL / Django),
        # not self.database_url — so the guard reflects exactly what get_db()
        # will use and can't false-warn when the URL came from the environment.
        if not get_setting("DATABASE_URL"):
            warnings.append("No DATABASE_URL configured")

        if not self.allowed_hosts:
            warnings.append("ALLOWED_HOSTS is empty — host header validation disabled")

        # SECRET_KEY is security-critical: in production an empty/default key
        # means forgeable sessions/CSRF. Fail closed rather than boot silently.
        if not self.secret_key:
            if prod:
                for w in warnings:
                    logger.warning("{msg}", msg=w)
                raise RuntimeError(
                    "SECRET_KEY is not set — refusing to start a production "
                    "server with an insecure default. Set SECRET_KEY (env "
                    "HYPER_SECRET_KEY / Django settings) before running with "
                    "prod=True."
                )
            warnings.append(
                "SECRET_KEY is not set — sessions and CSRF will be insecure"
            )

        if warnings:
            for w in warnings:
                logger.warning("{msg}", msg=w)

    def _render_security_header_block(self) -> str:
        """Render the configured SecurityHeadersMiddleware's static header dict
        into a native "\\r\\nKey: Value" block, or "" if none is installed.

        Only the STATIC security headers (X-Frame-Options/nosniff/HSTS/CSP/
        Referrer-Policy/COOP/Permissions-Policy) — the per-request SSL/UA logic
        stays in the middleware for routed responses.
        """
        from hyperdjango.standalone_middleware import SecurityHeadersMiddleware

        for mw in self._middleware._middleware or []:
            if isinstance(mw, SecurityHeadersMiddleware):
                return "".join(
                    f"\r\n{_sanitize_header(k)}: {_sanitize_header(v)}"
                    for k, v in mw._headers.items()
                )
        return ""

    def _run_native(self, host, port):
        """Run with the native Zig HTTP server.

        Reads HTTP_SERVER and THREAD_POOL_SIZE from conf settings.
        """
        http_server = get_setting("HTTP_SERVER")
        thread_pool_size = get_setting("THREAD_POOL_SIZE")

        # Hand the resolved settings down to the native Zig server.
        _export_native_config()

        # Use the already-resolved constructor value (ctor arg > setting > env),
        # matching the ASGI __call__ path which enforces self.max_body_size, so a
        # HyperApp(max_body_size=...) override is honored on the native path too.
        max_body_size = int(self.max_body_size)
        server = HyperServer(host, port, max_body_size)

        # Give framework-generated (short-circuit) native responses — the no-route
        # 404, framing 400s, 500/503, CORS preflight — the SAME security headers
        # (X-Frame-Options/nosniff/HSTS/CSP) the Python SecurityHeadersMiddleware
        # sets on routed responses. Routed responses keep getting them from the
        # middleware (a separate writer), so this never double-applies.
        sec_block = self._render_security_header_block()
        if sec_block:
            server.configure_security_headers(sec_block)

        # Thread pool size is configured via HYPER_THREAD_POOL_SIZE env var
        # (read directly by the Zig server at startup)

        # Configure database — ONE shared pool for both Zig server and Python ORM.
        # Pre-initialize get_db() so the pool exists, then pass the same pool handle
        # to the Zig server. This avoids the dual-pool problem where two separate
        # pools compete for PostgreSQL connections.
        if self.database_url:
            from hyperdjango.database import get_db

            db = get_db()  # Creates the pool lazily via _acquire_pool()
            server.configure_db_handle(db._pool_handle)

        route_count = 0
        for route in self.router.routes():
            handler = route.handler
            method = route.method
            pattern = route.pattern

            # Build param_types_json for Zig-native type coercion.
            # Format: "name:type|name:type|..." where type is int/float/str/bool.
            # Zig creates typed Python objects (PyLong, PyFloat, etc.) directly
            # instead of returning all strings for Python to convert.
            param_types_json = _build_param_types_json(
                route.param_names,
                route.param_converters,
            )

            # Wrap handler for Request construction, middleware, and response formatting.
            # Middleware stack runs on each request (SessionAuth, CORS, etc.).
            # No converter logic needed — Zig delivers pre-typed path_params.
            wrapped = self._wrap_handler_for_zig(
                handler, self._middleware, self._resolve_exception, app=self
            )

            # Register with Zig server — typed route for Zig-native coercion
            if param_types_json:
                server.add_route_typed(method, pattern, wrapped, param_types_json)
            else:
                server.add_route(method, pattern, wrapped)
            route_count += 1

        # Register native auto-CRUD routes (served entirely in Zig — see
        # add_db_route; these deliberately bypass the Python middleware chain).
        for method, pattern, op, table, pk_column, pk_param, columns in self._db_routes:
            server.add_db_route(
                method, pattern, op, table, pk_column, pk_param, columns
            )

        # Register WebSocket handlers
        ws_count = 0
        if self._ws_handlers:
            for ws_path, ws_handler in self._ws_handlers.items():
                # Wrap the async ws handler for sync Zig callback
                wrapped_ws = self._wrap_ws_handler_for_zig(ws_handler)
                _server_add_ws_route(ws_path, wrapped_ws)
                ws_count += 1

        # Publish the REQUESTED bind port before the startup hooks run
        # (HyperServer binds in its constructor above), so a hook can wire a
        # front-door — e.g. an mTLS terminator's upstream — to it. This is the
        # port asked for, not necessarily the one the OS assigned: the native
        # HyperServer exposes no getsockname-style accessor, so an ephemeral
        # bind (PORT=0) publishes 0 here and the real port is NOT resolvable.
        # A consumer that forwards to bound_port (mTLS install()) must fail loudly
        # on 0 rather than forward to a bogus upstream — see MTLSTerminator.install.
        self.bound_port = port

        # Run startup hooks (same as ASGI lifespan startup)
        # Use a single event loop for all async startup hooks, then set it as
        # the main thread's loop so shutdown hooks can reuse it.
        _startup_loop = asyncio.new_event_loop()
        # Single-flow loop: startup/shutdown hooks run one at a time — DB
        # calls run inline (the default; not flagged multiplexing).
        asyncio.set_event_loop(_startup_loop)
        for hook in self._on_startup:
            if inspect.iscoroutinefunction(hook):
                _startup_loop.run_until_complete(hook())
            else:
                hook()

        # Internal readiness probe — registered LAST so its presence proves
        # all routes, middleware, and startup hooks are fully initialized.
        # e2e tests poll this to avoid the race where TCP accepts connections
        # before routes are registered (HyperServer binds in constructor).
        def _ready_handler(request):
            return Response.json({"status": "ready"})

        server.add_route(
            "GET", "/_ready", self._wrap_handler_for_zig(_ready_handler, app=self)
        )

        logger.info(
            "HyperDjango '{title}' on http://{host}:{port}",
            title=self.title,
            host=host,
            port=port,
        )
        logger.info(
            "{route_count} routes + {ws_count} WebSocket handlers, native Zig HTTP serving",
            route_count=route_count,
            ws_count=ws_count,
        )
        server.run()

        # server.run() returns after SIGTERM/SIGINT — run cleanup
        shutdown_timeout = int(get_setting("TASK_SHUTDOWN_TIMEOUT"))
        logger.info("Running on_shutdown hooks (timeout {t}s)...", t=shutdown_timeout)
        for hook in self._on_shutdown:
            try:
                if inspect.iscoroutinefunction(hook):
                    _startup_loop.run_until_complete(
                        asyncio.wait_for(hook(), timeout=shutdown_timeout)
                    )
                else:
                    hook()
            except TimeoutError:
                logger.error(
                    "Shutdown hook {hook} timed out after {t}s",
                    hook=hook,
                    t=shutdown_timeout,
                )
            # blind-except: shutdown teardown — one failing hook is logged and must not abort the remaining hooks or loop cleanup
            except Exception as exc:
                logger.error("Shutdown hook {hook} failed: {err}", hook=hook, err=exc)

        # Symmetric DB teardown: the ASGI _shutdown() disconnects the pool after
        # its hooks; the native path never did, leaking the pool. Disconnect the
        # SAME Database the native path connected — self._db if the `db` property
        # created one, and the module-level get_db() singleton the native run
        # path pre-initializes (line ~1644). Guarded/idempotent for the case
        # where neither was ever started.
        for _db_obj in self._native_shutdown_databases():
            try:
                _startup_loop.run_until_complete(_db_obj.disconnect())
            # blind-except: shutdown teardown — a failed disconnect is logged and must not abort loop cleanup
            except Exception as exc:
                logger.error("Database disconnect failed: {err}", err=exc)

        # Drop the LISTEN/NOTIFY dedup registry so a subsequent run() re-arms
        # listeners cleanly (there is no native unlisten to release the threads;
        # process exit reclaims them).
        with self._listeners_lock:
            self._listeners.clear()

        # Close all event loops cleanly to prevent __del__ fd=-1 errors
        _close_all_thread_loops()
        _startup_loop.close()
        logger.info("Cleanup complete. Exiting.")

        # Stop logger background writer before interpreter finalization.
        # Without this, the daemon writer thread may hold stdout lock when Python
        # finalizes, causing Fatal error: _enter_buffered_busy.
        with contextlib.suppress(Exception):
            _logger_core.stop_writer()
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        # blind-except: best-effort stream flush during interpreter shutdown; nothing remains that could handle the error
        except Exception:
            pass

    @staticmethod
    def _wrap_ws_handler_for_zig(handler):
        """Wrap an async WebSocket handler for the native Zig server.

        Zig calls this ONCE on connect with (conn_id, headers, path, query).
        We create a ZigWebSocket adapter and pass it to the handler,
        giving the handler full bidirectional I/O via _ws_send/_ws_recv.
        """
        from hyperdjango.websocket import (
            WebSocketDisconnect,
            ZigWebSocket,
            _ws_loop_pool,
        )

        # The ZigWebSocket is a self-managing connection object: entering its
        # context registers nothing extra and exiting it (finalize()) releases
        # everything the connection owns — selector reader, close frame,
        # telemetry, and the native fd/registry entry — exactly once. Both
        # wrappers below therefore just run the handler inside `with ws:`; no
        # site has to remember individual release steps.
        pool = _ws_loop_pool()

        if pool is None:
            # Thread-per-connection model (WEBSOCKET_CONCURRENCY=thread): run
            # the handler to completion on the accepting worker thread. Max
            # live connections == thread-pool size.
            def ws_callback(conn_id, headers, path, query_string):
                ws = ZigWebSocket(conn_id, headers, path, query_string)
                # If loop creation fails before `with ws:` takes ownership, the
                # native connection would leak (finalize never runs). finalize()
                # is idempotent, so release it here on a setup failure.
                try:
                    loop = asyncio.new_event_loop()
                except BaseException:
                    ws.finalize()
                    raise
                # Thread mode: one connection per loop — DB calls run inline
                # (the default). The shared-loop pool is the multiplexing model
                # and flags its loops for offload; see _WsLoopPool.
                try:
                    with ws:
                        try:
                            loop.run_until_complete(handler(ws))
                        except WebSocketDisconnect:
                            raise
                        # blind-except: close 1011 (ASGI parity) before finalize, then re-raise to the outer logger
                        except Exception:
                            loop.run_until_complete(ws.close(1011, "Internal error"))
                            raise
                except WebSocketDisconnect:
                    pass
                # blind-except: per-connection websocket handler failure is logged; must not crash the accept-worker thread
                except Exception:
                    logger.exception("WebSocket handler error")
                finally:
                    loop.close()

            return ws_callback

        # Shared event-loop pool (WEBSOCKET_CONCURRENCY=shared, the default):
        # schedule the handler on a persistent event-loop pool and return the
        # accept-worker immediately, so live connections are bounded by
        # fds/memory instead of the thread pool.
        #
        # CONTRACT: handlers must be cooperative — they may await network I/O
        # (send/recv here are non-blocking + selector-driven) but must NOT park
        # a thread per connection for its lifetime (e.g.
        # `await loop.run_in_executor(None, blocking_queue.get)`). Many such
        # handlers on one shared loop exhaust that loop's default executor and
        # stall. Feed handlers from other threads via an asyncio.Queue +
        # call_soon_threadsafe (as the framework's channel/room helpers do), or
        # select WEBSOCKET_CONCURRENCY=thread.
        async def _run_on_loop(ws):
            try:
                with ws:
                    try:
                        await handler(ws)
                    except WebSocketDisconnect:
                        raise
                    # blind-except: an unhandled handler error closes with 1011 (parity with the ASGI path), then re-raises to the outer logger
                    except Exception:
                        # Close with 1011 Internal Error (not the default 1000)
                        # BEFORE `with ws:` finalize()s — matches the ASGI path.
                        await ws.close(1011, "Internal error")
                        raise
            except WebSocketDisconnect:
                pass
            # blind-except: per-connection websocket handler failure is logged; must not kill the shared event-loop pool
            except Exception:
                logger.exception("WebSocket handler error")

        def ws_callback(conn_id, headers, path, query_string):
            ws = ZigWebSocket(conn_id, headers, path, query_string)
            # Ownership of ws transfers to `with ws:` INSIDE _run_on_loop, but
            # that only runs once the coroutine is scheduled. If submit() raises
            # (pool saturated/shutting down), the coroutine is never awaited and
            # the native connection would leak — finalize it here. finalize() is
            # idempotent, so the handler's `with ws:` stays correct on success.
            coro = _run_on_loop(ws)
            try:
                pool.submit(coro, conn_id)
            except BaseException:
                coro.close()
                ws.finalize()
                raise

        return ws_callback

    @staticmethod
    def _classify_for_zig(handler):
        """Classify a handler for Zig fast dispatch."""
        sig = inspect.signature(handler)
        params = list(sig.parameters.keys())
        param_types = {}

        for name in params:
            if name == "request":
                continue
            ann = sig.parameters[name].annotation
            if ann is int:
                param_types[name] = "int"
            elif ann is str:
                param_types[name] = "str"
            elif ann is float:
                param_types[name] = "float"
            else:
                param_types[name] = "str"

        is_async = inspect.iscoroutinefunction(handler)
        if not params or params == ["request"]:
            return ("simple_sync_noargs" if not is_async else "simple_async"), {}
        return ("simple_sync" if not is_async else "simple_async"), param_types

    @staticmethod
    def _wrap_async_for_zig(handler):
        """Wrap async handler to be called synchronously from Zig.

        Reuses the per-worker-thread event loop and the _run_dispatch fast path
        instead of creating and closing a fresh loop on every request
        (new_event_loop + run_until_complete + close was ~29µs/request; the
        fast path is ~0.2µs when the handler completes without suspending).
        """

        def sync_wrapper(*args, **kwargs):
            loop = _get_thread_event_loop()
            return _run_dispatch(loop, handler(*args, **kwargs))

        sync_wrapper.__name__ = handler.__name__
        sync_wrapper.__wrapped__ = handler
        return sync_wrapper

    @staticmethod
    def _wrap_handler_for_zig(
        handler, middleware_stack=None, exc_resolver=None, app=None
    ):
        """Wrap handler for Zig enhanced dispatch.

        The Zig server calls enhanced handlers with **kwargs containing:
            method, path, body (bytes), query_string, headers (dict), path_params (dict)

        Path params arrive pre-typed from Zig (int, float, bool, str) — no Python-side
        conversion needed. Zig creates PyLong/PyFloat/PyBool/PyUnicode objects directly
        via the C API when param_types_json metadata is registered with the route.

        Middleware (SessionAuth, CORS, etc.) runs on each request before the handler.

        Our handlers expect (request, **path_params). This wrapper bridges that gap
        by constructing a Request from the Zig-provided kwargs.
        """

        sig = inspect.signature(handler)
        params = list(sig.parameters.keys())
        has_request = len(params) > 0 and params[0] == "request"

        # Build the middleware-wrapped dispatch function once at registration time.
        # The inner async function dispatches to the route handler.
        async def _inner_dispatch(req):
            # Innermost exception-to-Response boundary: normalize any exception
            # raised by the user handler into a Response *before* the middleware
            # chain unwinds. This is what lets response-decorating middleware
            # (SecurityHeaders, CORS, RateLimit headers, SessionAuth cookie
            # re-save, Version, telemetry finalize) run on 4xx/5xx responses too:
            # a raised HTTPException/Exception is normalized before they unwind.
            try:
                call_kwargs = dict(req.path_params)
                # Auto-inject registered services by type annotation — identical
                # policy to the ASGI path (_dispatch) via the shared routine, so
                # a handler like `async def h(request, db: Database)` resolves
                # `db` on the native path exactly as under ASGI.
                if app is not None:
                    app._inject_services(handler, call_kwargs)
                if has_request:
                    result = handler(req, **call_kwargs)
                else:
                    result = handler(**call_kwargs)
                # Await coroutines (async handlers wrapped by _wrap_async_for_zig
                # are already sync, but decorators like @require_auth are async)
                if asyncio.iscoroutine(result):
                    result = await result
            # blind-except: request error boundary on the native path — any handler exception is normalized into a Response via the resolver, matching the ASGI path
            except Exception as exc:
                # Route through the app's resolver so custom
                # @app.exception_handler handlers fire on the Zig path exactly
                # as they do on the ASGI path (single policy). Falls back to the
                # module normalizer for internal routes wired without a resolver.
                if exc_resolver is not None:
                    return await exc_resolver(req, exc)
                return _zig_exception_to_response(exc)
            # ONE shared return-type contract with the ASGI path (coerce_response):
            # str→text/plain, dict/list→JSON 200, (body,status[,headers])→coerced,
            # any other scalar→JSON 200 — identical to ASGI, no divergence.
            return coerce_response(result)

        # Wrap with middleware stack if available
        if middleware_stack is not None and middleware_stack._middleware:
            _dispatch_chain = middleware_stack.wrap(_inner_dispatch)
        else:
            _dispatch_chain = _inner_dispatch

        def wrapper(
            method="GET",
            path="/",
            body=b"",
            query_string="",
            headers=None,
            path_params=None,
            multipart_parts=None,
            stream_content_length=None,
            peer=None,
            headers_lowercased=False,
        ):
            # POSITIONAL signature (round-9 Part 5): the native Zig server calls
            # this via PyObject_Vectorcall with a fixed positional arg vector —
            # no per-request kwargs dict on either side. Every parameter keeps a
            # default so the historical keyword-subset callers (the native-path
            # parity/dispatch unit tests that invoke this wrapper directly) still
            # work unchanged.
            #
            # Headers: the native server pre-lowercases header names (ASCII byte
            # op in the request arena) and passes headers_lowercased=True, so we
            # ADOPT the dict directly (Part 6) — skipping the per-key k.lower()
            # that CaseInsensitiveDict.__init__ runs. Any non-native caller omits
            # the flag (defaults False), so its keys are lowered normally and
            # mixed-case lookups keep working.
            if headers is None:
                req_headers = {}
            elif headers_lowercased:
                req_headers = CaseInsensitiveDict._adopt_lowercased(headers)
            else:
                req_headers = headers

            # A minimal scope (peer address when the Zig side threads it via the
            # _peer hook) makes peer_ip / client_ip / is_secure resolve through
            # the SAME Request code as ASGI instead of collapsing every client to
            # 127.0.0.1. Inlined _build_native_scope (identical policy).
            scope = {"client": (peer[0], int(peer[1]))} if peer is not None else None
            req = Request(
                method=method,
                path=path,
                headers=req_headers,
                query_string=query_string,
                body=body,
                path_params=path_params if path_params is not None else {},
                scope=scope,
            )

            # Parity with the ASGI path (handle() sets request.app before the
            # chain): the native path must also expose the owning HyperApp so
            # handlers/helpers that reach through request.app (e.g.
            # shortcuts.render → request.app.render(...)) work in production.
            req.app = app

            # Inject pre-parsed multipart — skips FFI round-trip in files()/form()
            if multipart_parts is not None:
                req._multipart_parts = multipart_parts

            # Streaming body: large uploads pulled from socket on demand
            if stream_content_length is not None:
                req._stream_content_length = int(stream_content_length)

            try:
                # Run through middleware → handler chain.
                # Thread-local event loop: created once per Zig worker thread,
                # reused for every request. 24 threads = 24 loops, never destroyed.
                loop = _get_thread_event_loop()
                # Fast-path dispatch: skips the event-loop round-trip when the
                # handler completes without suspending on real I/O (see
                # _run_dispatch). _finalize_native wraps the chain to add the
                # shared request-id observability.
                result = _run_dispatch(
                    loop, _finalize_native(app, req, _dispatch_chain(req))
                )

                # Enhanced-response contract to Zig:
                #   non-streaming → (status, content_type, body:bytes, extra_headers)
                #   streaming     → (status, content_type, b"", extra_headers, pull)
                # A streaming Response is driven ONE CHUNK AT A TIME by the Zig
                # chunked-send loop (Transfer-Encoding: chunked); pull() is bound
                # to THIS worker's event loop so the iterator advances on the same
                # loop/thread the handler ran on.
                if (
                    isinstance(result, Response)
                    and result._streaming
                    and result._stream_iter is not None
                ):
                    return _response_to_zig_stream_tuple(result, loop)
                return _response_to_zig_tuple(result)
            # blind-except: native-path safety net — a middleware/finalize exception is normalized into a Response via the resolver rather than leaked back to Zig
            except Exception as exc:
                # Safety net: _inner_dispatch normally converts exceptions to a
                # Response before unwinding, so reaching here means a middleware
                # (or _finalize_native) itself raised. Route through the app's
                # resolver — so custom @app.exception_handler + the debug page
                # fire on the native path exactly as on ASGI (parity with
                # _resolve_exception), not the bare exception_to_response mapper.
                loop = _get_thread_event_loop()
                # Re-establish the request-id log context around the safety-net
                # resolution: _finalize_native already reset its token before this
                # exception unwound to here, so WITHOUT this the unhandled-500 log
                # line (emitted inside _resolve_exception → exception_to_response
                # → _logger.exception) would carry NO request_id — the hardest
                # native 500 to correlate. Mirror the ASGI path, which keeps its
                # safety net inside the same request-id context scope.
                ctx_token = None
                if req.request_id:
                    ctx_token = log_context.set(_request_log_context(req.request_id))
                try:
                    if app is not None:
                        resolved = _run_dispatch(loop, app._resolve_exception(req, exc))
                    else:
                        resolved = exception_to_response(exc)
                # blind-except: last-resort fallback — the exception resolver itself failed; emit a unified error response, never leak a raw exception back to Zig
                except Exception:
                    # Last resort: the resolver itself failed — never leak a raw
                    # exception back to Zig; emit the unified {"detail","status"}.
                    resolved = exception_to_response(exc)
                finally:
                    if ctx_token is not None:
                        log_context.reset(ctx_token)
                # Echo the correlation id on the safety-net response too (parity
                # with the success path + the ASGI 500 path).
                if isinstance(resolved, Response) and req.request_id:
                    resolved.headers.setdefault("x-request-id", req.request_id)
                return _response_to_zig_tuple(resolved)

        wrapper.__name__ = handler.__name__
        wrapper.__wrapped__ = handler
        return wrapper


# Public re-export: HTTPException and exception_to_response are part of the
# hyperdjango.app surface; hyperdjango.exceptions is their canonical home.
from hyperdjango.exceptions import (  # noqa: F811,E402
    HTTPException,
    exception_to_response,
)
