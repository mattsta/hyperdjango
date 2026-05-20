"""Machine-capacity detection and self-scaling worker count — the Python
mirror of the native server's equations in ``zig/src/server.zig``.

The native HTTP server sizes its worker pool from usable CPU capacity when
the operator hasn't pinned ``HYPER_THREAD_POOL_SIZE``. Anything on the Python
side that must be sized to match the running server — chiefly the database
connection pool, which needs one connection per worker that pins one — has to
resolve the SAME number, or a big machine runs (say) 128 server workers
against a 24-connection pool and DB-backed handlers fail with an
undersized-pool error.

So this module reproduces the native equation exactly. The constants below
MUST equal their Zig counterparts (``DEFAULT_POOL_SIZE``,
``WORKER_AUTO_CEILING``, ``WORKER_HARD_MAX``); ``test_capacity_scaling`` pins
that lockstep. The env vars read here (``HYPER_THREAD_POOL_SIZE``,
``HYPER_CPU_BUDGET``) are the NATIVE server's own env contract — the Zig side
reads them directly at startup — so this is a deliberate native-boundary
mirror, not framework runtime configuration (which goes through get_setting).
"""

from __future__ import annotations

import math
import os

# Lockstep with zig/src/server.zig — see module docstring.
WORKER_AUTO_MIN = 24  # DEFAULT_POOL_SIZE: floor; never size below the historic default
WORKER_AUTO_CEILING = 512  # auto never exceeds this; an explicit override may
WORKER_HARD_MAX = 4096  # clamp for an explicit override (guards a fat-finger)


def detect_cores() -> int:
    """Usable core count. Prefers the affinity-aware count so that inside a
    cpuset/cgroup (a container pinned to N of many host cores) it reports N —
    matching the native side's ``sched_getaffinity``-based detection."""
    # dynamic-attr: os.sched_getaffinity is a platform-conditional stdlib attribute — absent on macOS/Windows — so its presence must be probed, not assumed.
    affinity = getattr(os, "sched_getaffinity", None)
    if affinity is not None:
        try:
            return max(len(affinity(0)), 1)
        except OSError:
            pass
    return os.cpu_count() or WORKER_AUTO_MIN


def _scale(cores: int, frac: float) -> int:
    """Scale ``cores`` by a fraction; only finite fractions in (0, 1] apply,
    anything else falls back to all cores. Result floored at 1."""
    if not math.isfinite(frac) or frac <= 0 or frac > 1.0:
        return cores
    return max(math.ceil(cores * frac), 1)


def cpu_budget() -> int:
    """How much of the detected machine to use. Unset = all cores. A fraction
    (``0.5``) or percent (``50%``) scales the core count; a bare integer
    (``8``) is an absolute core budget."""
    cores = detect_cores()
    # env-boundary: HYPER_CPU_BUDGET is the native server's own env contract (the Zig side reads it at startup); mirrored here to keep pool sizing coherent, not framework config.
    raw = os.environ.get("HYPER_CPU_BUDGET")
    if not raw:
        return cores
    raw = raw.strip()
    if raw.endswith("%"):
        try:
            return _scale(cores, float(raw[:-1]) / 100.0)
        except ValueError:
            return cores
    if "." in raw:
        try:
            return _scale(cores, float(raw))
        except ValueError:
            return cores
    try:
        n = int(raw)
    except ValueError:
        return cores
    return cores if n <= 0 else min(n, cores)


def auto_workers(budget: int | None = None) -> int:
    """Auto worker count from the CPU budget: at least the historic default
    (so small machines are unchanged), at most the auto ceiling."""
    b = cpu_budget() if budget is None else budget
    return min(max(b, WORKER_AUTO_MIN), WORKER_AUTO_CEILING)


def resolve_worker_count() -> int:
    """The worker count the native server will actually run: an explicit
    ``HYPER_THREAD_POOL_SIZE`` (the same env the Zig server reads) wins; else
    the capacity-scaled auto value. This is the number pool sizing must match.
    """
    # env-boundary: HYPER_THREAD_POOL_SIZE is the native server's env contract — the Zig getPoolSize reads exactly this; mirrored so pool sizing matches the running server.
    raw = os.environ.get("HYPER_THREAD_POOL_SIZE")
    if raw:
        try:
            n = int(raw)
        except ValueError:
            n = 0
        if n > 0:
            return min(n, WORKER_HARD_MAX)
    return auto_workers()
