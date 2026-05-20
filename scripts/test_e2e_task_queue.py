"""
End-to-end tests for Task Queue service.

# hyper-test: e2e

Tests the full background task system:
- Task submission via API (.delay() behind the scenes)
- Task status polling
- Task result retrieval (blocking wait)
- Priority ordering (CRITICAL > HIGH > NORMAL > LOW)
- Retry with exponential backoff
- Dead letter queue for permanently failed tasks
- Task cancellation
- TaskGroup batch execution
- Synchronous compute tasks
- Queue stats monitoring
- Scheduled tasks listing
- Task execution log
"""

import subprocess
import sys
import time

from e2e_helper import (
    TEST_PORTS,
    AppRunner,
    http_get,
    http_post,
)

PASS = 0
FAIL = 0
ERRORS: list[str] = []


def check(name, response, expected_status=200):
    global PASS, FAIL
    if response.status == expected_status:
        PASS += 1
        print(f"  PASS  {name} ({response.status})")
        return True
    FAIL += 1
    msg = f"  FAIL  {name}: expected {expected_status}, got {response.status}"
    print(msg)
    ERRORS.append(msg)
    if response.body:
        print(f"        body: {response.body[:300]}")
    return False


def check_true(name, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
        return True
    FAIL += 1
    msg = f"  FAIL  {name}: condition was False"
    print(msg)
    ERRORS.append(msg)
    return False


def check_val(name, actual, expected):
    global PASS, FAIL
    if actual == expected:
        PASS += 1
        print(f"  PASS  {name}")
        return True
    FAIL += 1
    msg = f"  FAIL  {name}: expected {expected!r}, got {actual!r}"
    print(msg)
    ERRORS.append(msg)
    return False


def wait_for_task(base, task_id, timeout=10.0):
    """Poll task status until done or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = http_get(f"{base}/api/tasks/{task_id}")
        if r.status == 200:
            data = r.json
            status = data.get("status", "")
            if status in ("success", "failed", "cancelled"):
                return data
        time.sleep(0.1)
    return None


def wait_for(pred, timeout: float = 10.0, interval: float = 0.05) -> bool:
    """Poll ``pred`` until true or the deadline — a condition wait, not a sleep.

    Asynchronous consequences (a task reaching a terminal state, a dead-letter
    row landing) must be WAITED for, never slept past: a fixed sleep is right
    only on an unloaded machine and turns into an intermittent failure on a busy
    one. The bound is a generous ceiling; the poll returns the moment the
    condition holds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


def main() -> None:
    global PASS, FAIL

    print("=" * 60)
    print("Task Queue E2E Tests")
    print("=" * 60)

    port = TEST_PORTS["task_queue"]

    # Setup tables + seed
    subprocess.run(
        [
            "uv",
            "run",
            "hyper",
            "setup",
            "--app",
            "services.task_queue.app:app",
            "--drop",
            "--seed",
            "services.task_queue.seed:run",
        ],
        capture_output=True,
        timeout=60,
    )

    with AppRunner(
        "services.task_queue.app:app",
        host="127.0.0.1",
        port=port,
        readiness_path="/health",
    ) as runner:
        base = runner.url()
        print(f"\nServer running at {base}\n")

        # ── Health ──────────────────────────────────────────────
        print("--- Health ---")
        r = http_get(f"{base}/health")
        check("health endpoint", r, 200)
        if r.status == 200:
            data = r.json
            check_true("health status ok", data.get("status") == "ok")

        # ── OpenAPI ─────────────────────────────────────────────
        print("\n--- OpenAPI ---")
        r = http_get(f"{base}/docs")
        check("swagger UI", r, 200)
        r = http_get(f"{base}/openapi.json")
        check("openapi spec", r, 200)

        # ── Task: Send Email ────────────────────────────────────
        print("\n--- Task: Send Email ---")
        r = http_post(
            f"{base}/api/tasks/send-email",
            body={
                "to": "user@example.com",
                "subject": "Test Email",
                "body": "Hello from task queue!",
            },
        )
        check("submit email task", r, 202)
        email_task_id = None
        if r.status == 202:
            data = r.json
            email_task_id = data.get("task_id")
            check_true("has task_id", email_task_id is not None)
            check_val("task type", data.get("task"), "send_email")

        # Wait for completion
        if email_task_id:
            result = wait_for_task(base, email_task_id)
            check_true("email task completed", result is not None)
            if result:
                check_val("email task status", result.get("status"), "success")
                check_true("has duration_ms", "duration_ms" in result)

        # Get result via blocking endpoint
        if email_task_id:
            r = http_get(f"{base}/api/tasks/{email_task_id}/result")
            check("get email result", r, 200)
            if r.status == 200:
                data = r.json
                check_true(
                    "result contains 'Email sent'",
                    "Email sent" in str(data.get("result", "")),
                )

        # ── Task: Process Data (High Priority) ──────────────────
        print("\n--- Task: Process Data (High Priority) ---")
        r = http_post(
            f"{base}/api/tasks/process-data",
            body={
                "dataset": "users_2026",
                "operation": "aggregate",
            },
        )
        check("submit data task", r, 202)
        data_task_id = None
        if r.status == 202:
            data = r.json
            data_task_id = data.get("task_id")
            check_val("priority", data.get("priority"), "high")

        if data_task_id:
            result = wait_for_task(base, data_task_id)
            check_true("data task completed", result is not None)
            if result:
                check_val("data task status", result.get("status"), "success")

            r = http_get(f"{base}/api/tasks/{data_task_id}/result")
            check("get data result", r, 200)
            if r.status == 200:
                res = r.json.get("result", {})
                check_val("result dataset", res.get("dataset"), "users_2026")
                check_true("result has checksum", "checksum" in res)
                check_true(
                    "result has rows_processed", res.get("rows_processed", 0) > 0
                )

        # ── Task: Fetch URL (Success) ───────────────────────────
        print("\n--- Task: Fetch URL ---")
        r = http_post(
            f"{base}/api/tasks/fetch-url",
            body={
                "url": "https://example.com/data",
            },
        )
        check("submit fetch task", r, 202)
        fetch_task_id = None
        if r.status == 202:
            fetch_task_id = r.json.get("task_id")

        if fetch_task_id:
            result = wait_for_task(base, fetch_task_id)
            check_true("fetch task completed", result is not None)
            if result:
                check_val("fetch task status", result.get("status"), "success")

        # ── Task: Critical Alert ────────────────────────────────
        print("\n--- Task: Critical Alert ---")
        r = http_post(
            f"{base}/api/tasks/alert",
            body={
                "message": "Server overload detected",
                "severity": "critical",
            },
        )
        check("submit alert task", r, 202)
        alert_task_id = None
        if r.status == 202:
            data = r.json
            alert_task_id = data.get("task_id")
            check_val("alert priority", data.get("priority"), "critical")

        if alert_task_id:
            result = wait_for_task(base, alert_task_id)
            check_true("alert task completed", result is not None)
            if result:
                check_val("alert status", result.get("status"), "success")

        # ── Task: Sync Compute ──────────────────────────────────
        print("\n--- Task: Sync Compute ---")
        r = http_post(f"{base}/api/tasks/compute", body={"n": 100})
        check("submit compute task", r, 202)
        compute_id = None
        if r.status == 202:
            compute_id = r.json.get("task_id")

        if compute_id:
            result = wait_for_task(base, compute_id)
            check_true("compute task completed", result is not None)
            if result:
                check_val("compute status", result.get("status"), "success")

            r = http_get(f"{base}/api/tasks/{compute_id}/result")
            check("get compute result", r, 200)
            if r.status == 200:
                res = r.json.get("result", {})
                check_val("sum(0..100)", res.get("sum"), 4950)

        # ── Task: Batch (TaskGroup) ─────────────────────────────
        print("\n--- Task: Batch (TaskGroup) ---")
        r = http_post(
            f"{base}/api/tasks/batch",
            body={
                "items": ["dataset_a", "dataset_b", "dataset_c"],
            },
        )
        check("submit batch (group.run waits for all)", r, 200)
        if r.status == 200:
            data = r.json
            check_val("group size", data.get("group_size"), 3)
            results = data.get("results", [])
            check_true("has results", len(results) == 3)
            check_true(
                "all batch succeeded",
                all(r.get("status") == "success" for r in results),
            )

        # ── Task: Fail → Dead Letter Queue ──────────────────────
        print("\n--- Task: Fail → Dead Letter Queue ---")
        r = http_post(f"{base}/api/tasks/fail", body={"message": "test DLQ"})
        check("submit failing task", r, 202)
        fail_task_id = None
        if r.status == 202:
            fail_task_id = r.json.get("task_id")

        # Wait for it to fail
        if fail_task_id:
            result = wait_for_task(base, fail_task_id, timeout=5.0)
            check_true("failing task finished", result is not None)
            if result:
                check_val("fail status", result.get("status"), "failed")

        # Check dead letter queue. The DLQ append happens after the task's
        # terminal transition, so wait for the endpoint that reports it instead
        # of sleeping a guess at how long that takes.
        wait_for(
            lambda: http_get(f"{base}/api/queue/dead-letters").json.get("count", 0) > 0
        )
        r = http_get(f"{base}/api/queue/dead-letters")
        check("dead letters endpoint", r, 200)
        if r.status == 200:
            data = r.json
            check_true("has dead letters", data.get("count", 0) > 0)
            letters = data.get("letters", [])
            if letters:
                check_true("dead letter has error", "error" in letters[0])
                check_true("dead letter has func_name", "func_name" in letters[0])

        # ── Task: Cancellation ──────────────────────────────────
        print("\n--- Task: Cancellation ---")
        # Submit multiple tasks quickly and try to cancel one
        r = http_post(
            f"{base}/api/tasks/send-email",
            body={
                "to": "cancel@test.com",
                "subject": "Will be cancelled",
                "body": "This task may be cancelled",
            },
        )
        cancel_task_id = None
        if r.status == 202:
            cancel_task_id = r.json.get("task_id")

        if cancel_task_id:
            r = http_post(f"{base}/api/tasks/{cancel_task_id}/cancel")
            check("cancel task endpoint", r, 200)
            if r.status == 200:
                data = r.json
                # May or may not be cancelled (might have already started)
                check_true("cancel response has cancelled field", "cancelled" in data)

        # ── Queue Stats ─────────────────────────────────────────
        print("\n--- Queue Stats ---")
        r = http_get(f"{base}/api/queue/stats")
        check("queue stats", r, 200)
        if r.status == 200:
            data = r.json
            check_true("stats has processed", "processed" in data)
            check_true("stats has workers", "workers" in data)
            check_true("stats has failed", "failed" in data)
            check_true("stats has pending", "pending" in data)
            check_true("stats has running", "running" in data)
            check_true("stats has dead_letters", "dead_letters" in data)
            check_true("stats has scheduled", "scheduled" in data)
            check_true(
                "stats has avg_execution_time_ms", "avg_execution_time_ms" in data
            )
            check_true("stats has tasks_per_second", "tasks_per_second" in data)
            check_true("processed > 0", data.get("processed", 0) > 0)
            check_true("queue is running", data.get("queue_running") is True)

        # ── Scheduled Tasks ─────────────────────────────────────
        print("\n--- Scheduled Tasks ---")
        r = http_get(f"{base}/api/schedule")
        check("schedule endpoint", r, 200)
        if r.status == 200:
            data = r.json
            tasks = data.get("scheduled_tasks", [])
            check_true("has scheduled tasks", len(tasks) > 0)
            if tasks:
                check_true("scheduled task has name", "task" in tasks[0])
                check_true(
                    "scheduled task has interval", "interval_seconds" in tasks[0]
                )

        # ── Task Log (DB persistence) ───────────────────────────
        print("\n--- Task Log ---")
        r = http_get(f"{base}/api/task-log")
        check("task log endpoint", r, 200)
        if r.status == 200:
            data = r.json
            check_true("has log entries", data.get("total", 0) > 0)
            logs = data.get("logs", [])
            if logs:
                check_true("log has task_name", "task_name" in logs[0])
                check_true("log has status", "status" in logs[0])

        # ── Validation ──────────────────────────────────────────
        print("\n--- Validation ---")
        r = http_post(f"{base}/api/tasks/send-email", body={})
        check("email without required fields → 400", r, 400)

        r = http_post(f"{base}/api/tasks/process-data", body={})
        check("process-data without dataset → 400", r, 400)

        r = http_post(f"{base}/api/tasks/fetch-url", body={})
        check("fetch-url without url → 400", r, 400)

        r = http_post(f"{base}/api/tasks/alert", body={})
        check("alert without message → 400", r, 400)

        r = http_post(f"{base}/api/tasks/batch", body={})
        check("batch without items → 400", r, 400)

        r = http_post(f"{base}/api/tasks/compute", body={"n": 100_000_000})
        check("compute with n too large → 400", r, 400)

        # ── Task Timeout ───────────────────────────────────────
        print("\n--- Task Timeout ---")
        # Fast task (1s sleep, 2s timeout) should succeed
        r = http_post(f"{base}/api/tasks/slow", body={"seconds": 0})
        check("submit fast slow_task", r, 202)
        if r.status == 202:
            tid = r.json["task_id"]
            # Wait for the terminal transition itself, not a guess at its cost.
            wait_for_task(base, tid, timeout=15.0)
            r = http_get(f"{base}/api/tasks/{tid}")
            check("fast task completed", r, 200)
            if r.status == 200:
                check_val("fast task success", r.json.get("status"), "success")

        # Slow task (5s sleep, 2s timeout) should fail with timeout
        r = http_post(f"{base}/api/tasks/slow", body={"seconds": 5})
        check("submit slow slow_task", r, 202)
        if r.status == 202:
            tid = r.json["task_id"]
            # The task's own 2s timeout drives it to `failed`; wait for that
            # terminal state rather than sleeping past the deadline and hoping.
            wait_for_task(base, tid, timeout=20.0)
            r = http_get(f"{base}/api/tasks/{tid}")
            check("slow task timed out", r, 200)
            if r.status == 200:
                check_val("slow task failed", r.json.get("status"), "failed")
                err = r.json.get("error", "")
                check_true("timeout in error", "timed out" in err.lower())

        # ── Unknown Task ────────────────────────────────────────
        print("\n--- Edge Cases ---")
        r = http_get(f"{base}/api/tasks/nonexistent-id-12345")
        check("unknown task status", r, 200)
        if r.status == 200:
            check_val("unknown status", r.json.get("status"), "unknown")

        r = http_get(f"{base}/api/tasks/nonexistent-id-12345/result")
        check("unknown task result → 404", r, 404)

        r = http_post(f"{base}/api/tasks/nonexistent-id-12345/cancel")
        check("cancel unknown task → 404", r, 404)

        # ── User Limits Endpoint ─────────────────────────────────
        print("\n--- User Limits + Circuit Breakers ---")
        r = http_get(f"{base}/api/queue/user-limits")
        check("user-limits endpoint", r, 200)
        if r.status == 200:
            check_true("has max_pending", "max_pending_per_user" in r.json)
            check_true("has users dict", "users" in r.json)

        r = http_get(f"{base}/api/queue/circuit-breakers")
        check("circuit-breakers endpoint", r, 200)
        if r.status == 200:
            check_true("has failure_threshold", "failure_threshold" in r.json)
            check_true("has recovery_timeout", "recovery_timeout_s" in r.json)
            check_true("has breakers dict", "breakers" in r.json)

        # ── OpenAPI ─────────────────────────────────────────────
        print("\n--- OpenAPI ---")
        r = http_get(f"{base}/openapi.json")
        check("openapi.json loads", r, 200)
        if r.status == 200:
            spec = r.json
            check_true("Has openapi version", "openapi" in spec)
            check_true("Has paths", len(spec.get("paths", {})) > 0)

        r = http_get(f"{base}/docs")
        check("Swagger UI loads", r, 200)

        # ── HyperAdmin ─────────────────────────────────────────
        print("\n--- HyperAdmin ---")
        r = http_get(f"{base}/admin/login/")
        check("admin login page", r, 200)
        check_true("admin login has form", "username" in r.body)

        r = http_get(f"{base}/admin/")
        check_true(
            "admin requires auth",
            r.status in (302, 303) or "login" in r.body.lower(),
        )

    # ── Summary ─────────────────────────────────────────────
    print("\n" + "=" * 60)
    print(f"Results: {PASS} passed, {FAIL} failed")
    if ERRORS:
        print("\nFailures:")
        for err in ERRORS:
            print(f"  {err}")
    print("=" * 60)

    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
