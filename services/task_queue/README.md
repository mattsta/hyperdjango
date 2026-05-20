# Task Queue

Background task system showcasing @app.task, priority levels, retry with exponential backoff, dead letter queue, TaskGroup, and cron scheduling.

## Quick Start

```bash
uv run hyper setup --app services.task_queue.app:app --seed services.task_queue.seed:run
uv run hyper run --app services.task_queue.app:app --port 8910
```

## Features

- `@app.task` decorator for async and sync background tasks
- `.delay()` for background enqueue returning a TaskHandle
- Four priority levels: LOW, NORMAL, HIGH, CRITICAL
- Retry with exponential backoff + jitter, conditional on exception type
- Dead letter queue for permanently failed tasks with retry capability
- Task lifecycle hooks: on_success, on_failure, on_retry
- TaskGroup for parallel execution with wait-for-all
- TaskScheduler with interval-based and cron expression scheduling
- Task cancellation
- Queue statistics and monitoring API
- Per-user pending task limits (configurable via `TASK_MAX_PENDING_PER_USER`)
- Circuit breaker per task type (auto-opens after configurable failure threshold)
- Persistent task execution log in database
- OpenAPI docs at `/docs`

## Platform Features Demonstrated

- **@app.task** decorator with priority, retry, and lifecycle hooks
- **.delay()** returning TaskHandle with task_id, status, result, cancel
- **TaskPriority** levels (LOW, NORMAL, HIGH, CRITICAL)
- **Retry** with max_retries, retry_delay, retry_backoff, retry_on exception types
- **Dead letter queue** with peek and retry operations
- **TaskGroup** for parallel task execution
- **TaskScheduler** with interval and cron scheduling
- **Per-user limits** via `user_id=` on `.delay()` with `TaskUserLimitError`
- **Circuit breaker** per function name: CLOSED -> OPEN -> HALF_OPEN state machine
- **mount_docs()** for OpenAPI generation

## API Endpoints

Task submission (all return 202 Accepted with task_id):

```
POST /api/tasks/send-email          Enqueue email task (lifecycle hooks demo)
POST /api/tasks/process-data        Enqueue high-priority data processing
POST /api/tasks/fetch-url           Enqueue URL fetch (retries on ConnectionError)
POST /api/tasks/alert               Enqueue critical-priority alert
POST /api/tasks/batch               Parallel TaskGroup execution
POST /api/tasks/fail                Always-failing task (DLQ demo)
POST /api/tasks/compute             Synchronous compute task
```

Task status and results:

```
GET  /api/tasks/{task_id}           Check task status and attempts
GET  /api/tasks/{task_id}/result    Get result (waits up to 10s)
POST /api/tasks/{task_id}/cancel    Cancel a pending task
```

Queue monitoring:

```
GET  /api/queue/stats                       Queue statistics (pending, running, processed, failed)
GET  /api/queue/dead-letters                View dead letter queue
POST /api/queue/dead-letters/{id}/retry     Retry a dead letter
GET  /api/queue/user-limits                 Per-user pending task counts
GET  /api/queue/circuit-breakers            Circuit breaker status per task type
GET  /api/schedule                          List scheduled tasks
GET  /api/task-log                          Persistent task execution log
```

## HyperAdmin Panel

Admin panel at `/admin/` with TaskLog model:

- Search by task_name/task_id
- Filter by status and priority
- Ordered by most recent

## Project Structure

```
task_queue/
    app.py          Task definitions, scheduler, submission and monitoring API, admin
    seed.py         Initial task log setup
    templates/      (reserved for future dashboard UI)
```
