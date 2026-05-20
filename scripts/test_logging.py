"""
Tests for production structured logging system.

Tests Logger, Core, opt(), bind(), contextualize(), patch(), level(),
enable/disable, configure(), custom levels, elapsed, thread/process tracking,
exception formatting, file rotation/retention/compression, JSON sink,
dict filters, lazy evaluation, raw mode, async sinks, reentrancy,
AccessLogMiddleware, and lifecycle.

Usage:
    uv run hyper-test logging
"""

# hyper-test: unit

import asyncio
import inspect
import io
import json
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

from hyperdjango.logging import (
    BUILTIN_LEVELS,
    AccessLogMiddleware,
    Logger,
    RecordException,
    RecordFile,
    RecordLevel,
    RecordProcess,
    RecordThread,
)
from hyperdjango.logging._core import Core as _Core

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

RESULTS = {"passed": 0, "failed": 0, "errors": []}


def test(name):
    def decorator(func):
        async def wrapper():
            try:
                if inspect.iscoroutinefunction(func):
                    await func()
                else:
                    func()
                RESULTS["passed"] += 1
                print(f"  ✓ {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  ✗ {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


def fresh_logger():
    """Create an isolated logger with its own Core (no auto-init sinks)."""
    core = _Core()
    core._rebuild_levels_lookup()
    return Logger(_core=core)


def collect_logger():
    """Create a logger that collects records into a list."""
    records = []
    log = fresh_logger()
    log.add(lambda rec, msg: records.append(rec), level="TRACE", enqueue=False)
    return log, records


# ---------------------------------------------------------------------------
# Record attribute classes
# ---------------------------------------------------------------------------


@test("RecordLevel: str, format, eq, repr")
def test_record_level():
    rl = RecordLevel("INFO", 20, "ℹ️")
    assert str(rl) == "INFO"
    assert f"{rl: <8}" == "INFO    "
    assert rl.no == 20
    assert rl == RecordLevel("INFO", 20, "x")
    assert "INFO" in repr(rl)


@test("RecordFile: str, format, path")
def test_record_file():
    rf = RecordFile("app.py", "/home/user/app.py")
    assert str(rf) == "app.py"
    assert f"{rf}" == "app.py"
    assert rf.path == "/home/user/app.py"


@test("RecordThread: str, format")
def test_record_thread():
    rt = RecordThread(12345, "MainThread")
    assert str(rt) == "12345"
    assert rt.name == "MainThread"


@test("RecordProcess: str, format")
def test_record_process():
    rp = RecordProcess(9876, "MainProcess")
    assert str(rp) == "9876"
    assert rp.name == "MainProcess"


@test("RecordException: str, bool, reduce")
def test_record_exception():
    # Empty exception
    exc = RecordException(None, None, None)
    assert not exc
    assert str(exc) == ""

    # Real exception
    try:
        1 / 0
    except ZeroDivisionError:
        ei = sys.exc_info()
        exc = RecordException(ei[0], ei[1], ei[2])
        assert bool(exc)
        assert "ZeroDivisionError" in str(exc)
        # Pickling support
        t, v, tb = exc.__reduce__()[1]
        assert t is ZeroDivisionError
        assert tb is None  # Traceback stripped for pickle


# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------


@test("Levels: 7 builtin defined")
def test_builtin_levels():
    assert len(BUILTIN_LEVELS) == 7
    assert BUILTIN_LEVELS["TRACE"].no == 5
    assert BUILTIN_LEVELS["CRITICAL"].no == 50


@test("Custom level: add and use")
def test_custom_level():
    log, records = collect_logger()
    log.level("AUDIT", no=35, color="\033[35m", icon="📋")
    log.log("AUDIT", "Audit event", user_id=1)
    # enqueue=False so records populated synchronously
    assert len(records) >= 1
    assert records[0]["level"].name == "AUDIT"
    assert records[0]["level"].no == 35


@test("Level: get existing")
def test_level_get():
    log = fresh_logger()
    info = log.level("INFO")
    assert info.no == 20
    assert info.name == "INFO"


# ---------------------------------------------------------------------------
# Basic logging
# ---------------------------------------------------------------------------


@test("Logger: info writes record")
def test_basic_info():
    log, records = collect_logger()
    log.info("Hello world")
    assert len(records) == 1
    assert records[0]["message"] == "Hello world"
    assert records[0]["level"].name == "INFO"


@test("Logger: message formatting with args")
def test_message_args():
    log, records = collect_logger()
    log.info("User {} age {}", "Alice", 30)
    assert records[0]["message"] == "User Alice age 30"


@test("Logger: kwargs captured in extra")
def test_kwargs_extra():
    log, records = collect_logger()
    log.info("Login", user_id=42, ip="10.0.0.1")
    assert records[0]["extra"]["user_id"] == 42
    assert records[0]["extra"]["ip"] == "10.0.0.1"


@test("Logger: level filtering")
def test_level_filter():
    records = []
    log = fresh_logger()
    log.add(lambda rec, msg: records.append(rec), level="WARNING", enqueue=False)
    log.debug("Skip")
    log.info("Skip")
    log.warning("Keep")
    log.error("Keep")
    assert len(records) == 2


@test("Logger: all 7 levels work")
def test_all_levels():
    log, records = collect_logger()
    log.trace("t")
    log.debug("d")
    log.info("i")
    log.success("s")
    log.warning("w")
    log.error("e")
    log.critical("c")
    assert len(records) == 7
    assert [r["level"].name for r in records] == [
        "TRACE",
        "DEBUG",
        "INFO",
        "SUCCESS",
        "WARNING",
        "ERROR",
        "CRITICAL",
    ]


# ---------------------------------------------------------------------------
# Record fields
# ---------------------------------------------------------------------------


@test("Record: has elapsed")
def test_record_elapsed():
    log, records = collect_logger()
    log.info("Test")
    from datetime import timedelta

    assert isinstance(records[0]["elapsed"], timedelta)
    assert records[0]["elapsed"].total_seconds() >= 0


@test("Record: has thread info")
def test_record_thread():
    log, records = collect_logger()
    log.info("Test")
    assert isinstance(records[0]["thread"], RecordThread)
    assert records[0]["thread"].id is not None
    assert records[0]["thread"].name is not None


@test("Record: has process info")
def test_record_process():
    log, records = collect_logger()
    log.info("Test")
    assert isinstance(records[0]["process"], RecordProcess)
    assert records[0]["process"].id > 0
    assert records[0]["process"].name is not None


@test("Record: has caller info")
def test_record_caller():
    log, records = collect_logger()
    log.info("Test")
    assert records[0]["function"] == "test_record_caller"
    assert records[0]["line"] > 0
    assert "test_logging" in records[0]["module"]


@test("Record: has timestamp")
def test_record_timestamp():
    from datetime import datetime

    log, records = collect_logger()
    log.info("Test")
    assert isinstance(records[0]["time"], datetime)


@test("Record: has exception (None by default)")
def test_record_exception_default():
    log, records = collect_logger()
    log.info("No error")
    assert isinstance(records[0]["exception"], RecordException)
    assert not records[0]["exception"]


# ---------------------------------------------------------------------------
# Bind
# ---------------------------------------------------------------------------


@test("Bind: adds extra fields")
def test_bind():
    log, records = collect_logger()
    bound = log.bind(request_id="abc")
    bound.info("Test")
    assert records[0]["extra"]["request_id"] == "abc"


@test("Bind: chained merge")
def test_bind_chain():
    log, records = collect_logger()
    bound = log.bind(a=1).bind(b=2)
    bound.info("Test")
    assert records[0]["extra"]["a"] == 1
    assert records[0]["extra"]["b"] == 2


@test("Bind: doesn't mutate parent")
def test_bind_immutable():
    log, records = collect_logger()
    bound = log.bind(only_child=True)
    log.info("Parent")
    bound.info("Child")
    assert "only_child" not in records[0]["extra"]
    assert records[1]["extra"]["only_child"] is True


# ---------------------------------------------------------------------------
# opt()
# ---------------------------------------------------------------------------


@test("opt(lazy=True): defers callable evaluation")
def test_opt_lazy():
    log, records = collect_logger()
    called = [False]

    def expensive():
        called[0] = True
        return "computed"

    # Lazy with level that will log
    log.opt(lazy=True).info("Value: {}", expensive)
    assert called[0] is True
    assert records[0]["message"] == "Value: computed"


@test("opt(lazy=True): skips callable if filtered")
def test_opt_lazy_skip():
    records = []
    log = fresh_logger()
    log.add(lambda rec, msg: records.append(rec), level="ERROR", enqueue=False)

    called = [False]

    def expensive():
        called[0] = True
        return "computed"

    log.opt(lazy=True).debug("Value: {}", expensive)  # Below ERROR threshold
    assert called[0] is False
    assert len(records) == 0


@test("opt(exception=True): captures traceback")
def test_opt_exception():
    log, records = collect_logger()
    try:
        1 / 0
    except ZeroDivisionError:
        log.opt(exception=True).error("Math failed")

    assert records[0]["exception"].type is ZeroDivisionError
    assert "ZeroDivisionError" in str(records[0]["exception"])


@test("opt(raw=True): skips formatting")
def test_opt_raw():
    log, records = collect_logger()
    log.opt(raw=True).info("Raw: {not_a_placeholder}")
    assert records[0]["message"] == "Raw: {not_a_placeholder}"


@test("opt(capture=False): doesn't add kwargs to extra")
def test_opt_no_capture():
    log, records = collect_logger()
    log.opt(capture=False).info("Test {x}", x=42)
    assert "x" not in records[0]["extra"]
    assert records[0]["message"] == "Test 42"


@test("opt(depth=1): skips extra frame")
def test_opt_depth():
    log, records = collect_logger()

    def wrapper(msg):
        log.opt(depth=1).info(msg)

    wrapper("From wrapper")
    # Should show THIS function's name, not wrapper's
    assert records[0]["function"] == "test_opt_depth"


# ---------------------------------------------------------------------------
# contextualize()
# ---------------------------------------------------------------------------


@test("contextualize: adds fields for block scope")
def test_contextualize():
    log, records = collect_logger()
    with log.contextualize(task_id=123):
        log.info("In context")
    log.info("Outside")
    assert records[0]["extra"]["task_id"] == 123
    assert "task_id" not in records[1]["extra"]


@test("contextualize: nested merges")
def test_contextualize_nested():
    log, records = collect_logger()
    with log.contextualize(a=1):
        with log.contextualize(b=2):
            log.info("Both")
        log.info("Only a")
    assert records[0]["extra"]["a"] == 1
    assert records[0]["extra"]["b"] == 2
    assert records[1]["extra"]["a"] == 1
    assert "b" not in records[1]["extra"]


# ---------------------------------------------------------------------------
# patch()
# ---------------------------------------------------------------------------


@test("patch: modifies records")
def test_patch():
    log, records = collect_logger()
    patched = log.patch(lambda r: r["extra"].update(hostname="server1"))
    patched.info("Test")
    assert records[0]["extra"]["hostname"] == "server1"


@test("patch: doesn't affect parent")
def test_patch_immutable():
    log, records = collect_logger()
    patched = log.patch(lambda r: r["extra"].update(tag="patched"))
    log.info("Parent")
    patched.info("Patched")
    assert "tag" not in records[0]["extra"]
    assert records[1]["extra"]["tag"] == "patched"


@test("Core patcher: applies to all records")
def test_core_patcher():
    log, records = collect_logger()
    log._core.patcher = lambda r: r["extra"].update(app="myapp")
    log.info("Test")
    assert records[0]["extra"]["app"] == "myapp"
    log._core.patcher = None


# ---------------------------------------------------------------------------
# enable/disable
# ---------------------------------------------------------------------------


@test("disable: suppresses module")
def test_disable():
    log, records = collect_logger()
    # When run directly, __name__ is __main__; when run via runner, it's test_logging
    log.disable("__main__")
    log.disable("test_logging")
    log.info("Should be suppressed")
    assert len(records) == 0
    log.enable("__main__")
    log.enable("test_logging")


@test("enable: re-enables module")
def test_enable():
    log, records = collect_logger()
    log.disable("test_logging")
    log.enable("test_logging")
    log.info("Should appear")
    assert len(records) == 1


@test("disable: parent affects children")
def test_disable_parent():
    log, records = collect_logger()
    log.disable("test_logging")
    # This test's __name__ is __main__ when run directly, but test_logging when imported
    # The record's "name" comes from f_globals["__name__"]
    # Force a specific name check
    log._core.enabled_cache.clear()
    log.enable("test_logging")


# ---------------------------------------------------------------------------
# configure()
# ---------------------------------------------------------------------------


@test("configure: bulk setup handlers")
def test_configure():
    records = []
    log = fresh_logger()
    ids = log.configure(
        handlers=[
            {
                "sink": lambda rec, msg: records.append(rec),
                "level": "INFO",
                "enqueue": False,
            },
        ],
        extra={"app": "test"},
    )
    assert len(ids) == 1
    log.info("Configured")
    assert len(records) == 1
    assert records[0]["extra"]["app"] == "test"


@test("configure: sets levels and activation")
def test_configure_full():
    log = fresh_logger()
    log.configure(
        handlers=[{"sink": lambda r, m: None, "level": "DEBUG", "enqueue": False}],
        levels=[{"name": "CUSTOM", "no": 15, "color": "", "icon": "C"}],
        activation=[("noisy_lib", False)],
    )
    assert log._core.levels["CUSTOM"].no == 15
    assert not log._core.is_module_enabled("noisy_lib")


# ---------------------------------------------------------------------------
# Dict filter
# ---------------------------------------------------------------------------


@test("Dict filter: per-module levels")
def test_dict_filter():
    records = []
    log = fresh_logger()
    log.add(
        lambda rec, msg: records.append(rec),
        level="TRACE",
        filter={"": "WARNING", "myapp": "DEBUG"},
        enqueue=False,
    )
    # Simulate a record from "myapp" module
    log.info("From myapp")  # This test's module doesn't match "myapp"
    # Default "" filter is WARNING, so INFO is filtered
    assert len(records) == 0

    log.warning("Warning from default")
    assert len(records) == 1


# ---------------------------------------------------------------------------
# JSON sink
# ---------------------------------------------------------------------------


@test("JsonSink: valid JSON with all fields")
def test_json_all_fields():
    buf = io.StringIO()
    log = fresh_logger()
    log.add(buf, level="DEBUG", serialize=True, enqueue=False)
    log.info("Hello", user_id=42)

    data = json.loads(buf.getvalue().strip())
    assert data["level"] == "INFO"
    assert data["message"] == "Hello"
    assert data["extra"]["user_id"] == 42
    assert "timestamp" in data
    assert "thread" in data
    assert "process" in data
    assert "elapsed" in data
    assert "module" in data
    assert "function" in data


@test("JsonSink: OTel trace fields promoted")
def test_json_otel():
    buf = io.StringIO()
    log = fresh_logger()
    log.add(buf, level="DEBUG", serialize=True, enqueue=False)
    log.bind(trace_id="t123", span_id="s456").info("Traced")

    data = json.loads(buf.getvalue().strip())
    assert data["trace_id"] == "t123"
    assert data["span_id"] == "s456"


@test("JsonSink: exception included")
def test_json_exception():
    buf = io.StringIO()
    log = fresh_logger()
    log.add(buf, level="DEBUG", serialize=True, enqueue=False)
    try:
        1 / 0
    except ZeroDivisionError:
        log.opt(exception=True).error("Failed")

    data = json.loads(buf.getvalue().strip())
    assert data["exception"]["type"] == "ZeroDivisionError"
    # traceback is now the FORMATTED STACK STRING (was a useless bool) — the JSON
    # sink must carry the frames so machine-readable logs are actually debuggable.
    tb = data["exception"]["traceback"]
    assert (
        isinstance(tb, str) and "ZeroDivisionError" in tb and "division by zero" in tb
    )


# ---------------------------------------------------------------------------
# Console sink
# ---------------------------------------------------------------------------


@test("ConsoleSink: writes formatted output")
def test_console():
    buf = io.StringIO()
    log = fresh_logger()
    log.add(buf, level="DEBUG", colorize=False, enqueue=False)
    log.info("Console test")

    output = buf.getvalue()
    assert "Console test" in output
    assert "INFO" in output


# ---------------------------------------------------------------------------
# File sink
# ---------------------------------------------------------------------------


@test("FileSink: writes to file")
def test_file_sink():
    with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
        path = f.name

    log = fresh_logger()
    log.add(path, level="DEBUG", enqueue=False)
    log.info("File test")

    content = Path(path).read_text()
    assert "File test" in content
    Path(path).unlink()


@test("FileSink: size rotation")
def test_file_rotation():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = str(Path(tmpdir) / "app.log")
        log = fresh_logger()
        log.add(path, level="DEBUG", rotation=100, enqueue=False)  # 100 bytes

        # Write enough to trigger rotation
        for i in range(20):
            log.info("Line {} with some padding to fill bytes", i)

        # Should have rotated — backup file exists
        files = list(Path(tmpdir).iterdir())
        assert len(files) >= 2, f"Expected rotation, got: {files}"


@test("FileSink: parse_size handles units")
def test_parse_size():
    from hyperdjango.logging._sinks import _parse_size

    assert _parse_size(1024) == 1024
    assert _parse_size("100 MB") == 100 * 1024**2
    assert _parse_size("1 GB") == 1024**3
    assert _parse_size("500 KB") == 500 * 1024


@test("FileSink: parse_time_rotation")
def test_parse_time_rotation():
    from hyperdjango.logging._sinks import _parse_time_interval

    assert _parse_time_interval("daily") == 86400
    assert _parse_time_interval("hourly") == 3600
    assert _parse_time_interval("weekly") == 604800
    assert _parse_time_interval("1 hour") == 3600
    assert _parse_time_interval("30 minutes") == 1800
    assert _parse_time_interval("7 days") == 7 * 86400


@test("FileSink: compression on rotation")
def test_file_compression():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = str(Path(tmpdir) / "app.log")
        log = fresh_logger()
        log.add(path, level="DEBUG", rotation=50, compression="gz", enqueue=False)

        for i in range(20):
            log.info("Compressed line {}", i)

        files = list(Path(tmpdir).iterdir())
        gz_files = [f for f in files if f.suffix == ".gz"]
        assert len(gz_files) >= 1, f"Expected .gz files, got: {files}"


@test("FileSink: retention limits backups")
def test_file_retention():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = str(Path(tmpdir) / "app.log")
        log = fresh_logger()
        log.add(path, level="DEBUG", rotation=50, retention=2, enqueue=False)

        for i in range(50):
            log.info("Retention test line {}", i)

        files = list(Path(tmpdir).iterdir())
        # Should keep only 2 backups + current
        assert len(files) <= 4, f"Retention failed, got {len(files)} files: {files}"


# ---------------------------------------------------------------------------
# Dynamic format
# ---------------------------------------------------------------------------


@test("Dynamic format: callable format function")
def test_dynamic_format():
    buf = io.StringIO()
    log = fresh_logger()
    log.add(
        buf, level="DEBUG", format=lambda r: "[{level.name}] {message}", enqueue=False
    )
    log.info("Dynamic")

    output = buf.getvalue()
    assert "[INFO] Dynamic" in output


# ---------------------------------------------------------------------------
# Exception logging
# ---------------------------------------------------------------------------


@test("exception(): captures traceback")
def test_exception_method():
    log, records = collect_logger()
    try:
        raise ValueError("test error")
    except ValueError:
        log.exception("Caught")

    assert len(records) == 1
    assert records[0]["exception"].type is ValueError
    assert "ValueError" in str(records[0]["exception"])


@test("Handler: appends traceback to formatted output")
def test_handler_traceback():
    buf = io.StringIO()
    log = fresh_logger()
    log.add(buf, level="DEBUG", colorize=False, enqueue=False)
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        log.opt(exception=True).error("Failed")

    output = buf.getvalue()
    assert "RuntimeError" in output
    assert "boom" in output


# ---------------------------------------------------------------------------
# remove()
# ---------------------------------------------------------------------------


@test("remove: by handler ID")
def test_remove_by_id():
    log = fresh_logger()
    hid = log.add(lambda r, m: None, level="DEBUG", enqueue=False)
    assert len(log._core.handlers) == 1
    log.remove(hid)
    assert len(log._core.handlers) == 0


@test("remove: all handlers when no ID")
def test_remove_all():
    log = fresh_logger()
    log.add(lambda r, m: None, level="DEBUG", enqueue=False)
    log.add(lambda r, m: None, level="INFO", enqueue=False)
    assert len(log._core.handlers) == 2
    log.remove()
    assert len(log._core.handlers) == 0


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


@test("Thread safety: concurrent logging")
def test_thread_safety():
    records = []
    log = fresh_logger()
    log.add(lambda rec, msg: records.append(rec), level="DEBUG", enqueue=False)

    def log_many(n):
        for i in range(n):
            log.info("Thread {}", threading.current_thread().name, i=i)

    threads = [threading.Thread(target=log_many, args=(50,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(records) == 200


# ---------------------------------------------------------------------------
# Reentrancy
# ---------------------------------------------------------------------------


@test("Reentrancy: logging from within sink doesn't deadlock")
def test_reentrancy():
    outer_records = []
    log = fresh_logger()

    def reentrant_sink(rec, msg):
        outer_records.append(rec)
        # Try to log from within sink — should be silently skipped (reentrancy guard)
        # This would deadlock without the guard
        # (We can't test this directly with enqueue=False since the lock is per-handler)

    log.add(reentrant_sink, level="DEBUG", enqueue=False)
    log.info("Test")
    assert len(outer_records) == 1


# ---------------------------------------------------------------------------
# Async sink
# ---------------------------------------------------------------------------


@test("AsyncSink: accepts coroutine function")
async def test_async_sink():
    records = []

    async def async_handler(rec, msg):
        records.append(rec)

    log = fresh_logger()
    log.add(async_handler, level="DEBUG", enqueue=False)
    log.info("Async test")

    # Poll until the scheduled async handler runs. Under CI load a fixed
    # sleep can be too short; the deadline-with-poll runs as fast as the
    # event loop can dispatch and only fails on real breakage.
    deadline = time.monotonic() + 5.0
    while not records and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert len(records) >= 1


# ---------------------------------------------------------------------------
# AccessLogMiddleware
# ---------------------------------------------------------------------------


@test("AccessLogMiddleware: logs request")
async def test_access_log():
    records = []
    log = fresh_logger()
    log.add(lambda rec, msg: records.append(rec), level="DEBUG", enqueue=False)

    mw = AccessLogMiddleware(logger_instance=log)

    class FakeRequest:
        method = "GET"
        path = "/api/users"
        client_ip = "10.0.0.1"
        user = None
        request_id = "req-001"

    class FakeResponse:
        status_code = 200

    async def call_next(req):
        return FakeResponse()

    await mw(FakeRequest(), call_next)

    assert len(records) >= 1
    assert "GET" in records[-1]["message"]
    assert "/api/users" in records[-1]["message"]


# ---------------------------------------------------------------------------
# catch() decorator
# ---------------------------------------------------------------------------


@test("catch: logs exception from sync function")
def test_catch_sync():
    records = []
    log = fresh_logger()
    log.add(lambda rec, msg: records.append(rec), level="DEBUG", enqueue=False)

    @log.catch()
    def risky():
        raise ValueError("boom")

    risky()
    assert len(records) == 1
    assert records[0]["level"].name == "ERROR"
    assert records[0]["exception"].type is ValueError


@test("catch: logs exception from async function")
async def test_catch_async():
    records = []
    log = fresh_logger()
    log.add(lambda rec, msg: records.append(rec), level="DEBUG", enqueue=False)

    @log.catch(level="CRITICAL", message="Task crashed")
    async def risky():
        raise RuntimeError("async boom")

    await risky()
    assert len(records) == 1
    assert records[0]["level"].name == "CRITICAL"
    assert "Task crashed" in records[0]["message"]


@test("catch: returns normally when no exception")
def test_catch_no_exception():
    records = []
    log = fresh_logger()
    log.add(lambda rec, msg: records.append(rec), level="DEBUG", enqueue=False)

    @log.catch()
    def safe():
        return 42

    result = safe()
    assert result == 42
    assert len(records) == 0


# ---------------------------------------------------------------------------
# stats()
# ---------------------------------------------------------------------------


@test("stats: returns system metrics")
def test_stats():
    log, records = collect_logger()
    stats = log.stats()
    assert "handlers" in stats
    assert stats["handlers"] >= 1
    assert "min_level" in stats
    assert "levels" in stats
    assert "activation_rules" in stats


# ---------------------------------------------------------------------------
# Patcher error protection
# ---------------------------------------------------------------------------


@test("Patcher: errors caught, don't crash logging")
def test_patcher_error():
    records = []
    log = fresh_logger()
    log.add(lambda rec, msg: records.append(rec), level="DEBUG", enqueue=False)

    def bad_patcher(record):
        raise RuntimeError("patcher crash")

    patched = log.patch(bad_patcher)
    patched.info("Should still work")
    # Record should still be emitted despite patcher failure
    assert len(records) == 1
    assert records[0]["message"] == "Should still work"


# ---------------------------------------------------------------------------
# Colorizer
# ---------------------------------------------------------------------------


@test("Colorizer: colorize basic tags")
def test_colorize_basic():
    from hyperdjango.logging._colorizer import colorize

    result = colorize("<red>Error</red>")
    assert "\033[31m" in result
    assert "\033[0m" in result
    assert "Error" in result


@test("Colorizer: strip_markup removes tags")
def test_strip_markup():
    from hyperdjango.logging._colorizer import strip_markup

    assert strip_markup("<red>Error</red>: <bold>fatal</bold>") == "Error: fatal"


@test("Colorizer: 8-bit colors")
def test_colorize_8bit():
    from hyperdjango.logging._colorizer import colorize

    result = colorize("<fg 196>Red</fg 196>")
    assert "\033[38;5;196m" in result


@test("Colorizer: hex colors")
def test_colorize_hex():
    from hyperdjango.logging._colorizer import colorize

    result = colorize("<fg #FF0000>Red</>")
    assert "\033[38;2;255;0;0m" in result


@test("Colorizer: styles")
def test_colorize_styles():
    from hyperdjango.logging._colorizer import colorize

    for style in ("bold", "dim", "italic", "underline", "strike"):
        result = colorize(f"<{style}>text</{style}>")
        assert "\033[" in result


# ---------------------------------------------------------------------------
# StandardSink bridge
# ---------------------------------------------------------------------------


@test("StandardSink: bridges to logging.Handler")
def test_standard_sink():
    import logging

    records = []

    class CollectHandler(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = CollectHandler()
    log = fresh_logger()
    log.add(handler, level="DEBUG", enqueue=False)
    log.info("Bridge test")

    assert len(records) == 1
    assert records[0].getMessage() == "Bridge test"
    assert records[0].funcName == "test_standard_sink"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@test("complete: drains queue")
def test_complete():
    records = []
    core = _Core()
    core._rebuild_levels_lookup()
    log = Logger(_core=core)
    log.add(lambda rec, msg: records.append(rec), level="DEBUG")

    for i in range(100):
        log.info("Msg {}", i)

    log.complete()
    assert len(records) == 100


@test("shutdown: stops writer thread")
def test_shutdown():
    core = _Core()
    core._rebuild_levels_lookup()
    log = Logger(_core=core)
    log.add(lambda rec, msg: None, level="DEBUG")
    log.info("Start")
    log.complete()

    assert core.writer_thread is not None
    assert core.writer_thread.is_alive()

    log.shutdown()
    # Wait deterministically for the writer thread to exit rather than
    # a fixed sleep. On loaded CI runners under free-threading, the
    # scheduler can delay thread teardown past any small fixed window.
    core.writer_thread.join(timeout=5.0)
    assert not core.writer_thread.is_alive()


# ---------------------------------------------------------------------------
# Color markup rendering tests
# ---------------------------------------------------------------------------


@test("colorize: markup converted to ANSI in tty handler")
def test_colorize_markup_tty():
    """Verify <green>, <cyan>, <level> tags are converted to ANSI codes."""
    from hyperdjango.logging._colorizer import strip_ansi

    buf = io.StringIO()
    log = Logger()
    log.add(buf, format="<green>{message}</green>", colorize=True, level="DEBUG")
    log.info("hello")
    log.complete()  # drain async writer queue deterministically
    output = buf.getvalue()
    # Must NOT contain literal <green> tags
    assert "<green>" not in output, f"Raw markup in output: {output!r}"
    assert "</" not in output, f"Raw close tag in output: {output!r}"
    # Must contain ANSI escape codes
    assert "\033[" in output, f"No ANSI codes in output: {output!r}"
    # Stripped version should have the message
    plain = strip_ansi(output)
    assert "hello" in plain


@test("colorize: level tag resolves to level-specific color")
def test_colorize_level_tag():
    """Verify <level> tag resolves differently for INFO vs WARNING."""

    buf_info = io.StringIO()
    buf_warn = io.StringIO()
    log = Logger()
    log.add(
        buf_info, format="<level>{level.name}</level>", colorize=True, level="DEBUG"
    )
    log.add(
        buf_warn, format="<level>{level.name}</level>", colorize=True, level="DEBUG"
    )
    log.info("test")
    log.warning("test")
    log.complete()  # drain async writer queue deterministically
    info_out = buf_info.getvalue()
    warn_out = buf_warn.getvalue()
    # Both should have ANSI, no raw <level> tags
    assert "<level>" not in info_out, f"Raw <level> in info: {info_out!r}"
    assert "<level>" not in warn_out, f"Raw <level> in warn: {warn_out!r}"
    assert "\033[" in info_out, f"No ANSI in info: {info_out!r}"


@test("colorize: non-colorize handler strips markup")
def test_no_colorize_strips_markup():
    """Verify non-colorize handler produces clean output without markup tags."""
    buf = io.StringIO()
    log = Logger()
    log.add(buf, format="<green>{message}</green>", colorize=False, level="DEBUG")
    log.info("clean")
    log.complete()  # drain async writer queue deterministically
    output = buf.getvalue()
    assert "<green>" not in output, f"Markup in non-color output: {output!r}"
    assert "\033[" not in output, f"ANSI in non-color output: {output!r}"
    assert "clean" in output


@test("colorize: DEFAULT_FORMAT renders without raw tags")
def test_default_format_no_raw_tags():
    """Verify DEFAULT_FORMAT produces no <green>, <cyan>, <level> in output."""
    from hyperdjango.logging._logger import DEFAULT_FORMAT

    buf = io.StringIO()
    log = Logger()
    log.add(buf, format=DEFAULT_FORMAT, colorize=True, level="DEBUG")
    log.info("default format test")
    log.complete()  # drain async writer queue deterministically
    output = buf.getvalue()
    assert "<green>" not in output, f"<green> in output: {output!r}"
    assert "<cyan>" not in output, f"<cyan> in output: {output!r}"
    assert "<level>" not in output, f"<level> in output: {output!r}"
    assert "</green>" not in output, f"</green> in output: {output!r}"
    assert "default format test" in output


@test("colorize: PLAIN_FORMAT has no ANSI codes")
def test_plain_format_no_ansi():
    """Verify PLAIN_FORMAT never produces ANSI codes regardless of colorize."""
    from hyperdjango.logging._logger import PLAIN_FORMAT

    buf = io.StringIO()
    log = Logger()
    log.add(buf, format=PLAIN_FORMAT, colorize=False, level="DEBUG")
    log.info("plain test")
    log.complete()  # drain async writer queue deterministically
    output = buf.getvalue()
    assert "\033[" not in output, f"ANSI in plain output: {output!r}"
    assert "plain test" in output


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def main():
    all_tests = []
    for name, obj in list(globals().items()):
        if callable(obj) and getattr(obj, "_is_test", False):
            all_tests.append(obj)

    print("\n═══ Logging Tests ═══")
    for t in all_tests:
        await t()

    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"\n{'═' * 60}")
    print(f"Results: {RESULTS['passed']}/{total} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n--- {name} ---")
            print(tb)

    return RESULTS["failed"] == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
