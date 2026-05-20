# Test Harness (`hyperdjango.testkit`)

`hyperdjango.testkit` is the shared surface for the standalone test suite — the
hundreds of `scripts/test_*.py` programs that the `hyper-test` runner executes,
one per subprocess. It is framework surface, like `django.test`, and gives every
test one import path for three things:

- an **assertion harness** (`TestRun`, `check`, `finish`, `run_main`);
- **determinism helpers** (`wait_until`, `await_until`, `tamper`);
- the **end-to-end HTTP/WebSocket primitives** (`AppRunner`, `Session`,
  `E2EResponse`, `http_get`/`http_post`/`http_put`/`http_delete`, `sse_post`,
  `build_multipart`).

For the in-process client (`TestClient`, `TestCase`, `TestWebSocket`) used by
pytest-style tests, see [Testing](testing.md); this page covers the standalone
harness and the runner contract around it.

```python
from hyperdjango.testkit import TestRun, check, finish, run_main
from hyperdjango.testkit import wait_until, await_until, tamper
from hyperdjango.testkit import AppRunner, Session, http_get, build_multipart
```

---

## Assertion harness

Each test file runs as its own subprocess, so a module-level default `TestRun`
is safe — there is no cross-test state within a process. The top-level `check`
and `finish` delegate to that default instance, so migrating a hand-rolled
`check()` is a pure import swap with no call-site changes.

```python
from hyperdjango.testkit import check, finish, run_main


def main() -> bool:
    check("addition works", 1 + 1 == 2)
    check("subtraction works", 2 - 1 == 1, detail="only shown on failure")
    return finish()


if __name__ == "__main__":
    run_main(main)
```

`check(name, cond, detail="")` prints `  PASS  <name>` or
`  FAIL  <name>  <detail>`, updates the tally, and returns the truthiness of
`cond` so a call site can branch on it. `finish()` prints exactly

```
Results: 2 passed, 0 failed
```

— the runner parses this line — and returns `True` when nothing failed.
`run_main(fn)` runs `fn` and exits `0` on truthy, `1` otherwise.

For files that need more than one independent tally, or that pass a run object
around explicitly, use `TestRun` directly:

```python
from hyperdjango.testkit import TestRun, run_main


def main() -> bool:
    run = TestRun()
    for i in range(3):
        run.check(f"iteration {i} ok", i < 3)
    return run.finish()


if __name__ == "__main__":
    run_main(main)
```

`TestRun` exposes `passed`, `failed`, and a `failures` list of the failed check
names.

---

## Determinism helpers

These replace ad-hoc timing code that caused real CI flakes: a fixed `sleep`
before asserting a converged metric (raced under CPU starvation), and an
"append `X`" token tamper that was a no-op whenever the token already ended in
`X`.

### `wait_until` / `await_until`

Poll a predicate until it is truthy instead of sleeping a fixed interval.

```python
from hyperdjango.testkit import wait_until, await_until

wait_until(lambda: counter.value == 10, timeout_s=2.0, desc="counter reaches 10")

await await_until(lambda: queue.empty(), timeout_s=2.0, desc="queue drains")
```

On expiry both raise `TimeoutError`, mentioning `desc` and the elapsed time.
`await_until` sleeps with `asyncio.sleep` and accepts a predicate that returns
either a value or an awaitable.

### `tamper`

Return a string that is **always** different from the input by cycling the last
character to its neighbour within its own alphabet class (digit, lowercase,
uppercase, or symbol). The class is preserved, so a tampered signed token stays
structurally valid (still base62) while its decoded bytes change — the exact
property a rejection test needs.

```python
from hyperdjango.testkit import tamper

bad_key = tamper(api_key)  # never equal to api_key
assert await Model.verify(bad_key) is None
```

---

## End-to-end primitives

`AppRunner` boots a real app server subprocess, waits for TCP accept and then
for HTTP readiness (`/_ready` returns 200 after all routes register), streams
its output, and tears it down (`SIGTERM`, then `SIGKILL` after 5 seconds). The
suite-local port registry `TEST_PORTS` lives in `scripts/e2e_helper.py`, which
re-exports these primitives so the existing `from e2e_helper import ...` call
sites keep working.

```python
from e2e_helper import TEST_PORTS
from hyperdjango.testkit import AppRunner, http_get

with AppRunner("services.rest_api.app:app", port=TEST_PORTS["rest_api"]) as app:
    resp = http_get(app.url("/health"))
    assert resp.status == 200
```

`Session` persists cookies across requests and adds the CSRF token as an
`X-CSRFToken` header on non-GET requests (double-submit). `build_multipart`
builds a `multipart/form-data` body (text, raw bytes, or
`(filename, content, content_type)` file fields); `sse_post` collects
`data:` lines from a Server-Sent-Events endpoint over a raw socket.

---

## Marker taxonomy

Every `scripts/test_*.py` declares exactly **one** resource kind, on its own
line:

```python
# hyper-test: <kind>
```

The kind is a **resource contract only** — what the file needs to run — not a
scheduling hint:

| Kind          | Needs                                   |
| ------------- | --------------------------------------- |
| `unit`        | No database, no native server.          |
| `db_isolated` | A private, per-run database.            |
| `db_django`   | The Django-integration database.        |
| `db_shared`   | The shared `hyperdjango_test` database. |
| `e2e`         | A live app server (`AppRunner`).        |

Scheduling and reliability concerns ride on **orthogonal markers**, one per
line, independent of the kind:

| Marker                          | Effect                                                                                                          |
| ------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `# hyper-test-timeout: <secs>`  | Per-file override of the global budget, for a genuinely heavy file.                                             |
| `# hyper-test-concurrency: low` | Schedule with reduced parallelism, for a starvation-sensitive file.                                             |
| `# hyper-test-flaky: <reason>`  | Quarantine with a mandatory reason; the file still runs, retries once, and is counted in a visible flaky tally. |

---

## Environment contract

The runner **owns** each test subprocess's environment. The ambient shell / CI
environment is **not** passed through, except a documented allowlist for
reaching Postgres: `PGHOST`, `PGUSER`, `PGPORT`, `PGPASSWORD`. Everything a test
observes about configuration is therefore what the runner set — never what the
invoking shell happened to export.

Database locators are assigned per kind, never inherited:

- **`unit` and `db_shared`** get DB locators **scrubbed** — `DATABASE_URL`,
  `HYPER_DATABASE_URL`, and `PGDATABASE` are removed — so they see the same
  clean environment as a fresh developer machine. A `unit` test that
  unexpectedly reaches the database takes the deterministic "no database
  configured" path instead of silently connecting to a foreign CI database;
  `db_shared` falls back to its hardcoded `hyperdjango_test`.
- **`db_isolated` and `e2e`** receive an **explicit per-run isolated**
  `DATABASE_URL` (with matching `HYPER_DATABASE_URL` / `PGDATABASE`), so
  concurrent files never share schema or rows.

Deployment-tuning variables (`HYPER_POOL_SIZE`, `HYPER_THREAD_POOL_SIZE`) are
**never** injected, so a test can assert the framework's built-in defaults.

This contract is mirrored in the `hyperdjango.testkit` module docstring, which
is the authoritative source.

---

## CI ladder

The native extension is built at three optimization levels, each a rung the
suite runs at, per platform:

| Rung             | Build                        | Catches                                           |
| ---------------- | ---------------------------- | ------------------------------------------------- |
| `test-safe`      | ReleaseSafe                  | Undefined behaviour and bounds violations.        |
| `test-sanitized` | ReleaseSafe + DebugAllocator | Heap misuse (use-after-free, leaks, double-free). |
| `test`           | ReleaseFast                  | Production codegen.                               |

**Reading a red run** starts by asking _which rung and which platform_ failed —
that localizes the bug class before you read a single line of the failure:

- **ReleaseSafe-only** (passes in ReleaseFast): undefined behaviour or a bounds
  violation that ReleaseSafe's checks trap and optimized codegen happens to
  paper over.
- **aarch64-ReleaseFast-only** (green on x86, green in ReleaseSafe): a codegen
  or UB bug whose symptom depends on the target's instruction selection and
  memory model.
- **single-platform timeout**: CPU starvation, not a logic bug — the file needs
  a `# hyper-test-timeout:` bump or `# hyper-test-concurrency: low`, not a code
  fix.

Consult this table first on any red run.

---

## Standards vs. bespoke

Each bespoke piece of the test infrastructure justifies itself against the
standard alternative:

| Piece                                                                       | Verdict          | Reason                                                                                                                                                                                                                                                                                                                                                                  |
| --------------------------------------------------------------------------- | ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Custom runner (subprocess-per-file, resource classes, DB/port provisioning) | **Keep bespoke** | We test a native extension under free-threading: a test can `SIGSEGV`. pytest-xdist shares long-lived workers, so one crash fails arbitrary co-scheduled tests and destroys crash attribution. Per-file processes give exact forensics, and direct `python scripts/test_X.py` runnability is how native bugs get isolated. xdist also has no resource-class scheduling. |
| pytest suites                                                               | **Keep**         | Fixtures and discovery are what Django-integration suites and the source-invariant gates (`tests/test_no_*.py`) need. Not expanded to the harness tests.                                                                                                                                                                                                                |
| Hypothesis (property/fuzz)                                                  | **Planned**      | Shrinking to minimal counterexamples, `@example` regression pinning, and a persistent example database are objectively stronger than hand-rolled random loops. It is a library, so it runs _inside_ harness files — process isolation preserved, no paradigm shift.                                                                                                     |

---

## Running individual tests

Any harness test is a plain program. Run one directly for the fastest feedback
and for isolating a native crash:

```bash
# unit
uv run python scripts/test_<name>.py

# db_isolated / db_shared / e2e — supply a database
DATABASE_URL="postgresql://$USER@localhost:5432/hyperdjango_test" \
  uv run python scripts/test_<name>.py
```

Exit `0` means pass. Do not run `hyper-test` concurrently with a direct run —
they collide on test databases and ports.
