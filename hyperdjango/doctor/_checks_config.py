"""Doctor checks: Configuration validation."""

import os
from urllib.parse import urlparse

from hyperdjango.doctor._registry import (
    CheckResult,
    CheckStatus,
    DoctorContext,
    doctor_check,
)

_POSTGRES_SCHEMES = frozenset({"postgres", "postgresql"})

# Auto-tune bounds mirror the native pool sizer in zig/src/db.zig
# (_db_configure): an explicit POOL_SIZE in 1..1024 is honored as-is; a
# 0/out-of-range value auto-tunes to min(max(cpu*2, 32), 128). The floor of
# 32 exists because pg.zig pins one connection per HTTP worker thread for the
# process lifetime.
_POOL_AUTOTUNE_FLOOR = 32
_POOL_AUTOTUNE_CAP = 128


def _effective_pool_size() -> tuple[int, bool]:
    """Return (effective_pool_size, is_auto) using the SAME formula the native
    layer uses, resolved from the effective POOL_SIZE setting (Django override
    > HYPER_POOL_SIZE env > default), NOT the raw DEFAULTS dict.

    Returning both formulas from one place keeps the two pool checks in sync.
    """
    from hyperdjango.conf import get_setting

    pool_size = int(get_setting("POOL_SIZE", 0) or 0)
    if 0 < pool_size <= 1024:
        return pool_size, False
    cpu_count = os.cpu_count() or 4
    effective = min(max(cpu_count * 2, _POOL_AUTOTUNE_FLOOR), _POOL_AUTOTUNE_CAP)
    return effective, True


@doctor_check("config", "database_url_format", order=10)
def check_database_url(ctx: DoctorContext) -> list[CheckResult]:
    from hyperdjango.conf import resolve_database_url

    # Fall back to the single connection-URL authority (honors DATABASE_URL,
    # HYPER_DATABASE_URL, and the libpq PG* set) — the same value the server
    # and CLI use — so the doctor checks the URL that will actually be used.
    url = ctx.database_url or resolve_database_url()
    if not url:
        return [
            CheckResult(
                name="database_url_format",
                category="config",
                status=CheckStatus.SKIP,
                message="No DATABASE_URL configured",
            )
        ]

    parsed = urlparse(url)
    if parsed.scheme in _POSTGRES_SCHEMES:
        return [
            CheckResult(
                name="database_url_format",
                category="config",
                status=CheckStatus.PASS,
                message="DATABASE_URL format valid",
                detail=f"{parsed.scheme}://{parsed.hostname}:{parsed.port or 5432}/{parsed.path.lstrip('/')}",
            )
        ]
    return [
        CheckResult(
            name="database_url_format",
            category="config",
            status=CheckStatus.FAIL,
            message=f"Invalid scheme: {parsed.scheme}",
            hint="Use postgres:// or postgresql:// scheme",
        )
    ]


@doctor_check("config", "pool_size_vs_cpu", order=20)
def check_pool_size(ctx: DoctorContext) -> list[CheckResult]:
    effective, is_auto = _effective_pool_size()
    cpu_count = os.cpu_count() or 4

    status = CheckStatus.PASS
    hint = ""
    if effective > cpu_count * 4:
        status = CheckStatus.WARN
        hint = f"Pool size {effective} is very high for {cpu_count} cores"
    elif effective < 2:
        status = CheckStatus.WARN
        hint = "Pool size too small for concurrent requests"

    return [
        CheckResult(
            name="pool_size_vs_cpu",
            category="config",
            status=status,
            message=f"Pool size: {effective} ({'auto' if is_auto else 'manual'}) for {cpu_count} cores",
            hint=hint,
        )
    ]


@doctor_check("config", "pool_size_vs_thread_count", order=25)
def check_pool_size_vs_thread_count(ctx: DoctorContext) -> list[CheckResult]:
    """Warn if pool_size is undersized for the configured thread count.

    pg.zig's thread-owned slot fast path (see zig/src/db.zig
    `acquireConnByHandle` → `tryThreadOwned`) pins one connection per
    Zig HTTP worker thread for the lifetime of the process. If
    pool_size < thread_pool_size, the excess worker threads block
    forever in pool.acquire with no wakeup path (slot-holders never
    release). Even if the Zig `_db_configure` auto-tune floor (task
    #186) guarantees pool_size ≥ 32, users who explicitly set
    HYPER_POOL_SIZE=N below their thread count can still hit this.
    """
    from hyperdjango.conf import get_setting

    thread_pool_size = int(get_setting("THREAD_POOL_SIZE", 24) or 24)
    # Compute effective pool size the same way _db_configure does, via the
    # shared helper so this check and pool_size_vs_cpu never disagree.
    effective, _is_auto = _effective_pool_size()

    # Need pool headroom above worker thread count for debug
    # endpoints, pool heartbeat, auto-tuner, and background tasks.
    required_minimum = thread_pool_size + 1
    comfortable_minimum = thread_pool_size + 8

    if effective < required_minimum:
        return [
            CheckResult(
                name="pool_size_vs_thread_count",
                category="config",
                status=CheckStatus.FAIL,
                message=(
                    f"Pool size {effective} < thread_pool_size "
                    f"{thread_pool_size} — PATHOLOGICAL"
                ),
                detail=(
                    "pg.zig pins one connection per worker thread for "
                    "the process lifetime. Excess worker threads will "
                    "block forever in pool.acquire with no wakeup path."
                ),
                hint=(
                    f"Set HYPER_POOL_SIZE >= {comfortable_minimum} "
                    f"(or raise thread_pool_size floor)"
                ),
            )
        ]
    if effective < comfortable_minimum:
        return [
            CheckResult(
                name="pool_size_vs_thread_count",
                category="config",
                status=CheckStatus.WARN,
                message=(
                    f"Pool size {effective} is tight vs thread_pool_size "
                    f"{thread_pool_size}"
                ),
                detail=(
                    "No headroom for debug endpoints, pool heartbeat, "
                    "auto-tuner, or background tasks — any of them "
                    "could cause per-request contention."
                ),
                hint=(
                    f"Consider raising HYPER_POOL_SIZE to "
                    f"{comfortable_minimum} for comfortable headroom"
                ),
            )
        ]
    return [
        CheckResult(
            name="pool_size_vs_thread_count",
            category="config",
            status=CheckStatus.PASS,
            message=(
                f"Pool size {effective} is adequate for "
                f"thread_pool_size {thread_pool_size}"
            ),
            detail=f"Headroom: {effective - thread_pool_size} slots above worker threads",
        )
    ]


@doctor_check("config", "cache_ttl", order=30)
def check_cache_ttl(ctx: DoctorContext) -> list[CheckResult]:
    from hyperdjango.conf import DEFAULT_CACHE_TTL

    return [
        CheckResult(
            name="cache_ttl",
            category="config",
            status=CheckStatus.PASS,
            message=f"Cache TTL: {DEFAULT_CACHE_TTL}s",
        )
    ]


@doctor_check("config", "connect_timeout", order=40)
def check_connect_timeout(ctx: DoctorContext) -> list[CheckResult]:
    from hyperdjango.conf import get_setting

    timeout_ms = int(get_setting("CONNECT_TIMEOUT", 10000) or 10000)
    status = CheckStatus.PASS
    if timeout_ms < 100 or timeout_ms > 300000:
        status = CheckStatus.WARN

    return [
        CheckResult(
            name="connect_timeout",
            category="config",
            status=status,
            message=f"Connect timeout: {timeout_ms}ms",
            hint="" if status == CheckStatus.PASS else "Recommended: 100ms-300s",
        )
    ]


# ── Native HTTP server tuning ─────────────────────────────────────────────
# These settings are read by the Zig HTTP layer (see zig/src/py.zig and
# zig/src/server.zig), some of them straight from the environment rather than
# through the conf.py DEFAULTS table. The doctor mirrors those exact defaults.

_DEFAULT_LISTEN_BACKLOG = 4096  # zig/src/py.zig getListenBacklog()
_DEFAULT_SEND_TIMEOUT_MS = 30_000  # zig/src/server.zig getSendTimeoutMs()


def _read_somaxconn() -> int | None:
    """Kernel accept-queue cap. Returns None if it can't be determined."""
    import sys

    if sys.platform == "linux":
        try:
            import pathlib

            return int(pathlib.Path("/proc/sys/net/core/somaxconn").read_text().strip())
        except OSError, ValueError:
            return None
    # darwin / *BSD expose it via sysctl kern.ipc.somaxconn
    import subprocess

    for key in ("kern.ipc.somaxconn", "net.core.somaxconn"):
        try:
            out = subprocess.run(
                ["sysctl", "-n", key],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if out.returncode == 0 and out.stdout.strip():
                return int(out.stdout.strip())
        except OSError, ValueError, subprocess.SubprocessError:
            continue
    return None


@doctor_check("config", "http_server_model", order=50)
def check_http_server_model(ctx: DoctorContext) -> list[CheckResult]:
    """Sanity-check the native HTTP connection model + its shedding cap."""
    from hyperdjango.conf import get_setting

    model = str(get_setting("HTTP_SERVER_MODEL", "reactor") or "reactor").lower()
    if model not in ("reactor", "threaded"):
        return [
            CheckResult(
                name="http_server_model",
                category="config",
                status=CheckStatus.WARN,
                message=f"Unknown HTTP_SERVER_MODEL: {model!r}",
                hint="Use 'reactor' (safe default) or 'threaded' (bounded conns)",
            )
        ]

    if model == "threaded":
        thread_pool_size = int(get_setting("THREAD_POOL_SIZE", 24) or 24)
        max_pending = int(get_setting("HTTP_MAX_PENDING", 0) or 0)
        effective_pending = max_pending if max_pending > 0 else thread_pool_size * 8
        return [
            CheckResult(
                name="http_server_model",
                category="config",
                status=CheckStatus.WARN,
                message=(
                    f"HTTP model 'threaded' — live connections capped at "
                    f"thread_pool_size ({thread_pool_size})"
                ),
                detail=(
                    f"Load-shed backlog ≈ {effective_pending} pending conns "
                    f"(HTTP_MAX_PENDING={max_pending or 'auto'}). Excess "
                    "connections get a fast 503. 'reactor' degrades more "
                    "gracefully under many idle connections."
                ),
                hint="Only use 'threaded' for known-bounded connection counts",
            )
        ]

    return [
        CheckResult(
            name="http_server_model",
            category="config",
            status=CheckStatus.PASS,
            message="HTTP model 'reactor' (safe default — graceful under load)",
        )
    ]


@doctor_check("config", "listen_backlog", order=51)
def check_listen_backlog(ctx: DoctorContext) -> list[CheckResult]:
    """Warn when the listen backlog exceeds the kernel somaxconn (silent clamp)."""
    from hyperdjango.conf import get_setting, is_explicitly_set

    backlog = int(
        get_setting("LISTEN_BACKLOG", _DEFAULT_LISTEN_BACKLOG)
        or _DEFAULT_LISTEN_BACKLOG
    )
    if backlog <= 0:
        backlog = _DEFAULT_LISTEN_BACKLOG

    somaxconn = _read_somaxconn()
    src = "configured" if is_explicitly_set("LISTEN_BACKLOG") else "default"

    if somaxconn is None:
        return [
            CheckResult(
                name="listen_backlog",
                category="config",
                status=CheckStatus.PASS,
                message=f"Listen backlog: {backlog} ({src})",
                detail="Could not read kernel somaxconn to cross-check.",
            )
        ]

    if backlog > somaxconn:
        return [
            CheckResult(
                name="listen_backlog",
                category="config",
                status=CheckStatus.WARN,
                message=(
                    f"Listen backlog {backlog} > somaxconn {somaxconn} — "
                    "kernel silently clamps the accept queue"
                ),
                detail=(
                    "Under a connection storm the accept queue fills at "
                    f"{somaxconn} and drops SYNs before userspace can shed."
                ),
                hint=(
                    "Raise the sysctl (net.core.somaxconn on Linux, "
                    "kern.ipc.somaxconn on macOS) to at least the backlog"
                ),
            )
        ]

    return [
        CheckResult(
            name="listen_backlog",
            category="config",
            status=CheckStatus.PASS,
            message=f"Listen backlog: {backlog} ({src}) ≤ somaxconn {somaxconn}",
        )
    ]


@doctor_check("config", "fd_limit", order=52)
def check_fd_limit(ctx: DoctorContext) -> list[CheckResult]:
    """In reactor mode live connections are bounded by open-fd limit, not the
    pool — warn if RLIMIT_NOFILE is too low to cover the accept backlog plus
    pool + worker headroom."""
    import resource

    from hyperdjango.conf import get_setting

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)

    backlog = int(
        get_setting("LISTEN_BACKLOG", _DEFAULT_LISTEN_BACKLOG)
        or _DEFAULT_LISTEN_BACKLOG
    )
    if backlog <= 0:
        backlog = _DEFAULT_LISTEN_BACKLOG

    pool_effective, _ = _effective_pool_size()
    thread_pool_size = int(get_setting("THREAD_POOL_SIZE", 24) or 24)
    # Rough floor: one fd per queued connection + pool sockets + worker fds,
    # plus a fixed slack for listeners, logs, the native runtime, etc.
    expected = backlog + pool_effective + thread_pool_size + 64

    if soft != resource.RLIM_INFINITY and soft < expected:
        return [
            CheckResult(
                name="fd_limit",
                category="config",
                status=CheckStatus.WARN,
                message=(
                    f"Open-fd limit {soft} < ~{expected} needed for "
                    f"backlog {backlog} + pool {pool_effective} + workers"
                ),
                detail=(
                    "In reactor mode live connections are bounded by fds, not "
                    "the pool. A low limit caps concurrent connections and can "
                    "cause accept() failures under load."
                ),
                hint=(
                    f"Raise the soft limit (ulimit -n / LimitNOFILE) toward "
                    f"{hard if hard != resource.RLIM_INFINITY else 65536}"
                ),
            )
        ]

    soft_disp = "unlimited" if soft == resource.RLIM_INFINITY else str(soft)
    return [
        CheckResult(
            name="fd_limit",
            category="config",
            status=CheckStatus.PASS,
            message=f"Open-fd limit: {soft_disp} (need ~{expected})",
        )
    ]


@doctor_check("config", "send_timeout", order=53)
def check_send_timeout(ctx: DoctorContext) -> list[CheckResult]:
    """Sanity-check the slow-client socket send timeout (SEND_TIMEOUT_MS)."""
    from hyperdjango.conf import get_setting, is_explicitly_set

    timeout_ms = int(
        get_setting("SEND_TIMEOUT_MS", _DEFAULT_SEND_TIMEOUT_MS)
        or 0  # 0 = the "disabled" value below; keep the explicit 0 the user set
    )
    if not is_explicitly_set("SEND_TIMEOUT_MS"):
        return [
            CheckResult(
                name="send_timeout",
                category="config",
                status=CheckStatus.PASS,
                message=f"Send timeout: {timeout_ms}ms (default)",
            )
        ]

    if timeout_ms == 0:
        return [
            CheckResult(
                name="send_timeout",
                category="config",
                status=CheckStatus.WARN,
                message="Send timeout disabled (0 = unbounded)",
                detail="Slow/stuck clients can pin a worker's send indefinitely.",
                hint="Set HYPER_SEND_TIMEOUT_MS to a positive value (e.g. 30000)",
            )
        ]

    return [
        CheckResult(
            name="send_timeout",
            category="config",
            status=CheckStatus.PASS,
            message=f"Send timeout: {timeout_ms}ms",
        )
    ]


@doctor_check("config", "ephemeral_port_overlap", order=60)
def check_ephemeral_port_overlap(ctx: DoctorContext) -> list[CheckResult]:
    """The server's fixed port must not sit inside the kernel's EPHEMERAL
    port range without a reservation — the kernel then hands it out as an
    outbound SOURCE port and bind() randomly fails with EADDRINUSE. Stock
    ranges (32768-60999) never overlap common server ports; the failure mode
    appears on boxes tuned for load generation (range widened to 1024-65535)
    and presents as nondeterministic startup failures under load."""
    from pathlib import Path

    from hyperdjango.conf import get_setting

    range_path = Path("/proc/sys/net/ipv4/ip_local_port_range")
    if not range_path.exists():
        return [
            CheckResult(
                name="ephemeral_port_overlap",
                category="config",
                status=CheckStatus.SKIP,
                message="ephemeral port range not inspectable (non-Linux)",
            )
        ]
    try:
        lo_s, hi_s = range_path.read_text().split()
        reserved = (
            Path("/proc/sys/net/ipv4/ip_local_reserved_ports").read_text().strip()
        )
        port = int(get_setting("PORT", 8000) or 8000)
    except OSError, ValueError:
        return [
            CheckResult(
                name="ephemeral_port_overlap",
                category="config",
                status=CheckStatus.SKIP,
                message="ephemeral port range unreadable",
            )
        ]
    lo, hi = int(lo_s), int(hi_s)

    def _is_reserved(p: int) -> bool:
        for part in reserved.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                a, b = part.split("-", 1)
                if int(a) <= p <= int(b):
                    return True
            elif int(part) == p:
                return True
        return False

    overlapped = lo <= port <= hi and not _is_reserved(port)
    return [
        CheckResult(
            name="ephemeral_port_overlap",
            category="config",
            status=CheckStatus.WARN if overlapped else CheckStatus.PASS,
            message=(
                f"server port {port} is INSIDE the ephemeral range {lo}-{hi} "
                "and not reserved — bind() can randomly fail with EADDRINUSE"
                if overlapped
                else f"server port {port} clear of ephemeral range {lo}-{hi}"
            ),
            hint=(
                f"sudo sysctl -w net.ipv4.ip_local_reserved_ports={port} "
                "(and persist it in /etc/sysctl.d/)"
                if overlapped
                else ""
            ),
        )
    ]
