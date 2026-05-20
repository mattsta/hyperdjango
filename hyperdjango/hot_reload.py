"""
Hot module reloading — native kqueue/inotify file watcher + selective reload + SSE browser push.

Uses the Zig native file watcher (kqueue on macOS, inotify on Linux). When a
Python file changes, only that module is reloaded via importlib.reload().
Browser clients connected to the ``/__hyper_reload`` SSE endpoint get a 'reload'
event pushed.

This is OPT-IN and is NOT wired into ``HyperApp`` — constructing
``HyperApp(debug=True)`` does NOT start a reloader (it only enables Jinja2
template auto-reload). Hot reload is activated explicitly:

    from hyperdjango.hot_reload import setup_hot_reload
    reloader = setup_hot_reload(app)  # no-op unless app.debug is True

The Django ``runziserver`` management command calls ``setup_hot_reload`` for
you when ``DEBUG=True``. Never start a reloader in production — it holds a
file watcher and reloads modules in-process.
"""

import asyncio
import contextlib
import importlib
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from hyperdjango import _hyperdjango_native as _native
from hyperdjango.conf import get_setting
from hyperdjango.logging import logger
from hyperdjango.response import Response

# SSE script injected into HTML responses in debug mode
HOT_RELOAD_SCRIPT = """<script>
(function() {
    var es = new EventSource('/__hyper_reload');
    es.onmessage = function(e) {
        if (e.data === 'reload') window.location.reload();
    };
    es.onerror = function() {
        setTimeout(function() { window.location.reload(); }, 1000);
    };
})();
</script>"""


@dataclass(slots=True)
class _SseClient:
    """A connected hot-reload EventSource: an asyncio.Event and its loop.

    The file watcher runs on a background thread; it wakes each client via
    ``loop.call_soon_threadsafe(event.set)`` so the SSE generator can ``await``
    instead of blocking a serving thread.
    """

    event: asyncio.Event
    loop: asyncio.AbstractEventLoop


@dataclass(slots=True)
class HotReloader:
    """Native hot module reloader with SSE browser push.

    Watches directories for file changes using kqueue (macOS) or inotify (Linux).
    When a .py file changes, reloads the module. When any watched file changes,
    notifies connected SSE clients to reload.
    """

    watch_dirs: list[str] = field(default_factory=lambda: [str(Path.cwd())])
    extensions: list[str] = field(
        default_factory=lambda: [".py", ".html", ".css", ".js"]
    )
    _watcher_handle: int | None = field(default=None, init=False, repr=False)
    # Each connected browser EventSource is represented by an asyncio.Event and
    # the loop it lives on, so the (background-thread) file watcher can wake it
    # WITHOUT parking a serving thread on a blocking wait. See sse_generator.
    _sse_clients: list[_SseClient] = field(default_factory=list, init=False, repr=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _module_mtimes: dict[str, float] = field(
        default_factory=dict, init=False, repr=False
    )
    _running: bool = field(default=False, init=False, repr=False)
    # Monotonic count of change notifications the watcher has DELIVERED, with a
    # Condition to wait on. `_running` only says start() was called — it says
    # nothing about whether the native watcher thread ever got as far as arming
    # kqueue/inotify, and there is no other way to ask "is my reloader actually
    # watching?". A dev whose edits stopped triggering reloads, and a test that
    # must not step on a watcher mid-startup, need the same answer. A count
    # rather than a flag because a change cannot be un-counted: an observer that
    # looks after the fact still sees it.
    _changes_seen: int = field(default=0, init=False, repr=False)
    _change_cond: threading.Condition = field(
        default_factory=threading.Condition, init=False, repr=False
    )

    def start(self):
        """Start the file watcher."""
        if self._running:
            return
        self._running = True

        # Snapshot current module mtimes for selective reload
        self._snapshot_modules()

        # Native kqueue/inotify watcher
        self._watcher_handle = _native._file_watcher_start(
            self.watch_dirs,
            self.extensions,
            self._on_change,
        )

    def stop(self):
        """Stop the file watcher."""
        self._running = False
        if self._watcher_handle is not None:
            _native._file_watcher_stop(self._watcher_handle)
            self._watcher_handle = None

    @property
    def changes_seen(self) -> int:
        """Number of change notifications the watcher has delivered so far.

        Monotonic and never reset, so it can be sampled before an edit and
        compared after — see ``wait_for_change``.
        """
        with self._change_cond:
            return self._changes_seen

    def wait_for_change(self, timeout: float, *, since: int = 0) -> bool:
        """Block until ``changes_seen`` exceeds ``since``; True if it did.

        Pass the value read from ``changes_seen`` BEFORE making the edit as
        ``since``, so a notification that arrives between the two calls is still
        observed — the count cannot be missed the way an edge can.
        """
        deadline = time.monotonic() + timeout
        with self._change_cond:
            while self._changes_seen <= since:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._change_cond.wait(remaining)
            return True

    def _on_change(self):
        """Called by native watcher when files change."""
        # Reload changed Python modules
        self._reload_changed_modules()

        # Publish the notification BEFORE waking SSE clients: the count is what
        # an observer polls to learn the watcher is live, and it must not be
        # withheld by a slow or failing client wake-up.
        with self._change_cond:
            self._changes_seen += 1
            self._change_cond.notify_all()

        # Wake connected SSE clients on THEIR event loops (this runs on the
        # watcher's background thread). Copy under the lock, then signal outside
        # it so a slow call_soon_threadsafe can't stall the watcher.
        with self._lock:
            clients = list(self._sse_clients)
        for client in clients:
            with contextlib.suppress(RuntimeError):  # loop may be closing
                client.loop.call_soon_threadsafe(client.event.set)

    def _snapshot_modules(self):
        """Record mtimes of all loaded Python modules for change detection."""
        self._module_mtimes.clear()
        for name, mod in sys.modules.items():
            if hasattr(mod, "__file__") and mod.__file__:
                try:
                    path = Path(mod.__file__)
                    if path.suffix == ".py" and path.exists():
                        self._module_mtimes[name] = path.stat().st_mtime
                except OSError, TypeError:
                    pass

    def _reload_changed_modules(self):
        """Selectively reload only modules whose files changed."""
        reloaded = []
        for name, mod in list(sys.modules.items()):
            if not hasattr(mod, "__file__") or not mod.__file__:
                continue
            try:
                path = Path(mod.__file__)
                if path.suffix != ".py" or not path.exists():
                    continue
                mtime = path.stat().st_mtime
                old_mtime = self._module_mtimes.get(name)
                if old_mtime is not None and mtime != old_mtime:
                    # Module changed — reload it. On failure, surface a warning
                    # instead of silently keeping the stale module: a swallowed
                    # error looks identical to a successful reload, so the dev
                    # keeps hitting old code with no clue why.
                    try:
                        importlib.reload(mod)
                        reloaded.append(name)
                    # blind-except: dev hot-reload — a module that fails to reload is logged and skipped so one bad module doesn't abort reloading the rest
                    except Exception as e:
                        logger.warning(
                            "Hot reload failed for '{name}' — keeping stale module: {err}",
                            name=name,
                            err=e,
                        )
                self._module_mtimes[name] = mtime
            except OSError, TypeError:
                pass

    async def sse_generator(self):
        """Async SSE event generator for browser clients.

        Yields raw tokens ("connected", "reload") — ``Response.sse`` frames them
        as ``data:`` events. Awaits a per-client asyncio.Event instead of
        blocking a serving thread; the watcher wakes it via ``_on_change``. On
        heartbeat timeout it yields a keepalive the client ignores. The
        ``finally`` deregisters the client when the EventSource disconnects.
        """
        client = _SseClient(event=asyncio.Event(), loop=asyncio.get_running_loop())
        with self._lock:
            self._sse_clients.append(client)

        heartbeat = float(get_setting("HOT_RELOAD_SSE_HEARTBEAT"))
        try:
            yield "connected"
            while self._running:
                try:
                    await asyncio.wait_for(client.event.wait(), timeout=heartbeat)
                    client.event.clear()
                    yield "reload"
                except TimeoutError:
                    yield {"data": ""}  # keepalive — client ignores non-"reload"
        finally:
            with self._lock:
                self._sse_clients = [c for c in self._sse_clients if c is not client]

    def handle_sse_request(self, request):
        """Handle the /__hyper_reload SSE endpoint."""
        return Response.sse(self.sse_generator())

    def inject_script(self, html: str) -> str:
        """Inject the hot reload script into HTML before </body>."""
        if "</body>" in html:
            return html.replace("</body>", HOT_RELOAD_SCRIPT + "</body>")
        return html


def setup_hot_reload(app):
    """Wire hot reload into a HyperApp instance.

    Adds the SSE endpoint and starts the file watcher.
    Only active when app.debug is True.
    """
    if not app.debug:
        return None

    reloader = HotReloader()

    # Register SSE endpoint
    @app.route("GET", "/__hyper_reload")
    def hot_reload_sse(request):
        return reloader.handle_sse_request(request)

    # Start the native file watcher
    reloader.start()

    return reloader
