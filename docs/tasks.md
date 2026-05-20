# Tasks -- In-Process Background Task Queue

In-process background task queue using Python's free-threading (3.14t). No external dependencies. Priority queue, result tracking, retry with exponential backoff, timer-based scheduling (via the `scheduler` library), dead letter queue, task groups, lifecycle hooks, per-user pending limits, and circuit breakers.

**WARNING: This task system is NOT persistent.** All pending tasks, results, schedules, and dead letters exist only in memory. If the process restarts, crashes, or is killed -- everything is lost. Do NOT use this for tasks that must reliably execute (billing, critical notifications, scheduled reports).

**Use this for:** Fire-and-forget side effects (send email after signup, resize image, push webhook), short-lived retryable operations (API calls with retry), and periodic housekeeping within a running process (clear expired sessions, flush metrics).

**Do NOT use this for:** Nightly reports, billing cycles, scheduled jobs that must survive deploys/restarts, distributed task processing across multiple workers, or anything where a missed execution has business consequences.

**Future:** For persistent, crash-recoverable, distributed recurring task workflows, HyperDjango will integrate [`pyjobby`](https://github.com/mattsta/pyjobby) -- a PostgreSQL-backed persistent job queue with durable scheduling, crash recovery, and multi-worker support.

```
Request --> Handler --> task.delay() --> Response (immediate)
                            |
                            +--> Worker Thread --> Execute Task
                                     |
                                     +--> on_success / on_failure / on_retry hooks
                                     +--> Dead Letter Queue (on permanent failure)
```

```python
from hyperdjango import HyperApp

app = HyperApp()


@app.task
async def send_email(to, subject, body):
    await email_service.send(to, subject, body)


# Call directly (synchronous):
send_email("user@example.com", "Hello", "Body")

# Enqueue for background execution:
handle = send_email.delay("user@example.com", "Hello", "Body")
handle.status()  # TaskStatus.PENDING / RUNNING / SUCCESS / FAILED
handle.result()  # blocks until done, returns result
```

---

## @task Decorator

The `@task` decorator wraps a function so it can be called normally (synchronous) or enqueued for background execution via `.delay()`. Both async and sync functions are supported.

### Basic Usage

```python
from hyperdjango.tasks import task


@task
async def process_order(order_id):
    order = await db.query_one("SELECT * FROM orders WHERE id = $1", order_id)
    await charge_payment(order)
    await send_confirmation(order)
    return order_id


# Synchronous call (blocks until done):
result = process_order(42)

# Background call (returns immediately):
handle = process_order.delay(42)
```

Sync functions work the same way:

```python
@task
def generate_report(report_id):
    data = compute_report(report_id)
    save_report(report_id, data)
    return report_id
```

### With Options

```python
from hyperdjango.tasks import task, TaskPriority


@task(
    priority=TaskPriority.HIGH,
    max_retries=3,
    retry_delay=1.0,
    retry_backoff=2.0,
    retry_on=(ConnectionError, TimeoutError),
    on_success=lambda result: print(f"Done: {result}"),
    on_failure=lambda exc: alert_team(str(exc)),
    on_retry=lambda exc, attempt: print(f"Retry {attempt}: {exc}"),
)
async def fetch_external_data(url):
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=30)
        resp.raise_for_status()
        return resp.json()
```

### Custom Task Queue

```python
from hyperdjango.tasks import task, TaskQueue

my_queue = TaskQueue(workers=8, max_queue_size=50000)


@task(queue=my_queue)
async def heavy_task(data): ...
```

---

## Task Priority Levels

Tasks are processed in priority order. Higher priority tasks are dequeued first.

| Priority                | Value | Use Case                              |
| ----------------------- | ----- | ------------------------------------- |
| `TaskPriority.LOW`      | 0     | Batch jobs, cleanup, reports          |
| `TaskPriority.NORMAL`   | 10    | Default -- most tasks                 |
| `TaskPriority.HIGH`     | 20    | User-triggered actions, notifications |
| `TaskPriority.CRITICAL` | 30    | Payment processing, security alerts   |

```python
from hyperdjango.tasks import task, TaskPriority


@task(priority=TaskPriority.CRITICAL)
async def process_payment(payment_id): ...


@task(priority=TaskPriority.LOW)
async def generate_daily_report(): ...
```

---

## TaskHandle -- Tracking Results

`.delay()` returns a `TaskHandle` for monitoring the task.

### `handle.status()`

Returns the current `TaskStatus`:

| Status      | Description                                |
| ----------- | ------------------------------------------ |
| `PENDING`   | Queued, not yet started                    |
| `RUNNING`   | Currently executing                        |
| `SUCCESS`   | Completed successfully                     |
| `FAILED`    | Permanently failed (all retries exhausted) |
| `RETRYING`  | Failed but will retry                      |
| `CANCELLED` | Cancelled before execution                 |

```python
handle = send_email.delay("user@example.com", "Hello", "Body")
print(handle.status())  # TaskStatus.PENDING
```

### `handle.result(timeout=None)`

Block until the task completes and return its result. Raises `RuntimeError` if the task failed or was cancelled. Raises `TimeoutError` if the timeout expires.

```python
handle = process_order.delay(42)
result = handle.result(timeout=30.0)  # blocks up to 30 seconds
print(result)  # 42 (the return value)
```

### `handle.cancel()`

Cancel the task if it is still pending. Returns `True` if successfully cancelled.

```python
handle = slow_task.delay()
cancelled = handle.cancel()  # True if it hadn't started yet
```

### `handle.is_done()`

Non-blocking check whether the task has finished (success, failed, or cancelled).

```python
if handle.is_done():
    print("Task completed")
```

---

## Retry with Exponential Backoff

Configure automatic retries with exponential backoff and jitter.

```python
@task(
    max_retries=5,  # retry up to 5 times
    retry_delay=1.0,  # initial delay: 1 second
    retry_backoff=2.0,  # multiply delay by 2 each attempt
    retry_on=(ConnectionError, TimeoutError),  # only retry these exceptions
)
async def call_external_api(endpoint): ...
```

Retry timing with the above settings:

| Attempt | Delay (base) | With Jitter (approx) |
| ------- | ------------ | -------------------- |
| 1       | 1.0s         | 1.0 - 1.25s          |
| 2       | 2.0s         | 2.0 - 2.50s          |
| 3       | 4.0s         | 4.0 - 5.00s          |
| 4       | 8.0s         | 8.0 - 10.0s          |
| 5       | 16.0s        | 16.0 - 20.0s         |

Jitter is random between 0 and 25% of the base delay, preventing thundering herd on retries.

If `retry_on` is `None` (default), all exceptions trigger a retry. Set it to a tuple of specific exception types to only retry those.

After all retries are exhausted, the task is sent to the dead letter queue.

---

## Task Scheduling

Schedule tasks using `TaskScheduler`, powered by the [`scheduler`](https://pypi.org/project/scheduler/) library's timer-based engine. No poll loops — the library calculates exact next-execution times with proper timezone support.

```python
class TaskScheduler:
    def __init__(self, task_queue: TaskQueue | None = None)
```

If `task_queue` is `None`, the global default queue is used.

### Timing Methods

The `add()` method accepts exactly one timing parameter:

| Parameter  | Type            | Description                                              |
| ---------- | --------------- | -------------------------------------------------------- |
| `interval` | `float`         | Seconds between executions (cyclic)                      |
| `daily`    | `datetime.time` | Run at this time every day                               |
| `hourly`   | `datetime.time` | Run at this minute:second every hour                     |
| `weekly`   | `Weekday(time)` | Run on a specific weekday (e.g., `Monday(time(hour=9))`) |
| `minutely` | `datetime.time` | Run at this second every minute                          |
| `cron`     | `str`           | 5-field cron expression (translated to scheduler timing) |

### Interval Scheduling

```python
from hyperdjango.tasks import TaskScheduler, task

scheduler = TaskScheduler()


@task
async def cleanup_expired_sessions():
    await db.execute("DELETE FROM sessions WHERE expires_at < NOW()")


# Run every 5 minutes (300 seconds)
schedule_id = scheduler.add(cleanup_expired_sessions, interval=300.0)

scheduler.start()  # starts the scheduler background thread
```

### Daily / Hourly / Weekly Scheduling

```python
from datetime import time
from hyperdjango.tasks import TaskScheduler, task, Monday, Friday

scheduler = TaskScheduler()


@task
async def send_daily_digest():
    users = await db.query("SELECT * FROM users WHERE digest_enabled = true")
    for user in users:
        await send_digest_email(user)


# Every day at 8:00 AM
scheduler.add(send_daily_digest, daily=time(hour=8))

# Every hour at :30
scheduler.add(check_health, hourly=time(minute=30))

# Every Monday at 9:30 AM
scheduler.add(generate_weekly_report, weekly=Monday(time(hour=9, minute=30)))

# Every Friday at 6 PM
scheduler.add(send_weekly_summary, weekly=Friday(time(hour=18)))
```

Weekday triggers available: `Monday`, `Tuesday`, `Wednesday`, `Thursday`, `Friday`, `Saturday`, `Sunday` — all re-exported from `hyperdjango.tasks`.

### Cron Scheduling

Standard 5-field cron expressions are still supported. They are automatically translated to the scheduler library's native timing:

```python
# Every 15 minutes → cyclic(timedelta(minutes=15))
scheduler.add(check_health, cron="*/15 * * * *")

# Daily at 8 AM → daily(time(hour=8))
scheduler.add(send_daily_digest, cron="0 8 * * *")

# Every Monday midnight → weekly(Monday(time(0,0)))
scheduler.add(weekly_report, cron="0 0 * * 1")
```

Cron field syntax supports: `*` (any), `*/N` (every N), `N` (exact), `N-M` (range), `N,M` (list). Day of week: 0 = Sunday.

### Managing Schedules

```python
# Add with arguments
schedule_id = scheduler.add(
    process_queue,
    interval=60.0,
    args=("high-priority",),
    kwargs={"batch_size": 100},
)

# Remove a schedule
scheduler.remove(schedule_id)  # returns True if found

# Check count
print(scheduler.count)  # number of scheduled entries

# Stop the scheduler
scheduler.stop()
```

---

## Dead Letter Queue

Tasks that permanently fail (all retries exhausted) are sent to the dead letter queue for inspection and manual retry.

```python
from hyperdjango.tasks import TaskQueue

queue = TaskQueue()
queue.start()

# Inspect dead letters
letters = queue.dead_letters.peek(10)  # last 10 dead letters
for letter in letters:
    print(f"{letter.func_name}: {letter.error}")
    print(f"  Args: {letter.args}")
    print(f"  Attempts: {letter.attempts}")
    print(f"  Traceback: {letter.traceback}")

# Retry a dead letter
handle = queue.dead_letters.retry(letter.task_id)
if handle is not None:
    result = handle.result(timeout=30)

# Pop the oldest dead letter
letter = queue.dead_letters.pop()

# Clear all dead letters
queue.dead_letters.clear()

# Check size
print(queue.dead_letters.size)
```

### DeadLetter Fields

| Field       | Type    | Description                    |
| ----------- | ------- | ------------------------------ |
| `task_id`   | `str`   | Original task ID               |
| `func_name` | `str`   | Function name                  |
| `args`      | `tuple` | Positional arguments           |
| `kwargs`    | `dict`  | Keyword arguments              |
| `error`     | `str`   | Error message                  |
| `traceback` | `str`   | Full traceback                 |
| `failed_at` | `float` | Monotonic timestamp of failure |
| `attempts`  | `int`   | Total attempts made            |

The DLQ has a configurable max size (default 10,000). When full, the oldest entries are evicted. Configure via the `TASK_DLQ_MAX_SIZE` setting.

---

## Per-User Task Limits

Prevent any single user from flooding the queue by setting a per-user pending task limit:

```python
from hyperdjango.tasks import TaskUserLimitError, _task_queue

# Set limit (0 = unlimited, which is the default)
_task_queue._max_pending_per_user = 10

# Pass user_id when enqueuing
handle = send_email.delay("user@example.com", "Hello", "Body", user_id="user_42")

# If user_42 already has 10 pending tasks:
try:
    handle = send_email.delay("user@example.com", "Again", "Body", user_id="user_42")
except TaskUserLimitError:
    # User has too many pending tasks — return 429 or similar
    pass

# Check current pending count
count = _task_queue.get_user_pending("user_42")
```

The pending count is decremented automatically when a task completes (success, failure, or cancellation). Configure the limit via the `TASK_MAX_PENDING_PER_USER` setting.

---

## Circuit Breaker

Automatically stop submitting tasks to a function that keeps failing. The circuit breaker tracks failures per function name over a rolling window:

- **CLOSED** (normal): Tasks execute normally. Failures are counted.
- **OPEN** (tripped): New submissions for this function are rejected with `TaskCircuitOpenError`. Opens after `failure_threshold` failures within the rolling window.
- **HALF_OPEN** (probing): After the recovery timeout, one task is allowed through as a probe. If it succeeds, the circuit closes. If it fails, the circuit re-opens.

```python
from hyperdjango.tasks import TaskCircuitOpenError, _task_queue

# Configure (defaults shown)
_task_queue._circuit_failure_threshold = 5  # failures before opening
_task_queue._circuit_recovery_timeout = 30.0  # seconds before half-open probe
_task_queue._circuit_window = 300.0  # rolling window in seconds

# Check circuit breaker state
cb = _task_queue.get_circuit_breaker("fetch_url")
if cb is not None:
    print(f"State: {cb.state}")  # closed / open / half_open
    print(f"Failures: {cb.failure_count}")

# Handle rejection
try:
    handle = fetch_url.delay("https://flaky-api.example.com")
except TaskCircuitOpenError:
    # Circuit is open — the target is known to be failing
    pass

# View all circuit breakers
all_breakers = _task_queue.get_all_circuit_breakers()
```

Configure thresholds via settings: `TASK_CIRCUIT_FAILURE_THRESHOLD`, `TASK_CIRCUIT_RECOVERY_TIMEOUT`, `TASK_CIRCUIT_WINDOW`.

---

## Task Groups

Run multiple tasks in parallel and wait for all to complete.

```python
from hyperdjango.tasks import TaskGroup, TaskStatus

group = TaskGroup()

# Add tasks to the group
group.add(fetch_user_data, user_id=1)
group.add(fetch_user_data, user_id=2)
group.add(fetch_user_data, user_id=3)
group.add(generate_thumbnail, image_id=42)

# Wait for all tasks (returns list of TaskResult)
results = group.run(timeout=30.0)

for result in results:
    print(f"Task {result.task_id}: {result.status}")
    if result.status == TaskStatus.SUCCESS:
        print(f"  Result: {result.result}")
    elif result.status == TaskStatus.FAILED:
        print(f"  Error: {result.error}")
```

Each `group.add()` call enqueues the task immediately and returns a `TaskHandle`. `group.run()` blocks until all tasks complete or the timeout expires (raises `TimeoutError`).

### TaskResult Fields

| Field         | Type             | Description                                 |
| ------------- | ---------------- | ------------------------------------------- |
| `task_id`     | `str`            | Task identifier                             |
| `status`      | `TaskStatus`     | Final status                                |
| `result`      | `object \| None` | Return value (if SUCCESS)                   |
| `error`       | `str \| None`    | Error message (if FAILED)                   |
| `started_at`  | `float \| None`  | Monotonic timestamp when execution started  |
| `finished_at` | `float \| None`  | Monotonic timestamp when execution finished |
| `attempts`    | `int`            | Total execution attempts                    |

---

## Lifecycle Hooks

Attach callbacks to task success, failure, and retry events.

```python
from hyperdjango.tasks import task


def on_payment_success(result):
    send_receipt(result["order_id"])


def on_payment_failure(exc):
    alert_ops_team(f"Payment failed: {exc}")


def on_payment_retry(exc, attempt):
    logger.warning(f"Payment retry {attempt}: {exc}")


@task(
    max_retries=3,
    retry_delay=5.0,
    on_success=on_payment_success,
    on_failure=on_payment_failure,
    on_retry=on_payment_retry,
)
async def process_payment(order_id, amount):
    result = await payment_gateway.charge(order_id, amount)
    return {"order_id": order_id, "charge_id": result.id}
```

| Hook         | Signature                       | When Called                                     |
| ------------ | ------------------------------- | ----------------------------------------------- |
| `on_success` | `fn(result)`                    | After successful execution                      |
| `on_failure` | `fn(exception)`                 | After all retries exhausted (permanent failure) |
| `on_retry`   | `fn(exception, attempt_number)` | Before each retry                               |

Exceptions in hooks are caught and logged -- they do not affect the task result.

---

## TaskQueueStats -- Monitoring

Get comprehensive queue statistics for dashboards and health checks.

```python
from hyperdjango.tasks import TaskQueue

queue = TaskQueue(workers=8)
queue.start()

stats = queue.stats
print(f"Pending: {stats.pending}")
print(f"Running: {stats.running}")
print(f"Processed: {stats.processed}")
print(f"Failed: {stats.failed}")
print(f"Retried: {stats.retried}")
print(f"Workers: {stats.workers}")
print(f"Queue running: {stats.queue_running}")
print(f"Dead letters: {stats.dead_letters}")
print(f"Scheduled: {stats.scheduled}")
print(f"Avg execution time: {stats.avg_execution_time_ms:.1f}ms")
print(f"Throughput: {stats.tasks_per_second:.1f} tasks/sec")
```

### TaskQueueStats Fields

| Field                   | Type    | Description                        |
| ----------------------- | ------- | ---------------------------------- |
| `pending`               | `int`   | Tasks waiting in the queue         |
| `running`               | `int`   | Tasks currently executing          |
| `processed`             | `int`   | Total successfully completed tasks |
| `failed`                | `int`   | Total permanently failed tasks     |
| `retried`               | `int`   | Total retry attempts               |
| `workers`               | `int`   | Number of worker threads           |
| `queue_running`         | `bool`  | Whether the queue is active        |
| `dead_letters`          | `int`   | Number of dead letters             |
| `scheduled`             | `int`   | Number of scheduled entries        |
| `avg_execution_time_ms` | `float` | Average task execution time        |
| `tasks_per_second`      | `float` | Task throughput                    |

---

## TaskQueue Configuration

```python
from hyperdjango.tasks import TaskQueue

queue = TaskQueue(
    workers=8,  # number of worker threads
    max_queue_size=50000,  # max pending tasks
)

queue.start()
# ... use the queue ...
queue.stop()  # graceful shutdown, waits up to 5s per worker
```

The queue starts automatically on the first `.delay()` call. Worker threads are daemon threads.

### Settings

These settings are defined in `hyperdjango.conf` and can be configured via `HYPERDJANGO_*` in Django settings, `HYPER_*` environment variables, or a `.env` file. See [settings.md](settings.md) for full details.

| Setting               | Default | Range        | Description                           |
| --------------------- | ------- | ------------ | ------------------------------------- |
| `TASK_WORKERS`        | `4`     | 1--64        | Number of worker threads              |
| `TASK_MAX_QUEUE_SIZE` | `10000` | 1--1,000,000 | Maximum pending tasks before dropping |
| `TASK_DLQ_MAX_SIZE`   | `10000` | 1--1,000,000 | Maximum dead letters retained         |

### Queue Full Behavior

When the queue reaches `max_queue_size`, new tasks are dropped with a warning log and the task handle status is set to `FAILED` with error `"Queue full"`.

---

## Worker Thread Architecture

Each worker thread creates its own `asyncio` event loop:

- Async tasks run in their own event loop, isolated from the main server loop
- Each worker processes one task at a time
- Workers pick tasks from a shared `PriorityQueue` (thread-safe)
- Workers use a sentinel message pattern for graceful shutdown
- Free-threaded Python 3.14t allows true parallel execution across workers

---

## Common Patterns

### Email Sending

```python
@task
async def send_welcome_email(user_id):
    user = await db.query_one("SELECT * FROM users WHERE id = $1", user_id)
    await send_mail(subject="Welcome!", to=[user.email], message=f"Hi {user.name}!")


@app.post("/register")
async def register(request):
    data = await request.json()
    user_id = await db.execute("INSERT INTO users ...")
    send_welcome_email.delay(user_id)
    return Response.json({"id": user_id}, status=201)
```

### Report Generation

```python
@task
async def generate_report(report_id):
    report = await db.query_one("SELECT * FROM reports WHERE id = $1", report_id)
    data = await compute_report_data(report)
    pdf = await render_pdf(data)
    await storage.save(f"reports/{report_id}.pdf", pdf)
    await db.execute("UPDATE reports SET status='complete' WHERE id = $1", report_id)


@app.post("/reports")
async def create_report(request):
    report_id = await db.execute(
        "INSERT INTO reports (status) VALUES ('pending') RETURNING id"
    )
    generate_report.delay(report_id)
    return Response.json({"id": report_id, "status": "pending"}, status=202)
```

### Webhook Delivery with Retry

```python
@task(
    max_retries=5,
    retry_delay=2.0,
    retry_backoff=3.0,
    retry_on=(ConnectionError, TimeoutError),
)
async def deliver_webhook(webhook_url, payload):
    async with httpx.AsyncClient() as client:
        resp = await client.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
    return resp.status_code
```

### Parallel Fan-Out

```python
from hyperdjango.tasks import TaskGroup


@app.post("/process-batch")
async def process_batch(request):
    data = await request.json()
    items = data["items"]

    group = TaskGroup()
    for item in items:
        group.add(process_item, item_id=item["id"])

    results = group.run(timeout=60.0)
    succeeded = sum(1 for r in results if r.status == TaskStatus.SUCCESS)
    return {"total": len(items), "succeeded": succeeded}
```

---

## Full Example

```python
from hyperdjango import HyperApp
from hyperdjango.tasks import task, TaskPriority, TaskGroup, TaskScheduler

app = HyperApp(title="Order System", database="postgres://localhost/orders")

# --- Task definitions ---


@app.task(
    max_retries=3, retry_delay=2.0, retry_backoff=2.0, retry_on=(ConnectionError,)
)
async def charge_payment(order_id, amount):
    result = await payment_api.charge(order_id, amount)
    return result.charge_id


@app.task(priority=TaskPriority.HIGH)
async def send_order_confirmation(order_id, email):
    order = await app.db.query_one("SELECT * FROM orders WHERE id = $1", order_id)
    await send_email(
        email, "Order Confirmed", render_template("order_email.html", order)
    )


@app.task(priority=TaskPriority.LOW)
async def update_analytics(event_type, data):
    await app.db.execute(
        "INSERT INTO analytics (event, data, created_at) VALUES ($1, $2, NOW())",
        event_type,
        data,
    )


# --- API endpoint ---


@app.post("/orders")
async def create_order(request):
    data = await request.json()
    order_id = await app.db.execute(
        "INSERT INTO orders (user_id, total) VALUES ($1, $2) RETURNING id",
        data["user_id"],
        data["total"],
    )
    charge_payment.delay(order_id, data["total"])
    send_order_confirmation.delay(order_id, data["email"])
    update_analytics.delay("order_created", {"order_id": order_id})
    return {"order_id": order_id, "status": "processing"}


# --- Scheduled tasks ---

scheduler = TaskScheduler()


@app.task
async def cleanup_expired_carts():
    await app.db.execute(
        "DELETE FROM carts WHERE updated_at < NOW() - INTERVAL '7 days'"
    )


@app.task
async def send_daily_summary():
    count = await app.db.query_one(
        "SELECT COUNT(*) FROM orders WHERE created_at > NOW() - INTERVAL '1 day'"
    )
    await send_email("ops@company.com", "Daily Summary", f"{count} orders today")


scheduler.add(cleanup_expired_carts, cron="0 3 * * *")  # 3 AM daily
scheduler.add(send_daily_summary, cron="0 18 * * 1-5")  # 6 PM weekdays

# --- Health endpoint ---


@app.get("/health/tasks")
async def task_health(request):
    stats = app.task_queue.stats
    return {
        "pending": stats.pending,
        "running": stats.running,
        "processed": stats.processed,
        "failed": stats.failed,
        "throughput": f"{stats.tasks_per_second:.1f}/s",
        "avg_time_ms": f"{stats.avg_execution_time_ms:.1f}",
        "dead_letters": stats.dead_letters,
    }


scheduler.start()
app.run(port=8000)
```

---

## Thread Safety

`TaskQueue` is fully thread-safe for Python 3.14t free-threading:

- The internal `PriorityQueue` is inherently thread-safe (Python `queue.PriorityQueue` uses a lock).
- Task result storage uses explicit `threading.Lock` for all reads and writes.
- Dead letter queue operations are protected by the same lock.
- Worker threads each create their own isolated `asyncio` event loop -- no event loop sharing.
- Stats computation acquires the lock atomically to produce consistent snapshots.
- No deadlock risk: all locks are acquired in a consistent order (single lock per operation).

---

## See Also

- [settings.md](settings.md) -- `TASK_WORKERS`, `TASK_MAX_QUEUE_SIZE`, `TASK_DLQ_MAX_SIZE` settings
- [realtime.md](realtime.md) -- Real-time WebSocket patterns (tasks can trigger real-time notifications)
- [formats.md](formats.md) -- Locale-aware formatting (for formatting dates/numbers in task outputs)
