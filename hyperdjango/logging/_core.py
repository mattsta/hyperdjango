"""
Core shared state for the logging system.

Single Core instance shared across all Logger instances.
Manages: handlers, levels, activation list, global extra, global patcher,
background writer thread, min_level cache.
"""

from __future__ import annotations

import atexit
import contextvars
import queue
import sys
import threading
from collections import namedtuple
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from hyperdjango.types import LogExtra

if TYPE_CHECKING:
    from hyperdjango.logging._handler import Handler
    from hyperdjango.logging._record import LogRecord

# ---------------------------------------------------------------------------
# Level definition
# ---------------------------------------------------------------------------

Level = namedtuple("Level", ["name", "no", "color", "icon"])

BUILTIN_LEVELS: dict[str, Level] = {
    "TRACE": Level("TRACE", 5, "\033[37m", "🔍"),
    "DEBUG": Level("DEBUG", 10, "\033[36m", "🐛"),
    "INFO": Level("INFO", 20, "\033[32m", "ℹ️"),
    "SUCCESS": Level("SUCCESS", 25, "\033[1;32m", "✅"),
    "WARNING": Level("WARNING", 30, "\033[33m", "⚠️"),
    "ERROR": Level("ERROR", 40, "\033[31m", "❌"),
    "CRITICAL": Level("CRITICAL", 50, "\033[1;31m", "💀"),
}

RESET = "\033[0m"


class _VersionedCache(dict):
    """``enabled_cache`` that stamps every entry with a monotonic version.

    ``is_module_enabled`` stores ``name -> (version, result)`` and trusts a
    cached entry only when its stamped version equals the current version.
    ``enable``/``disable`` invalidate by calling ``clear()`` (which bumps the
    version). This closes the free-threading race where a descheduled reader
    wrote its stale decision AFTER an invalidation ``clear()`` and pinned it:
    now such a write lands stamped with an already-superseded version, so the
    next reader sees ``stored_version != current_version``, ignores it, and
    recomputes — the cache always converges to the correct decision.
    """

    # Subclasses the builtin ``dict`` (a C type) to override ``clear()`` and
    # stamp a version on entries.
    # slots-required: a @dataclass cannot model a ``dict`` subclass.
    __slots__ = ("version",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.version = 0

    def clear(self):
        # Bump BEFORE emptying: any decision computed under the old version is
        # now stale by construction. Emptying is just memory hygiene — the
        # version check alone already invalidates every outstanding entry.
        self.version += 1
        super().clear()


# Program start time for elapsed calculation
START_TIME = datetime.now(UTC)

# Context variable for request-scoped/task-scoped extra data
log_context: contextvars.ContextVar[LogExtra | None] = contextvars.ContextVar(
    "hyper_log_context",
    default=None,
)

# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


class Core:
    """Shared state across all Logger instances.

    Thread-safe: lock protects handler/level/activation mutations.
    The background writer thread drains the queue and dispatches to handlers.
    """

    def __init__(self):
        self.levels: dict[str, Level] = dict(BUILTIN_LEVELS)
        # Fast lookup: level_name_or_no -> (name, no, icon)
        self.levels_lookup: dict[str | int, tuple[str, int, str]] = {}
        self.handlers: dict[int, Handler] = {}  # handler_id -> Handler
        self.extra: LogExtra = {}  # Global extra fields
        self.patcher: Callable[[LogRecord], None] | None = None  # Global record patcher
        self.min_level: int = 0
        # Module activation: list of (dotted_module_name + ".", enabled_bool)
        # Sorted longest-first so most specific match wins
        self.activation_list: list[tuple[str, bool]] = []
        self.enabled_cache: _VersionedCache = _VersionedCache()
        # Thread safety
        self.lock = threading.Lock()
        # Background writer
        self.queue: queue.SimpleQueue = queue.SimpleQueue()
        self.writer_thread: threading.Thread | None = None
        self.writer_started = False

        self._rebuild_levels_lookup()

    def _rebuild_levels_lookup(self):
        """Rebuild the fast level lookup cache."""
        self.levels_lookup.clear()
        for name, lvl in self.levels.items():
            self.levels_lookup[name] = (name, lvl.no, lvl.icon)
            self.levels_lookup[name.upper()] = (name, lvl.no, lvl.icon)
            self.levels_lookup[lvl.no] = (name, lvl.no, lvl.icon)

    def update_min_level(self):
        """Recalculate minimum level across all handlers."""
        if self.handlers:
            self.min_level = min(h.level_no for h in self.handlers.values())
        else:
            self.min_level = 100  # Effectively disabled

    def is_module_enabled(self, name: str | None) -> bool:
        """Check if logging is enabled for a module name.

        Uses activation_list with dotted-name prefix matching.
        Results are cached as ``name -> (version, result)`` and validated
        against the cache version on read, so a stale decision computed across
        a concurrent enable()/disable() can never be pinned (see
        _VersionedCache).
        """
        cache = self.enabled_cache
        # Read the version BEFORE computing so the entry we store is stamped
        # with the activation state we actually observed.
        version = cache.version
        entry = cache.get(name)
        if entry is not None and entry[0] == version:
            return entry[1]

        if name is None:
            status = True
        else:
            status = True
            dotted = name + "."
            # Snapshot the list reference once; iterate the local so a
            # concurrent reassignment doesn't swap the sequence mid-loop.
            for module_prefix, enabled in self.activation_list:
                if dotted.startswith(module_prefix):
                    status = enabled
                    break

        # Stamp with the version observed above. If an invalidation raced us,
        # cache.version has already advanced and this entry is ignored on the
        # next read instead of masking the new decision.
        cache[name] = (version, status)
        return status

    def ensure_writer(self):
        """Start the background writer thread if not already running."""
        if self.writer_started and self.writer_thread and self.writer_thread.is_alive():
            return
        with self.lock:
            if (
                self.writer_started
                and self.writer_thread
                and self.writer_thread.is_alive()
            ):
                return
            self.writer_thread = threading.Thread(
                target=self._writer_loop,
                daemon=True,
                name="hyper-logger",
            )
            self.writer_thread.start()
            self.writer_started = True
            atexit.register(self.stop_writer)

    def stop_writer(self):
        """Stop the background writer thread gracefully."""
        if self.writer_started and self.writer_thread and self.writer_thread.is_alive():
            self.queue.put(None)  # Sentinel to exit _writer_loop
            self.writer_thread.join(timeout=2.0)
            self.writer_started = False

    def _writer_loop(self):
        """Background thread: drain queue and emit to handlers."""
        while True:
            try:
                item = self.queue.get()
                if item is None:
                    break  # Shutdown sentinel
                record, handlers, direct = item
                for handler in handlers:
                    try:
                        handler.emit(record)
                    # blind-except: a single handler's emit failure on the background writer thread must not drop the rest of the batch; error is reported to stderr
                    except Exception as e:
                        sys.stderr.write(f"[hyper-logger] Handler error: {e}\n")
            # blind-except: top-level guard for the background writer thread — any unexpected error must not kill the thread, or all enqueued logging silently stops
            except Exception as e:
                sys.stderr.write(f"[hyper-logger] Writer loop error: {e}\n")

    # --- Metrics ---

    @property
    def stats(self) -> dict[str, int | bool]:
        """Return logging system metrics."""
        return {
            "handlers": len(self.handlers),
            "levels": len(self.levels),
            "min_level": self.min_level,
            "writer_alive": self.writer_thread.is_alive()
            if self.writer_thread
            else False,
            "queue_depth": self.queue.qsize() if hasattr(self.queue, "qsize") else -1,
            "activation_rules": len(self.activation_list),
        }


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_core = Core()


def get_core() -> Core:
    return _core
