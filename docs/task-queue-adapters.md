# Task Queue: Adapter Architecture

HyperDjango's built-in task queue (`hyperdjango.tasks`) is an **in-process, in-memory** priority queue backed by a thread pool. It provides `@task` decorators, `.delay()` enqueue, retry with exponential backoff, dead letter queues, circuit breakers, cron scheduling, and task groups — all with zero external dependencies.

**Limitation:** Tasks are not persistent. If the process crashes, pending and running tasks are lost. For many use cases (sending emails, refreshing caches, triggering webhooks) this is acceptable — the caller retries on the next request. For use cases requiring guaranteed delivery (payment processing, audit events, workflow orchestration), a durable backend is needed.

This document describes the adapter architecture for swapping the in-memory backend with a durable external system — without changing application code.

---

## Current Interface

The `@task` decorator and `.delay()` API are the user-facing surface:

```python
from hyperdjango.tasks import task, TaskPriority


@task(
    priority=TaskPriority.HIGH,
    max_retries=3,
    retry_delay=2.0,
    retry_backoff=2.0,
    retry_on=(ConnectionError, TimeoutError),
    timeout=30,
    on_success=notify_complete,
    on_failure=alert_ops,
)
async def process_order(order_id: int, amount: float):
    await payment_api.charge(order_id, amount)


# Synchronous call (runs in current thread):
await process_order(42, 99.99)

# Asynchronous enqueue (runs in background thread):
handle = process_order.delay(42, 99.99)
handle.status()  # TaskStatus.PENDING → RUNNING → SUCCESS / FAILED
handle.result()  # blocks until complete, returns result or raises
handle.cancel()  # cancel if not yet running
```

Additional constructs:

```python
from hyperdjango.tasks import TaskGroup, TaskScheduler

# Groups: wait for multiple tasks
group = TaskGroup()
group.add(send_email.delay("user1@example.com", "Hello"))
group.add(send_email.delay("user2@example.com", "Hello"))
results = group.wait_all(timeout=30)

# Cron scheduling
scheduler = TaskScheduler()
scheduler.add(cleanup_expired.delay, cron="0 3 * * *")  # 3 AM daily
scheduler.start()
```

### TaskQueue Internal API

The `TaskQueue` class manages the thread pool and priority queue:

```python
class TaskQueue:
    def enqueue(self, message: TaskMessage) -> TaskHandle: ...
    def stats(self) -> TaskQueueStats: ...
    def start(self, workers: int = 4): ...
    def stop(self, timeout: float = 30): ...
    def drain(self): ...
```

`TaskMessage` carries the function reference, args, kwargs, priority, retry config, and lifecycle hooks.

---

## Adapter Protocol

A durable backend must implement the `TaskBackend` protocol. This is the abstraction boundary — everything above it (decorators, groups, schedulers, circuit breakers) works unchanged.

```python
from dataclasses import dataclass
from hyperdjango.tasks import TaskMessage, TaskHandle, TaskStatus, TaskQueueStats


class TaskBackend:
    """Protocol for durable task queue backends."""

    async def enqueue(self, message: TaskMessage) -> str:
        """Submit a task. Returns a task ID (unique, durable).

        The backend must persist the message so it survives process restart.
        """
        ...

    async def dequeue(self, worker_id: str) -> TaskMessage | None:
        """Fetch the next available task for processing.

        Must be atomic — the task is "claimed" by this worker and not
        visible to other workers until acked or nacked.
        """
        ...

    async def ack(self, task_id: str) -> None:
        """Mark a task as successfully completed. Removes from the queue."""
        ...

    async def nack(self, task_id: str, error: str | None = None) -> None:
        """Return a task to the queue (failed, needs retry or DLQ routing).

        The backend should respect retry_count vs max_retries from the
        TaskMessage to decide whether to re-enqueue or route to DLQ.
        """
        ...

    async def status(self, task_id: str) -> TaskStatus:
        """Query the current status of a task."""
        ...

    async def result(self, task_id: str, timeout: float = 0) -> object:
        """Get the result of a completed task. Blocks up to timeout seconds."""
        ...

    async def cancel(self, task_id: str) -> bool:
        """Cancel a pending task. Returns True if cancelled, False if already running."""
        ...

    async def stats(self) -> TaskQueueStats:
        """Return queue statistics (pending, running, failed, DLQ counts)."""
        ...

    async def register_cron(
        self, name: str, cron_expr: str, message: TaskMessage
    ) -> None:
        """Register a recurring task. Backend ensures exactly-once per schedule tick."""
        ...

    async def cleanup(self, max_age_hours: int = 24) -> int:
        """Remove completed/failed tasks older than max_age. Returns count removed."""
        ...
```

---

## PostgreSQL Backend (sketch)

A PostgreSQL-backed adapter using `SKIP LOCKED` for distributed work stealing:

```sql
-- Task table (LOGGED — tasks must survive crashes)
CREATE TABLE hyper_task_queue (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending, running, success, failed, dlq
    priority    INTEGER NOT NULL DEFAULT 0,
    queue_name  TEXT NOT NULL DEFAULT 'default',
    payload     JSONB NOT NULL,                   -- serialized TaskMessage
    result      JSONB,
    error       TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 0,
    worker_id   TEXT,
    created_at  TIMESTAMPTZ DEFAULT now(),
    started_at  TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    scheduled_at TIMESTAMPTZ DEFAULT now(),       -- for delayed/cron tasks
    timeout_at  TIMESTAMPTZ                       -- auto-nack if not acked by this time
);

-- Fast dequeue: pending tasks in priority order, skip locked rows
CREATE INDEX idx_task_dequeue ON hyper_task_queue (priority, scheduled_at)
    WHERE status = 'pending' AND scheduled_at <= now();
```

**Dequeue pattern** (distributed work stealing):

```sql
UPDATE hyper_task_queue
SET status = 'running', worker_id = $1, started_at = now(),
    timeout_at = now() + interval '30 seconds' * (timeout / 1000.0)
WHERE id = (
    SELECT id FROM hyper_task_queue
    WHERE status = 'pending' AND scheduled_at <= now()
    ORDER BY priority DESC, scheduled_at ASC
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
RETURNING *;
```

`SKIP LOCKED` ensures multiple workers never claim the same task. Each worker polls this query (or uses `LISTEN/NOTIFY` for immediate dispatch).

**Advantages:** No external infrastructure (uses existing PostgreSQL), ACID guarantees, transactional enqueue (task enqueued in same transaction as the triggering write), SQL-queryable task history.

**Limitations:** Polling-based (unless LISTEN/NOTIFY is added), throughput limited to ~10K tasks/sec per PostgreSQL instance (sufficient for most apps).

---

## External Queue Adapter (sketch)

For high-throughput or complex workflow requirements, HyperDjango's task API can delegate to an external queue system via an adapter:

```python
class ExternalQueueAdapter:
    """Adapter bridging HyperDjango @task to an external queue system.

    Maps HyperDjango's TaskMessage to the external system's message format
    and routes .delay() calls to the external enqueue API.
    """

    def __init__(self, client, queue_name: str = "default"):
        self.client = client  # External queue client
        self.queue_name = queue_name

    async def enqueue(self, message: TaskMessage) -> str:
        # Serialize HyperDjango's TaskMessage to the external format
        payload = {
            "func": f"{message.func.__module__}.{message.func.__qualname__}",
            "args": message.args,
            "kwargs": message.kwargs,
            "priority": message.priority,
            "max_retries": message.max_retries,
            "retry_delay": message.retry_delay,
            "retry_backoff": message.retry_backoff,
            "timeout": message.timeout,
        }
        return await self.client.submit(
            queue=self.queue_name,
            payload=payload,
            priority=message.priority,
        )

    async def dequeue(self, worker_id: str) -> TaskMessage | None:
        # External workers call back into HyperDjango to execute
        # This method is for HyperDjango-side workers pulling from external queue
        job = await self.client.fetch(queue=self.queue_name, worker_id=worker_id)
        if job is None:
            return None
        return self._deserialize(job)

    # ... ack, nack, status, result follow the same pattern
```

### Integration Pattern

The adapter is configured at app startup via settings:

```python
# settings or app.py
from hyperdjango.conf import DEFAULTS

# Default: in-memory (current behavior)
DEFAULTS["TASK_BACKEND"] = "memory"

# PostgreSQL durable queue
DEFAULTS["TASK_BACKEND"] = "database"

# External system
DEFAULTS["TASK_BACKEND"] = "myapp.adapters.ExternalQueueAdapter"
DEFAULTS["TASK_BACKEND_OPTIONS"] = {"client": external_client, "queue_name": "hyper"}
```

Application code remains unchanged — `@task`, `.delay()`, `TaskGroup`, and `TaskScheduler` all work identically regardless of backend.

---

## Migration Path

Switching from in-memory to durable requires zero application code changes:

1. **Add backend setting** — set `TASK_BACKEND = "database"` (or external adapter class path)
2. **Create tables** — `hyper setup` generates the task queue table from model definitions
3. **Deploy** — new processes use the durable backend; in-flight tasks in the old in-memory queue complete normally
4. **Verify** — `hyper doctor` checks backend connectivity and queue health

The `@task` decorator resolves the backend at first `.delay()` call via `get_setting("TASK_BACKEND")`. The in-memory `TaskQueue` becomes one backend implementation alongside PostgreSQL and external adapters.

---

## Design Decisions

**Why not include a durable backend by default?**

The in-memory queue handles 75K+ enqueue/sec with zero external dependencies. For apps that need guaranteed delivery, the adapter protocol lets you plug in PostgreSQL (simple, zero-infrastructure) or a purpose-built queue system (high throughput, complex workflows). Including a full durable implementation in the framework would add complexity that most users don't need — the adapter architecture lets you choose the right tool for your scale.

**Why `SKIP LOCKED` for PostgreSQL?**

`SKIP LOCKED` (PostgreSQL 9.5+) provides distributed work stealing without advisory locks or external coordination. Multiple workers can poll the same table concurrently — each gets a different task, atomically. Combined with `LISTEN/NOTIFY` for immediate dispatch, this gives sub-second task latency with zero external infrastructure.

**Why no separate broker by default?**

Same principle as the cache system: PostgreSQL UNLOGGED/LOGGED tables handle the common case without additional infrastructure. The adapter protocol is open, so a custom backend can be plugged in when profiling shows PostgreSQL is the bottleneck — which for task queues typically means > 10K tasks/sec sustained throughput.
