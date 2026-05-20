"""
WebSocket support for HyperApp.

Features:
- Subprotocol negotiation (Sec-WebSocket-Protocol)
- Permessage-deflate extension awareness
- Configurable ping/pong keepalive
- Max message size enforcement
- Per-connection backpressure (bounded queue)

Usage:
    @app.websocket("/ws/chat")
    async def chat(ws):
        await ws.accept(subprotocol="chat-v1")
        while True:
            data = await ws.receive_text()
            await ws.send_text(f"Echo: {data}")
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from hyperdjango._lazy import SafeLazy
from hyperdjango.native import fast_json_dumps, fast_json_loads
from hyperdjango.telemetry import metrics as _tel_metrics

# ── Fallback blocking-recv executor ────────────────────────────────────────
#
# The receive path is normally non-blocking and selector-driven (see
# _recv_one: _ws_try_recv + loop.add_reader), so no thread is parked per
# message. This executor is a defensive FALLBACK, used only if the connection
# fd is unavailable or the running loop doesn't support add_reader — then the
# blocking `_ws_recv` is offloaded here. A single process-wide, bounded pool
# (never one-per-connection) keeps that fallback from multiplying threads.


# Double-checked locking, NOT functools.cache: under free-threaded CPython
# functools.cache is not atomic, so a racing first-call would build several
# ThreadPoolExecutors — extra threads that leak and blow the pool budget.
# The lock guarantees exactly one executor is ever constructed and published.
def _make_ws_recv_executor() -> ThreadPoolExecutor:
    from hyperdjango.conf import get_setting

    workers = int(get_setting("THREAD_POOL_SIZE", 24) or 24)
    return ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="hyperdjango-ws-recv"
    )


# Exactly one recv executor per process (SafeLazy — the audited DCL primitive):
# a fresh one per fallback recv would leak threads and blow the pool budget.
_ws_recv_executor_lazy: SafeLazy[ThreadPoolExecutor] = SafeLazy(_make_ws_recv_executor)


def _ws_recv_executor() -> ThreadPoolExecutor:
    return _ws_recv_executor_lazy.get()


# ── Shared event-loop pool (the default WebSocket concurrency model) ────────
#
# Default model: the native accept-worker thread runs a connection's handler
# to completion and is blocked for the connection's whole lifetime, so the
# max number of *live* connections equals the thread-pool size. That's great
# for throughput (real multi-core parallelism under free-threaded Python) but
# caps concurrent connections at HYPER_THREAD_POOL_SIZE.
#
# Shared-loop model: a small fixed set of persistent asyncio event loops (one
# per thread), each MULTIPLEXING many connections via the same add_reader
# receive path used everywhere else. The accept-worker only performs the
# handshake, then hands the connection to a loop and returns immediately — so
# concurrent connections are bounded by fds/memory, not by the thread pool,
# while still using every core (one loop per core). This is the standard
# high-connection-count async-server shape; it's opt-in while it soaks.


class _WsLoopPool:
    """A fixed set of event loops, each running forever on its own thread.

    Connections are assigned to loops round-robin by connection id, so a
    given connection's coroutine — and therefore all of its I/O and its final
    release — always run on exactly one loop thread (the single-owner
    invariant the native release path relies on)."""

    def __init__(self, n: int):
        from hyperdjango.database import mark_loop_multiplexing

        self._loops: list[asyncio.AbstractEventLoop] = []
        for i in range(n):
            loop = asyncio.new_event_loop()
            # Each loop multiplexes many connections, so a blocking DB
            # round-trip in a handler would stall all of them. Flag the loop
            # so the DB layer offloads those round-trips to its bounded
            # executor instead of running them inline. See
            # database.mark_loop_multiplexing.
            mark_loop_multiplexing(loop)
            threading.Thread(
                target=loop.run_forever, daemon=True, name=f"hyperdjango-ws-loop-{i}"
            ).start()
            self._loops.append(loop)

    def submit(self, coro, conn_id: int) -> None:
        loop = self._loops[conn_id % len(self._loops)]
        asyncio.run_coroutine_threadsafe(coro, loop)


_ws_pool: _WsLoopPool | None = None
_ws_pool_lock = threading.Lock()


def _ws_concurrency_mode() -> str:
    """Resolve the WebSocket concurrency model: "shared" (default) or "thread".

    Discoverable/overridable via the WEBSOCKET_CONCURRENCY setting
    (Django `HYPERDJANGO_WEBSOCKET_CONCURRENCY`, env
    `HYPER_WEBSOCKET_CONCURRENCY`, or DEFAULTS): "shared" or "thread".
    """
    try:
        from hyperdjango.conf import get_setting

        mode = str(get_setting("WEBSOCKET_CONCURRENCY", "shared")).lower()
    # If settings can't be imported/read (early startup, misconfigured conf
    # module) fall back to the documented default "shared" mode.
    # blind-except: config-read failure falls back to the default WS mode.
    except Exception:
        mode = "shared"
    return "thread" if mode == "thread" else "shared"


def _ws_loop_pool() -> _WsLoopPool | None:
    """Return the shared event-loop pool (the DEFAULT WebSocket model), or
    None when the concurrency model is explicitly set to "thread"
    (one-OS-thread-per-connection).

    Multiplexing connections over a small pool of event loops is the correct
    default for a WebSocket server: it removes the thread-pool connection
    ceiling, keeps multi-core throughput, and holds memory ~flat as
    connections grow. "thread" mode remains available for handlers that do
    heavy synchronous per-message CPU work.
    """
    if _ws_concurrency_mode() == "thread":
        return None
    global _ws_pool
    if _ws_pool is None:
        with _ws_pool_lock:
            if _ws_pool is None:
                try:
                    from hyperdjango.conf import get_setting

                    configured = int(get_setting("WEBSOCKET_LOOP_COUNT", 0) or 0)
                # If settings can't be imported/read fall back to 0 (auto-size
                # from cpu_count below) rather than failing pool construction.
                # blind-except: config-read failure auto-sizes the loop pool.
                except Exception:
                    configured = 0
                n = configured if configured > 0 else min(os.cpu_count() or 4, 8)
                _ws_pool = _WsLoopPool(max(1, n))
    return _ws_pool


# ── Native telemetry metrics (P5.2) ────────────────────────────────────────

_ws_connections_accepted = _tel_metrics.Counter(
    "hyperdjango_ws_connections_accepted_total",
    "Total WebSocket connections accepted",
)
_ws_connections_closed = _tel_metrics.Counter(
    "hyperdjango_ws_connections_closed_total",
    "Total WebSocket connections closed",
)
_ws_active_connections = _tel_metrics.Gauge(
    "hyperdjango_ws_active_connections",
    "Current number of open WebSocket connections",
)
_ws_messages_sent = _tel_metrics.Counter(
    "hyperdjango_ws_messages_sent_total",
    "Total WebSocket messages sent by the server",
)

# RFC 6455 opcodes passed to the native _ws_try_send / _ws_send_ping.
_WS_OP_TEXT = 0x1
_WS_OP_BINARY = 0x2
_WS_OP_PING = 0x9

# SendResult codes returned by _ws_try_send / _ws_flush_send / _ws_send_ping
# (must match SendResult in websocket_server.zig).
_WS_SENT = 0  # fully drained
_WS_WOULD_BLOCK = 1  # buffered natively — register add_writer and flush later
_WS_SHED = 2  # backlog past high-water — drop the slow consumer (close 1013)
_WS_CLOSED = 3  # transport error / connection dead — disconnect


def is_ws_origin_allowed(ws) -> bool:
    """CSWSH defense: may this WebSocket's ``Origin`` connect? (single authority).

    Cross-Site WebSocket Hijacking: a malicious page opens ``wss://your-app/…``
    from a victim's browser, which auto-sends the app's ambient cookies. SameSite
    cookies mitigate this, but Origin validation is the defense-in-depth every
    auth path should apply. Policy:

    - **No Origin header → allowed.** Only browsers send Origin, and only browsers
      auto-attach the victim's cookies, so CSWSH is browser-only; a native/CLI
      client (no Origin) must supply its own credentials and can't be hijacked.
    - **Same-origin** (Origin host == Host header) → allowed.
    - **Cross-origin** → allowed only if listed in ``WS_ALLOWED_ORIGINS`` (or ``*``).
    """
    from urllib.parse import urlparse

    from hyperdjango.conf import get_setting

    headers = ws.headers or {}
    origin = (headers.get("origin") or "").strip()
    if not origin:
        return True
    host = (headers.get("host") or "").strip()
    if host and urlparse(origin).netloc == host:
        return True
    allowed = get_setting("WS_ALLOWED_ORIGINS") or []
    return "*" in allowed or origin in allowed


@dataclass(slots=True, frozen=True)
class WebSocketConfig:
    """WebSocket server configuration."""

    max_message_size: int = 16 * 1024 * 1024  # 16 MB
    ping_interval: int = 30  # seconds, 0 = disabled
    pong_timeout: int = 120  # seconds — generous for mobile/wireless/laptop

    def apply(self):
        """Apply configuration to the native WebSocket server."""
        try:
            from hyperdjango._hyperdjango_native import _server_set_ws_config

            _server_set_ws_config(
                self.max_message_size,
                self.ping_interval,
                self.pong_timeout,
            )
        except ImportError, AttributeError:
            pass

    @staticmethod
    def current() -> WebSocketConfig:
        """Read current configuration from native server."""
        try:
            from hyperdjango._hyperdjango_native import _server_get_ws_config

            size, ping, pong = _server_get_ws_config()
            return WebSocketConfig(
                max_message_size=size,
                ping_interval=ping,
                pong_timeout=pong,
            )
        except ImportError, AttributeError:
            return WebSocketConfig()


class ZigWebSocket:
    """WebSocket connection backed by the native Zig server.

    Provides the same API as the ASGI WebSocket but backed by Zig I/O:
    - _ws_try_send(conn_id, opcode, data) sends a frame non-blocking (buffering
      any remainder); the writer selector drains the backlog (no HOL blocking)
    - _ws_try_recv(conn_id) reads the next frame non-blocking (selector-driven)
    - _ws_send_ping(conn_id, payload) drives loop-side keepalive
    - _ws_close(conn_id, code, reason) sends a close frame

    The Zig server calls the Python handler ONCE on connect with
    (conn_id, headers_dict, path, query_string). The handler uses
    this adapter to drive bidirectional communication.
    """

    def __init__(
        self, conn_id: int, headers: dict[str, str], path: str, query_string: str
    ):
        from hyperdjango._hyperdjango_native import (
            _ws_close,
            _ws_get_fd,
            _ws_recv,
            _ws_release,
            _ws_send,
            _ws_send_bytes,
            _ws_try_recv,
        )

        # Optional native symbol: a version-skewed older .so may not export it.
        # Import it separately so its absence degrades send_json to the str path
        # instead of breaking EVERY WebSocket connection with an ImportError.
        try:
            from hyperdjango._hyperdjango_native import _ws_send_text_bytes
        except ImportError:
            _ws_send_text_bytes = None

        # Non-blocking send path (HOL-blocking fix). Optional native symbols: an
        # older .so without them degrades every send to the blocking primitives
        # rather than breaking the connection. See _send_frame / _flush.
        try:
            from hyperdjango._hyperdjango_native import (
                _ws_flush_send,
                _ws_pong_age,
                _ws_send_ping,
                _ws_try_send,
            )
        except ImportError:
            _ws_try_send = None
            _ws_flush_send = None
            _ws_send_ping = None
            _ws_pong_age = None

        self._conn_id = conn_id
        self._send = _ws_send
        self._send_binary = _ws_send_bytes
        self._native_send_text_bytes = _ws_send_text_bytes
        self._try_send = _ws_try_send
        self._flush_send = _ws_flush_send
        self._send_ping = _ws_send_ping
        self._pong_age_fn = _ws_pong_age
        self._recv = _ws_recv
        self._try_recv = _ws_try_recv
        self._close_fn = _ws_close
        self._release_fn = _ws_release
        self.headers = headers
        self.path = path
        self.query_string = query_string
        self._accepted = False
        # Non-blocking receive path (see _recv_one): waits for readability via
        # the event loop's own selector instead of a ThreadPoolExecutor. Falls
        # back to the executor-based blocking path only if the fd is
        # unavailable or the loop doesn't support add_reader.
        try:
            self._fd = _ws_get_fd(conn_id)
        # Probing the native fd is an optimization; if it's unavailable we set it
        # None and fall back to the executor-based blocking receive path
        # (see _recv_one). Correctness is unaffected, only the receive strategy.
        # blind-except: fd probe is best-effort — falls back to blocking recv.
        except Exception:
            self._fd = None
        # Receive-side idle deadline (half-open detection). If the peer vanishes
        # without a FIN, the fd never becomes readable and _wait_readable would
        # park forever — leaking the receive coroutine, the fd, and the native
        # WsConn. Bound each readability wait by the configured pong_timeout so a
        # dead peer is reclaimed. This is what makes the pong_timeout WebSocket
        # config actually take effect on the receive path. <=0 disables it.
        _cfg = WebSocketConfig.current()
        pong_timeout = _cfg.pong_timeout
        self._idle_timeout: float | None = (
            float(pong_timeout) if pong_timeout and pong_timeout > 0 else None
        )
        self._pong_timeout: float | None = self._idle_timeout
        # Keepalive: proactively ping an idle-but-alive peer so its pong keeps the
        # idle deadline from firing (the real gain over idle-only reaping), and
        # reap a peer that has gone silent past pong_timeout. Driven from the loop
        # via call_later on top of the non-blocking send path — no background
        # thread. <=0 disables. Makes the ping_interval config actually take effect.
        ping_interval = _cfg.ping_interval
        self._ping_interval: float = (
            float(ping_interval) if ping_interval and ping_interval > 0 else 0.0
        )
        self._keepalive_handle: asyncio.TimerHandle | None = None
        # The selector reader is registered once (lazily, on first wait) and
        # left registered for the connection's lifetime — see _wait_readable.
        self._reader_active = False
        self._readable_fut: asyncio.Future | None = None
        # Send-side selector writer (symmetric to the reader): registered when a
        # send would block, drives _flush_send, removed once the backlog drains.
        # Futures parked in _drain_waiters are resolved when the buffer empties.
        self._writer_active = False
        self._drain_waiters: list[asyncio.Future] = []
        # Readability gate for the receive path. When False we KNOW the last
        # non-blocking recv drained to EAGAIN (no complete frame buffered, kernel
        # not readable), so _recv_one skips the optimistic _try_recv and waits for
        # the add_reader callback instead of paying a wasted would-block recv per
        # loop. The callback (_on_readable) sets it True; an EAGAIN clears it; a
        # returned frame LEAVES it True so a buffered batch keeps draining without
        # ever waiting on the kernel. Starts True so the first receive still does
        # one optimistic recv — that safely drains any frame the native handshake
        # handoff already buffered (which would fire no fresh kevent).
        self._readable = True
        # Single-shot teardown guard: everything this connection owns (selector
        # reader, close-frame protocol, telemetry, native fd+registry release)
        # is released exactly once, by finalize().
        self._finalized = False
        self._close_frame_sent = False

    # ── Lifetime: this object owns every resource tied to the connection and
    # releases them as a unit via finalize(). The handler wrapper drives it as
    # a context manager (`with ws:`), so no caller has to remember to release
    # the reader, reconcile telemetry, and free the native connection in
    # separate places — the object knows its own lifetime. ───────────────────

    def __enter__(self) -> ZigWebSocket:
        return self

    def __exit__(self, *exc_info) -> None:
        self.finalize()

    def finalize(self) -> None:
        """Release everything this connection owns, exactly once (idempotent).

        Ordering matters: drop the selector registration BEFORE the native
        layer closes the fd (a stale add_reader on a reused fd would misfire),
        send a best-effort graceful close frame if the handler didn't, then
        reconcile telemetry and release the native fd + registry entry.
        """
        if self._finalized:
            return
        self._finalized = True
        if self._keepalive_handle is not None:
            self._keepalive_handle.cancel()
            self._keepalive_handle = None
        self._remove_reader()
        self._remove_writer()
        # Fail any sender still parked on backpressure so it unwinds rather than
        # awaiting a drain that will never complete once the fd is closed.
        self._wake_drain_waiters(WebSocketDisconnect(1006))
        if not self._close_frame_sent:
            with contextlib.suppress(Exception):
                self._close_fn(self._conn_id, 1000, "")
            self._close_frame_sent = True
        if self._accepted:
            _ws_connections_closed.inc(1)
            _ws_active_connections.dec(1)
        self._release_fn(self._conn_id)

    async def accept(self, subprotocol=None):
        # Idempotent: a double accept() must not double-count the connection
        # (the RFC handshake already completed natively before the handler ran;
        # accept() here just marks acceptance and records telemetry once).
        if self._accepted:
            return
        self._accepted = True
        _ws_connections_accepted.inc(1)
        _ws_active_connections.inc(1)
        self._start_keepalive()

    # ── Keepalive (native ping, driven from the loop) ──────────────────────────

    def _start_keepalive(self) -> None:
        if (
            self._ping_interval <= 0
            or self._fd is None
            or self._send_ping is None
            or self._keepalive_handle is not None
        ):
            return
        try:
            loop = asyncio.get_running_loop()
        # No running loop (e.g. a test double calling accept() bare) → keepalive
        # simply doesn't arm; the receive-side idle deadline still applies.
        except RuntimeError:
            return
        self._keepalive_handle = loop.call_later(
            self._ping_interval, self._keepalive_tick
        )

    def _keepalive_tick(self) -> None:
        self._keepalive_handle = None
        if self._finalized:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        # Reap a peer that has gone silent past pong_timeout. last_recv is
        # refreshed natively on EVERY inbound frame (data/ping/pong), so a chatty
        # or ponging peer never trips this — only a genuinely dead one does.
        if self._pong_timeout and self._pong_age_fn is not None:
            age = self._pong_age_fn(self._conn_id)
            if age is not None and age > self._pong_timeout:
                self._fail_keepalive()
                return
        # Proactive ping through the non-blocking send path (empty payload). A
        # ping can never HOL-block; on backpressure it buffers + drains via the
        # writer, and a stuck backlog sheds like any other send.
        res = self._send_ping(self._conn_id, b"")
        if res == _WS_WOULD_BLOCK:
            self._ensure_writer(loop)
        elif res != _WS_SENT:
            self._fail_keepalive()
            return
        self._keepalive_handle = loop.call_later(
            self._ping_interval, self._keepalive_tick
        )

    def _fail_keepalive(self) -> None:
        """Dead peer: wake a parked receive so the handler unwinds to finalize().
        A handler not currently receiving hits the disconnect on its next I/O."""
        self._mark_peer_gone()
        fut = self._readable_fut
        if fut is not None and not fut.done():
            fut.set_exception(WebSocketDisconnect(1001))

    def _remove_reader(self) -> None:
        if self._reader_active and self._fd is not None:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(self._fd)
            self._reader_active = False

    async def close(self, code=1000, reason=""):
        # Idempotent: a second close() (or close() after the wrapper already
        # finalized) must not send another close frame or double-release.
        if self._finalized:
            return
        # Send the caller's close frame, then hand off to the single teardown
        # path. finalize() sees the frame was already sent and won't duplicate
        # it; it still releases the reader, telemetry, and native connection.
        with contextlib.suppress(Exception):
            self._close_fn(self._conn_id, code, reason)
        self._close_frame_sent = True
        self.finalize()

    async def send_text(self, text: str):
        await self._send_frame(_WS_OP_TEXT, text.encode("utf-8"))
        _ws_messages_sent.inc(1)

    async def send_json(self, data):
        # fast_json_dumps returns UTF-8 bytes from our own JSON encoder, so the
        # payload is guaranteed valid UTF-8 — send it straight as a text frame
        # without decode()/re-encode. One encode, zero decode.
        await self._send_frame(_WS_OP_TEXT, fast_json_dumps(data))
        _ws_messages_sent.inc(1)

    async def send_bytes(self, data: bytes):
        await self._send_frame(_WS_OP_BINARY, data)
        _ws_messages_sent.inc(1)

    # ── Non-blocking send path (head-of-line-blocking fix) ─────────────────────
    #
    # A send never issues a blocking syscall on the (shared) loop thread. The
    # native _ws_try_send does a SINGLE MSG_DONTWAIT send and buffers any
    # remainder; on backpressure we register the loop's own selector writer
    # (add_writer) and the await yields until the backlog drains — exactly
    # mirroring the receive path (add_reader). One slow/zero-window consumer
    # cannot stall the other connections multiplexed on the loop. A backlog
    # past the native high-water mark sheds that consumer (close 1013) instead of
    # buffering without bound.

    def _blocking_send(self, opcode: int, data: bytes) -> None:
        """Fallback for a version-skewed .so or an unavailable fd: the
        synchronous native send. Retains correctness at the cost of
        HOL-blocking when the non-blocking primitives aren't usable."""
        if opcode == _WS_OP_BINARY:
            self._send_binary(self._conn_id, data)
        elif self._native_send_text_bytes is not None:
            self._native_send_text_bytes(self._conn_id, data)
        else:
            self._send(self._conn_id, data.decode())

    def _handle_send_result(self, res: int) -> bool:
        """Map a native SendResult to control flow. Returns True if the caller
        must wait for the backlog to drain (WOULD_BLOCK); raises on shed/closed."""
        if res == _WS_SENT:
            return False
        if res == _WS_WOULD_BLOCK:
            return True
        # A slow consumer past the high-water mark, or a broken transport: both
        # force-drop this connection. Suppress the blocking graceful close frame
        # (the peer is unreachable/stalled — it would just block); the fd close
        # in finalize() is the teardown. 1013 = "try again later" for shed.
        self._mark_peer_gone()
        raise WebSocketDisconnect(1013 if res == _WS_SHED else 1006)

    async def _send_frame(self, opcode: int, data: bytes) -> None:
        if self._try_send is None or self._fd is None:
            self._blocking_send(opcode, data)
            return
        if not self._handle_send_result(self._try_send(self._conn_id, opcode, data)):
            return
        # WOULD_BLOCK: the frame is already buffered natively — wait until the
        # backlog drains before returning, applying backpressure to this caller
        # without blocking the loop or reordering frames.
        await self._drain()

    def _send_text_bytes(self, data: bytes) -> None:
        """Internal fast path: emit a TEXT frame from already-UTF-8 bytes.

        Used by send_json and the channels/realtime fan-out, which pass payloads
        straight from our own JSON encoder — the bytes are known-valid UTF-8, so
        this deliberately does NOT re-validate. It is underscore-internal
        precisely because it trusts the caller; there is no public bytes→TEXT
        entry point that could emit an RFC 6455-violating frame from arbitrary
        user bytes.

        SYNCHRONOUS + fire-and-forget: the fan-out callers invoke it without
        awaiting, so on backpressure it hands the buffered frame to the selector
        writer to drain in the background rather than awaiting here. Degrades to
        the blocking str path when the non-blocking primitives are unavailable.
        """
        loop = None
        if self._try_send is not None and self._fd is not None:
            try:
                loop = asyncio.get_running_loop()
            # No running loop on this thread → can't drive add_writer; use the
            # blocking path so the frame still goes out.
            except RuntimeError:
                loop = None
        if loop is not None:
            res = self._try_send(self._conn_id, _WS_OP_TEXT, data)
            if res == _WS_WOULD_BLOCK:
                # Buffered natively; drain in the background (no await).
                self._ensure_writer(loop)
            elif res != _WS_SENT:
                self._handle_send_result(res)  # raises (shed/closed)
        else:
            self._blocking_send(_WS_OP_TEXT, data)
        _ws_messages_sent.inc(1)

    def _mark_peer_gone(self) -> None:
        """The peer is unreachable/stalled: suppress finalize()'s blocking
        graceful close frame (it would just block on the dead/full socket); the
        fd close (RST/FIN) is the only teardown that won't stall the loop."""
        self._close_frame_sent = True

    def _on_writable(self) -> None:
        """Selector says the socket has send capacity — flush the backlog."""
        res = self._flush_send(self._conn_id)
        if res == _WS_WOULD_BLOCK:
            return  # more remains; stay registered, wait for the next writable
        self._remove_writer()
        if res == _WS_CLOSED:
            self._mark_peer_gone()
            self._wake_drain_waiters(WebSocketDisconnect(1006))
        else:  # _WS_SENT — fully drained
            self._wake_drain_waiters(None)

    def _wake_drain_waiters(self, exc: BaseException | None) -> None:
        waiters, self._drain_waiters = self._drain_waiters, []
        for fut in waiters:
            if not fut.done():
                if exc is None:
                    fut.set_result(None)
                else:
                    fut.set_exception(exc)

    def _ensure_writer(self, loop: asyncio.AbstractEventLoop) -> None:
        if not self._writer_active and self._fd is not None:
            loop.add_writer(self._fd, self._on_writable)
            self._writer_active = True

    def _remove_writer(self) -> None:
        if self._writer_active and self._fd is not None:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_writer(self._fd)
            self._writer_active = False

    async def _drain(self) -> None:
        """Suspend until the outbound backlog fully drains (bounded by the idle
        deadline so a stalled peer can't park the sender forever)."""
        loop = asyncio.get_running_loop()
        self._ensure_writer(loop)
        fut = loop.create_future()
        self._drain_waiters.append(fut)
        try:
            if self._idle_timeout is None:
                await fut
            else:
                await asyncio.wait_for(fut, self._idle_timeout)
        except TimeoutError:
            self._mark_peer_gone()
            raise WebSocketDisconnect(1001) from None
        finally:
            # Whether we drained, errored, or timed out, this waiter is done.
            with contextlib.suppress(ValueError):
                self._drain_waiters.remove(fut)

    def _on_readable(self) -> None:
        # The kernel says the fd is readable — record it so _recv_one will attempt
        # a recv, and wake any receive currently parked on readability.
        self._readable = True
        fut = self._readable_fut
        if fut is not None and not fut.done():
            fut.set_result(None)

    async def _wait_readable(self, loop: asyncio.AbstractEventLoop) -> None:
        """Suspend until the connection's socket has data to read.

        The reader callback is registered once per connection (not once
        per message) and left in place — kqueue/epoll are level-triggered,
        so it's safe for the callback to fire as a no-op between waits
        (e.g. if data races in before the next receive_text() call starts);
        we only ever pay the add_reader/remove_reader syscalls once per
        connection instead of twice per message.
        """
        if not self._reader_active:
            loop.add_reader(self._fd, self._on_readable)
            self._reader_active = True
        fut = loop.create_future()
        self._readable_fut = fut
        try:
            timeout = self._idle_timeout
            if timeout is None:
                await fut
            else:
                # Bound the wait: a half-open peer (gone without FIN) never makes
                # the fd readable, so an unbounded await would leak this coroutine
                # + fd + native connection forever. On the deadline, surface a
                # disconnect so the handler unwinds and finalize() reclaims them.
                try:
                    await asyncio.wait_for(fut, timeout)
                except TimeoutError:
                    raise WebSocketDisconnect(1001) from None
        finally:
            self._readable_fut = None

    async def _recv_one(self) -> str | bytes:
        """Return the next frame, preferring the non-blocking add_reader
        path (no thread-hop) over the executor-based blocking recv.

        `_ws_try_recv` never blocks (single MSG_DONTWAIT attempt), so it's
        safe to call directly from the event loop thread. It returns
        `False` when no complete frame is available yet — wait for
        readability and retry — or `None` on disconnect.
        """
        if self._fd is not None:
            loop = asyncio.get_running_loop()
            while True:
                if self._readable:
                    result = self._try_recv(self._conn_id)
                    if result is None:
                        # Disconnect. Teardown (including selector-reader removal
                        # before the fd is closed/reused) is owned by finalize(),
                        # which the handler wrapper runs synchronously as this
                        # exception unwinds — there is no await in between, so the
                        # stale-reader window never opens.
                        raise WebSocketDisconnect(1000)
                    if result is not False:
                        # A frame (from the kernel or an already-buffered batch).
                        # Leave _readable True: more frames may be buffered, and
                        # they must drain without waiting on the kernel.
                        return result
                    # EAGAIN: no complete frame buffered and the kernel would
                    # block. Clear the gate so we wait for the reader callback
                    # rather than re-issuing an optimistic recv on the next turn.
                    self._readable = False
                try:
                    await self._wait_readable(loop)
                    continue
                except NotImplementedError:
                    # Loop doesn't support add_reader — fall back permanently.
                    self._remove_reader()
                    self._fd = None
                    break

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            _ws_recv_executor(), self._recv, self._conn_id
        )
        if result is None:
            raise WebSocketDisconnect(1000)
        return result

    async def receive(self) -> str | bytes:
        """Receive the next message, preserving its frame type: ``str`` for a
        text frame, ``bytes`` for a binary frame. Use this for mixed-type
        protocols; use receive_text()/receive_bytes() when you want a
        guaranteed type. Raises WebSocketDisconnect when the peer closes."""
        return await self._recv_one()

    async def receive_text(self) -> str:
        """Receive the next message as ``str`` (a binary frame is decoded as
        UTF-8)."""
        msg = await self._recv_one()
        return msg if isinstance(msg, str) else msg.decode("utf-8")

    async def receive_bytes(self) -> bytes:
        """Receive the next message as ``bytes`` (a text frame is encoded as
        UTF-8)."""
        msg = await self._recv_one()
        return msg if isinstance(msg, bytes) else msg.encode("utf-8")

    async def receive_json(self) -> Any:
        return fast_json_loads(await self._recv_one())

    async def iter_text(self):
        """Async-iterate messages as ``str`` until the peer disconnects."""
        while True:
            try:
                yield await self.receive_text()
            except WebSocketDisconnect:
                return

    async def iter_bytes(self):
        """Async-iterate messages as ``bytes`` until the peer disconnects."""
        while True:
            try:
                yield await self.receive_bytes()
            except WebSocketDisconnect:
                return

    async def iter_json(self):
        while True:
            try:
                yield fast_json_loads(await self._recv_one())
            except WebSocketDisconnect:
                return


class WebSocket:
    """WebSocket connection wrapper for ASGI.

    Supports subprotocol negotiation and extension awareness.
    """

    def __init__(self, scope: dict[str, Any], receive, send):
        self.scope = scope
        self._receive = receive
        self._send = send
        self.path: str = scope.get("path", "/")
        raw_headers = scope.get("headers", [])
        self.headers: dict[str, str] = {
            (k.decode("latin-1") if isinstance(k, bytes) else k): (
                v.decode("latin-1") if isinstance(v, bytes) else v
            )
            for k, v in raw_headers
        }
        self.query_string: str = scope.get("query_string", b"").decode()
        self._accepted: bool = False
        self._closed: bool = False

        # Subprotocol negotiation — client's requested protocols
        self.requested_subprotocols: list[str] = [
            p.strip() for p in scope.get("subprotocols", [])
        ] or self._parse_subprotocols()
        self.accepted_subprotocol: str | None = None

        # Extension awareness
        self.extensions: dict[str, dict[str, str]] = scope.get("extensions", {})

    def _parse_subprotocols(self) -> list[str]:
        """Parse Sec-WebSocket-Protocol from headers."""
        proto_header = self.headers.get("sec-websocket-protocol", "")
        if not proto_header:
            return []
        return [p.strip() for p in proto_header.split(",") if p.strip()]

    @property
    def has_compression(self) -> bool:
        """Check if permessage-deflate was negotiated."""
        return "permessage-deflate" in self.extensions

    async def accept(self, subprotocol: str | None = None):
        """Accept the WebSocket connection.

        Args:
            subprotocol: Selected subprotocol from client's requested list.
                Should be one of self.requested_subprotocols.
        """
        if self._accepted:  # idempotent (matches ZigWebSocket)
            return
        msg: dict[str, Any] = {"type": "websocket.accept"}
        if subprotocol:
            msg["subprotocol"] = subprotocol
            self.accepted_subprotocol = subprotocol
        await self._send(msg)
        self._accepted = True

    async def close(self, code: int = 1000, reason: str = ""):
        """Close the WebSocket connection (idempotent)."""
        if self._closed:
            return
        self._closed = True
        await self._send(
            {
                "type": "websocket.close",
                "code": code,
                "reason": reason,
            }
        )

    async def receive_raw(self) -> dict[str, str | bytes]:
        """Receive the raw ASGI event dict (ASGI backend only, low-level).

        Prefer the unified receive()/receive_text()/receive_bytes()."""
        return await self._receive()

    async def _receive_message(self) -> str | bytes:
        """Next message payload, type-preserving (str=text, bytes=binary).
        Raises WebSocketDisconnect on close. Shared by the typed receivers so
        this backend matches ZigWebSocket's semantics exactly."""
        msg = await self._receive()
        if msg.get("type") == "websocket.disconnect":
            raise WebSocketDisconnect(msg.get("code", 1000))
        text = msg.get("text")
        return text if text is not None else msg.get("bytes", b"")

    async def receive(self) -> str | bytes:
        """Type-preserving receive: str for a text frame, bytes for a binary
        frame. Raises WebSocketDisconnect on close."""
        return await self._receive_message()

    async def receive_text(self) -> str:
        """Receive as str (a binary frame is UTF-8 decoded)."""
        msg = await self._receive_message()
        return msg if isinstance(msg, str) else msg.decode("utf-8")

    async def receive_bytes(self) -> bytes:
        """Receive as bytes (a text frame is UTF-8 encoded)."""
        msg = await self._receive_message()
        return msg if isinstance(msg, bytes) else msg.encode("utf-8")

    async def receive_json(self) -> Any:
        """Receive and parse a JSON message."""
        return fast_json_loads(await self._receive_message())

    async def send_text(self, data: str):
        """Send a text message."""
        await self._send({"type": "websocket.send", "text": data})

    async def send_bytes(self, data: bytes):
        """Send a binary message."""
        await self._send({"type": "websocket.send", "bytes": data})

    async def send_json(self, data: Any):
        """Send a JSON message."""
        await self.send_text(fast_json_dumps(data).decode())

    async def iter_text(self):
        """Async iterator for text messages."""
        while True:
            try:
                yield await self.receive_text()
            except WebSocketDisconnect:
                return

    async def iter_bytes(self):
        """Async iterator for binary messages (matches ZigWebSocket)."""
        while True:
            try:
                yield await self.receive_bytes()
            except WebSocketDisconnect:
                return

    async def iter_json(self):
        """Async iterator for JSON messages."""
        while True:
            try:
                yield await self.receive_json()
            except WebSocketDisconnect:
                return


class WebSocketDisconnect(Exception):
    """Raised when a WebSocket client disconnects."""

    def __init__(self, code: int = 1000):
        self.code = code
        super().__init__(f"WebSocket disconnected with code {code}")
