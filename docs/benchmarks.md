# Benchmarks

All benchmarks run on Apple M-series, PostgreSQL 18, Python 3.14t (free-threaded), Zig ReleaseFast. Methodology: wrk -t4 -c20 -d8s, 3 runs, median selected, jitter reported.

## REST API Throughput (Bookstore API)

| Endpoint                                      | rps    | p50    | p99     | Jitter |
| --------------------------------------------- | ------ | ------ | ------- | ------ |
| GET /health (no DB)                           | 19,890 | 0.98ms | 2.79ms  | 1.1%   |
| GET /api/v1/books/stats (aggregate)           | 11,420 | 1.73ms | 12.21ms | 7.6%   |
| GET /api/v1/reviews/ (cursor pagination)      | 7,500  | 2.51ms | 6.58ms  | 0.7%   |
| GET /api/v1/books/1 (detail + select_related) | 6,349  | 2.85ms | 14.19ms | 1.1%   |
| GET /api/v1/books/ (list + serializer)        | 5,337  | 3.37ms | 20.92ms | 7.7%   |
| GET /api/v1/books/?search=python (FTS)        | 4,340  | 4.14ms | 15.99ms | 0.4%   |

## Template Rendering (HyperNews)

| Endpoint                      | rps     | p50    | p99    |
| ----------------------------- | ------- | ------ | ------ |
| GET /login (template-only)    | 40,459  | 0.38ms | 2.38ms |
| GET /forums (directory)       | 40,328  | 0.35ms | 2.67ms |
| GET / (cached homepage)       | 40,000+ | 0.36ms | 3.35ms |
| GET /user/alice (multi-query) | 40,608  | 0.35ms | 5.38ms |

## Database (pg.zig vs psycopg3)

| Operation        | pg.zig      | psycopg3   | Speedup |
| ---------------- | ----------- | ---------- | ------- |
| SELECT by PK     | 21K ops/s   | 10K ops/s  | 2.06x   |
| SELECT range     | —           | —          | 4.18x   |
| UPDATE           | —           | —          | 1.52x   |
| COPY bulk import | 536K rows/s | 12K rows/s | 42.8x   |

## Guard System Overhead

| Guard Type                  | Overhead |
| --------------------------- | -------- |
| Single guard (Require.role) | 0.21 us  |
| 3-guard chain               | 0.40 us  |
| GuardSpec creation          | 0.85 us  |

## JSON (SIMD Zig vs Python stdlib)

| Operation                | Native | stdlib | Speedup |
| ------------------------ | ------ | ------ | ------- |
| json_loads (tiny object) | 94ns   | 576ns  | 6.1x    |
| json_loads (integer)     | 48ns   | 467ns  | 9.8x    |
| json_loads (float)       | 80ns   | 518ns  | 6.5x    |
| json_loads (boolean)     | 49ns   | 441ns  | 9.0x    |
| json_dumps (dict)        | 196ns  | —      | —       |

## String Operations (SIMD Zig vs Python stdlib)

| Operation                | Native | stdlib | Speedup |
| ------------------------ | ------ | ------ | ------- |
| html_escape (with chars) | 111ns  | 376ns  | 3.4x    |
| url_encode (long path)   | 113ns  | 1390ns | 12.3x   |
| url_decode (percent)     | 88ns   | 1505ns | 17.1x   |
| parse_query_string (10p) | 1574ns | 5596ns | 3.6x    |

## Validation (Native Zig)

| Operation                        | Throughput       |
| -------------------------------- | ---------------- |
| Model creation (init_model_full) | 1.6M/sec         |
| Per-field validation             | 6.7M fields/sec  |
| Batch int validation (SIMD)      | 51.5M ints/sec   |
| Batch model validation           | 13.1M models/sec |
| SIMD email validation            | 63ns/email       |

## Template Compilation

| Operation       | Native | Jinja2 | Speedup |
| --------------- | ------ | ------ | ------- |
| Compile         | 7.1us  | 1.66ms | 234x    |
| Render (cached) | 36us   | 61us   | 1.7x    |

## WhereNode Compile (Zig vs Python)

| Scenario                    | Python | Zig   | Speedup |
| --------------------------- | ------ | ----- | ------- |
| Simple leaf                 | 442ns  | 169ns | 2.6x    |
| 3-filter AND                | 1737ns | 464ns | 3.7x    |
| Complex nested (4 children) | 3364ns | 868ns | 3.9x    |

## Native Metric Primitives

| Operation             | Latency | Target |
| --------------------- | ------- | ------ |
| counter_inc           | 78ns    | 50ns   |
| gauge_set             | 76ns    | 50ns   |
| histogram_observe     | 81ns    | 100ns  |
| counter_vec_inc       | 132ns   | 250ns  |
| histogram_vec_observe | 124ns   | 300ns  |

## WebSocket: native Zig server vs. `websockets` (PyPI reference)

**Headline: native is 1.8–2.3× faster than `websockets` on throughput and
lower-latency at every payload size.** This is only visible when the
benchmark is driven by a _multi-process_ load generator
(`benchmarks/websocket/loadgen.py`). A single asyncio client process does
the same per-message work as a single-threaded server, so it caps at one
core's worth of load and cannot saturate a multi-core server — earlier
single-client numbers were measuring the _client's_ ceiling and undercounting
native. Native scales with cores (one OS thread per connection under
free-threaded Python 3.14t); the single-loop reference is pinned to one core
and its throughput actually _degrades_ as more client load is applied. Every
connection also does an out-of-band warmup burst before the timed window
opens; latency is measured single-connection, unpipelined, with its own
warmup.

| Payload | native msgs/sec | reference msgs/sec | speedup | native p50 | reference p50 |
| ------- | --------------- | ------------------ | ------- | ---------- | ------------- |
| 32 B    | 145k            | 81k                | 1.8×    | 46us       | 77us          |
| 4096 B  | 140k            | 72k                | 1.9×    | 50us       | 80us          |
| 65536 B | 75k             | 32k                | 2.3×    | 82us       | 105us         |

Native also wins at concurrency=1 (19k vs 13k msgs/sec) and its aggregate
throughput keeps climbing with offered load (up to ~145k) while the
reference plateaus around ~85k and then declines.

**Startup latency** (process spawn to `/health` responding, median of 5
trials, 5ms readiness-poll granularity so quantization doesn't hide the
number): native ~85ms vs. reference ~40ms. Confirmed via direct measurement
that `HYPER_THREAD_POOL_SIZE` is _not_ the driver here — startup time is
flat (~86ms) whether the pool spawns 4, 8, or 24 threads, so the gap is
import time + interpreter/native-extension load + Zig server bind, not
thread provisioning. The import-time fixes below (#8) reduced this
component; the remainder is inherent to loading a fuller framework than a
single-purpose library.

**Connection model — the default (shared) vs. the `thread` opt-out.**
`WEBSOCKET_CONCURRENCY=shared` (the **default**) multiplexes connections over
a small event-loop pool; `WEBSOCKET_CONCURRENCY=thread` dedicates one OS
thread per connection (max live connections = `THREAD_POOL_SIZE`). Both
driven to 96 concurrent connections:

| Model              | Connections held | Throughput | Peak RSS | Peak threads |
| ------------------ | ---------------- | ---------- | -------- | ------------ |
| `shared` (default) | 96 / 96          | 165k msg/s | 83 MB    | 32           |
| `thread` (opt-out) | 24 / 96          | 145k msg/s | 82 MB    | 26           |

The default `shared` model holds **all** connections (the `thread` opt-out
caps at its thread-pool size), at _higher_ throughput and essentially the
same memory — and memory stays ~flat as connections grow. This is why it's
the default. It requires cooperative handlers (no thread parked per
connection); see [server.md](server.md) and [realtime.md](realtime.md).

**Interop:** both servers pass all 9 RFC 6455 correctness checks (text/binary/
Unicode/empty-message echo, ping/pong, clean close, concurrent send ordering,
multi-connection isolation) — see `benchmarks/websocket/interop.py`.

**Architecture, not a bug:** the native server dedicates one OS thread (from a
fixed pool, default 24, `HYPER_THREAD_POOL_SIZE`) to each live connection so
it can genuinely use multiple CPU cores under Python 3.14's free-threaded
build; connections beyond the pool size queue rather than fail. The
`websockets` reference runs a single-process asyncio event loop with no such
ceiling, but no multi-core parallelism either. Spending more memory/threads
for more concurrent-connection capacity is a deliberate, good trade as long
as it's a tunable knob — which it is (`HYPER_THREAD_POOL_SIZE`,
`HYPER_THREAD_STACK_SIZE`).

**Perf audit — findings, in order of impact** (full detail, including a
`python -X importtime` breakdown and a `cProfile` trace that caught a
kqueue-syscall regression mid-fix, is in the generated report):

0. **Benchmark methodology (biggest finding)** — the throughput comparison
   was client-limited; a single asyncio client can't saturate a multi-core
   server. Fixed with a multi-process load generator, which revealed native
   is 1.8–2.3× _faster_, not slower. The premise that native was losing was
   itself a measurement artifact.
1. RFC 6455 handshake bug (wrong magic GUID) — made the native server unable
   to complete a handshake with any spec-compliant client at all.
2. Executor-per-connection leak — a brand-new `ThreadPoolExecutor` per
   connection instead of one shared bounded pool.
3. Per-message thread-hop on receive — `loop.run_in_executor` cost ~27-29us
   per round trip in isolation (vs. ~0.01us direct). Fixed with a
   non-blocking `_ws_try_recv` (`MSG_DONTWAIT` against a per-connection
   buffer) plus `_ws_get_fd`, so Python awaits `loop.add_reader(fd, ...)`
   instead of a thread pool. Old executor path kept as an automatic
   fallback.
4. Reader re-registered every message — fix 3's first version paid two
   kqueue syscalls per message; `cProfile` showed `select.kqueue.control()`
   at 61% of all time. Fixed by registering the reader once per connection.
5. No `TCP_NODELAY` — Nagle's algorithm was active on every WS socket;
   asyncio enables `TCP_NODELAY` by default. Fixed via `setsockopt`.
6. Two syscalls per frame write (header, then payload) — fixed with a single
   `writev()` call (`NetStream.writeAllVectored2`).
7. O(n) memmove per frame extracted from the receive buffer — fixed with a
   read cursor that only compacts lazily, so a batch of N buffered frames
   costs zero memmoves instead of N.
8. **113MB import-time bloat** — `from hyperdjango import HyperApp` alone
   cost 113MB / ~153ms (vs. 15.7MB / ~20ms for `websockets`), traced via
   `python -X importtime` to three places eagerly importing Django's
   forms/ORM/template compat layer for features (`SessionAuth`/`.oauth2()`,
   `HyperForm`/`HyperSerializer`, file-based route discovery) a bare
   WebSocket app never touches. Deferred all three to first-use (matching
   the framework's own lazy `__getattr__` convention) — cut baseline import
   memory to 51.5MB, zero functional change.
9. Thread-pool stack size was hardcoded at Zig's 16MB default — now tunable
   via `HYPER_THREAD_STACK_SIZE`, default unchanged (a 1MB override crashed
   the server outright in testing, confirming the default is load-bearing,
   not just caution).

Net effect on this quick-matrix run (concurrency=8, before → after all
fixes): p50 latency now **beats** the reference at every payload size
tested; throughput rose from ~65-70% of reference to ~90-97%; peak RSS under
sustained load dropped from ~155MB to ~87MB. The full websocket test suite
(152/152), 217/217 core HTTP/server tests, and the full repo suite
(11521/11684, remaining failures pre-existing and environment-specific)
passed after every change. The remaining throughput gap at higher
concurrency reflects the thread-pool-vs-single-event-loop architectural
trade-off above, not an unaddressed inefficiency. See the full report for
details.

## HTTP framework comparison (2× EPYC 7702, corrected harness)

Worker sweep on a 256-logical-CPU box (2× EPYC 7702): server pinned to socket
0's 64 physical cores, wrk client to socket 1's, `performance` governor,
c=1024 busy keep-alive, 10 s cells. Throughput counts **2xx/3xx only** and
uvicorn runs through the proto-correct launcher (TCP_NODELAY actually on) —
see "Measurement-validity rules" below for why both matter; earlier runs
without them produced numbers off by up to 10×.

| W (parallelism) | hyperdjango-reactor                    | FastAPI (uvicorn, W worker procs) | Flask (gunicorn, W procs × 4 threads) |
| --------------- | -------------------------------------- | --------------------------------- | ------------------------------------- |
| 8               | 151k rps                               | 41k rps                           | 99k rps                               |
| 16              | 269k rps                               | 81k rps                           | 151k rps                              |
| 32              | 360k rps                               | 149k rps                          | 171k rps                              |
| 64 (= cores)    | **479–548k rps** · p99 3.4 ms · 91 MiB | 239k rps · p99 6.3 ms · 4.1 GiB   | 179k rps · p99 35 ms · 3.6 GiB        |
| 96 (oversub)    | 296–414k rps                           | 238k rps (flat past cores)        | 183k rps · p99 190 ms                 |
| 128 (oversub)   | 92k rps                                | 230k rps                          | 180k rps · p99 330–380 ms             |

Readings:

- **Reactor scales monotonically to the core budget** (151k → 548k, W=8→64),
  peaks at 2.3× FastAPI / 3× Flask in ~1/45th their memory, then declines
  past it — worker threads beyond pinned cores is scheduler oversubscription,
  which the sweep tags per cell. Process-per-worker models flatten instead of
  collapsing (idle processes just don't run).
- **FastAPI scales cleanly once TCP_NODELAY is fixed** (41k → 239k). The
  broken multi-worker launch measured a 40 ms delayed-ACK artifact: flat
  ~24.6k rps at every W.
- **Flask/gunicorn plateaus ~180k from W=32** with a latency tail that grows
  with worker count (keep-alive queueing across 4-thread workers).
- **hyperdjango-threaded is excluded from the c=1024 table by design**: with
  c ≫ W it load-sheds (fast 503s) rather than serving every connection, so
  its "throughput" there is a shedding measurement. Its regime is bounded
  connections — measured on the same box below.

Threaded vs reactor in the BOUNDED regime (c ≤ 2W, same box/pins/governor):

| Workload    | Threaded                 | Reactor               |
| ----------- | ------------------------ | --------------------- |
| W=8, c=8    | **109k rps, p99 85 µs**  | 91k rps, p99 103 µs   |
| W=16, c=16  | **209k rps, p99 92 µs**  | 172k rps, p99 119 µs  |
| W=32, c=32  | 232k rps, p99 487 µs     | 234k rps, p99 403 µs  |
| W=64, c=128 | **561k rps, p99 299 µs** | 564k rps, p99 1.76 ms |

Both connection models reach the same ~560k rps machine ceiling at
saturation; the threaded model does it with ~6× lower p99 (no dispatch hop).
Two measurement notes: at exactly c=W each threaded worker idle-wakes per
request, making the cell wakeup-latency-bound rather than capacity-bound
(W=64/c=64 reads 156k for that reason — drive c ≈ 2W to saturate), and the
c=1024 reactor peak (479–548k) matching the bounded-regime ceiling confirms
the reactor holds full throughput while ALSO carrying 16× more connections.

## Reproduce

```bash
uv run hyper-build --release
uv run python scripts/bench_bookstore_wrk.py
uv run python scripts/bench_hypernews_wrk.py
uv run python scripts/bench_guard_overhead.py

# WebSocket: native vs. websockets (PyPI), interop + throughput + latency +
# memory + a live-measured perf audit. --full for the complete matrix,
# --profile to also attempt py-spy flamegraphs (usually needs sudo).
uv run --group benchmark-comparison python -m benchmarks.websocket.run
uv run --group benchmark-comparison python -m benchmarks.websocket.run --full
```

Results saved to `logs/bench_*.json` and `logs/bench_*.txt`, and to
`benchmarks/websocket/out/{results.json,report.md,report.html}` for the
WebSocket suite.

### The canonical record: `make bench-all`

`make bench-all` is THE canonical-record recipe: it runs both suites — the HTTP
framework comparison and the WebSocket comparison — and produces **one record
containing both**, so the dashboard's Suite selector switches views of the _same_
entry rather than making you flip between two half-records.

```bash
make bench-all                          # one record, both suites
make bench-all BENCH_LABEL=epyc-tuned   # same, under your own record label
```

**How one record ends up with two suites.** Both runners feed the shared history
under the same `--label` (`BENCH_LABEL`, default `canonical`), and
`benchmarks.core.results.save_run` uses that label as a **merge key**: a feed
whose label matches an existing entry — same commit, same host, inside the
24-hour merge window, and carrying no suite that entry already holds — is merged
_into_ that entry instead of minting a new one. So `bench-http` creates the
record and `bench-websocket` merges into it. Re-feeding a suite the record
already has never overwrites it; that starts a fresh record, which keeps the
archive non-destructive. Every suite carries a `provenance` stamp (source
timestamp, label, commit, host, run id), and the dashboard prints when the
selected suite was measured whenever it differs from the record's timestamp.

**Combining records after the fact.** Two runs that should have been one can be
merged without re-measuring. Source entries keep their JSON on disk with a
`merged_into` pointer and only leave the index, so the dashboard shows one record:

```bash
uv run python -m benchmarks.core.merge canonical-baseline ws-full-refeed
uv run python -m benchmarks.core.merge <run-id> <run-id> --label canonical
```

Re-running the same merge is a no-op (already-absorbed sources are skipped).

**Coverage is always stated.** Running a single suite still archives a record —
just a single-suite one, and the dashboard never lets that pass as the whole
battery: every Run-picker entry is tagged with the suites it contains
(`[http+websocket]` vs `[websocket only]`), the Suite selector renders a missing
suite as a disabled entry rather than hiding it, and a coverage line sits above
the chart.

**Coverage is DECLARED, not inferred.** A record also states what it was
_supposed_ to contain (`expected_suites`), so a missing suite is visible even
when nothing else in the archive would reveal it. `make bench-all` sets
`BENCH_EXPECT=http,websocket` for both halves, so the record declares the full
battery the moment the HTTP half is fed; if the WebSocket half never lands, the
dashboard labels that record **INCOMPLETE** ("declared http + websocket but
recorded only http") and shows the absent suite as _declared, not recorded_ —
instead of it reading as complete simply because no other record in the archive
happens to carry a WebSocket suite. Single-suite runs declare only themselves;
pass `--expect-suites http,websocket` to either runner to declare more.

Records archived before the declaration existed carry none, and are labeled
exactly as they always were (coverage falls back to the suites seen across the
history). The archive is never rewritten to backfill it.

```bash
uv run --group benchmark-comparison hyper-bench --mode all --client wrk \
    --label canonical --expect-suites http,websocket
```

An already-measured WebSocket run can be fed into the unified record without
re-measuring (nothing is re-run; the existing `results.json` is replayed, and the
same merge-into semantics apply):

```bash
uv run python -m benchmarks.websocket.refeed --label canonical
```

**The unified record has the same complete/diagnostic split as the HTTP
archive.** A restricted cross-suite run — the WebSocket smoke matrix
(`make bench-websocket`, no `--full`), a `benchmarks.core.runner --quick`, or a
battery that lost a suite mid-run — archives under `benchmarks/out/diagnostics/`
instead of `benchmarks/out/history/`: same format, kept for the investigation
record, and structurally invisible to the unified dashboard, the run index and
the merge-target lookup. It therefore can never merge into a canonical record
either, which is why `make bench-all` runs the WebSocket half with `--full`.
As on the HTTP side, flags gate the intent and RESULTS gate the classification:
a `--full` WebSocket run that measured an empty section, or only one arm of the
connection-model comparison, is a diagnostic too, and the runner prints exactly
which cells are missing.

```bash
make bench-websocket        # smoke matrix -> benchmarks/out/diagnostics/
make bench-websocket-full   # full matrix  -> benchmarks/out/history/
```

## Clean Ubuntu machine — full setup from scratch

**One command does almost all of this now:**

```bash
bash scripts/dev_bootstrap.sh --with-postgres --bench   # or plain `make bootstrap`
```

It self-installs uv + free-threaded 3.14t, downloads the pinned Zig into
`.toolchain/` (hyper-build auto-discovers repo-local toolchains — no PATH
setup), builds and smoke-tests the native extension, apt-installs
PostgreSQL 18 + pgvector from PGDG (`--with-postgres`), provisions the role /
`hyperdjango_test` database / trusted pgvector, installs wrk + numactl
(`--bench`), and finishes with `hyper db doctor`. Idempotent — re-run any
time. The manual steps below remain as the reference for what it does (and
for non-Ubuntu machines).

Bringing up a bare Ubuntu 24.04 box (everything runs on `localhost`) to build,
run the suite, and benchmark. Commands assume `sudo`.

### 1. System packages

```bash
sudo apt-get update
sudo apt-get install -y build-essential curl git wget xz-utils ca-certificates wrk

# PostgreSQL 18 + pgvector from the PGDG apt repo (Ubuntu's default repos are older)
sudo install -d /usr/share/postgresql-common/pgdg
sudo curl -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
  https://www.postgresql.org/media/keys/ACCC4CF8.asc
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
  | sudo tee /etc/apt/sources.list.d/pgdg.list
sudo apt-get update
sudo apt-get install -y postgresql-18 postgresql-18-pgvector
```

### 2. Zig 0.16+ and uv

```bash
# Zig 0.16 — grab the linux-x86_64 0.16.x tarball from https://ziglang.org/download/
# (apt's zig is too old). Example:
cd /opt && sudo wget https://ziglang.org/download/0.16.0/zig-x86_64-linux-0.16.0.tar.xz
sudo tar xf zig-x86_64-linux-0.16.0.tar.xz
echo 'export PATH=/opt/zig-x86_64-linux-0.16.0:$PATH' | sudo tee /etc/profile.d/zig.sh
source /etc/profile.d/zig.sh && zig version   # expect 0.16.x

# uv (manages Python 3.14t for you — no system Python needed)
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"
```

### 3. PostgreSQL: role, database, extension

```bash
sudo systemctl enable --now postgresql
# Create a role matching your login user + the test database it connects to
sudo -u postgres createuser -s "$USER"
sudo -u postgres createdb -O "$USER" hyperdjango_test
psql -d hyperdjango_test -c 'CREATE EXTENSION IF NOT EXISTS vector;'   # pgvector
```

The `DATABASE_URL` connects over TCP to `localhost` with **no password**, but
Ubuntu's default `pg_hba.conf` requires one for TCP. On a dedicated benchmark
box, trust local connections (localhost only — do **not** do this on a shared or
internet-facing host):

```bash
HBA=/etc/postgresql/18/main/pg_hba.conf
sudo sed -i -E 's|^(host\s+all\s+all\s+127\.0\.0\.1/32\s+).*|\1trust|' "$HBA"
sudo sed -i -E 's|^(host\s+all\s+all\s+::1/128\s+).*|\1trust|' "$HBA"
sudo systemctl reload postgresql
psql "postgresql://$USER@localhost:5432/hyperdjango_test" -c 'SELECT 1;'   # verify passwordless
```

### 4. Build and verify

```bash
git clone https://github.com/anthropics/hyperdjango.git && cd hyperdjango
uv sync --group dev --group benchmark-comparison     # deps incl. FastAPI/Flask baselines
uv run hyper-build --install --release               # optimized native ext
export DATABASE_URL="postgresql://$USER@localhost:5432/hyperdjango_test"
uv run hyper-test rest serializers                   # quick smoke; drop args for the full suite
```

### 5. Kernel + PostgreSQL tuning (throughput + stability)

Everything is localhost, so the usual ceiling is **ephemeral-port and accept-queue
exhaustion**, not bandwidth. Persist these and apply for the session:

```bash
# /etc/sysctl.d/99-hyperdjango-bench.conf
sudo tee /etc/sysctl.d/99-hyperdjango-bench.conf >/dev/null <<'SYSCTL'
net.core.somaxconn = 65535
net.ipv4.tcp_max_syn_backlog = 65535
net.core.netdev_max_backlog = 65535
net.ipv4.ip_local_port_range = 1024 65535
# CRITICAL companion to the wide port range above: without this, the kernel
# hands out the suite's fixed server ports (18000-19999) as EPHEMERAL source
# ports for outbound connections, and test servers then randomly fail to bind
# with EADDRINUSE — a nondeterministic set of e2e files failing every parallel
# run, only on tuned boxes (the stock 32768-60999 range never overlaps).
# Reserved ports are skipped for ephemeral allocation but stay explicitly
# bindable.
net.ipv4.ip_local_reserved_ports = 18000-19999
net.ipv4.tcp_tw_reuse = 1
net.ipv4.tcp_fin_timeout = 15
fs.file-max = 2097152
SYSCTL
sudo sysctl --system

# File-descriptor limits (the load generator + server open many sockets)
echo "* soft nofile 1048576" | sudo tee -a /etc/security/limits.conf
echo "* hard nofile 1048576" | sudo tee -a /etc/security/limits.conf
ulimit -n 1048576   # for the current shell
```

PostgreSQL — edit `/etc/postgresql/18/main/postgresql.conf` (sizes assume a
large box; scale to your RAM), then `sudo systemctl restart postgresql`:

```ini
max_connections = 10000            # project-recommended headroom for the parallel suite
shared_buffers = 8GB               # ~25% of RAM
effective_cache_size = 24GB        # ~50-75% of RAM
work_mem = 32MB
maintenance_work_mem = 1GB
max_worker_processes = 64          # ~= core count
max_parallel_workers = 64
max_parallel_workers_per_gather = 4
synchronous_commit = off           # benchmark throughput (relaxes durability — bench DB only)
```

`max_connections = 10000` needs enough SysV shared memory / semaphores; on most
modern kernels the defaults are fine, but if PostgreSQL refuses to start, raise
`kernel.shmmax` / `kernel.sem` accordingly.

## HTTP scaling on large / high-core machines

**One command runs the whole comparison suite:**

```bash
make bench-http          # or: uv run hyper-bench --mode all --client wrk --label canonical
make bench-http-quick    # fast smoke first — validates the box setup in ~10 min
```

`--mode all` does everything the individual recipes below do, in sequence,
archived as ONE run in the combined `report.html`:

1. **Machine prep** — raises the fd limit, switches the cpufreq governor to
   `performance` when passwordless sudo allows (and restores it after), warns
   about anything it can't fix.
2. **Topology auto-pin** — reads sysfs and pins server/client to disjoint
   physical cores (NUMA-node split on multi-socket boxes, SMT siblings left
   idle) — the layout validated on the 2×EPYC box, derived instead of
   hand-written. Explicit `--server-cores`/`--client-cores` still win.

   **Large-payload caveat:** the NUMA-node split routes every response byte
   across the socket interconnect, and at ≥64 KiB payloads the cell measures
   that interconnect, not the server — an A/B on the 2×EPYC box moved a
   64 KiB cell +34% (7.14 → 9.59 GB/s of body bandwidth) purely by pinning
   server and client to halves of ONE node. The cross-node default is kept
   because core isolation (no cache/core stealing between server and client)
   matters more for the small-payload cells that dominate the matrix; for
   absolute large-payload numbers, pin both sides inside one node and watch
   body bandwidth, which exists so those cells state the bandwidth they are
   actually measuring. `body_gbps` (throughput × payload size) is recorded on
   EVERY result row of every sweep, charted as the **Body bandwidth** metric
   in the HTML report and the unified dashboard, and tabulated per payload
   ≥16 KiB in the markdown reports.

3. **Worker sweep** (c ≫ W): threaded/reactor/FastAPI/Flask across a
   power-of-two W ladder up to the pinned core budget.
4. **Bounded sweep** (c=W and c=2W): threaded vs reactor in the threaded
   model's design regime.
5. **Concurrency sweep** at peak W, all four frameworks.
6. **Connection-scaling sweep** (mostly-idle keep-alive) — the
   connection-capacity workload.

Every regime lands in the same dashboard run, so the report can show the full
comparative picture — where the reactor wins, where threaded wins, and where
the process-per-worker models sit — without stitching runs together.

The individual sweeps below remain available (`--mode
workers|bounded|concurrency|conn|both`) for targeted investigation.

### Connection scaling: read `max_held`, not peak rps

The conn-scaling sweep (`--mode conn`, and phase 6 of `--mode all`) is a
different regime from every other sweep in the suite. It opens N keep-alive
connections that are **mostly idle** — request, ~25 ms of think-time, repeat —
and sweeps N from 8 to 4096. That is the shape of real web traffic, and it
answers a question the busy closed-loop sweeps cannot: **how many connections
does the server actually hold in service at once?**

**The headline is `max_held`**: the largest N in the ladder the server held
with `served_frac >= 99%` of connections **and** `p99 <= 250 ms` (the bound is
`--cs-p99-bound-ms`, recorded in the run's meta as `cs_p99_bound_ms` so a
max_held read off an archive is always a comparable number). A framework that
holds the top of the ladder is reported as `>=4096` — the sweep did not find
its cap, and the number to quote is "at least this", not "this".

**Peak rps is the wrong headline here, in both directions:**

- In this regime rps is roughly _(connections held) × (1 / think-time)_. A
  thread-per-connection server that caps at ~W held connections therefore
  reports the SAME rps at every N past W. That flat number is an architectural
  cap being restated, not a throughput result — headlining it hides the actual
  finding (the cap, visible only in `served_frac`) behind a number that merely
  looks "lower than the others".
- A multiplexing server holds every connection offered, so its rps is bounded
  by whatever is slowest in the loop — **including the load generator**. The
  think-time client is Python; a single process saturates in the tens of
  thousands of requests/sec. That failure is silent and looks exactly like a
  result: full service, healthy p99, flat rps.

So the report annotates it. Any series whose adjacent-N rps gain falls under
5% _while_ still serving ≥99% of connections inside the p99 bound is tagged
**"plateau with full service and healthy latency — load-generator ceiling; rps
is a LOWER BOUND, capacity verdict comes from max_held"**. When two or more
frameworks plateau within 15% of each other, the sweep carries an extra note
naming them: architecturally different servers do not share a throughput
ceiling, so the shared generator is the suspect. Both appear in the run's
console auto-verdict, in `report_connscaling.md`, and in the capacity panel
above the conn-scaling chart in `report.html`.

**Generator headroom.** The think-time client shards its connections across
worker processes (`--cs-client-procs`, default one per 8 client cores with a
floor of 2, recorded as `cs_client_procs`), each running its own event loop
over N/K connections and rendezvousing on a barrier so all shards measure the
same window. Per-cell counts are aggregated afterwards — rates and connection
counts sum, percentiles come from the merged latency sample — and the recorded
field names are identical whether K is 1 or 8, so sharded and unsharded runs
stay directly comparable. `--cs-client-procs 1` restores the original
single-process path. If a sweep still shows several frameworks clustered at
one rps, raise K before quoting any of those numbers.

**What this sweep does NOT measure.** `max_held` is bounded by the ladder AND
by the think-time load it applies; it is a _served-capacity under load_
number, not an idle-connection capacity. Truly idle connections (open, no
traffic) cost only an fd plus a small per-connection buffer, so a multiplexing
server's idle ceiling is memory/fd-bound and far higher than any think-time
sweep will show — raise `nofile`, size the buffers, and measure that
separately if it is the number you need. Conversely a thread-per-connection
server's cap is the SAME in both tests, because a blocked worker is pinned to
its connection whether or not that connection is doing anything.

### Complete runs vs diagnostics — the archive doctrine

A benchmark RESULT compares everything against everything else: all four
frameworks, the full payload ladder, every regime (`--mode all`, no
`--frameworks`/`--payloads` restriction). Only such complete runs archive
into `history/` — the sole source for the comparison dashboard, the HTML
report, the unified dashboard, and the gate's baseline lookup.

Anything narrower — fix validation, bisection, A/B probes — is a
DIAGNOSTIC: archived under `diagnostics/` in the same format for the
investigation record, structurally invisible to every comparison surface.
The harness classifies each run automatically and says which it archived.
A diagnostic run may still gate itself against a complete baseline
(`--check-against`) to check "does my fix regress the record"; it can never
BECOME a baseline.

**Flags gate the intent; RESULTS gate the classification.** Asking for the
whole matrix is not the same as measuring it — a comparison framework whose
optional deps are missing is skipped mid-run, a server can fail to boot at
one worker count, a payload can die on an OOM. So after the sweeps finish the
harness verifies the archived entry itself:

- the `workers` and `concurrency` sweeps each carry every framework × every
  payload in the ladder;
- a cell counts as measured only with positive throughput (a 0 rps cell is a
  dead server, not a data point);
- the `bounded` and `connscaling` sweeps are present and non-empty.

A flag-complete run that fails this verification archives as DIAGNOSTIC and
prints exactly which framework × payload × regime cells are missing — a run
with holes must never become a baseline nobody can reproduce.

### Regression gate (`--check-against`)

Every run is archived under `benchmarks/http/out/history/` with its `--label`.
A later run can gate itself against the most recent archived run carrying a
given label:

```bash
# one-time: record the reference run
uv run hyper-bench --mode all --client wrk --label baseline

# after a change: measure AND gate in one command
uv run hyper-bench --mode all --client wrk --label candidate \
    --check-against baseline            # exit 3 on regression
```

The unit of comparison is each series' **peak** throughput cell
(`sweep × framework × payload`): single cells are ~±10% noisy run-to-run, but
a series' peak is its capacity statement — the number a real regression moves.
The gate fails (exit code 3) only when a series' peak drops more than
`--check-tolerance` (default 15%) below the baseline; series missing from
either side are reported but never fail, and the dashboards still render on a
failed gate — a regressed run is exactly the one whose report you want open.
Comparisons are only meaningful between runs on the same machine with the same
pinning AND the same mode/duration — gating a full `--mode all` run against a
`--quick` baseline compares 10-second cells to smoke cells and produces
spurious double-digit deltas in both directions.

The numbers above come from an 18-core dev box (~137K rps plaintext at W=8). On
a large server (64/128/200+ cores), run the worker-scaling sweep through the
`hyper-bench` entry point to find the peak as worker threads scale toward the
core count — the threaded model keeps climbing on more cores before the
native-dispatch floor, and typically beats the reactor at peak (the reactor
wins on idle-connection scaling / low worker counts).

The server **auto-scales workers and reactor shards to the machine** by default
(see [Reference architecture → capacity](reference-architecture.md)), so a bare
run already uses the box. The sweep pins `HYPER_THREAD_POOL_SIZE` per cell to
measure the whole curve, and `--reactor-counts` pins the reactor's queue
sharding so its effect is a report axis rather than a hidden default.

```bash
# One-time OS/loadgen prep (so the kernel and load generator aren't the ceiling)
ulimit -n 1048576
sudo sysctl -w net.core.somaxconn=65535 net.ipv4.tcp_max_syn_backlog=65535
# install the lightweight load generator + (Linux) perf for contention traces:
#   apt-get install -y wrk linux-tools-common linux-tools-$(uname -r)

export DATABASE_URL="postgresql://$USER@localhost:5432/hyperdjango_test"
uv run hyper-build --release

# Worker-scaling sweep — threaded vs reactor, push W toward the core count.
# --profile attributes each cell with perf counters (Linux); the run prints an
# AUTO-VERDICT flagging any adjacent-W step where rps DROPS (negative scaling)
# with the contention delta as the suspected cause.
uv run hyper-bench \
  --mode workers \
  --frameworks hyperdjango-threaded,hyperdjango-reactor \
  --worker-counts 8,16,32,64,96,128,160,200,256 \
  --sweep-concurrency 512 \
  --duration 10 --warmup 2 \
  --client wrk --profile

# Reactor-shard axis — confirm queue sharding, not worker count, is what lets
# the reactor scale past the single-queue knee. rc=1 is the old default;
# 'auto' is the capacity-scaled default; higher values pin more shards.
uv run hyper-bench \
  --mode workers \
  --frameworks hyperdjango-reactor \
  --worker-counts 32,64,128,256 \
  --reactor-counts 1,auto,8,16 \
  --sweep-concurrency 512 \
  --duration 10 --warmup 2 --client wrk --profile

# Concurrency sweep at a high fixed W, with FastAPI/Flask baselines for context.
uv run hyper-bench \
  --mode concurrency \
  --frameworks hyperdjango-threaded,hyperdjango-reactor,fastapi,flask \
  --workers 128 --duration 10 --warmup 2 --client wrk
```

### Core isolation on the same box (avoid measuring the wrong thing)

When the load generator runs on the **same machine** as the server, two
artifacts hide the server's real ceiling and must be controlled:

- **Client/server core contention.** `wrk`, the server, and `perf` all float
  across the same logical CPUs and steal cores from each other.
- **NUMA migration.** On a multi-socket box (e.g. 128 physical cores / 256
  hyperthreads across several NUMA nodes) worker threads migrate across nodes,
  paying remote-memory penalties — the source of the _chaotic, non-monotonic_
  collapse in an unpinned sweep.
- **An under-threaded client.** `wrk` defaults to `min(concurrency, 4)`
  threads; **4 threads cannot saturate a many-core server**, so throughput
  plateaus at the _client's_ ceiling (a flat rps across every server config is
  the tell). Pin the client to a core set and its thread count auto-sizes to
  that set — or set `--client-threads` explicitly.

Pin the server and the client to **disjoint** physical-core sets (add `--numa`
to also keep each process's memory node-local). On a 128-physical-core box,
give the server half and the client the other half:

```bash
uv run hyper-bench \
  --mode workers \
  --frameworks hyperdjango-threaded,hyperdjango-reactor \
  --worker-counts 8,16,32,64,96,128 \
  --sweep-concurrency 1024 \
  --server-cores 0-63 --client-cores 64-127 --numa \
  --duration 10 --warmup 2 --client wrk --profile
```

`--server-cores` / `--client-cores` take taskset-style lists and use
`taskset` (or `numactl --physcpubind --localalloc` with `--numa`); they are
Linux-only and a clean no-op elsewhere. Best of all is still a **separate
load-generator machine** — pinning is the single-box approximation of that.
Note that `hyper-bench`'s auto worker/reactor defaults count _logical_ CPUs
(hyperthreads); for a CPU-bound server, peak real throughput is often near the
_physical_ core count, so sweep W both up to and past it and read the curve.

Results land in `benchmarks/http/out/`: **`report.html`** (interactive Plotly
dashboard over the full run history), `report_workers.md`, and
`results_workers.json`. Each worker-sweep cell in `results_workers.json`
carries the usual `throughput_rps` / `p99_ms` / `rss_mb` plus `reactor_count`,
a `contention` block (peak in-flight gauge, native response-count delta, pool
waiters — scraped from the server's own `/metrics`), and, with `--profile`, a
`profile` block (perf `context-switches` / `cache-misses` / `cpu-migrations`,
or a py-spy flamegraph path on macOS). `meta.negative_scaling_flags` holds the
auto-verdict lines. Read the **rps-vs-worker-count plateau** to find where
throughput saturates, and the flagged steps to see _why_. Add `--quick` for a
fast smoke test to validate the setup before the full sweep.

### Measurement-validity rules (learned the hard way on a 2×64-core EPYC)

A worker sweep on a 256-logical-CPU box (2× EPYC 7702, server pinned to socket
0's 64 physical cores, client to socket 1's) produced wildly non-monotonic
curves. Every anomaly traced to a measurement-validity problem, not a server
regression. The harness now guards each one, but they're worth knowing:

- **Set the `performance` cpufreq governor before benchmarking.** Under
  `schedutil` (the Ubuntu default), partially-loaded cores idle near min
  frequency (observed: 1.5 GHz floor, ~2.1 GHz average during a W=32 run on
  a 3.35 GHz part) and the per-cell clock speed becomes a hidden variable that
  differs _per worker count_. The sweep records the governor in the run meta
  and warns when it isn't `performance`:
  `echo performance | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor`

- **Throughput only counts 2xx/3xx.** wrk folds error responses into
  `Requests/sec`. The threaded model **load-sheds** with fast
  `503 + Connection: close` once its backlog cap (`W × 8`) is exceeded, so at
  `c=1024`, W≤64 cells were 23–66% shed responses + ~27k/s of close/reconnect
  churn being reported as "throughput". `throughput_rps` is now the
  2xx/3xx-only service rate (`raw_rps` keeps the wire rate), and non-2xx +
  socket-error counts are first-class columns.

- **Starvation is invisible to wrk — detect it with Little's law.** At
  `c=1024`, W=128 the shed cap (128×8=1024) admits every connection, 128 get
  served and ~896 starve forever — and wrk reports _zero_ errors and a
  beautiful p99 (only served requests have latencies). The tell:
  `rps × mean latency = connections actually in service` (Little's law) came
  out at exactly 128 of 1024. Cells now carry `served_frac`, and the verdict
  flags anything under 90%.

- **Overload cells are bistable — don't read them as capacity.** Driving the
  threaded model with far more connections than it can serve creates a
  shed/reconnect feedback loop with the closed-loop client; identical configs
  measured 162k one run and 444k another. Those cells characterize _overload
  behavior_, not capacity. For capacity, keep `c` within serving capacity (or
  read the reactor curve, which holds every connection and stays stable).

- **Don't sweep W past the pinned core budget and call it scaling.** W=96/128
  on a 64-core pin measures scheduler oversubscription (reactor: −30% at
  W=96 with the `performance` governor; up to −90% under `schedutil`). Cells
  where `W > server_core_budget` are now tagged `oversubscribed` and the
  verdict names it.

- **Keep worker threads off SMT siblings of busy cores.** Reactor W=96 on
  64 physical + 32 SMT-sibling CPUs (same socket) measured 93k vs 313k on the
  64 physical cores alone. Prefer whole physical cores for the server; leave
  siblings idle.

- **Comparison frameworks must get TCP_NODELAY.** `python -m uvicorn
--workers N` pre-binds its shared listen socket with `proto=0`; accepted
  sockets inherit that, asyncio's `_set_nodelay()` silently skips them, and
  every response then stalls ~40 ms on Nagle + delayed ACK (the classic
  two-small-writes pattern). Result: FastAPI flat at ~24.6k rps
  (= 1024 conns / 41 ms) with p99 pinned at ~43 ms _regardless of worker
  count_ — a pure artifact. The harness launches FastAPI through
  `benchmarks/http/apps/uvicorn_main.py`, which binds a proto-correct socket.
  Sanity check for any framework: single-connection latency (`wrk -t1 -c1`)
  must be sub-millisecond, never ~40 ms. (Flask/gunicorn was clean: 297 µs.)

- **Verify the client isn't the ceiling.** During a 478k rps reactor run the
  64 pinned client cores were 84% idle — headroom confirmed. Re-check with
  `mpstat -P <client-cores>` whenever peak numbers approach a suspicious
  plateau shared across configs.
