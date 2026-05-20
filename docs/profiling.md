# Profiling

Nanosecond-precision profiler built into the native Zig extension. Zero overhead when disabled, sub-microsecond timing resolution when active.

## Overview

HyperDjango's profiling system operates at three levels:

1. **Handler profiling** -- `@profile_handler` decorator for individual route timing
2. **Performance middleware** -- `PerformanceMiddleware` for per-request query tracking, N+1 detection, and slow query logging
3. **Manual profiling** -- `Profiler` and `ProfileStore` for custom instrumentation and flame graph generation

All timing uses the native Zig `std.time.nanoTimestamp()` function, which reads the hardware timestamp counter directly. This provides ~45 ns resolution with zero system call overhead, compared to Python's `time.monotonic_ns()` which goes through the kernel.

## Nanosecond Timer

The profiler uses two native functions exposed from the Zig extension:

- `_profiler_nanos()` -- returns the current monotonic nanosecond timestamp
- `_profiler_diff_nanos(start)` -- returns elapsed nanoseconds since `start`

These are wrapped in Python helpers:

```python
from hyperdjango.profiling import nanos, elapsed_nanos

start = nanos()
# ... do work ...
duration = elapsed_nanos(start)
print(f"Took {duration} ns")
```

The timer resolution is approximately 45 ns on modern hardware. This means you can meaningfully profile operations down to the microsecond level -- useful for measuring individual SQL queries, template renders, and serialization passes.

## @profile_handler

Profile individual route handlers by adding the `@profile_handler` decorator:

```python
from hyperdjango.profiling import profile_handler


@app.get("/api/users")
@profile_handler
async def list_users(request):
    users = await User.objects.all()
    return {"users": [u.to_dict() for u in users]}
```

When a profiled handler executes, the decorator:

1. Creates a `RequestProfile` with the request method and path
2. Records the handler start time in nanoseconds
3. Executes the handler
4. Records handler duration
5. Attaches the profile as an `X-Profile` header on the response

The `X-Profile` header contains a human-readable timing breakdown:

```
X-Profile: total=1.2ms handler=0.8ms sql=0.3ms(2q) middleware=0.1ms
```

The timing categories are:

| Category     | What it measures                             |
| ------------ | -------------------------------------------- |
| `total`      | End-to-end request time                      |
| `handler`    | Time spent in the route handler function     |
| `sql`        | Total database query time (with query count) |
| `middleware` | Time spent in middleware processing          |
| `routing`    | Time spent resolving the route               |

## X-Profile Header

The `X-Profile` header is added automatically by `@profile_handler`. You can also enable it per-request by passing `?profile=1` as a query parameter:

```
GET /api/users?profile=1 HTTP/1.1

HTTP/1.1 200 OK
X-Profile: total=1.2ms handler=0.8ms sql=0.3ms(2q) middleware=0.1ms routing=45ns
Content-Type: application/json
```

To get the full profile as JSON instead of the header summary, use `?profile=json`:

```python
profile = get_current_profile()
if profile:
    data = profile.to_dict()
    # Returns:
    # {
    #     "method": "GET",
    #     "path": "/api/users",
    #     "total_ns": 1200000,
    #     "total": "1.2ms",
    #     "middleware_ns": 100000,
    #     "routing_ns": 45,
    #     "handler_ns": 800000,
    #     "sql_total_ns": 300000,
    #     "sql_count": 2,
    #     "queries": [
    #         {"sql": "SELECT * FROM users...", "duration_ns": 150000, "duration": "150.0us"},
    #         {"sql": "SELECT * FROM profiles...", "duration_ns": 150000, "duration": "150.0us"},
    #     ]
    # }
```

## RequestProfile

Every profiled request produces a `RequestProfile` dataclass:

```python
from hyperdjango.profiling import RequestProfile, SQLQuery


@dataclass(slots=True)
class RequestProfile:
    method: str = ""
    path: str = ""
    start_ns: int = 0
    total_ns: int = 0
    middleware_ns: int = 0
    routing_ns: int = 0
    handler_ns: int = 0
    sql_total_ns: int = 0
    sql_queries: list[SQLQuery] = field(default_factory=list)
```

Each SQL query is recorded as an `SQLQuery`:

```python
@dataclass(slots=True)
class SQLQuery:
    sql: str
    duration_ns: int
    params: tuple | None = None
```

Key methods on `RequestProfile`:

| Method                 | Returns | Description                             |
| ---------------------- | ------- | --------------------------------------- |
| `to_header()`          | `str`   | Format as X-Profile header value        |
| `to_dict()`            | `dict`  | Full profile as JSON-serializable dict  |
| `to_collapsed_stack()` | `str`   | Collapsed stack format for flame graphs |
| `sql_count`            | `int`   | Number of SQL queries (property)        |

## Query Tracking

SQL queries are recorded into the current profile automatically when the database layer is instrumented. You can also record queries manually:

```python
from hyperdjango.profiling import record_sql, nanos, elapsed_nanos

start = nanos()
result = await db.query("SELECT * FROM users WHERE active = $1", [True])
duration = elapsed_nanos(start)

record_sql("SELECT * FROM users WHERE active = $1", duration, params=(True,))
```

Per-request query statistics are available via the `RequestProfile`:

```python
profile = end_profile()
print(f"Queries: {profile.sql_count}")
print(f"Total SQL time: {profile.sql_total_ns / 1_000_000:.1f}ms")
for q in profile.sql_queries:
    print(f"  {q.duration_ns / 1000:.0f}us  {q.sql[:80]}")
```

## Performance Middleware

The `PerformanceMiddleware` provides automatic per-request monitoring without decorating individual handlers:

```python
from hyperdjango.performance import PerformanceMiddleware

perf = PerformanceMiddleware(
    slow_query_threshold_ms=100.0,  # Log queries slower than 100ms
    n_plus_one_threshold=5,  # Flag SQL patterns repeated 5+ times
    max_history=1000,  # Keep last 1000 request stats
    dashboard_path="/debug/performance",  # Dashboard URL
    enabled=True,
)
app.use(perf)
```

### Configuration Options

| Parameter                 | Default                | Description                                                 |
| ------------------------- | ---------------------- | ----------------------------------------------------------- |
| `slow_query_threshold_ms` | `100.0`                | Queries slower than this are flagged as slow                |
| `n_plus_one_threshold`    | `5`                    | A SQL pattern repeated this many times triggers N+1 warning |
| `max_history`             | `1000`                 | Ring buffer size for recent request stats                   |
| `dashboard_path`          | `"/debug/performance"` | URL path for the HTML dashboard                             |
| `enabled`                 | `True`                 | Toggle middleware on/off                                    |

### Response Headers

The middleware adds headers to every response:

```
X-Query-Count: 7
X-Query-Time: 12.3ms
X-N-Plus-One: 1        # Only present when N+1 detected
```

## N+1 Detection

N+1 query detection works by normalizing SQL queries (replacing literals with `?` placeholders) and counting how many times each normalized pattern appears in a single request. If any pattern exceeds the `n_plus_one_threshold`, it is flagged.

For example, if a request produces these queries:

```sql
SELECT * FROM articles
SELECT * FROM authors WHERE id = 1
SELECT * FROM authors WHERE id = 2
SELECT * FROM authors WHERE id = 3
SELECT * FROM authors WHERE id = 4
SELECT * FROM authors WHERE id = 5
```

The middleware normalizes them:

```sql
SELECT * FROM articles
SELECT * FROM authors WHERE id = ?   (x5)
```

The second pattern appears 5 times, triggering the N+1 alert. The normalized SQL pattern is included in the `X-N-Plus-One` header and recorded in the request stats.

To fix N+1 queries, use `select_related()` or `prefetch_related()`:

```python
# N+1: one query per author
articles = await Article.objects.all()
for article in articles:
    print(article.author.name)  # Each access triggers a query

# Fixed: single JOIN query
articles = await Article.objects.select_related("author").all()
for article in articles:
    print(article.author.name)  # No additional queries
```

## Performance Dashboard

The middleware serves an HTML dashboard at the configured `dashboard_path` (default: `/debug/performance`). The dashboard displays:

- **Total Requests** -- number of requests since server start
- **Total Queries** -- total SQL queries executed
- **Avg Queries/Req** -- average queries per request
- **Slow Queries** -- count of queries exceeding the threshold
- **N+1 Patterns** -- count of detected N+1 query patterns
- **Recent Slow Queries** -- table of recent slow queries with SQL and duration
- **N+1 Query Patterns** -- table of detected N+1 SQL patterns

A JSON API is also available at `/debug/performance/json` for programmatic access:

```python
# GET /debug/performance/json
{
    "total_requests": 1542,
    "total_queries": 8734,
    "avg_queries_per_request": 5.7,
    "slow_query_count": 12,
    "n_plus_one_count": 3,
    "recent_slow_queries": [
        {"sql": "SELECT * FROM ...", "duration_ms": 245.3},
    ],
    "recent_n_plus_one": ["SELECT * FROM authors WHERE id = ?"],
}
```

## SlowQueryLog Integration

For persistent slow query tracking that survives server restarts, use `SlowQueryLog` from the pool module:

```python
from hyperdjango.pool import SlowQueryLog, QueryTimer

# Create persistent slow query log (PostgreSQL UNLOGGED table)
slow_log = SlowQueryLog(db, threshold_ms=100)
await slow_log.ensure_table()

# Auto-instrument all database queries
timer = QueryTimer(db, slow_log=slow_log, threshold_ms=100)
timer.install()
```

The `SlowQueryLog` writes to an UNLOGGED PostgreSQL table (`hyper_slow_queries`) with columns:

| Column           | Type          | Description                    |
| ---------------- | ------------- | ------------------------------ |
| `id`             | `SERIAL`      | Primary key                    |
| `sql_text`       | `TEXT`        | The slow query SQL             |
| `duration_ms`    | `REAL`        | Query duration in milliseconds |
| `params_summary` | `TEXT`        | Summary of query parameters    |
| `timestamp`      | `TIMESTAMPTZ` | When the query was recorded    |

Indexes on `timestamp DESC` and `duration_ms DESC` enable fast lookups of recent and slowest queries.

## Manual Profiling with QueryTimer

The `QueryTimer` wraps database methods with automatic timing:

```python
from hyperdjango.pool import QueryTimer

timer = QueryTimer(db, threshold_ms=50)
timer.install()  # Patches db.query and db.execute

# All subsequent queries are automatically timed
result = await db.query("SELECT * FROM large_table")
# If > 50ms, logged to SlowQueryLog
```

For manual timing of specific operations:

```python
from hyperdjango.profiling import nanos, elapsed_nanos, start_profile, end_profile

# Profile a specific code block
profile = start_profile(method="TASK", path="generate_report")

start = nanos()
data = await compute_report(report_id)
profile.handler_ns = elapsed_nanos(start)

completed = end_profile()
print(completed.to_dict())
```

## ProfileStore and Flame Graphs

The global `ProfileStore` accumulates profiles for analysis:

```python
from hyperdjango.profiling import get_store

store = get_store()

# Get the 10 slowest requests
slowest = store.get_slowest(n=10)
for p in slowest:
    print(
        f"{p.method} {p.path}: {p.total_ns / 1_000_000:.1f}ms ({p.sql_count} queries)"
    )

# Generate collapsed stack format for flame graphs
collapsed = store.get_flame_graph()
```

The collapsed stack format is compatible with speedscope and Brendan Gregg's FlameGraph tools:

```
request;middleware 100000
request;routing 45000
request;handler;python 500000
request;handler;sql;SELECT * FROM users WHERE id = ? 150000
request;handler;sql;SELECT * FROM profiles WHERE user_id = ? 150000
```

To generate an SVG flame graph:

```python
from hyperdjango.profiling import Profiler

profiler = Profiler()
with profiler.trace("my_operation"):
    result = await expensive_function()

profiler.dump_flamegraph("profile.svg")
```

The `ProfileStore` is a thread-safe ring buffer (default capacity: 1000 profiles) that can be cleared with `store.clear()`.

## Production vs Development Profiling

### Development

In development, enable full profiling with handler decorators and the performance dashboard:

```python
from hyperdjango.profiling import profile_handler
from hyperdjango.performance import PerformanceMiddleware


# Profile all routes of interest
@app.get("/api/users")
@profile_handler
async def list_users(request): ...


# Enable query tracking and N+1 detection
app.use(
    PerformanceMiddleware(
        slow_query_threshold_ms=50,
        n_plus_one_threshold=3,
        dashboard_path="/debug/performance",
    )
)
```

### Production

In production, use the persistent `SlowQueryLog` and disable the dashboard:

```python
from hyperdjango.pool import SlowQueryLog, QueryTimer
from hyperdjango.performance import PerformanceMiddleware

# Persistent slow query log
slow_log = SlowQueryLog(db, threshold_ms=200)
await slow_log.ensure_table()
timer = QueryTimer(db, slow_log=slow_log, threshold_ms=200)
timer.install()

# Lightweight request stats (no dashboard)
app.use(
    PerformanceMiddleware(
        slow_query_threshold_ms=200,
        n_plus_one_threshold=10,
        dashboard_path=None,  # Disable dashboard in prod
        enabled=True,
    )
)
```

Key differences:

| Setting                   | Development | Production                |
| ------------------------- | ----------- | ------------------------- |
| `slow_query_threshold_ms` | 50 ms       | 200 ms                    |
| `n_plus_one_threshold`    | 3           | 10                        |
| Dashboard                 | Enabled     | Disabled                  |
| `@profile_handler`        | Per-route   | Disabled (zero overhead)  |
| `SlowQueryLog`            | Optional    | Enabled                   |
| `X-Profile` header        | Shown       | Stripped by reverse proxy |

## Benchmarking Patterns

### Timing a Database Query

```python
from hyperdjango.profiling import nanos, elapsed_nanos

iterations = 1000
start = nanos()
for _ in range(iterations):
    await db.query("SELECT 1")
total = elapsed_nanos(start)
print(f"Avg: {total / iterations / 1000:.1f} us/query")
```

### Comparing ORM vs Raw SQL

```python
# ORM path
start = nanos()
users = await User.objects.filter(active=True).all()
orm_time = elapsed_nanos(start)

# Raw SQL path
start = nanos()
rows = await db.query("SELECT * FROM users WHERE active = $1", [True])
raw_time = elapsed_nanos(start)

print(f"ORM: {orm_time / 1_000_000:.1f}ms, Raw: {raw_time / 1_000_000:.1f}ms")
print(f"ORM overhead: {orm_time / raw_time:.1f}x")
```

### Profiling Template Rendering

```python
from hyperdjango.profiling import nanos, elapsed_nanos

start = nanos()
html = engine.render("complex_template.html", context)
render_time = elapsed_nanos(start)
print(f"Template render: {render_time / 1000:.0f} us")
```

## Precision

- Timing resolution: ~45 ns (hardware timestamp counter via Zig `std.time.nanoTimestamp`)
- Zero overhead when profiling is disabled (no function wrapping, no thread-local access)
- Thread-safe timing via per-thread counters (`threading.local`)

---

## Profile-Driven Optimization Workflow (THE PROCESS)

**Do not optimize what you think is slow. Measure it first.** This section
codifies the methodology for finding real throughput improvements that are
hiding behind wrong assumptions.

### The Rule

> Every performance change must be preceded by a profile that identifies the
> hotspot, and followed by a re-profile that proves the fix worked. No
> "I think this will be faster" commits.

### The Stability Rule

> **Any performance claim requires a multi-run median measurement with
> jitter under 5%. Single-run micro-benchmarks are inadmissible evidence.**

Short-run measurements (100K-300K iterations at sub-microsecond per-op costs)
have **±30% run-to-run variance** on modern CPUs due to:

- CPU frequency scaling (P-cores ramping up/down)
- GC collection pauses
- Scheduler noise from other processes
- Inline cache + branch predictor warmup

Concrete example: `scripts/bench_where_compile.py` at 100K × 1 run reported
the Zig `_where_compile` speedup as "1.26-1.28x" in three successive runs.
At 500K × 5 runs (median), the **actual** speedup is **2.64-3.99x** depending
on scenario — a **3x underestimate** from bad measurement methodology.

#### Required benchmark structure

Every micro-benchmark script must:

1. **Total operations ≥ 1 million** (or the measurement runs ≥ 1 second total)
2. **Warmup ≥ 10× the per-op call-site interpreter caches** (500-10,000 ops typical)
3. **Multi-run median** (N=3-5) with **per-run values printed** so jitter is visible
4. **Jitter percentage computed and reported** — `(max - min) / median / 2 * 100`
5. **Refuse to report a "win" if jitter > 5%**

Reference implementations:

**cProfile (in-process, structural identification)**:

- `scripts/profile_hypernews_cprofile.py` — 5 hypernews endpoints, per-endpoint
  iteration counts so each run hits ≥5s at the endpoint's expected rps,
  ⚠ SHORT RUN warning if any run drops below
- `scripts/profile_list_cprofile.py` — bookstore List endpoint, 10K iters × 3
  runs since the endpoint is faster than hypernews
- `scripts/profile_admin_cprofile.py` — HyperAdmin dashboard / changelist /
  add form / login page; logs in via `/admin/login/` then profiles each

**wrk (wire-speed verification)**:

- `scripts/bench_hypernews_wrk.py` — 5 hypernews endpoints, 4t/20c/8s × 3
  runs, multi-run median, per-run jitter, drop+seed integration,
  `HYPER_POOL_SIZE` env override forwarded to AppRunner
- `scripts/bench_bookstore_wrk.py` — 6 bookstore REST endpoints, same
  structure as hypernews wrk

**Microbench (single-function isolation)**:

- `scripts/bench_where_compile.py` — 500K × 5 runs, jitter per scenario
- `scripts/bench_hmac_paths.py` — 1M × 5 runs, per-run ns shown
- `scripts/bench_from_record_bulk.py` — 4-variant Model.from_record dispatch
- `scripts/bench_time_ago.py` — time_bucket_cached vs uncached vs lru_cache

#### Required endpoint profile structure

For in-process TestClient + cProfile profiling:

1. **≥ 5,000 iterations per run** (or total cProfile time ≥ 5 seconds per endpoint)
2. **Warmup ≥ 500** requests to prime: LRU cache, prepared statement cache,
   connection pool, template bytecode cache, OS page cache
3. **3 runs minimum** with `per_run_rps: [a, b, c]` in the report
4. **Jitter ≤ 5%** required to quote the number; if higher, either increase
   iterations or investigate what's adding noise (CPU throttling, concurrent
   work, memory pressure)

#### What to write in a performance report entry

WRONG:

> "/login endpoint improved from 3,132 to 5,746 rps (+83.5%)"

This is a single-run peak compared to a single-run baseline. The next run of
the same code with no changes might produce 4,091 rps. The "+83.5%" is
wishful thinking.

RIGHT:

> "/login endpoint improved from **3,132** to **4,863 ± 1.6% rps (median of
> 3 runs × 5,000 iters)** — +55.3%"

The jitter bound and per-run count make the number auditable. Future readers
can tell whether a subsequent regression is real or within noise.

### Why this rule exists

Intuition about bottlenecks is wrong most of the time. A function can look
hot in a micro-benchmark and contribute nothing to production throughput
because it's never in the top 30 by self-time. The only way to know is to
profile the actual endpoint first — `cProfile` will surface the real hotspot,
which often turns out to be a small Python method (MRO walks, attribute
lookups, repeated dict construction) fixable in a metaclass cache with no
native code.

Profiling is cheap. Always run it first.

### The Six-Phase Workflow

**Phase 0: Preconditions**

- Release-mode Zig build active: `uv run hyper-build --release` — profiling a
  Debug build wastes time; tracing overhead dominates the real signal
- A stable reproducible workload (wrk hitting a real endpoint, or a scripted
  TestClient loop)
- The baseline numbers already recorded in a JSON file you can diff against later

**Phase 1: Baseline throughput**

Run the full production benchmark and save the JSON baseline:

```bash
uv run python scripts/bench_load_orm.py
cp logs/load_orm_baseline.json logs/profile_baseline_before.json
```

Write down the rps and avg_ms for every endpoint. This is your "before" state.

**Phase 2: In-process cProfile of the slowest endpoint**

Use `scripts/profile_list_cprofile.py` as a template (or create a sibling for
a different endpoint). The script does:

1. `hyper setup --drop --seed` to prepare a clean DB
2. Import the app and create an in-process `TestClient`
3. Warmup (50 requests) so JIT caches, prepared statement cache, query cache
   are all populated
4. `cProfile.Profile()` over N iterations of the target endpoint
5. Dump stats sorted by both `tottime` (self-time) and `cumtime` (cumulative)
6. Write JSON report with top-30 functions for programmatic comparison

Run it:

```bash
uv run python scripts/profile_list_cprofile.py
```

Read `logs/profile_list_cprofile.txt` top-15 by self-time. **The answer will
almost always surprise you.** Key questions:

- What percentage of total time is the top function? (If >10%, it's worth fixing)
- Are any of the top 10 pure Python? (Pure Python is cheap to optimize)
- What is the **call count**? (High count × moderate per-call cost is the
  fat middle that micro-benchmarks miss)
- Is your assumed hotspot in the top 30? (If not, **do not work on it**)

**Phase 3: Identify the optimization target**

Look for one of these patterns in the top entries:

- **High self-time + high call count + pure Python** → cacheable computation
  (e.g., 87ms / 50,000 calls / pure MRO walk → dict cache)
- **High self-time + native function** → irreducible floor, only beatable by
  changing the call pattern (fewer queries, batching, etc.)
- **High self-time + I/O (kqueue, epoll, socket)** → async scheduling issue,
  look at concurrency patterns

For each candidate, estimate the theoretical ceiling:

- Current time: `X ms total`
- If this function drops to zero: `X - Y ms = Z ms`
- Expected throughput improvement: `1/Z - 1/X` as a percentage

If the ceiling is <5%, keep looking. If the ceiling is >20%, you have a target.

**Phase 4: Implement the fix**

Write the minimum change that addresses the hotspot. Favor:

- Caching on class/module level (one-time cost, amortized across all calls)
- Avoiding allocations in hot loops
- Replacing dynamic attribute access (`getattr`, `type(x).__dict__.get`) with
  pre-resolved direct references

Do NOT:

- Rewrite in Zig unless you've proven Python allocation or interpreter
  overhead is the bottleneck AND you've measured that FFI overhead is lower
  than what you save
- Add complexity (flags, indirection layers) to handle cases the profile
  didn't show

**Phase 5: Re-profile and verify**

Re-run Phase 2 (cProfile) and Phase 1 (production benchmark). Both must show
improvement. If the cProfile drops but wrk stays flat, you optimized something
that doesn't matter at wire speed (e.g., your fix only helps non-DB-bound
endpoints, or the connection pool is saturated).

Record the delta as a markdown table comparing before/after rps, avg_ms, and
the specific pstats entries you were targeting. Example:

```
Before: _get_compute_method = 87ms (14% total), avg_ms = 1.27
After:  _get_compute_method = 20ms (4% total),  avg_ms = 1.02
Wire:   List endpoint 4,076 rps → 6,639 rps (+62.9%)
```

If the numbers don't match the theoretical ceiling, investigate why — either
your profile didn't capture something, or your fix has a hidden cost.

**Phase 6: Commit the report**

Every optimization must include:

1. The profiling script (so anyone can reproduce)
2. The before/after numbers in `logs/profile_<target>_report.md`
3. A test verifying the functional change is parity-preserving
   (e.g., existing serializer tests still pass)
4. A performance report entry mentioning the real-world throughput delta

### Worked Example: Serializer Compute-Method Cache

A canonical reference for "what this workflow produces":

**Before**:

```
cProfile top 3 by self-time on /api/v1/books/ (500 iter):
  1. _db_query_dicts (Zig native): 111ms
  2. _db_query (Zig native):       108ms
  3. _get_compute_method:           87ms ← pure Python, 50,000 calls
```

Root cause: `Serializer._get_compute_method` walked the MRO (`type(self).__mro__`)
and called `type(self).__dict__.get(name)` on every candidate name, for every
field, for every serialized object. For a 10-item list with a 10-field
serializer, that's 100 field resolutions per request × ~16 mappingproxy gets
each = **1,600 MRO walks per request, almost all returning `None`**.

**Fix** (in `serializers.py`, `SerializerMeta.__new__`):

```python
# Pre-compute compute-method lookup table at class creation time.
compute_cache: dict[str, object] = {}
for candidate_name in dir(cls):
    if candidate_name.startswith("_"):
        continue
    attr = cls.__dict__.get(candidate_name)
    if attr is None:
        for base in cls.__mro__[1:]:
            attr = base.__dict__.get(candidate_name)
            if attr is not None:
                break
    if attr is not None and callable(attr):
        compute_cache[candidate_name] = attr
cls._compute_method_cache = compute_cache
```

```python
def _get_compute_method(self, source: str):
    cache = type(self)._compute_method_cache
    method = cache.get(source)
    if method is None:
        method = cache.get(f"get_{source}")
    if method is None:
        return None
    return lambda obj, m=method: m(self, obj)
```

**After**:

```
cProfile:
  _get_compute_method: 87ms → 20ms (4.35x faster)
  _serialize_one total: 1.27ms → 1.02ms avg (20% faster)

Production (wrk 16t/200c/10s):
  List:    4,076 → 6,639 rps (+62.9%)
  Reviews: 5,401 → 7,424 rps (+37.4%)
  Stats:   9,920 → 10,474 rps (+5.6%)
  Detail:  6,962 → 6,959 rps  (0%, FK-bound)
  Search:  4,163 → 4,159 rps  (0%, FTS-bound)
  Health:  11,653 → 11,648 rps (0%, no serializer)
```

Total cost: ~15 lines of Python, zero new tests needed (existing 74 pass),
one file touched. **The profile made the win obvious — the code change was trivial.**

### Anti-Patterns to Avoid

1. **"This looks like it could be slow"** — without a profile, you are
   guessing.
2. **Profiling a Debug build** — Zig debug traces can emit megabytes of stderr
   and dominate the wall-clock time. Always `uv run hyper-build --release` first.
3. **Using py-spy without `--native` on macOS** — py-spy can't sample Zig
   frames on macOS. Use in-process `cProfile` via `TestClient` instead; it
   gets Python frames without sudo and without platform-specific native support.
4. **Measuring one endpoint, optimizing another** — the hotspot for List may
   not be the hotspot for Search. Profile the endpoint whose throughput you
   want to improve.
5. **Optimizing without a re-profile** — you must prove the fix worked. Run
   the profile again after the change and diff the top-15 table. If the
   targeted function didn't drop, the fix is wrong.
6. **Chasing microseconds on a millisecond call** — a function that takes
   2μs in a 5,000μs request is 0.04% of total time. Even a 100x speedup
   there yields a 0.04% improvement. The per-call cost matters less than
   `calls × tottime` — always sort by self-time first.

### Measurement caveats

- **Wire-speed wrk numbers from single-run comparisons should be treated
  with caution.** Back-to-back wrk runs against the **same code** on an
  unloaded machine can vary by ±20%. cProfile per-call self-time numbers
  are the reliable attribution — they exclude DB roundtrip variance.
- **When wire-speed claims contradict cProfile claims**, trust cProfile.
  A 24% per-call cProfile delta is a Python structural win; a 40% wrk
  delta on the same change is at least partly environment.
- **Matched-state baselines matter**: if you measure "before" on Monday
  and "after" on Tuesday, the delta is noise. Measure before + after
  within the same session, ideally within minutes of each other.

### Tools

| Tool                                        | Use When                                                                     | Cost                                                   |
| ------------------------------------------- | ---------------------------------------------------------------------------- | ------------------------------------------------------ |
| `cProfile` + `pstats`                       | First-pass hotspot discovery in Python code                                  | Zero setup, in-process, portable                       |
| `py-spy`                                    | Sampling profile under sustained real load                                   | Requires sudo on macOS, `--native` Linux-only          |
| `scripts/profile_hypernews_cprofile.py`     | Multi-endpoint cProfile with Stability Rule built-in                         | Copy & modify endpoint set; per-endpoint iter counts   |
| `scripts/profile_list_cprofile.py`          | bookstore List cProfile (template for any single REST endpoint)              | Copy and modify the target path                        |
| `scripts/profile_admin_cprofile.py`         | HyperAdmin pages with auth flow handled                                      | Logs in via `/admin/login/` automatically              |
| `scripts/profile_openapi_cprofile.py`       | OpenAPI spec build via direct call + HTTP path                               | Two-scenario harness                                   |
| `scripts/profile_queue_channel_cprofile.py` | task_queue enqueue/execute + channel.publish                                 | Multi-scenario                                         |
| `scripts/profile_write_cprofile.py`         | REST framework write/POST path                                               | Counterpart to profile_list_cprofile.py                |
| `scripts/bench_hypernews_wrk.py`            | Hypernews wire-speed verification                                            | Multi-run median, jitter tracked, drop+seed integrated |
| `scripts/bench_bookstore_wrk.py`            | Bookstore REST wire-speed verification                                       | Same structure, different endpoint set                 |
| `scripts/_wrk_bench.py`                     | Shared wrk harness — import `WrkBenchmark`, `WrkTarget`, `run_wrk_benchmark` | Use this when adding a new wrk bench                   |
| `scripts/bench_pool_queue_depth.py`         | pg.zig pool acquire contention sampler during wrk run                        | Histograms waiters, recommends pool sizing             |
| `scripts/bench_db_query_dicts.py`           | Row-shape microbench: `_db_query` vs `_db_query_dicts`                       | Isolates per-row dict construction                     |
| `scripts/bench_load_orm.py`                 | Original wrk bench, deeper concurrency (200 conns)                           | Single-run, no median                                  |
| `scripts/bench_profile.py`                  | Per-endpoint header inspection (X-Query-Count)                               | Relies on `PerformanceMiddleware` being wired          |
| `scripts/bench_where_compile.py`            | Micro-benchmark for `_where_compile`                                         | Only trust if function is in cProfile top 10           |
| `scripts/bench_from_record_bulk.py`         | Micro-benchmark for Model hydration variants                                 | Hydration variant comparison                           |
| `scripts/bench_time_ago.py`                 | Micro-benchmark for time_bucket_cached                                       | Bucket cache microbench                                |
| Built-in `@profile_handler`                 | Per-request X-Profile header timing breakdown                                | Zero external deps, in-process                         |

### When to use which

- **Start with `cProfile`** (via `profile_list_cprofile.py`). It's the fastest
  path from question to answer. 500 iterations complete in <1 second and
  surface Python-level hotspots with full call counts.
- **Follow with `bench_load_orm.py`** for the before/after throughput delta.
  This is the only number that matters for the performance report.
- **Use `py-spy` only** when cProfile points to a native function and you need
  to see _inside_ it (which on macOS you cannot, so this is Linux-only).
- **Use `bench_where_compile.py`-style micro-benchmarks only after** cProfile
  has confirmed the function is in the top 10. Don't write a micro-benchmark
  for a function you think might be slow.

- Profile storage uses a thread-safe ring buffer with a configurable capacity

## Free-threaded performance rules (3.14t)

Three findings that cost days to discover and are invisible in a profile until
you know to look for them. Each was measured on a 128-core box under load.

### `threading.local` WRITES serialize process-wide

Reading a `threading.local` attribute is cheap. **Writing one is not** — under
free-threading it takes a process-wide lock:

| operation, 64 threads                    | throughput   |
| ---------------------------------------- | ------------ |
| `threading.local` attribute **write**    | 0.18 M ops/s |
| plain attribute on a thread-owned object | 158 M ops/s  |

That 880× cliff is not a micro-optimization: two such writes per request capped
the whole reactor at **66k rps** where it otherwise sustained **579k**.

The fix is not to avoid `threading.local` — it is to stop _writing_ to it. Put
one long-lived state object in the thread-local **once**, then mutate that
object's ordinary attributes:

```python
@dataclass(slots=True)
class _State:  # thread-owned; plain attribute writes are free
    depth: int = 0


class _Local(threading.local):
    def __init__(self) -> None:
        self.state = _State()  # the ONLY threading.local write, once per thread


_local = _Local()
_local.state.depth += 1  # hot path: not a threading.local write
```

Applied in `hyperdjango/database.py` (transaction depth), `profiling.py`,
`standalone_middleware.py`, and `native/_coro.py`. Where the state has a natural
home on an object the thread already owns — an event loop, a connection — put it
there instead and skip the thread-local entirely.

### Function-body imports on a hot path convoy on the import lock

A per-request `from x import y` inside a handler serialized requests on
CPython's import lock and produced _bistable_ server instances: identical
processes serving either ~155k or ~550k rps depending on startup timing, with
89% of samples parked on that one line. Hoisting the import to module scope
eliminated it. This is why the codebase bans function-body imports outright —
the rule reads like style, but it is a throughput cliff.

### One shared counter is a cache line every core fights over

A single global counter incremented per request ping-pongs its cache line across
sockets. Use sharded, cache-line-padded cells summed on read
(`zig/src/metrics_py.zig`'s `ShardedCount`, 128 × 64 B cells, thread-assigned
slot). Measured 26 M → 377 M ops/s at the primitive level.

### How these were found

None was visible in `cProfile` — all three live below Python. The sequence that
worked: wire-level A/B first (is the regression real?), then in-process frame
sampling to find _where_, then a primitive-level microbench to prove the
mechanism. A microbench alone would have measured the wrong thing; a profile
alone would have shown a flat, innocent-looking call tree.
