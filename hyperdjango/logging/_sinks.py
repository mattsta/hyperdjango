"""
Sink implementations for the logging system.

Sinks are the output destinations for log records. Each sink implements
write(message, record) and stop().

Sinks:
- ConsoleSink: Colorized output to stderr/stdout
- JsonSink: Structured JSON (one object per line) via fast_json_dumps
- FileSink: File output with rotation (size/time), retention, compression
- CallableSink: Wraps any callable(record, message)
- AsyncSink: Wraps async coroutine function
- StandardSink: Bridge to Python stdlib logging.Handler
"""

import asyncio
import bz2
import contextlib
import gzip
import io
import logging
import lzma
import re
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TextIO

from hyperdjango.logging._core import BUILTIN_LEVELS, get_core
from hyperdjango.logging._native_compat import (
    json_dumps as fast_json_dumps,
)
from hyperdjango.logging._native_compat import (
    log_timestamp_iso as _native_timestamp,
)
from hyperdjango.logging._record import LogRecord

# OTel fields promoted from extra to top-level in JSON output
_OTEL_PROMOTE_KEYS = ("trace_id", "span_id", "trace_flags")

# ---------------------------------------------------------------------------
# Console Sink
# ---------------------------------------------------------------------------


@dataclass
class ConsoleSink:
    """Colorized console output to a stream (default stderr).

    Auto-detects TTY for colorization. Caches level colors on first use.
    Flushes after every write.
    """

    stream: TextIO = field(default=None)
    colorize: bool = True
    _is_tty: bool = field(default=False, init=False, repr=False)
    _color_cache: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self):
        if self.stream is None:
            self.stream = sys.stderr
        self._is_tty = (
            self.colorize and hasattr(self.stream, "isatty") and self.stream.isatty()
        )

    def _get_color(self, level_name: str) -> str:
        """Get ANSI color for level, cached after first lookup."""
        try:
            return self._color_cache[level_name]
        except KeyError:
            core = get_core()
            lvl = core.levels.get(level_name) or BUILTIN_LEVELS.get(level_name)
            color = lvl.color if lvl else ""
            self._color_cache[level_name] = color
            return color

    def write(self, message: str, record: LogRecord):
        # Handler already applies colorize() markup → ANSI conversion,
        # so we just write the message directly. No extra level-color wrap.
        self.stream.write(message)
        self.stream.flush()

    def stop(self):
        pass


# ---------------------------------------------------------------------------
# JSON Sink
# ---------------------------------------------------------------------------


@dataclass
class JsonSink:
    """Structured JSON output — one JSON object per line.

    Uses native fast_json_dumps for speed. Promotes OTel trace_id/span_id
    to top level for easy filtering. Includes full record metadata.
    """

    stream: TextIO = field(default=None)

    def __post_init__(self):
        if self.stream is None:
            self.stream = sys.stderr

    def write(self, message: str, record: LogRecord):
        # Use native Zig timestamp (8.3x faster than datetime.isoformat())
        ts_bytes = _native_timestamp()
        obj = {
            "timestamp": ts_bytes.decode("ascii")
            if isinstance(ts_bytes, bytes)
            else record["time"].isoformat(),
            "level": record["level"].name,
            "message": record["message"],
            "module": record["module"],
            "function": record["function"],
            "line": record["line"],
            "name": record.get("name", ""),
            "thread": {"id": record["thread"].id, "name": record["thread"].name},
            "process": {"id": record["process"].id, "name": record["process"].name},
            "elapsed": record["elapsed"].total_seconds(),
        }

        extra = record.get("extra", {})
        if extra:
            obj["extra"] = extra

        # Promote OTel fields to top level
        for key in _OTEL_PROMOTE_KEYS:
            val = extra.get(key)
            if val is not None:
                obj[key] = val

        # Exception
        exc = record.get("exception")
        if exc and exc.type is not None:
            # Serialize the *formatted* traceback string (frames included), not a
            # bare bool. RecordException.__str__ renders the full
            # traceback.format_exception() output; when the traceback object is
            # absent (e.g. after pickling across processes) it still yields the
            # "Type: value" header so the field is never a useless boolean.
            tb_str = str(exc)
            obj["exception"] = {
                "type": exc.type.__name__ if exc.type else None,
                "value": str(exc.value) if exc.value else None,
                "traceback": tb_str or None,
            }

        json_bytes = fast_json_dumps(obj)
        if isinstance(json_bytes, bytes):
            json_bytes = json_bytes.decode("utf-8")
        self.stream.write(json_bytes + "\n")
        self.stream.flush()

    def stop(self):
        pass


# ---------------------------------------------------------------------------
# File Sink
# ---------------------------------------------------------------------------

# Size unit multipliers
_SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}

# Time interval multipliers (seconds)
_TIME_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

# Named time intervals
_NAMED_INTERVALS = {
    "hourly": 3600,
    "daily": 86400,
    "weekly": 604800,
    "monthly": 2592000,
}

# Supported compression formats
_COMPRESSION_OPENERS = {
    "gz": gzip.open,
    "bz2": bz2.open,
    "xz": lzma.open,
}


def _parse_size(rotation: str | int) -> int | None:
    """Parse a size string like '100 MB' or int bytes."""
    if isinstance(rotation, int):
        return rotation
    if not isinstance(rotation, str):
        return None
    s = rotation.strip().upper()
    match = re.match(r"^(\d+)\s*(B|KB|MB|GB|TB)?$", s)
    if not match:
        return None
    num = int(match.group(1))
    unit = match.group(2) or "B"
    return num * _SIZE_UNITS.get(unit, 1)


def _parse_time_interval(rotation: str) -> float | None:
    """Parse a time rotation string like 'daily', '1 hour', '30 minutes'."""
    if not isinstance(rotation, str):
        return None
    s = rotation.strip().lower()

    # Named intervals
    if s in _NAMED_INTERVALS:
        return _NAMED_INTERVALS[s]

    # Numeric with unit: "1 hour", "30 minutes", "7 days"
    match = re.match(
        r"^(\d+)\s*(s|sec|seconds?|m|min|minutes?|h|hours?|d|days?|w|weeks?)$",
        s,
    )
    if not match:
        return None
    num = int(match.group(1))
    unit_char = match.group(2)[0]
    return num * _TIME_UNITS.get(unit_char, 1)


def _parse_retention(retention: str | int) -> int | None:
    """Parse retention as count (int) or extract leading int from string."""
    if isinstance(retention, int):
        return retention
    if isinstance(retention, str):
        match = re.match(r"^(\d+)", retention.strip())
        if match:
            return int(match.group(1))
    return None


@dataclass
class FileSink:
    """File output with rotation, retention, and compression.

    Rotation:
        - By size: "100 MB", "1 GB", or int (bytes)
        - By time: "daily", "hourly", "1 hour", "7 days"
        - By callable: func(message, file) -> bool

    Retention:
        - By count: 10 (keep last 10 backups)
        - By string: "7 days" (parsed as count)

    Compression:
        - "gz", "bz2", "xz" — applied to rotated files
    """

    path: str
    rotation: str | int | Callable[[str, io.TextIOWrapper], bool] | None = None
    retention: str | int | None = None
    compression: str | None = None
    mode: str = "a"
    delay: bool = False

    # Internal state
    _file: io.TextIOWrapper | None = field(default=None, init=False, repr=False)
    _bytes_written: int = field(default=0, init=False, repr=False)
    _created_at: float = field(default_factory=time.time, init=False, repr=False)
    _rotation_size: int | None = field(default=None, init=False, repr=False)
    _rotation_time: float | None = field(default=None, init=False, repr=False)
    _rotation_fn: Callable[[str, io.TextIOWrapper], bool] | None = field(
        default=None, init=False, repr=False
    )
    _retention_count: int | None = field(default=None, init=False, repr=False)
    _inode: tuple | None = field(default=None, init=False, repr=False)
    _write_count: int = field(default=0, init=False, repr=False)
    _inode_check_interval: int = field(default=1000, init=False, repr=False)

    def __post_init__(self):
        # Parse rotation
        if callable(self.rotation):
            self._rotation_fn = self.rotation
        elif isinstance(self.rotation, (str, int)):
            self._rotation_size = _parse_size(self.rotation)
            if self._rotation_size is None and isinstance(self.rotation, str):
                self._rotation_time = _parse_time_interval(self.rotation)

        # Parse retention
        self._retention_count = _parse_retention(self.retention)

        # Open file unless delay
        if not self.delay:
            self._open()

    def _open(self):
        self._file = Path(self.path).open(self.mode, encoding="utf-8")  # noqa: SIM115 — long-lived handle; closed by _rotate()/_check_inode()
        self._bytes_written = 0
        self._created_at = time.time()
        self._update_inode()

    def _update_inode(self):
        """Track file inode for external rotation detection."""
        try:
            stat = Path(self.path).stat()
            self._inode = (stat.st_dev, stat.st_ino)
        except OSError:
            self._inode = None

    def _check_inode(self):
        """Detect if file was rotated externally (logrotate, etc)."""
        try:
            stat = Path(self.path).stat()
            current = (stat.st_dev, stat.st_ino)
            if self._inode and current != self._inode:
                # File was replaced externally — reopen
                self._file.close()
                self._open()
        except OSError:
            pass

    def write(self, message: str, record: LogRecord):
        if self._file is None:
            self._open()

        # Batch inode checks — every N writes instead of every write
        self._write_count += 1
        if self._write_count % self._inode_check_interval == 0:
            self._check_inode()

        self._file.write(message)
        self._file.flush()
        # Approximate byte count without encoding overhead (ASCII fast path)
        self._bytes_written += len(message)

        # Check rotation
        if self._should_rotate(message):
            self._rotate()

    def _should_rotate(self, message: str = "") -> bool:
        if self._rotation_size and self._bytes_written >= self._rotation_size:
            return True
        if (
            self._rotation_time
            and (time.time() - self._created_at) >= self._rotation_time
        ):
            return True
        if self._rotation_fn:
            try:
                return bool(self._rotation_fn(message, self._file))
            # blind-except: a user-supplied rotation predicate must not break the write path; treat a failing predicate as "do not rotate"
            except Exception:
                return False
        return False

    def _rotate(self):
        if self._file:
            self._file.close()

        # Generate timestamped backup name
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        backup = f"{self.path}.{ts}"
        counter = 0
        while Path(backup).exists():
            counter += 1
            backup = f"{self.path}.{ts}.{counter}"

        if Path(self.path).exists():
            Path(self.path).rename(backup)

            # Compress if configured
            if self.compression:
                self._compress(backup)

        # Apply retention
        self._apply_retention()

        # Reopen
        self._open()

    def _compress(self, filepath: str):
        ext = self.compression.lower()
        opener = _COMPRESSION_OPENERS.get(ext)
        if opener is None:
            return

        compressed_path = f"{filepath}.{ext}"
        try:
            with (
                Path(filepath).open("rb") as f_in,
                opener(compressed_path, "wb") as f_out,
            ):
                shutil.copyfileobj(f_in, f_out)
            Path(filepath).unlink()
        # blind-except: post-rotation backup compression is best-effort housekeeping; a failure must not break the log-rotation/write path
        except Exception:
            pass  # Don't break logging over compression failure

    def _apply_retention(self):
        """Keep only the N most recent backup files."""
        if self._retention_count is None:
            return
        log_path = Path(self.path)
        dirname = str(log_path.parent) if str(log_path.parent) != "." else "."
        basename = log_path.name
        try:
            backups = sorted(
                [
                    f.name
                    for f in Path(dirname).iterdir()
                    if f.name.startswith(basename + ".") and f.name != basename
                ],
                reverse=True,
            )
            for old in backups[self._retention_count :]:
                with contextlib.suppress(Exception):
                    (Path(dirname) / old).unlink()
        except OSError:
            pass

    def stop(self):
        if self._file:
            self._file.close()
            self._file = None


# ---------------------------------------------------------------------------
# Callable Sink
# ---------------------------------------------------------------------------


@dataclass
class CallableSink:
    """Wraps any callable(record, message) as a sink."""

    func: Callable[[LogRecord, str], None]

    def write(self, message: str, record: LogRecord):
        self.func(record, message)

    def stop(self):
        pass


# ---------------------------------------------------------------------------
# Async Sink
# ---------------------------------------------------------------------------


@dataclass
class AsyncSink:
    """Wraps an async coroutine function as a sink.

    Schedules the coroutine on the running event loop.
    Logs warning to stderr if no loop available.
    """

    func: Callable[[LogRecord, str], None]

    def write(self, message: str, record: LogRecord):
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.func(record, message))
        except RuntimeError:
            # No running loop — warn and skip (don't block writer thread)
            sys.stderr.write(
                "[hyper-logger] AsyncSink: no running event loop, message dropped\n"
            )

    def stop(self):
        pass


# ---------------------------------------------------------------------------
# Standard Sink (bridge to stdlib logging.Handler)
# ---------------------------------------------------------------------------


@dataclass
class StandardSink:
    """Bridge to Python's standard logging.Handler.

    Converts log records to stdlib logging.LogRecord format so existing
    logging.Handlers (FileHandler, SocketHandler, SysLogHandler, etc.)
    can be used as sinks.
    """

    handler: logging.Handler

    def write(self, message: str, record: LogRecord):
        level_no = record["level"].no
        # Map to stdlib levels (loguru and stdlib share the same numbers for standard levels)
        stdlib_level = min(level_no, 50)

        log_record = logging.LogRecord(
            name=record.get("name", "hyper"),
            level=stdlib_level,
            pathname=record["file"].path,
            lineno=record["line"],
            msg=record["message"],
            args=None,
            exc_info=(
                record["exception"].type,
                record["exception"].value,
                record["exception"].traceback,
            )
            if record.get("exception") and record["exception"].type
            else None,
            func=record["function"],
        )
        log_record.created = record["time"].timestamp()
        log_record.thread = record["thread"].id
        log_record.threadName = record["thread"].name
        log_record.process = record["process"].id
        log_record.processName = record["process"].name

        self.handler.emit(log_record)

    def stop(self):
        with contextlib.suppress(Exception):
            self.handler.close()
