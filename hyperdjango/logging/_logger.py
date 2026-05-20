"""
Logger — the main user-facing interface.

Wraps Core (shared state) with per-instance options (extra, patchers,
exception, depth, lazy, raw, capture). Each bind/opt/patch call returns
a new Logger with modified options, leaving the original unchanged.

This is the loguru-compatible API surface:
- logger.info/debug/warning/error/critical/trace/success/log/exception
- logger.add/remove/level/enable/disable/configure
- logger.bind/opt/contextualize/patch
- logger.complete/shutdown
"""

import functools
import inspect
import io
import logging
import multiprocessing
import sys
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TextIO

# Log helpers: native hot-path acceleration when built, identical Python
# fallbacks otherwise (see _native_compat — the bootstrap boundary).
from hyperdjango.logging._colorizer import decolorize_format
from hyperdjango.logging._core import (
    START_TIME,
    Core,
    Level,
    get_core,
    log_context,
)
from hyperdjango.logging._handler import Handler, make_dict_filter
from hyperdjango.logging._native_compat import (
    log_basename as _native_basename,
)
from hyperdjango.logging._native_compat import (
    log_module_name as _native_module_name,
)
from hyperdjango.logging._record import (
    NO_EXCEPTION,
    LogRecord,
    RecordException,
    RecordFile,
    RecordLevel,
    RecordProcess,
    RecordThread,
)
from hyperdjango.logging._sinks import (
    AsyncSink,
    CallableSink,
    ConsoleSink,
    FileSink,
    JsonSink,
    StandardSink,
)
from hyperdjango.types import LogExtra

# ---------------------------------------------------------------------------
# Default format
# ---------------------------------------------------------------------------

DEFAULT_FORMAT = (
    "<green>{time:%Y-%m-%d %H:%M:%S.%f}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)

# Plain (no color markup) for non-tty or for fallback
PLAIN_FORMAT = (
    "{time:%Y-%m-%d %H:%M:%S.%f} | {level: <8} | {name}:{function}:{line} - {message}"
)

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------


@dataclass
class Logger:
    """Production structured logger with loguru-compatible API.

    Non-blocking: records queued to background writer thread (configurable).
    Thread-safe: shared Core protected by lock, per-handler reentrancy guard.
    Async-safe: contextvars for request-scoped data.
    Immutable options: bind/opt/patch return new Logger, never mutate.
    """

    _core: Core = field(default_factory=get_core)
    _extra: LogExtra = field(default_factory=dict)
    _patchers: list[Callable[[LogRecord], None]] = field(default_factory=list)
    _exception: (
        bool
        | BaseException
        | tuple[type[BaseException] | None, BaseException | None, None]
        | None
    ) = None
    _depth: int = 0
    _lazy: bool = False
    _raw: bool = False
    _capture: bool = True

    # --- Sink management ---

    def add(
        self,
        sink: TextIO
        | str
        | logging.Handler
        | Callable[[LogRecord, str], None]
        | None = None,
        *,
        level: str | int = "DEBUG",
        format: str | Callable[[LogRecord], str] = "",
        filter: Callable[[LogRecord], bool] | dict[str, str | int | bool] | None = None,
        colorize: bool | None = None,
        serialize: bool = False,
        enqueue: bool = True,
        rotation: str | int | Callable[[str, io.TextIOWrapper], bool] | None = None,
        retention: str | int | None = None,
        compression: str | None = None,
    ) -> int:
        """Add a logging sink.

        Args:
            sink: Output destination:
                - None or sys.stderr: Console (default)
                - sys.stdout: Console to stdout
                - str: File path
                - callable: func(record, message)
                - async callable: coroutine(record, message)
                - "json": Structured JSON to stderr
                - logging.Handler: Stdlib handler bridge
            level: Minimum level (name or int).
            format: Format template or callable(record) -> str.
                Default uses color markup for tty, plain otherwise.
            filter: callable(record)->bool, or dict {"module": "LEVEL"}.
            colorize: Force color on/off (None=auto-detect).
            serialize: Output as structured JSON.
            enqueue: Use background queue for non-blocking I/O (default True).
            rotation: File rotation: "100 MB", "daily", int (bytes), callable.
            retention: File retention: int (count) or "7 days".
            compression: Compress rotated: "gz", "bz2", "xz".

        Returns:
            Handler ID for removal via remove().
        """
        core = self._core

        # Resolve level
        if isinstance(level, str):
            lvl = core.levels.get(level.upper())
            level_no = lvl.no if lvl else 0
        else:
            level_no = level

        # Resolve filter
        filter_fn = None
        if callable(filter) and not isinstance(filter, dict):
            filter_fn = filter
        elif isinstance(filter, dict):
            filter_fn = make_dict_filter(filter, core)

        # Resolve format
        format_str = ""
        format_fn = None
        if callable(format):
            format_fn = format
        elif isinstance(format, str) and format:
            format_str = format
        # else: use default based on colorize detection

        # Resolve sink + determine if colorize
        is_json = serialize or sink == "json"
        sink_name = repr(sink)
        auto_colorize = False

        if is_json:
            stream = sys.stderr if sink in (None, "json") else sink
            actual_sink = JsonSink(stream=stream)
            sink_name = "json"
        elif sink is None or sink is sys.stderr or sink is sys.stdout:
            stream = sink if sink in (sys.stderr, sys.stdout) else sys.stderr
            auto_colorize = hasattr(stream, "isatty") and stream.isatty()
            do_colorize = colorize if colorize is not None else auto_colorize
            actual_sink = ConsoleSink(stream=stream, colorize=do_colorize)
            sink_name = "stderr" if stream is sys.stderr else "stdout"
            if not format_str and not format_fn:
                format_str = DEFAULT_FORMAT if do_colorize else PLAIN_FORMAT
        elif isinstance(sink, str):
            actual_sink = FileSink(
                path=sink,
                rotation=rotation,
                retention=retention,
                compression=compression,
            )
            sink_name = sink
            if not format_str and not format_fn:
                format_str = PLAIN_FORMAT
        elif hasattr(sink, "emit") and hasattr(sink, "handle"):
            # stdlib logging.Handler
            if isinstance(sink, logging.Handler):
                actual_sink = StandardSink(handler=sink)
                sink_name = f"stdlib:{type(sink).__name__}"
                if not format_str and not format_fn:
                    format_str = "{message}"  # Handler does its own formatting
        elif inspect.iscoroutinefunction(sink):
            actual_sink = AsyncSink(func=sink)
            # dynamic-attr: sink is an arbitrary user-supplied callable (function, partial, or callable object) — __name__ is not guaranteed
            sink_name = f"async:{getattr(sink, '__name__', '?')}"
            if not format_str and not format_fn:
                format_str = PLAIN_FORMAT
        elif callable(sink):
            actual_sink = CallableSink(func=sink)
            # dynamic-attr: sink is an arbitrary user-supplied callable (function, partial, or callable object) — __name__ is not guaranteed
            sink_name = f"callable:{getattr(sink, '__name__', '?')}"
            if not format_str and not format_fn:
                format_str = PLAIN_FORMAT
        else:
            # Assume file-like object
            actual_sink = ConsoleSink(stream=sink, colorize=colorize or False)
            sink_name = repr(sink)
            if not format_str and not format_fn:
                format_str = PLAIN_FORMAT

        # Strip color markup from format if not colorizing
        if format_str and not (colorize or auto_colorize):
            format_str = decolorize_format(format_str)

        handler = Handler(
            sink=actual_sink,
            level_no=level_no,
            format_str=format_str,
            format_fn=format_fn,
            is_json=is_json,
            serialize=serialize,
            filter_fn=filter_fn,
            name=sink_name,
            colorize=colorize or auto_colorize,
        )

        with core.lock:
            core.handlers[handler.id] = handler
            core.update_min_level()

        if enqueue:
            core.ensure_writer()

        return handler.id

    def remove(self, handler_id: int = None):
        """Remove handler by ID. If no ID, remove ALL handlers."""
        core = self._core
        with core.lock:
            if handler_id is None:
                for h in core.handlers.values():
                    h.stop()
                core.handlers.clear()
            else:
                handler = core.handlers.pop(handler_id, None)
                if handler:
                    handler.stop()
            core.update_min_level()

    # --- Level management ---

    def level(
        self, name: str, no: int = None, color: str = "", icon: str = " "
    ) -> Level | None:
        """Add/update/get a custom log level.

        Add:    logger.level("AUDIT", no=35, color="\\033[35m", icon="📋")
        Get:    logger.level("INFO")
        Update: logger.level("WARNING", no=30, icon="⚠")
        """
        core = self._core
        if no is not None:
            lvl = Level(name, no, color, icon)
            with core.lock:
                core.levels[name] = lvl
                core._rebuild_levels_lookup()
            return lvl
        return core.levels.get(name)

    # --- Module activation ---

    def enable(self, name: str):
        """Enable logging for a module and its children."""
        core = self._core
        with core.lock:
            prefix = name + "."
            core.activation_list = [
                (n, s) for n, s in core.activation_list if n != prefix
            ]
            core.activation_list.insert(0, (prefix, True))
            core.activation_list.sort(key=lambda x: -len(x[0]))
            core.enabled_cache.clear()

    def disable(self, name: str):
        """Disable logging for a module and its children."""
        core = self._core
        with core.lock:
            prefix = name + "."
            core.activation_list = [
                (n, s) for n, s in core.activation_list if n != prefix
            ]
            core.activation_list.insert(0, (prefix, False))
            core.activation_list.sort(key=lambda x: -len(x[0]))
            core.enabled_cache.clear()

    # --- Bulk configuration ---

    def configure(
        self,
        *,
        handlers: list[dict[str, str | int | bool]] = None,
        levels: list[dict[str, str | int]] = None,
        extra: LogExtra = None,
        patcher: Callable[[LogRecord], None] = None,
        activation: list[tuple[str, bool]] = None,
    ) -> list[int]:
        """Bulk configure the logger. Returns list of handler IDs."""
        core = self._core
        ids = []

        if extra is not None:
            core.extra = extra
        if patcher is not None:
            core.patcher = patcher
        if levels:
            for lvl_dict in levels:
                self.level(**lvl_dict)
        if activation:
            for name, status in activation:
                if status:
                    self.enable(name)
                else:
                    self.disable(name)
        if handlers is not None:
            self.remove()  # Remove existing
            for h_dict in handlers:
                hid = self.add(**h_dict)
                ids.append(hid)

        return ids

    # --- Binding, context, patching ---

    def bind(self, **kwargs) -> Logger:
        """Return new Logger with extra context fields merged in."""
        merged = {**self._extra, **kwargs}
        return Logger(
            _core=self._core,
            _extra=merged,
            _patchers=list(self._patchers),
            _exception=self._exception,
            _depth=self._depth,
            _lazy=self._lazy,
            _raw=self._raw,
            _capture=self._capture,
        )

    def opt(
        self,
        *,
        exception: bool
        | BaseException
        | tuple[type[BaseException] | None, BaseException | None, None]
        | None = None,
        depth: int = 0,
        lazy: bool = False,
        raw: bool = False,
        capture: bool = True,
    ) -> Logger:
        """Return new Logger with modified options.

        Args:
            exception: True (capture current), BaseException, tuple, or False.
            depth: Extra frames to skip for caller detection (wrapper functions).
            lazy: Only evaluate callable args/kwargs if message will be logged.
            raw: Bypass format template, send message directly to sink.
            capture: If False, don't add kwargs to extra dict.
        """
        return Logger(
            _core=self._core,
            _extra=dict(self._extra),
            _patchers=list(self._patchers),
            _exception=exception,
            _depth=depth,
            _lazy=lazy,
            _raw=raw,
            _capture=capture,
        )

    @contextmanager
    def contextualize(self, **kwargs):
        """Context manager to add extra fields for the scope of a block.

        Thread/async-safe via contextvars. Automatically restored on exit.

        Usage:
            with logger.contextualize(task_id=123, user="alice"):
                logger.info("In context")  # extra has task_id and user
        """
        old = log_context.get() or {}
        merged = {**old, **kwargs}
        token = log_context.set(merged)
        try:
            yield
        finally:
            log_context.reset(token)

    def patch(self, patcher: Callable[[LogRecord], None]) -> Logger:
        """Return new Logger that applies patcher to every record.

        The patcher modifies the record dict in-place before emission:
            logger.patch(lambda r: r["extra"].update(host="server1"))
        """
        return Logger(
            _core=self._core,
            _extra=dict(self._extra),
            _patchers=list(self._patchers) + [patcher],
            _exception=self._exception,
            _depth=self._depth,
            _lazy=self._lazy,
            _raw=self._raw,
            _capture=self._capture,
        )

    # --- Core log method (HOT PATH) ---

    def _log(
        self,
        level_name: str,
        level_no: int,
        message: str,
        args: tuple[str | int | float, ...],
        kwargs: LogExtra,
        depth_offset: int = 0,
    ):
        """Build a log record and dispatch to handlers.

        This is the critical hot path. Every logger.info() etc. calls this.
        Optimized order: fast exits first, frame capture, record build, dispatch.
        """
        core = self._core

        # ── Fast exit 1: min level check (no lock, int comparison) ──
        if level_no < core.min_level:
            return

        # ── Fast exit 2: no handlers ──
        if not core.handlers:
            return

        # ── Frame capture ──
        try:
            frame = sys._getframe(2 + self._depth + depth_offset)
            f_globals = frame.f_globals
            f_lineno = frame.f_lineno
            code = frame.f_code
            co_name = code.co_name
            co_filename = code.co_filename
        except ValueError:
            f_globals = {}
            f_lineno = 0
            co_name = "<unknown>"
            co_filename = "<unknown>"

        # ── Module name + activation check ──
        name = f_globals.get("__name__")
        if not core.is_module_enabled(name):
            return

        # ── Timestamp + elapsed (single datetime call) ──
        now = datetime.now(UTC)
        elapsed = now - START_TIME

        # ── Thread / process (cached per-thread for most calls) ──
        ct = threading.current_thread()
        cp = multiprocessing.current_process()

        # ── Exception handling (atomic capture) ──
        exception = self._exception
        if exception is True:
            ei = sys.exc_info()
            exception = RecordException(ei[0], ei[1], ei[2])
        elif isinstance(exception, BaseException):
            exception = RecordException(
                type(exception), exception, exception.__traceback__
            )
        elif isinstance(exception, tuple) and len(exception) == 3:
            exception = RecordException(exception[0], exception[1], exception[2])
        elif exception:
            ei = sys.exc_info()
            exception = RecordException(ei[0], ei[1], ei[2])
        else:
            exception = NO_EXCEPTION

        # ── Lazy evaluation (before any use of args/kwargs) ──
        if self._lazy:
            args = tuple(a() if callable(a) else a for a in args)
            kwargs = {k: v() if callable(v) else v for k, v in kwargs.items()}

        # ── Merge extra (single dict construction instead of 3 .update() calls) ──
        ctx = log_context.get() or {}
        if self._capture and kwargs:
            merged_extra = {**core.extra, **ctx, **self._extra, **kwargs}
        elif self._extra:
            merged_extra = {**core.extra, **ctx, **self._extra}
        elif ctx:
            merged_extra = {**core.extra, **ctx}
        elif core.extra:
            merged_extra = dict(core.extra)
        else:
            merged_extra = {}

        # ── Format message ──
        if self._raw:
            msg = str(message)
        elif args or kwargs:
            try:
                msg = message.format(*args, **kwargs)
            except IndexError, KeyError, ValueError, AttributeError:
                msg = str(message)
        else:
            msg = str(message)

        # ── Resolve level info (single lookup) ──
        lookup = core.levels_lookup.get(level_name)
        icon = lookup[2] if lookup else " "

        # ── Build record (native Zig basename + module extraction) ──
        file_name = _native_basename(co_filename)
        module_name = _native_module_name(file_name)

        record = {
            "elapsed": elapsed,
            "exception": exception,
            "extra": merged_extra,
            "file": RecordFile(file_name, co_filename),
            "function": co_name,
            "level": RecordLevel(level_name, level_no, icon),
            "line": f_lineno,
            "message": msg,
            "module": module_name,
            "name": name or "",
            "process": RecordProcess(cp.pid, cp.name),
            "thread": RecordThread(ct.ident, ct.name),
            "time": now,
            "_raw": self._raw,
        }

        # ── Apply patchers (with error protection) ──
        if core.patcher:
            try:
                core.patcher(record)
            # blind-except: a user-supplied core patcher must not abort record dispatch; error goes to stderr and dispatch continues
            except Exception as e:
                sys.stderr.write(f"[hyper-logger] Core patcher error: {e}\n")
        for patcher in self._patchers:
            try:
                patcher(record)
            # blind-except: a user-supplied per-logger patcher must not abort record dispatch; error goes to stderr and dispatch continues
            except Exception as e:
                sys.stderr.write(f"[hyper-logger] Patcher error: {e}\n")

        # ── Dispatch to handlers ──
        handlers = list(core.handlers.values())

        if core.writer_started and core.writer_thread and core.writer_thread.is_alive():
            # Enqueued: non-blocking put to background writer
            core.queue.put((record, handlers, False))
        else:
            # Direct: synchronous emit (for enqueue=False or writer not started)
            for handler in handlers:
                try:
                    handler.emit(record)
                # blind-except: a handler/sink emit failure must not propagate into the caller's request path; error is written to stderr and remaining handlers still run
                except Exception as e:
                    sys.stderr.write(f"[hyper-logger] Emit error: {e}\n")

    # --- Level convenience methods ---

    def trace(self, message: str, *args, **kwargs):
        """Log at TRACE level (5)."""
        self._log("TRACE", 5, message, args, kwargs)

    def debug(self, message: str, *args, **kwargs):
        """Log at DEBUG level (10)."""
        self._log("DEBUG", 10, message, args, kwargs)

    def info(self, message: str, *args, **kwargs):
        """Log at INFO level (20)."""
        self._log("INFO", 20, message, args, kwargs)

    def success(self, message: str, *args, **kwargs):
        """Log at SUCCESS level (25)."""
        self._log("SUCCESS", 25, message, args, kwargs)

    def warning(self, message: str, *args, **kwargs):
        """Log at WARNING level (30)."""
        self._log("WARNING", 30, message, args, kwargs)

    def error(self, message: str, *args, **kwargs):
        """Log at ERROR level (40)."""
        self._log("ERROR", 40, message, args, kwargs)

    def critical(self, message: str, *args, **kwargs):
        """Log at CRITICAL level (50)."""
        self._log("CRITICAL", 50, message, args, kwargs)

    def log(self, level: str | int, message: str, *args, **kwargs):
        """Log at an arbitrary level (by name or number)."""
        core = self._core
        if isinstance(level, str):
            info = core.levels_lookup.get(level.upper())
            if info:
                self._log(info[0], info[1], message, args, kwargs)
            else:
                self._log(level, 20, message, args, kwargs)
        else:
            info = core.levels_lookup.get(level)
            if info:
                self._log(info[0], info[1], message, args, kwargs)
            else:
                self._log(f"Level {level}", level, message, args, kwargs)

    def exception(self, message: str, *args, **kwargs):
        """Log at ERROR with current exception traceback attached."""
        self.opt(exception=True)._log("ERROR", 40, message, args, kwargs)

    def catch(self, level: str = "ERROR", message: str = "An error occurred"):
        """Decorator that catches exceptions and logs them.

        Usage:
            @logger.catch()
            def risky():
                1 / 0

            @logger.catch(level="CRITICAL", message="Task failed")
            async def task():
                ...
        """

        def decorator(func):
            if inspect.iscoroutinefunction(func):

                @functools.wraps(func)
                async def async_wrapper(*args, **kwargs):
                    try:
                        return await func(*args, **kwargs)
                    # blind-except: logger.catch() decorator's contract is to catch any exception from the wrapped callable and log it; swallow-after-log is the documented default (reraise is opt-in)
                    except Exception:
                        self.opt(exception=True, depth=1)._log(
                            level.upper(),
                            self._core.levels.get(
                                level.upper(), Level("ERROR", 40, "", "")
                            ).no,
                            message,
                            (),
                            {},
                        )

                return async_wrapper
            else:

                @functools.wraps(func)
                def sync_wrapper(*args, **kwargs):
                    try:
                        return func(*args, **kwargs)
                    # blind-except: logger.catch() decorator's contract is to catch any exception from the wrapped callable and log it; swallow-after-log is the documented default (reraise is opt-in)
                    except Exception:
                        self.opt(exception=True, depth=1)._log(
                            level.upper(),
                            self._core.levels.get(
                                level.upper(), Level("ERROR", 40, "", "")
                            ).no,
                            message,
                            (),
                            {},
                        )

                return sync_wrapper

        return decorator

    def stats(self) -> dict[str, int | bool]:
        """Return logging system metrics."""
        return self._core.stats

    # --- Lifecycle ---

    def complete(self):
        """Block until all queued records have been written."""
        core = self._core
        if not core.writer_started:
            return

        event = threading.Event()

        class _SignalSink:
            def write(self_, msg, rec):
                event.set()

            def stop(self_):
                pass

        sentinel_handler = Handler(
            sink=_SignalSink(),
            level_no=0,
            name="_sentinel",
        )
        sentinel_record = {
            "elapsed": timedelta(),
            "exception": NO_EXCEPTION,
            "extra": {},
            "file": RecordFile("", ""),
            "function": "",
            "level": RecordLevel("TRACE", 0, ""),
            "line": 0,
            "message": "",
            "module": "",
            "name": "",
            "process": RecordProcess(0, ""),
            "thread": RecordThread(0, ""),
            "time": datetime.now(UTC),
        }
        core.queue.put((sentinel_record, [sentinel_handler], False))
        event.wait(timeout=10.0)

    def shutdown(self):
        """Flush all pending records and stop the background writer."""
        core = self._core
        self.complete()
        core.queue.put(None)
        if core.writer_thread:
            core.writer_thread.join(timeout=5.0)
        core.writer_started = False
