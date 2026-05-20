"""
Background Task Queue — Premier Task System Showcase.

Demonstrates every major task queue feature end-to-end:
  - @app.task decorator for sync and async background tasks
  - .delay() for background enqueue with TaskHandle
  - Priority levels (LOW, NORMAL, HIGH, CRITICAL)
  - Retry with exponential backoff + jitter
  - Conditional retry (retry_on specific exceptions)
  - Dead letter queue for permanently failed tasks
  - Task lifecycle hooks (on_success, on_failure, on_retry)
  - TaskGroup for parallel execution + wait-for-all
  - Cron scheduling (interval and cron expressions)
  - Task cancellation
  - Queue stats and monitoring API

Run:
    uv run hyper setup --app services.task_queue.app:app --seed services.task_queue.seed:run
    uv run hyper run --app services.task_queue.app:app --port 8910

API:
    POST /api/tasks/send-email          → Enqueue email send task
    POST /api/tasks/process-data        → Enqueue data processing task (with priority)
    POST /api/tasks/fetch-url           → Enqueue URL fetch (retries on failure)
    POST /api/tasks/batch               → Enqueue parallel task group
    POST /api/tasks/fail                → Enqueue a task that always fails (DLQ demo)
    GET  /api/tasks/{task_id}           → Check task status
    GET  /api/tasks/{task_id}/result    → Get task result (blocks until done)
    POST /api/tasks/{task_id}/cancel    → Cancel a pending task
    GET  /api/queue/stats               → Queue statistics
    GET  /api/queue/dead-letters        → View dead letter queue
    POST /api/queue/dead-letters/{id}/retry → Retry a dead letter
    GET  /api/schedule                  → List scheduled tasks
    GET  /health                        → Health check

SECURITY (read before copying this into a real app):
    These endpoints are intentionally UNAUTHENTICATED so the demo runs with no
    setup — do NOT ship them that way. In production every task-submission and
    task-inspection route must be gated (SessionAuth or an API key) and the
    task status/result/cancel routes must additionally check that the task
    belongs to the caller (bind an owner id at enqueue and verify it on lookup)
    — otherwise anyone can submit work (spam/DoS) or read another user's result.
    The send_email / fetch_url tasks here are SIMULATED stubs; a real
    implementation must send through an authenticated mailer and fetch only via
    hyperdjango.net.safe_get / validate_public_url (SSRF-safe), never a raw
    client against user-supplied URLs.
"""

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path

from hyperdjango import BaseModel as ValidatedModel
from hyperdjango import HTTPException, HyperApp, Response
from hyperdjango.admin import HyperAdmin
from hyperdjango.auth.sessions import SessionAuth
from hyperdjango.conf import DEFAULTS, get_setting
from hyperdjango.database import get_db
from hyperdjango.logging import logger
from hyperdjango.mixins import TimestampMixin
from hyperdjango.models import Field, Model
from hyperdjango.openapi import mount_docs


@dataclass(slots=True)
class CircuitBreakerStatus:
    """Typed wire shape for the /api/queue/circuit-breakers endpoint.

    Replaces the previous `dict[str, object]` response body — the
    platform rule is "no `object` or `Any` in public shapes". A
    dataclass makes the contract obvious to anyone reading the
    OpenAPI spec or integrating the endpoint.
    """

    state: str
    failure_count: int
    success_count: int
    opened_at: float | None


from hyperdjango.ratelimit import RateLimitMiddleware
from hyperdjango.signing import SigningKey, TokenEngine
from hyperdjango.standalone_middleware import (
    CORSMiddleware,
    SecurityHeadersMiddleware,
    TimingMiddleware,
)
from hyperdjango.tasks import (
    TaskCircuitOpenError,
    TaskGroup,
    TaskPriority,
    TaskScheduler,
    TaskStatus,
    TaskUserLimitError,
    _task_queue,
)
from hyperdjango.telemetry import configure_from_settings
from hyperdjango.validation.core.fields import Field as VField
from hyperdjango.validation.core.validator import ValidationErrors

_APP_DIR = Path(__file__).resolve().parent

# Set per-app defaults (DEFAULTS tier — env vars still override)
DEFAULTS["DATABASE_URL"] = (
    get_setting("DATABASE_URL") or "postgres://localhost/hyperdjango_test"
)

DATABASE_URL = get_setting("DATABASE_URL")
_DEBUG = get_setting("DEBUG")

app = HyperApp(
    title="Task Queue Demo",
    database=DATABASE_URL,
    templates=str(_APP_DIR / "templates"),
    debug=_DEBUG,
)

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

# --- Native telemetry (v0.15.1) -----------------------------------------------
if _DEBUG:
    DEFAULTS["TELEMETRY_ENABLED"] = True
    DEFAULTS["TELEMETRY_SAMPLE_RATIO"] = 1.0
_telemetry = configure_from_settings(app)
if _telemetry is not None and _telemetry.prometheus_sink is not None:
    app.get("/metrics")(_telemetry.prometheus_sink.handler)

app.use(TimingMiddleware())
app.use(SecurityHeadersMiddleware(hsts=False))
app.use(
    CORSMiddleware(origins=["*"], methods=["GET", "POST"], headers=["Content-Type"])
)
app.use(RateLimitMiddleware(max_requests=120, window=60))

_session_engine = TokenEngine(
    keys=[
        SigningKey(
            secret=get_setting("SESSION_SIGNING_KEY"),
            version=1,
        ),
    ]
)
auth = SessionAuth(
    secret=get_setting("SESSION_SECRET"),
    token_engine=_session_engine,
)
app.use(auth)

mount_docs(
    app,
    title="Task Queue API",
    version="1.0.0",
    description="Background task queue with priorities, retry, scheduling, and monitoring",
)

# HyperAdmin
admin = HyperAdmin(
    app,
    prefix="/admin",
    title="Task Queue Admin",
    secret_key=get_setting("ADMIN_SECRET"),
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class User(TimestampMixin, Model):
    """App user for task queue auth."""

    class Meta:
        table = "tq_users"

    id: int = Field(primary_key=True, auto=True)
    username: str = Field(unique=True)
    password_hash: str = Field(exclude=True)


class TaskLog(TimestampMixin, Model):
    class Meta:
        table = "tq_task_log"

    id: int = Field(primary_key=True, auto=True)
    task_name: str = Field()
    task_id: str = Field()
    status: TaskStatus = Field(default=TaskStatus.PENDING)
    priority: TaskPriority = Field(default=TaskPriority.NORMAL)
    input_data: str = Field(default="")
    result_data: str = Field(default="")
    error: str = Field(default="")
    attempts: int = Field(default=0)
    started_at: str = Field(default="")
    finished_at: str = Field(default="")


# ---------------------------------------------------------------------------
# Exception handlers — consistent JSON error format
# ---------------------------------------------------------------------------


@app.exception_handler(TaskUserLimitError)
async def _handle_user_limit(request, exc):
    # Platform convention: `{"detail": "..."}` only — HTTP status on
    # the wire is canonical.
    return Response.json({"detail": str(exc)}, status=429)


@app.exception_handler(TaskCircuitOpenError)
async def _handle_circuit_open(request, exc):
    return Response.json({"detail": str(exc)}, status=503)


@app.exception_handler(Exception)
async def _handle_generic(request, exc):
    logger.exception("Unhandled error: {err}", err=str(exc))
    return Response.json({"detail": "Internal server error"}, status=500)


# ---------------------------------------------------------------------------
# Startup hook
# ---------------------------------------------------------------------------


@app.on_startup
async def _startup():
    # Start the task queue workers
    _task_queue.start()
    logger.info("Task queue started with {w} workers", w=len(_task_queue._workers))

    # Start scheduler
    _scheduler.start()
    logger.info(
        "Task scheduler started with {n} scheduled tasks", n=len(_scheduler._entries)
    )


# ---------------------------------------------------------------------------
# Task definitions — showcase all decorator options
# ---------------------------------------------------------------------------


def _on_email_success(result):
    """Lifecycle hook: called when send_email succeeds."""
    logger.info("Email task succeeded: {result}", result=result)


def _on_email_failure(exc):
    """Lifecycle hook: called when send_email permanently fails."""
    logger.error("Email task failed: {err}", err=str(exc))


@app.task(on_success=_on_email_success, on_failure=_on_email_failure)
async def send_email(to: str, subject: str, body: str):
    """Simulate sending an email (async task, normal priority).

    Demonstrates lifecycle hooks: on_success and on_failure.
    """
    await asyncio.sleep(0.1)  # Simulate I/O
    result = f"Email sent to {to}: {subject}"
    await TaskLog(
        task_name="send_email",
        task_id="",
        status=TaskStatus.SUCCESS,
        input_data=f"to={to}, subject={subject}",
        result_data=result,
    ).save()
    return result


@app.task(priority=TaskPriority.HIGH)
async def process_data(dataset: str, operation: str):
    """High-priority data processing task."""
    await asyncio.sleep(0.05)  # Simulate processing
    checksum = hashlib.md5(f"{dataset}:{operation}".encode()).hexdigest()[:8]
    result = {
        "dataset": dataset,
        "operation": operation,
        "checksum": checksum,
        "rows_processed": 1000,
    }
    await TaskLog(
        task_name="process_data",
        task_id="",
        status=TaskStatus.SUCCESS,
        priority=TaskPriority.HIGH,
        input_data=f"dataset={dataset}, op={operation}",
        result_data=str(result),
    ).save()
    return result


def _on_fetch_retry(exc, attempt):
    """Lifecycle hook: called on each fetch_url retry attempt."""
    logger.warning(
        "Fetch retry attempt {attempt}: {err}", attempt=attempt, err=str(exc)
    )


@app.task(
    max_retries=3,
    retry_delay=0.5,
    retry_backoff=2.0,
    retry_on=(ConnectionError, TimeoutError, OSError),
    on_retry=_on_fetch_retry,
)
async def fetch_url(url: str):
    """Fetch a URL with automatic retry on connection errors.

    Demonstrates retry with exponential backoff + on_retry lifecycle hook.
    On the test endpoint, simulates transient failures.
    """
    await asyncio.sleep(0.05)

    # Simulate transient failures for testing
    if url.startswith("fail://"):
        raise ConnectionError(f"Connection refused: {url}")

    # Simulate success
    result = f"Fetched {url}: 200 OK, {len(url) * 10} bytes"
    return result


@app.task(priority=TaskPriority.CRITICAL)
async def critical_alert(message: str, severity: str):
    """Critical-priority alert task (always processed first)."""
    await asyncio.sleep(0.01)
    result = f"ALERT [{severity.upper()}]: {message}"
    await TaskLog(
        task_name="critical_alert",
        task_id="",
        status=TaskStatus.SUCCESS,
        priority=TaskPriority.CRITICAL,
        input_data=f"severity={severity}",
        result_data=result,
    ).save()
    return result


@app.task(priority=TaskPriority.LOW)
async def cleanup_old_logs(days: int = 30):
    """Low-priority cleanup task."""
    await asyncio.sleep(0.05)
    db = get_db()
    # Clean up old task logs (make_interval for parameterized interval)
    count = await db.execute(
        "DELETE FROM tq_task_log WHERE created_at < NOW() - make_interval(days => $1)",
        days,
    )
    return {"deleted": count, "older_than_days": days}


@app.task
async def always_fails(message: str):
    """A task that always raises an error (for DLQ testing)."""
    raise ValueError(f"Intentional failure: {message}")


@app.task
def compute_sync(n: int):
    """Synchronous compute task (demonstrates non-async tasks)."""
    total = sum(range(n))
    return {"n": n, "sum": total}


@app.task(timeout=2)
async def slow_task(seconds: int):
    """Task with 2s timeout. Used to test timeout enforcement.

    If seconds > 2, the task will be killed by the timeout enforcer
    and end up in the dead letter queue.
    """
    await asyncio.sleep(seconds)
    return {"slept": seconds}


# ---------------------------------------------------------------------------
# Scheduler — demonstrate cron and interval scheduling
# ---------------------------------------------------------------------------

_scheduler = TaskScheduler()

# Schedule cleanup every 5 minutes (interval-based)
_scheduler.add(cleanup_old_logs, interval=300, args=(7,))

# Schedule a daily alert at midnight (cron expression: "0 0 * * *")
_scheduler.add(critical_alert, cron="0 0 * * *", args=("Daily health check", "info"))


# ---------------------------------------------------------------------------
# Input validation schemas
# ---------------------------------------------------------------------------


class ComputeInput(ValidatedModel):
    """Validated input for /api/tasks/compute."""

    n: int = VField(default=1000, ge=1, le=10_000_000)


def _require_str(value: str, name: str) -> str:
    """Validate a string input is non-empty."""
    if not value:
        raise HTTPException(400, f"{name} required")
    return value


# ---------------------------------------------------------------------------
# Root redirect
# ---------------------------------------------------------------------------


@app.get("/")
async def root(request):
    return Response.redirect("/docs/")


# ---------------------------------------------------------------------------
# API Routes — task submission
# ---------------------------------------------------------------------------


@app.post("/api/tasks/send-email")
async def api_send_email(request):
    """Submit an email send task."""
    data = await request.json()
    to = _require_str(data.get("to", ""), "to")
    subject = _require_str(data.get("subject", ""), "subject")
    body = data.get("body", "")

    handle = send_email.delay(to, subject, body)
    return Response.json(
        {
            "task_id": handle.task_id,
            "status": "pending",
            "task": "send_email",
        },
        status=202,
    )


@app.post("/api/tasks/process-data")
async def api_process_data(request):
    """Submit a data processing task (high priority)."""
    data = await request.json()
    dataset = _require_str(data.get("dataset", ""), "dataset")
    operation = data.get("operation", "transform")

    handle = process_data.delay(dataset, operation)
    return Response.json(
        {
            "task_id": handle.task_id,
            "status": "pending",
            "task": "process_data",
            "priority": "high",
        },
        status=202,
    )


@app.post("/api/tasks/fetch-url")
async def api_fetch_url(request):
    """Submit a URL fetch task (retries on connection errors)."""
    data = await request.json()
    url = _require_str(data.get("url", ""), "url")

    handle = fetch_url.delay(url)
    return Response.json(
        {
            "task_id": handle.task_id,
            "status": "pending",
            "task": "fetch_url",
            "max_retries": 3,
        },
        status=202,
    )


@app.post("/api/tasks/alert")
async def api_critical_alert(request):
    """Submit a critical alert (highest priority)."""
    data = await request.json()
    message = _require_str(data.get("message", ""), "message")
    severity = data.get("severity", "warning")

    handle = critical_alert.delay(message, severity)
    return Response.json(
        {
            "task_id": handle.task_id,
            "status": "pending",
            "task": "critical_alert",
            "priority": "critical",
        },
        status=202,
    )


@app.post("/api/tasks/batch")
async def api_batch_tasks(request):
    """Submit a batch of tasks using TaskGroup (parallel execution + wait-for-all)."""
    data = await request.json()
    items = data.get("items", [])
    if not items:
        raise HTTPException(400, "items required (list of datasets)")

    group = TaskGroup()
    for item in items[:10]:  # Cap at 10
        group.add(process_data, str(item), "batch_process")

    # Wait for all tasks to complete (demonstrates TaskGroup.run())
    loop = asyncio.get_running_loop()
    results = await loop.run_in_executor(None, group.run, 30.0)

    return Response.json(
        {
            "group_size": len(results),
            "results": [
                {
                    "task_id": r.task_id,
                    "status": r.status,
                    "attempts": r.attempts,
                }
                for r in results
            ],
        }
    )


@app.post("/api/tasks/fail")
async def api_fail_task(request):
    """Submit a task that always fails (dead letter queue demo)."""
    data = await request.json()
    message = data.get("message", "test failure")

    handle = always_fails.delay(message)
    return Response.json(
        {
            "task_id": handle.task_id,
            "status": "pending",
            "task": "always_fails",
            "note": "This task will fail and end up in the dead letter queue",
        },
        status=202,
    )


@app.post("/api/tasks/compute")
async def api_compute(request):
    """Submit a synchronous compute task."""
    data = await request.json()
    try:
        validated = ComputeInput.model_validate(data)
    except ValidationErrors as exc:
        raise HTTPException(400, str(exc))

    handle = compute_sync.delay(validated.n)
    return Response.json(
        {
            "task_id": handle.task_id,
            "status": "pending",
            "task": "compute_sync",
        },
        status=202,
    )


@app.post("/api/tasks/slow")
async def api_slow_task(request):
    """Submit a slow task with a 2s timeout.

    Send {"seconds": 1} for success, {"seconds": 5} for timeout failure.
    The task has a 2s timeout — anything over 2s is killed.
    """
    data = await request.json()
    seconds = int(data.get("seconds", 1))
    if seconds < 0:
        raise HTTPException(400, "seconds must be non-negative")
    handle = slow_task.delay(seconds)
    return Response.json(
        {
            "task_id": handle.task_id,
            "status": "pending",
            "task": "slow_task",
            "timeout": 2,
            "requested_seconds": seconds,
        },
        status=202,
    )


# ---------------------------------------------------------------------------
# API Routes — task status and results
# ---------------------------------------------------------------------------


@app.get("/api/tasks/{task_id}")
async def api_task_status(request, task_id: str):
    """Check the status of a task."""
    with _task_queue._results_lock:
        tr = _task_queue._results.get(task_id)
    if tr is None:
        return Response.json({"task_id": task_id, "status": "unknown"})

    result = {
        "task_id": task_id,
        "status": tr.status,
        "attempts": tr.attempts,
    }
    if tr.error:
        result["error"] = tr.error
    if tr.started_at:
        result["started_at"] = tr.started_at
    if tr.finished_at:
        result["finished_at"] = tr.finished_at
        if tr.started_at:
            result["duration_ms"] = round((tr.finished_at - tr.started_at) * 1000, 2)
    return Response.json(result)


@app.get("/api/tasks/{task_id}/result")
async def api_task_result(request, task_id: str):
    """Get the result of a completed task. Waits up to 10s."""
    with _task_queue._results_lock:
        handle = _task_queue._handles.get(task_id)

    if handle is None:
        return Response.json({"error": "Task not found"}, status=404)

    # Wait for completion (up to 10s) via executor to avoid blocking the server thread
    loop = asyncio.get_running_loop()
    try:
        result_val = await loop.run_in_executor(None, handle.result, 10.0)
        return Response.json(
            {
                "task_id": task_id,
                "status": "success",
                "result": result_val,
            }
        )
    except TimeoutError:
        return Response.json(
            {
                "task_id": task_id,
                "status": "timeout",
                "error": "Task did not complete within 10s",
            },
            status=408,
        )
    except RuntimeError as e:
        return Response.json(
            {
                "task_id": task_id,
                "status": "failed",
                "error": str(e),
            },
            status=500,
        )


@app.post("/api/tasks/{task_id}/cancel")
async def api_cancel_task(request, task_id: str):
    """Cancel a pending task.

    SECURITY NOTE (task_queue demo scope): this route is intentionally
    public — the task_queue service exposes the task queue
    platform internals for demonstration. In production, this route
    should either (a) be `@guard(Require.authenticated())`-protected
    AND verify the requester submitted the task (e.g., via a
    submission-time signed cancel_token), or (b) be restricted to
    the HyperAdmin surface only. Cancellation by task_id guess is
    bounded by UUID4 entropy (2^128) in practice, but any info
    disclosure of the task_id via logs/URLs enables remote cancel.
    """
    with _task_queue._results_lock:
        handle = _task_queue._handles.get(task_id)

    if handle is None:
        return Response.json({"error": "Task not found"}, status=404)

    cancelled = handle.cancel()
    return Response.json(
        {
            "task_id": task_id,
            "cancelled": cancelled,
        }
    )


# ---------------------------------------------------------------------------
# API Routes — queue monitoring
# ---------------------------------------------------------------------------


@app.get("/api/queue/stats")
async def api_queue_stats(request):
    """Get task queue statistics."""
    s = _task_queue.stats
    return Response.json(
        {
            "pending": s.pending,
            "running": s.running,
            "processed": s.processed,
            "failed": s.failed,
            "retried": s.retried,
            "workers": s.workers,
            "queue_running": s.queue_running,
            "dead_letters": s.dead_letters,
            "scheduled": len(_scheduler._entries),
            "avg_execution_time_ms": round(s.avg_execution_time_ms, 2),
            "tasks_per_second": round(s.tasks_per_second, 2),
        }
    )


@app.get("/api/queue/dead-letters")
async def api_dead_letters(request):
    """List tasks in the dead letter queue."""
    letters = _task_queue.dead_letters.peek(20)
    return Response.json(
        {
            "count": _task_queue.dead_letters.size,
            "letters": [
                {
                    "task_id": dl.task_id,
                    "func_name": dl.func_name,
                    "error": dl.error,
                    "attempts": dl.attempts,
                    "failed_at": dl.failed_at,
                }
                for dl in letters
            ],
        }
    )


@app.post("/api/queue/dead-letters/{task_id}/retry")
async def api_retry_dead_letter(request, task_id: str):
    """Retry a task from the dead letter queue."""
    handle = _task_queue.dead_letters.retry(task_id)
    if handle is None:
        return Response.json({"error": "Dead letter not found"}, status=404)

    return Response.json(
        {
            "task_id": handle.task_id,
            "status": "requeued",
            "note": "Task re-enqueued for processing",
        },
        status=202,
    )


@app.get("/api/schedule")
async def api_schedule(request):
    """List scheduled tasks."""
    entries = []
    for sid, entry in _scheduler._entries.items():
        entries.append(
            {
                "schedule_id": sid,
                "task": entry.task.__name__,
                "interval_seconds": entry.interval_seconds,
                "cron": entry.cron,
                "enabled": entry.enabled,
            }
        )
    return Response.json({"scheduled_tasks": entries})


@app.get("/api/task-log")
async def api_task_log(request):
    """View persistent task execution log from DB."""
    db = get_db()
    rows = await db.query(
        "SELECT id, task_name, status, priority, input_data, result_data, error, created_at "
        "FROM tq_task_log ORDER BY id DESC LIMIT 50"
    )
    return Response.json(
        {
            "total": len(rows),
            "logs": [{**dict(r), "created_at": str(r["created_at"])} for r in rows],
        }
    )


# ---------------------------------------------------------------------------
# Per-user limits + Circuit breakers
# ---------------------------------------------------------------------------


@app.get("/api/queue/user-limits")
async def api_user_limits(request):
    """View per-user pending task counts."""
    with _task_queue._user_pending_lock:
        user_counts = dict(_task_queue._user_pending)
    return Response.json(
        {
            "max_pending_per_user": _task_queue._max_pending_per_user,
            "users": user_counts,
        }
    )


def _circuit_breaker_to_wire(cb) -> CircuitBreakerStatus:
    """Typed adapter from the task queue's internal CircuitBreakerState
    to the public wire shape."""
    return CircuitBreakerStatus(
        state=cb.state.value,
        failure_count=cb.failure_count,
        success_count=cb.success_count,
        opened_at=cb.opened_at or None,
    )


@app.get("/api/queue/circuit-breakers")
async def api_circuit_breakers(request):
    """View circuit breaker status per task type."""
    breakers = _task_queue.get_all_circuit_breakers()
    # Typed pass-through via CircuitBreakerStatus dataclass; the wire
    # format is a plain dict built from the dataclass fields so the
    # JSON shape is stable regardless of internal task queue changes.
    wire_breakers: dict[str, dict[str, str | int | float | None]] = {}
    for func_name, cb in breakers.items():
        status = _circuit_breaker_to_wire(cb)
        wire_breakers[func_name] = {
            "state": status.state,
            "failure_count": status.failure_count,
            "success_count": status.success_count,
            "opened_at": status.opened_at,
        }
    return Response.json(
        {
            "failure_threshold": _task_queue._circuit_failure_threshold,
            "recovery_timeout_s": _task_queue._circuit_recovery_timeout,
            "window_s": _task_queue._circuit_window,
            "breakers": wire_breakers,
        }
    )


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


app.mount_health()


# ---------------------------------------------------------------------------
# HyperAdmin model registration
# ---------------------------------------------------------------------------

admin.register(
    TaskLog,
    list_display=[
        "id",
        "task_name",
        "task_id",
        "status",
        "priority",
        "attempts",
        "created_at",
    ],
    search_fields=["task_name", "task_id"],
    list_filter=["status", "priority"],
    ordering="-id",
)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8910)
