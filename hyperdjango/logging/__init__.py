"""
HyperDjango Logging — production structured logging system.

Loguru-compatible API with non-blocking background writer, structured JSON,
color markup, file rotation/retention/compression, request context propagation,
OTel trace support, custom levels, module enable/disable, and lazy evaluation.

Usage:
    from hyperdjango.logging import logger

    # Basic logging
    logger.info("User logged in", user_id=42)
    logger.warning("Slow query", duration_ms=1500, sql="SELECT ...")

    # Structured JSON output
    logger.add(sys.stderr, serialize=True)

    # File with rotation + retention + compression
    logger.add("app.log", rotation="100 MB", retention=10, compression="gz")

    # Bind context
    log = logger.bind(request_id="abc-123")
    log.info("Processing")

    # Lazy evaluation (only compute if level passes)
    logger.opt(lazy=True).debug("Data: {x}", x=lambda: expensive())

    # Context manager
    with logger.contextualize(task_id=42):
        logger.info("In task")

    # Custom levels
    logger.level("AUDIT", no=35, color="\\033[35m", icon="📋")
    logger.log("AUDIT", "Password changed", user_id=1)

    # Module control
    logger.disable("noisy_lib")

    # Exception with traceback
    try:
        risky()
    except Exception:
        logger.exception("Operation failed")

    # Access log middleware
    from hyperdjango.logging import AccessLogMiddleware
    app.use(AccessLogMiddleware())
"""

# ruff: noqa: F401  — public API re-exports

import logging as _stdlib_logging
import os
import sys
import threading
import time
from collections.abc import Mapping

from hyperdjango.conf import get_setting
from hyperdjango.logging._colorizer import (
    colorize,
    colorize_format,
    decolorize_format,
    strip_ansi,
    strip_markup,
)
from hyperdjango.logging._core import (
    BUILTIN_LEVELS,
    RESET,
    START_TIME,
    Core,
    Level,
    get_core,
    log_context,
)
from hyperdjango.logging._handler import Handler, make_dict_filter
from hyperdjango.logging._logger import DEFAULT_FORMAT, PLAIN_FORMAT, Logger
from hyperdjango.logging._record import (
    NO_EXCEPTION,
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

# ---------------------------------------------------------------------------
# Access Log Middleware
# ---------------------------------------------------------------------------


class AccessLogMiddleware:
    """Log every HTTP request with method, path, status, and duration.

    Automatically injects request context (request_id, path, method, client_ip,
    user_id) into log_context for all log calls during the request.

    Usage:
        app.use(AccessLogMiddleware())
        app.use(AccessLogMiddleware(level="WARNING"))  # Only log slow/error
    """

    def __init__(self, logger_instance: Logger = None, level: str = "INFO"):
        self._logger = logger_instance
        self._level = level

    async def __call__(self, request, call_next):
        log = self._logger or logger

        start = time.perf_counter()

        ctx = {
            # request_id is now a declared Request field minted at the dispatch
            # boundary (falls back to id(request) if this middleware runs before
            # the id is assigned, or on a bare test Request).
            "request_id": request.request_id or id(request),
            # Strip CR/LF from the (user-controlled, percent-decoded) path so a
            # `/foo%0d%0a...` request can't forge a log line on the console/file
            # sink (the JSON sink escapes control chars itself; this makes the
            # field safe for every sink). A bare newline is the log-injection
            # vector; legitimate multi-line log messages (tracebacks) are
            # unaffected because we sanitize the FIELD, not the whole message.
            "path": request.path.replace("\r", "").replace("\n", ""),
            "method": request.method,
            "client_ip": request.client_ip,
        }

        user = request.user
        if user is not None:
            # request.user is polymorphic: a plain dict (raw SessionAuth
            # payload) or a user object (User / SessionUser / AnonymousUser —
            # all expose .id and .pk). dict keys are NOT attributes, so
            # getattr(user, "id") silently misses for the dict shape and loses
            # user correlation across all of SessionAuth. Branch on the concrete
            # shape and read each via its own accessor.
            if isinstance(user, Mapping):
                ctx["user_id"] = user.get("id", user.get("pk"))
            else:
                # id first, pk fallback (mirrors the prior intent); anonymous
                # users carry id == pk == None, so user_id stays None.
                ctx["user_id"] = user.id if user.id is not None else user.pk

        token = log_context.set(ctx)
        try:
            response = await call_next(request)
        finally:
            log_context.reset(token)

        duration_ms = (time.perf_counter() - start) * 1000
        # Imported here, not at module top: logging sits on the BOOTSTRAP
        # import spine (hyper-build, gates, test runner classification) and
        # must import without the native extension; hyperdjango.response is
        # runtime surface that hard-requires it. This middleware only runs
        # inside a serving app, where the extension is present by definition.
        from hyperdjango.response import Response

        # Response is a known dataclass whose status field is `status` (NOT
        # `status_code`), so the old getattr default fired for every request and
        # logged status 0 for 200/404/500 alike. call_next returns a Response on
        # every normal path; the only way a non-Response reaches here is a custom
        # exception handler returning one (app.handle guards the same way).
        # -1 is an explicit "unknown status" marker — never silently record
        # 0/200 for a non-Response that escaped a custom exception handler.
        status = response.status if isinstance(response, Response) else -1

        log.info(
            "{} {} {} {:.1f}ms",
            request.method,
            request.path,
            status,
            duration_ms,
            status=status,
            duration_ms=duration_ms,
        )

        return response


# ---------------------------------------------------------------------------
# Stdlib logging bridge
# ---------------------------------------------------------------------------


class InterceptHandler(_stdlib_logging.Handler):
    """Redirect Python stdlib ``logging`` records into ``hyperdjango.logging``.

    Framework internals (exceptions.py, rest.py, db, third-party libs) use the
    stdlib ``logging.getLogger(__name__)`` API. Without a bridge those records —
    including 500-error tracebacks — never reach the framework sink and are
    emitted with a different format (or via stdlib's lastResort handler to
    stderr). Installing one instance of this handler on the stdlib root logger
    funnels every stdlib record through the same pipeline/format as the native
    logger, mapping level names and ``exc_info`` faithfully.
    """

    def emit(self, record: _stdlib_logging.LogRecord) -> None:
        # Map the stdlib level to a native level. Prefer the name (INFO/WARNING/
        # …) so custom native levels line up; fall back to the numeric level for
        # non-standard values.
        lvl = logger.level(record.levelname)
        level: str | int = lvl.name if lvl is not None else record.levelno

        # Walk out of the stdlib logging machinery so the reported source frame
        # points at the real caller rather than logging internals. Mirrors the
        # canonical loguru InterceptHandler recipe. Robust: any failure just
        # falls back to depth 0.
        depth = 0
        try:
            frame = sys._getframe(0)  # this emit() frame
            while frame is not None and (
                depth == 0 or frame.f_code.co_filename == _stdlib_logging.__file__
            ):
                frame = frame.f_back
                depth += 1
        except ValueError, AttributeError:
            depth = 0

        # getMessage() applies stdlib %-args; pass the finished string with no
        # further args so native brace-formatting never re-interprets it.
        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


_intercept_lock = threading.Lock()
_intercept_installed = False


def install_stdlib_intercept(level: str | int | None = None) -> None:
    """Install the stdlib→framework logging bridge on the root logger (once).

    Idempotent and thread-safe under free-threading: the module-level lock plus
    the ``_intercept_installed`` flag guarantee exactly one handler is attached
    even if multiple threads import/init concurrently.
    """
    global _intercept_installed
    with _intercept_lock:
        if _intercept_installed:
            return
        root = _stdlib_logging.getLogger()
        if not any(isinstance(h, InterceptHandler) for h in root.handlers):
            root.addHandler(InterceptHandler())
        # Lower the root threshold so framework/library records actually reach
        # the handler (stdlib default is WARNING). Only apply a recognised
        # numeric level; unknown names (native TRACE/SUCCESS) leave it untouched.
        if level is not None:
            std_level = _stdlib_logging.getLevelName(str(level).upper())
            if isinstance(std_level, int):
                root.setLevel(std_level)
        _intercept_installed = True


# ---------------------------------------------------------------------------
# Global logger singleton (auto-configured with stderr)
# ---------------------------------------------------------------------------

logger = Logger()

# Auto-add stderr console sink (like loguru's default behavior).
# Priority: env var > conf.py setting > hardcoded default.
# Sanctioned env boundary: logging bootstraps at import time, before the settings
# system is available, so the HYPERDJANGO_LOG_* env vars are read directly here.
_auto_init = os.environ.get("HYPERDJANGO_LOG_AUTOINIT", "1") != "0"
if _auto_init:
    _default_level = os.environ.get(
        "HYPERDJANGO_LOG_LEVEL",
        get_setting("LOG_LEVEL"),
    )
    _default_format_env = os.environ.get(
        "HYPERDJANGO_LOG_FORMAT",
        get_setting("LOG_FORMAT"),
    )
    if _default_format_env == "json":
        logger.add("json", level=_default_level)
    else:
        # "text" is the setting choice meaning "use default format", not a literal template
        _fmt = (
            None if _default_format_env in (None, "", "text") else _default_format_env
        )
        logger.add(
            sys.stdout,
            level=_default_level,
            format=_fmt,
        )

    # Bridge stdlib logging into the framework pipeline so records emitted via
    # `logging.getLogger(...)` (exceptions.py, rest.py, db, third-party libs)
    # flow through the same sink/format instead of bypassing it.
    install_stdlib_intercept(_default_level)
