# Where to pick this up

A handoff written at the end of a long stabilization cycle, while the reasoning
was still fresh. It records what is settled, what is deliberately unfinished,
and the traps that cost days — so resuming does not start with archaeology.

Everything here is a suggestion, not a queue. Nothing is blocking.

---

## What just landed

The cycle ended with CI green after a long red streak, a full-matrix benchmark
record, and the bundled apps promoted from "examples" to production **services**
with a one-command runner and systemd units.

The stabilization was one finding, not many: **a single defect replicated across
dozens of test files** — a fixed `sleep` standing in for a condition nobody
stated. Each CI run rolled dice against all of them, which is why a different
test failed every time and why fixing them one per cycle never converged. The
platform was correct in every case examined; the tests were measuring machine
speed. Three genuine platform bugs surfaced only once those sleeps stopped
hiding them (arbitrary same-tick ordering in timeline history, task results
published before their counters/DLQ/hooks, and a hot-reloader that could not
report whether it was watching).

The lesson generalized: **when the same class of failure recurs, stop fixing
instances and go find the population.** A checker that finds all of them at once
turns an endless streak into one commit.

---

## Open threads, most valuable first

### 1. Reload semantics for the systemd units

The generated units are **restart-only**, deliberately: `SIGHUP` appears nowhere
in the tree, so emitting an `ExecReload` would have shipped a directive that
silently does nothing. Implementing genuine graceful reload — re-reading config
and rebuilding the router without dropping connections — is a real feature, not
a cleanup. It touches `HyperApp` construction, the router trie, the pool, and
the Zig worker pool from a signal context, so it deserves a design pass rather
than an afternoon.

Start at `zig/src/server.zig`'s signal handling (TERM/INT today) and
`hyperdjango/services_systemd.py`, which documents why the directive is absent.

### 2. The reactor's large-payload scaling shape

Measured and attributed, not mysterious: at large payloads the worker ladder
loses throughput to **per-request allocation** plus the **cross-socket
interconnect** under the default NUMA-split pin. The response-body copy that
looked like the obvious culprit was measured and deliberately kept — the
removable part is tens of nanoseconds against a request measured in hundreds of
microseconds, and the rationale plus numbers sit in a comment at the call site.

If this is picked up, the honest next step is a same-NUMA-node measurement to
remove the interconnect from the picture first (`docs/benchmarks.md` has the
recipe and the caveat), then decide whether anything remains worth chasing.

### 3. `benchmarks/core` still has two archive writers

The HTTP archive and the cross-suite archive have separate `save_run`
implementations. They were deliberately **not** unified — different schemas,
call shapes, and consumers — but they now express the same doctrine
(complete-vs-diagnostic routing) in two places, which is a slow drift risk.
Worth revisiting only if a third suite appears; two is not yet a pattern.

### 4. Dormancy risks worth a five-minute check on return

- **The pinned Zig download URL.** The proven `git clone` → `make bootstrap` →
  running service path depends on it. If upstream reorganizes old releases, that
  path breaks for every new user and nothing will announce it. Verify before
  trusting the runbook.
- **Dependency drift.** Free-threaded Python, a development Django, and a Zig
  release all move independently. `uv run hyper doctor` is the fastest signal.
- **`CLAUDE.md` is gitignored**, so its conventions never reach contributors.
  The parts that are universal engineering doctrine were copied into
  `CONTRIBUTING.md` and `docs/`; the rest is deliberately local. If it has grown
  again, consider what else deserves promoting.

---

## Traps that cost days (do not rediscover these)

Each is now documented where it will be found; this is the index.

- **`threading.local` WRITES serialize process-wide** under free-threading —
  hundreds of times slower than a plain attribute on a thread-owned object, and
  enough to cap the whole reactor when done per request. Also: function-body
  imports on a hot path convoy on the import lock, and one shared counter is a
  cache line every core fights over. → `docs/profiling.md`, "Free-threaded
  performance rules".
- **Benchmark numbers lie in specific, learnable ways**: a Little's-law
  "served fraction" charges the *client's* turnaround to the server once the
  server outruns the load generator; large-payload cells under a NUMA-split pin
  measure the interconnect; and a run measured right after the full test suite
  is not comparable to one measured on a quiet machine. → `docs/benchmarks.md`.
- **A benchmark result compares everything against everything.** Partial runs
  are archived as diagnostics and are structurally invisible to the comparison
  history, so they cannot quietly become a baseline. If a run seems to be
  missing from the dashboard, that is why.
- **Never overwrite a mapped `.so` in place.** The extension is `mmap`'d by
  every process that imported it; rewriting it swaps executable pages under
  running code and produces a segfault whose instruction pointer lands in the
  ELF string table. The build installs atomically via rename; keep it that way.
- **Assertions wait for conditions, never for the clock.** Enforced by a gate;
  the reasoning and the fix pattern are in `CONTRIBUTING.md`.

---

## Cheap verification recipes

- **Reproduce a CI failure without guessing.** The workflow uploads the test
  logs as an artifact; download it and read the failing subprocess log rather
  than inferring from the summary. This turns a 30-minute guess-and-push cycle
  into a single fetch.
- **Emulate the CI runner before pushing.** On a Linux box, pin the suite to two
  cores *and* select the CI concurrency profile — both, or the emulation is
  wildly harsher than reality and produces failures that are purely artifacts of
  the harness. Repeat runs matter: a one-in-three flake needs several passes to
  surface.
- **All remote operations go through `scripts/box.py`** (host from the
  `HYPER_BOX` environment variable — never hardcoded). Jobs are tmux-managed and
  marker-wrapped, so they survive a dropped session and can be re-attached.

---

## Ideas worth considering, in rough order of appeal

1. **A flake-hunting mode for the test runner.** The infrastructure now exists to
   prove a fix statistically (repeat a selection, report the pass count) but it
   lives in ad-hoc invocations. Making it a first-class flag would make "prove
   this is not flaky" a one-liner.
2. **Extend the timing gate's reach.** It catches a fixed sleep followed closely
   by an assertion. It does not catch an assertion further downstream, or a
   flake with no sleep at all. Widening it carefully — with the false-positive
   rate watched — would close more of the same class.
3. **Push the services further as reference architecture.** They are already
   production-quality and one command away from running; the missing piece is a
   narrative that walks a reader through *why* each one is built the way it is.
   That is the difference between "sample code" and "how to build this well".
4. **Native metrics for the remaining subsystems.** The pattern is established
   and cheap when disabled; the gaps are where nobody has needed the signal yet.

---

## A note on method

The things that worked in this cycle, offered because they were not obvious at
the start:

- **Get the evidence before forming the theory.** Several confident diagnoses
  here were wrong, and each time the artifact or a deterministic reproduction
  settled it in minutes.
- **Build the instrument before trusting it.** A measurement that cannot detect
  a known injected effect cannot validate a fix. One microbenchmark reported an
  allocation as free because the compiler had eliminated it.
- **A negative result is a result.** Two investigations correctly ended in "do
  not change this, here is the measurement, here is the note so nobody asks
  again". That is cheaper than a plausible change nobody can defend.
