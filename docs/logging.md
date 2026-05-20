# Logging

Production-grade structured logging with loguru-compatible API, native Zig timestamp acceleration, ANSI colorization, and four sink types. HyperDjango's logging system is purpose-built for high-throughput servers: timestamps are generated in Zig (8.3x faster than Python's `datetime.now()`), basename extraction is 1.9x faster, and module resolution is 3.7x faster than stdlib equivalents.

Unlike Python's `logging` module which requires configuring loggers, handlers, filters, and formatters separately, HyperDjango logging uses a single `logger` object with a chainable API. Add sinks, bind context, filter by level or module -- all from one import.

## Quick Start

```python
from hyperdjango.logging import logger

logger.info("Server started on {host}:{port}", host="0.0.0.0", port=8000)
logger.warning("Slow query: {ms}ms", ms=150)
logger.error("Connection failed: {err}", err=e)
```

Output (with default console sink):

```
2026-03-26 14:30:00.123 | INFO     | app:startup:42 - Server started on 0.0.0.0:8000
2026-03-26 14:30:00.456 | WARNING  | app:handle:87 - Slow query: 150ms
2026-03-26 14:30:01.789 | ERROR    | app:connect:23 - Connection failed: ConnectionRefusedError(...)
```

## Logger API

The global `logger` singleton is the primary interface. All logging methods accept a message template with `{}` placeholders and keyword arguments for interpolation.

### Log Level Methods

```python
logger.trace("Entered function {name}", name="process_order")
logger.debug("Cache lookup key={key} hit={hit}", key="user:42", hit=True)
logger.info("User {user} logged in from {ip}", user="alice", ip="10.0.0.1")
logger.success("Migration {name} applied in {ms}ms", name="0042_add_index", ms=12)
logger.warning("Connection pool at {pct}% capacity", pct=85)
logger.error("Failed to send email to {to}: {err}", to="bob@example.com", err=e)
logger.critical("Database connection lost, shutting down")
```

Each method has the signature:

```python
def info(message: str, *args: object, **kwargs: object) -> None
```

The `message` parameter supports Python-style `{}` format placeholders. Named placeholders are filled from `**kwargs`. Positional `{0}`, `{1}` placeholders are filled from `*args`.

### Log Levels

| Level      | Value | Description                                                   |
| ---------- | ----- | ------------------------------------------------------------- |
| `TRACE`    | 5     | Fine-grained debugging (function entry/exit, variable values) |
| `DEBUG`    | 10    | Diagnostic information for developers                         |
| `INFO`     | 20    | General operational messages (startup, shutdown, config)      |
| `SUCCESS`  | 25    | Confirmation that something worked as expected                |
| `WARNING`  | 30    | Something unexpected but non-fatal (deprecation, fallback)    |
| `ERROR`    | 40    | A significant problem that prevented an operation             |
| `CRITICAL` | 50    | A fatal condition requiring immediate attention               |

### Custom Levels

Register application-specific log levels:

```python
logger.level("AUDIT", no=35, color="<cyan>", icon="@")

logger.log("AUDIT", "User {user} changed password", user="alice")
```

Parameters:

- `name` -- level name (uppercase string)
- `no` -- numeric severity (determines filtering order)
- `color` -- ANSI markup tag for console output (optional)
- `icon` -- single-character icon shown in formatted output (optional)

## Sinks

Sinks are output destinations for log records. The default configuration includes a single `ConsoleSink` writing to stdout. Add additional sinks with `logger.add()`.

### logger.add()

```python
handler_id = logger.add(
    sink,  # Sink target (see below)
    level="DEBUG",  # Minimum level for this sink
    format=None,  # Custom format string (None = default)
    filter=None,  # Filter function or dict
    colorize=None,  # Force color on/off (None = auto-detect)
    serialize=False,  # Output as JSON lines
    backtrace=True,  # Show full traceback on exceptions
    diagnose=True,  # Show variable values in tracebacks
    enqueue=False,  # Write via background thread (non-blocking)
    catch=True,  # Catch exceptions in sink itself
)
```

Returns an integer `handler_id` that can be passed to `logger.remove()`.

### logger.remove()

```python
logger.remove(handler_id)  # Remove a specific sink
logger.remove()  # Remove ALL sinks (including default stdout)
```

### ConsoleSink

ANSI-colored terminal output with automatic TTY detection. When stdout is not a terminal (e.g., piped to a file or running in Docker), colors are automatically disabled unless `colorize=True` is forced.

```python
from hyperdjango.logging import ConsoleSink

logger.add(
    ConsoleSink(
        colorize=True,  # Force ANSI colors (default: auto-detect)
        stream="stderr",  # "stderr" or "stdout"
    )
)
```

The colorizer supports full ANSI markup in format strings:

```python
logger.add(
    ConsoleSink(),
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
)
```

Supported color tags:

- Named colors: `<red>`, `<green>`, `<blue>`, `<yellow>`, `<cyan>`, `<magenta>`, `<white>`, `<black>`
- Styles: `<bold>`, `<dim>`, `<italic>`, `<underline>`, `<strike>`
- 256-color: `<fg 196>`, `<bg 232>`
- True-color: `<fg #FF5733>`, `<bg #1A1A2E>`
- Level-aware: `<level>` uses the color defined for each log level

### JsonSink

Structured JSON lines output, one JSON object per log record. Ideal for log aggregation systems.

```python
from hyperdjango.logging import JsonSink

logger.add(
    JsonSink(
        path="app.json",  # File path (or file-like object)
        ensure_ascii=False,  # Allow UTF-8 in output
    )
)
```

Each line is a complete JSON object:

```json
{
  "time": "2026-03-26T14:30:00.123456",
  "level": "INFO",
  "message": "User alice logged in",
  "module": "auth",
  "function": "login",
  "line": 42,
  "extra": { "user": "alice", "ip": "10.0.0.1" }
}
```

You can also use the `serialize=True` shorthand on any sink:

```python
logger.add("app.json", serialize=True)
```

### FileSink

File output with automatic rotation, retention, and compression. The workhorse for production logging.

```python
from hyperdjango.logging import FileSink

logger.add(
    FileSink(
        path="logs/app.log",
        rotation="10 MB",  # Rotate when file exceeds 10 MB
        retention="30 days",  # Delete rotated files older than 30 days
        compression="gz",  # Compress rotated files with gzip
        encoding="utf-8",  # File encoding
        mode="a",  # Append mode (default)
        buffering=8192,  # Write buffer size in bytes
    )
)
```

Or use the shorthand string form:

```python
logger.add("logs/app.log", rotation="10 MB", retention="7 days", compression="gz")
```

#### Rotation Options

Rotation determines when a new log file is created. The current file is renamed with a timestamp suffix (e.g., `app.2026-03-26_14-30-00.log`) and a fresh file is started.

| Value       | Description                               | Example                             |
| ----------- | ----------------------------------------- | ----------------------------------- |
| Size string | Rotate when file exceeds size             | `"10 MB"`, `"500 KB"`, `"1 GB"`     |
| Time string | Rotate at interval                        | `"1 day"`, `"12 hours"`, `"1 week"` |
| Time of day | Rotate at specific time daily             | `"00:00"`, `"12:00"`, `"23:59"`     |
| `int`       | Rotate after N bytes                      | `10_000_000`                        |
| `callable`  | Custom function `(message, file) -> bool` | `lambda m, f: f.tell() > 5e6`       |

```python
# Rotate at midnight every day
logger.add("logs/app.log", rotation="00:00")

# Rotate every 500 KB
logger.add("logs/app.log", rotation="500 KB")

# Rotate weekly
logger.add("logs/app.log", rotation="1 week")


# Custom rotation logic
def should_rotate(message, file):
    # Rotate if file is over 5 MB and it's a new hour
    return file.tell() > 5_000_000


logger.add("logs/app.log", rotation=should_rotate)
```

#### Retention Options

Retention determines how long rotated files are kept. Old files are deleted automatically.

| Value           | Description                            | Example                              |
| --------------- | -------------------------------------- | ------------------------------------ |
| Duration string | Keep files for this long               | `"7 days"`, `"1 month"`, `"90 days"` |
| `int`           | Keep this many rotated files           | `10` (keep 10 most recent)           |
| `callable`      | Custom function `(list[Path]) -> None` | Delete files yourself                |

```python
# Keep 30 days of logs
logger.add("logs/app.log", rotation="10 MB", retention="30 days")

# Keep only the 5 most recent rotated files
logger.add("logs/app.log", rotation="10 MB", retention=5)
```

#### Compression Options

Compression is applied to rotated files (not the active file).

| Value      | Description                               |
| ---------- | ----------------------------------------- |
| `"gz"`     | Gzip compression (recommended)            |
| `"bz2"`    | Bzip2 compression (higher ratio, slower)  |
| `"xz"`     | LZMA compression (highest ratio, slowest) |
| `"zip"`    | ZIP archive                               |
| `"tar"`    | Tar archive (no compression)              |
| `"tar.gz"` | Gzipped tar archive                       |

```python
logger.add("logs/app.log", rotation="10 MB", retention="30 days", compression="gz")
```

### AsyncSink

Non-blocking background writer. Log calls return immediately; actual I/O happens in a dedicated background thread. Essential for latency-sensitive hot paths.

```python
from hyperdjango.logging import AsyncSink

logger.add(
    AsyncSink(
        sink="logs/app.log",  # Inner sink (file path, callable, or sink object)
        buffer_size=1000,  # Max messages to buffer before blocking
    )
)
```

Or use the `enqueue=True` shorthand on any sink:

```python
logger.add("logs/app.log", rotation="10 MB", enqueue=True)
```

The background writer flushes on:

- Buffer reaching capacity
- Application shutdown (graceful drain)
- Explicit `logger.complete()` call

```python
# Wait for all enqueued messages to be written
await logger.complete()
```

## Context Binding

Attach structured key-value context to log records without repeating it in every message.

### bind()

Create a child logger with permanently bound context. The child inherits all sinks and configuration from the parent.

```python
log = logger.bind(request_id="abc-123", user="alice")
log.info("Processing request")  # includes request_id and user
log.info("Query executed in {ms}ms", ms=12)  # also includes request_id and user

# Bind additional context (creates a new child, does not mutate)
log2 = log.bind(db="primary")
log2.info("Connected")  # has request_id, user, AND db
```

### contextualize()

Temporary context scoped to a `with` block. Uses Python's `contextvars` so it works correctly across async tasks.

```python
with logger.contextualize(trace_id="xyz-789", span="auth"):
    logger.info("Checking credentials")  # has trace_id and span
    await verify_password(user, pw)  # any logging inside also has trace_id and span
logger.info("Outside context")  # no trace_id or span
```

This is particularly useful in middleware to tag all log messages for a request:

```python
async def logging_middleware(request, handler):
    with logger.contextualize(
        request_id=request.headers.get("X-Request-ID", ""),
        method=request.method,
        path=request.path,
    ):
        return await handler(request)
```

### patch()

Modify log records dynamically with a function. Applied to every record produced by the patched logger.

```python
log = logger.patch(lambda record: record["extra"].update(app="myservice"))
log.info("Started")  # record["extra"]["app"] == "myservice"

# Useful for adding dynamic context like elapsed time
import time

start = time.monotonic()
log = logger.patch(lambda r: r["extra"].update(elapsed=time.monotonic() - start))
log.info("Step 1 done")  # elapsed=0.012
log.info("Step 2 done")  # elapsed=0.037
```

## opt() Options

Fine-tune individual log calls with `logger.opt()`. Returns a temporary logger for a single use.

### Lazy Evaluation

Defer expensive computations until the message is actually processed (i.e., not filtered out).

```python
logger.opt(lazy=True).debug(
    "Cache stats: {stats}",
    stats=lambda: compute_expensive_stats(),
)
# compute_expensive_stats() is only called if DEBUG level is active
```

With `lazy=True`, all keyword arguments must be zero-argument callables.

### Exception Logging

Attach the current exception traceback to a log record, even outside an `except` block.

```python
try:
    process_order(order_id)
except OrderError:
    logger.opt(exception=True).error("Failed to process order {id}", id=order_id)
    # The full traceback is included in the log record
```

You can also pass an explicit exception:

```python
logger.opt(exception=exc).error("Caught: {err}", err=exc)
```

### Call Depth

Adjust the caller frame depth for correct file/function/line reporting when logging from wrapper functions.

```python
def my_log_wrapper(msg, **kwargs):
    logger.opt(depth=1).info(msg, **kwargs)
    # depth=1 means "report the caller of my_log_wrapper, not my_log_wrapper itself"


my_log_wrapper("Hello from {place}", place="wrapper")
# Correctly reports the file/line of the call to my_log_wrapper
```

### Raw Mode

Write the message without any formatting or metadata (no timestamp, no level prefix).

```python
logger.opt(raw=True).info("This is written exactly as-is\n")
```

### Combining Options

Options can be combined:

```python
logger.opt(lazy=True, exception=True).error(
    "Failed: {details}",
    details=lambda: gather_diagnostic_info(),
)
```

## catch Decorator

Automatically catch and log exceptions from decorated functions. Works as both a decorator and a context manager.

### As a Decorator

```python
@logger.catch
def process_webhook(payload):
    # If this raises, the exception is logged with full traceback
    # and the function returns None (default)
    validate(payload)
    handle(payload)


@logger.catch(reraise=True)
def critical_operation():
    # Exception is logged AND re-raised
    do_something_important()


@logger.catch(default={"error": "internal"}, message="Webhook handler failed")
def webhook(data):
    # On exception: logs with custom message, returns default value
    return process(data)
```

### As a Context Manager

```python
with logger.catch(message="Background task failed"):
    await run_cleanup()
```

### Parameters

```python
@logger.catch(
    exception=Exception,     # Exception type(s) to catch (default: Exception)
    level="ERROR",           # Log level for caught exceptions
    reraise=False,           # Re-raise after logging
    default=None,            # Return value on exception
    message="An error occurred",  # Custom log message
)
```

## Module-Level Filtering

Enable or disable logging for specific modules (Python dotted paths). This is useful for silencing noisy third-party libraries or focusing on specific parts of your application.

```python
# Disable all logging from a noisy library
logger.disable("noisy_library")

# Re-enable it later
logger.enable("noisy_library")

# Disable your own module's debug spam in production
logger.disable("myapp.internal.cache")
```

The module name is matched as a prefix: disabling `"myapp"` also disables `"myapp.models"`, `"myapp.views"`, etc.

By default, all modules are enabled. The `"hyperdjango"` module itself is always enabled.

## configure() -- Bulk Setup

Set up the entire logging configuration in a single call. Useful at application startup.

```python
logger.configure(
    handlers=[
        {
            "sink": "logs/app.log",
            "level": "INFO",
            "rotation": "10 MB",
            "retention": "30 days",
            "compression": "gz",
        },
        {
            "sink": "logs/errors.log",
            "level": "ERROR",
            "rotation": "50 MB",
        },
        {
            "sink": "logs/app.json",
            "serialize": True,
            "level": "DEBUG",
        },
    ],
    levels=[
        {"name": "AUDIT", "no": 35, "color": "<cyan>"},
    ],
    extra={"app": "myservice", "env": "production"},
    activation=[
        ("noisy_lib", False),  # disable noisy_lib
        ("myapp.debug", False),  # disable myapp.debug
    ],
)
```

Parameters:

- `handlers` -- list of dicts, each passed as kwargs to `logger.add()`
- `levels` -- list of dicts, each passed as kwargs to `logger.level()`
- `extra` -- default extra context bound to all loggers
- `activation` -- list of `(module_name, enabled)` tuples

Calling `configure()` removes all existing sinks first, then adds the new ones.

## stats()

Inspect the current state of all registered handlers.

```python
stats = logger.stats()
for handler_id, info in stats.items():
    print(f"Handler {handler_id}: level={info['level']}, messages={info['count']}")
```

Returns a dict mapping handler IDs to dicts with:

- `level` -- minimum level for the handler
- `sink` -- string representation of the sink
- `count` -- number of messages processed
- `id` -- handler ID

## AccessLogMiddleware

Pre-built middleware for HTTP access logging. Logs every request with method, path, status code, and response time.

```python
from hyperdjango.logging import AccessLogMiddleware

# Basic usage
app.use(AccessLogMiddleware())
# Logs: 200 GET /api/users 3.2ms

# With options
app.use(
    AccessLogMiddleware(
        level="INFO",  # Log level (default: "INFO")
        include_headers=["User-Agent"],  # Include specific request headers
        exclude_paths=["/health", "/ready"],  # Skip logging for these paths
        log_query_params=False,  # Don't log query string (default: True)
        slow_request_threshold=1.0,  # Log requests > 1s at WARNING level
    )
)
```

Example output:

```
2026-03-26 14:30:00.123 | INFO     | 200 GET /api/users?page=2 3.2ms
2026-03-26 14:30:00.456 | INFO     | 201 POST /api/orders 12.7ms
2026-03-26 14:30:01.789 | WARNING  | 200 GET /api/reports/annual 1523.4ms [SLOW]
2026-03-26 14:30:02.012 | INFO     | 304 GET /static/app.js 0.4ms
```

When used with `contextualize`, the access log automatically includes bound context:

```python
# In auth middleware:
with logger.contextualize(user=request.user.username):
    response = await handler(request)
# Access log now includes user in extra fields
```

## Native Zig Timestamp Performance

HyperDjango's logging module uses native Zig for timestamp generation, file basename extraction, and module name resolution. These are hot-path operations called on every log record.

| Operation   | Python stdlib        | Zig native                   | Speedup     |
| ----------- | -------------------- | ---------------------------- | ----------- |
| Timestamp   | `datetime.now()`     | Zig `clock_gettime` + format | 8.3x faster |
| Basename    | `os.path.basename()` | Zig reverse scan             | 1.9x faster |
| Module name | `inspect` frame walk | Zig frame introspection      | 3.7x faster |

These optimizations are always active -- there is no fallback. The native extension must be built.

## Standard Library Bridge

HyperDjango's logger can intercept records from Python's standard `logging` module. This captures log messages from third-party libraries (SQLAlchemy, urllib3, etc.) and routes them through HyperDjango's sinks with consistent formatting.

```python
from hyperdjango.logging import logger

# Intercept all stdlib logging
logger.add_stdlib_bridge(level="WARNING")

# Now any library using stdlib logging gets routed to HyperDjango sinks:
import logging

std_logger = logging.getLogger("urllib3")
std_logger.warning("Connection pool full")
# → appears in HyperDjango sinks with consistent formatting
```

The bridge installs a custom `logging.Handler` on Python's root logger. Level mapping is preserved: `logging.WARNING` maps to HyperDjango's `WARNING`, etc.

## Filter Functions

Sinks can be filtered with callable or dict-based filters.

### Callable Filter

```python
def only_errors_from_db(record):
    return record["level"].no >= 40 and record["module"] == "database"


logger.add("db_errors.log", filter=only_errors_from_db)
```

### Dict Filter

A dict maps module names to minimum levels:

```python
logger.add(
    "app.log",
    filter={
        "myapp.views": "DEBUG",  # Verbose logging for views
        "myapp.models": "WARNING",  # Only warnings+ from models
        "": "INFO",  # Everything else at INFO
    },
)
```

The empty string key `""` sets the default level for unmatched modules.

## Format Strings

Customize log record formatting with Python-style format strings. Available fields:

| Field          | Description                              | Example                                |
| -------------- | ---------------------------------------- | -------------------------------------- |
| `{time}`       | Timestamp (supports strftime sub-format) | `{time:YYYY-MM-DD HH:mm:ss.SSS}`       |
| `{level}`      | Log level name                           | `INFO`                                 |
| `{level.no}`   | Numeric level                            | `20`                                   |
| `{message}`    | Formatted message                        | `User alice logged in`                 |
| `{module}`     | Python module name                       | `views`                                |
| `{function}`   | Function name                            | `login`                                |
| `{line}`       | Line number                              | `42`                                   |
| `{file}`       | Source file path                         | `views.py`                             |
| `{file.name}`  | Source file basename                     | `views.py`                             |
| `{name}`       | Logger name (module path)                | `myapp.views`                          |
| `{thread}`     | Thread name                              | `MainThread`                           |
| `{thread.id}`  | Thread ID                                | `140234567`                            |
| `{process}`    | Process name                             | `MainProcess`                          |
| `{process.id}` | Process ID (PID)                         | `12345`                                |
| `{elapsed}`    | Time since logger creation               | `0:00:12.345678`                       |
| `{extra}`      | Dict of all bound extra context          | `{"request_id": "abc"}`                |
| `{exception}`  | Exception traceback (if any)             | `Traceback (most recent call last)...` |

```python
# Compact format for development
logger.add(
    "logs/dev.log",
    format="{time:HH:mm:ss} | {level: <8} | {module}:{function}:{line} - {message}",
)

# Verbose format for production
logger.add(
    "logs/prod.log",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {process.id}:{thread.id} | {name}:{function}:{line} - {message}",
)
```

## Complete Configuration Example

A production-ready logging setup:

```python
from hyperdjango.logging import logger

# Remove default stdout handler
logger.remove()

logger.configure(
    handlers=[
        # Console: colored, human-readable, INFO+
        {
            "sink": "ext://stderr",
            "level": "INFO",
            "format": "<green>{time:HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
            "colorize": True,
        },
        # Application log: rotated daily, 30 days retention
        {
            "sink": "logs/app.log",
            "level": "DEBUG",
            "rotation": "00:00",
            "retention": "30 days",
            "compression": "gz",
            "format": "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
        },
        # Error log: separate file, rotated by size
        {
            "sink": "logs/errors.log",
            "level": "ERROR",
            "rotation": "50 MB",
            "retention": "90 days",
            "compression": "gz",
            "backtrace": True,
            "diagnose": True,
        },
        # JSON log: for log aggregation systems
        {
            "sink": "logs/structured.json",
            "serialize": True,
            "level": "INFO",
            "rotation": "100 MB",
            "retention": "7 days",
            "enqueue": True,
        },
    ],
    levels=[
        {"name": "AUDIT", "no": 35, "color": "<cyan>"},
    ],
    extra={"service": "myapp", "env": "production"},
    activation=[
        ("urllib3", False),
        ("botocore", False),
    ],
)

# Bridge stdlib logging from third-party libraries
logger.add_stdlib_bridge(level="WARNING")

# Access log middleware
from hyperdjango.logging import AccessLogMiddleware

app.use(
    AccessLogMiddleware(
        exclude_paths=["/health", "/ready"],
        slow_request_threshold=2.0,
    )
)
```

## API Reference

| Method                                                      | Description                                     |
| ----------------------------------------------------------- | ----------------------------------------------- |
| `logger.trace(msg, **kw)`                                   | Log at TRACE level (5)                          |
| `logger.debug(msg, **kw)`                                   | Log at DEBUG level (10)                         |
| `logger.info(msg, **kw)`                                    | Log at INFO level (20)                          |
| `logger.success(msg, **kw)`                                 | Log at SUCCESS level (25)                       |
| `logger.warning(msg, **kw)`                                 | Log at WARNING level (30)                       |
| `logger.error(msg, **kw)`                                   | Log at ERROR level (40)                         |
| `logger.critical(msg, **kw)`                                | Log at CRITICAL level (50)                      |
| `logger.log(level, msg, **kw)`                              | Log at a named or numeric level                 |
| `logger.add(sink, **opts)`                                  | Add an output sink, returns handler ID          |
| `logger.remove(id=None)`                                    | Remove a sink by ID, or all sinks               |
| `logger.bind(**kw)`                                         | Create child logger with bound context          |
| `logger.contextualize(**kw)`                                | Context manager for temporary context           |
| `logger.patch(fn)`                                          | Create logger with record-patching function     |
| `logger.opt(lazy, exception, depth, raw)`                   | Temporary logger with per-call options          |
| `logger.catch(exception, level, reraise, default, message)` | Decorator/context manager for exception logging |
| `logger.level(name, no, color, icon)`                       | Register or query a custom level                |
| `logger.enable(name)`                                       | Enable logging for a module prefix              |
| `logger.disable(name)`                                      | Disable logging for a module prefix             |
| `logger.configure(handlers, levels, extra, activation)`     | Bulk configuration (replaces all sinks)         |
| `logger.stats()`                                            | Handler statistics (count, level, sink info)    |
| `logger.complete()`                                         | Await flush of all async sinks                  |
| `logger.add_stdlib_bridge(level)`                           | Intercept Python stdlib logging records         |
