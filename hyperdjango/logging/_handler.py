"""
Log handler — wraps a sink with level filtering, formatting, and thread-safe emission.

Features:
- Level threshold filtering
- Static or dynamic (callable) format strings
- Dict-based per-module filter support
- Reentrancy detection (prevents deadlock when logging from within a sink)
- Per-handler lock for thread safety
- Error interception (sink errors go to stderr, never crash the app)
- Stopped state tracking
- Format caching for precompiled templates
"""

import contextlib
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from hyperdjango.logging._colorizer import colorize as _colorize_markup
from hyperdjango.logging._core import BUILTIN_LEVELS, Core, get_core
from hyperdjango.logging._record import LogRecord

# ---------------------------------------------------------------------------
# Sink Protocol
# ---------------------------------------------------------------------------


class SinkProtocol(Protocol):
    """Protocol for log sinks -- must implement write(), optionally stop()."""

    def write(self, message: str, record: LogRecord) -> None: ...

    def stop(self) -> None: ...


# ---------------------------------------------------------------------------
# Handler ID generation
# ---------------------------------------------------------------------------

_handler_id_counter = 0
_handler_id_lock = threading.Lock()


def _next_handler_id() -> int:
    global _handler_id_counter
    with _handler_id_lock:
        _handler_id_counter += 1
        return _handler_id_counter


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


@dataclass
class Handler:
    """Wraps a sink with level filtering, formatting, and thread-safe emission.

    The emit() method is the hot path — called for every log record that
    passes the level check. Optimized for minimal overhead.
    """

    sink: SinkProtocol
    level_no: int
    format_str: str = ""
    format_fn: Callable[[LogRecord], str] | None = (
        None  # Dynamic format: callable(record) -> str
    )
    is_json: bool = False
    serialize: bool = False
    filter_fn: Callable[[LogRecord], bool] | None = None
    name: str = ""
    colorize: bool = False
    id: int = field(default_factory=_next_handler_id)

    # Internal state (not constructor args for external use)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _lock_acquired: threading.local = field(default_factory=threading.local, repr=False)
    _stopped: bool = field(default=False, repr=False)

    def _get_level_color(self, record: LogRecord) -> str:
        """Get the ANSI color escape for the record's log level."""
        level_name = record["level"].name
        core = get_core()
        lvl = core.levels.get(level_name) or BUILTIN_LEVELS.get(level_name)
        return lvl.color if lvl else ""

    def emit(self, record: LogRecord):
        """Emit a log record to this handler's sink.

        Checks level, applies filter, formats message, appends exception
        traceback, and writes to sink with reentrancy protection.
        """
        # Level check (fast path — no lock needed for int comparison)
        if record["level"].no < self.level_no:
            return

        # Reentrancy guard: skip if we're already inside this handler's emit
        # dynamic-attr: threading.local attributes are per-thread and set lazily — "acquired" is absent on first touch in a thread
        if getattr(self._lock_acquired, "acquired", False):
            return

        # Filter check
        if self.filter_fn is not None and not self.filter_fn(record):
            return

        # Format the message
        if record.get("_raw"):
            message = record["message"]
        elif self.is_json or self.serialize:
            message = ""
        elif self.format_fn is not None:
            try:
                fmt = self.format_fn(record)
                message = fmt.format_map(record)
            # blind-except: a user-supplied format_fn must not break emission; fall back to a plain message and report the error to stderr
            except Exception as e:
                message = f"[{record['level'].name}] {record['message']}"
                sys.stderr.write(f"[hyper-logger] Format error: {e}\n")
        else:
            try:
                message = self.format_str.format_map(record)
            except KeyError, IndexError, ValueError, AttributeError:
                message = f"[{record['level'].name}] {record['message']}"

        # Convert color markup tags (<green>, <level>, etc.) to ANSI codes
        if self.colorize and message:
            level_color = self._get_level_color(record)
            message = _colorize_markup(message, level_color)

        # Append exception traceback if present
        exc = record.get("exception")
        if exc and exc.type is not None:
            try:
                tb_str = str(exc)
                if tb_str:
                    message = message.rstrip("\n") + "\n" + tb_str
            # blind-except: rendering a captured exception's traceback must not break emission of the log line itself; message is emitted without the traceback
            except Exception:
                pass  # Don't let traceback formatting break logging

        # Ensure trailing newline
        if not message.endswith("\n"):
            message += "\n"

        self._lock_acquired.acquired = True
        try:
            with self._lock:
                if self._stopped:
                    return
                try:
                    self.sink.write(message, record)
                # blind-except: a sink write failure (disk full, broken pipe, etc.) must not propagate out of emit and break the caller; error is reported to stderr
                except Exception as e:
                    sys.stderr.write(
                        f"[hyper-logger] Sink error in '{self.name}': {e}\n"
                    )
        finally:
            self._lock_acquired.acquired = False

    def stop(self):
        """Stop this handler and clean up the sink."""
        self._stopped = True
        with contextlib.suppress(Exception):
            self.sink.stop()


# ---------------------------------------------------------------------------
# Dict-based filter factory
# ---------------------------------------------------------------------------


def make_dict_filter(
    filter_dict: dict[str, str | int | bool], core: Core
) -> Callable[[LogRecord], bool]:
    """Convert a dict filter to a callable.

    Dict maps module names to minimum levels:
        {"": "DEBUG", "noisy_lib": "WARNING", "secret": False}

    Empty string "" is the default. False disables entirely.
    """
    resolved: dict[str, int] = {}
    for module, level in filter_dict.items():
        if isinstance(level, bool):
            resolved[module] = 0 if level else 999
        elif isinstance(level, str):
            lvl = core.levels.get(level.upper())
            resolved[module] = lvl.no if lvl else 0
        elif isinstance(level, int):
            resolved[module] = level
        else:
            resolved[module] = 0

    default_level = resolved.get("", 0)

    def _filter(record: LogRecord) -> bool:
        name = record.get("name", "") or ""
        # Find most specific module match
        best_match_len = 0
        threshold = default_level
        for mod, lvl in resolved.items():
            if mod and name.startswith(mod) and len(mod) > best_match_len:
                best_match_len = len(mod)
                threshold = lvl
        return record["level"].no >= threshold

    return _filter
