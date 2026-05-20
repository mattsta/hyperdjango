"""
Unified test runner for HyperDjango.

Usage:
    uv run hyper-test                    # Run all tests
    uv run hyper-test rest               # Run tests matching "rest"
    uv run hyper-test rest serializers   # Multiple patterns
    uv run hyper-test --list             # List tests with classification
    uv run hyper-test --scripts-only     # Only scripts/test_*.py
    uv run hyper-test --pytest-only      # Only tests/ pytest suites
    uv run hyper-test --serial           # Force sequential execution

Classifies each scripts/test_*.py by its declared `# hyper-test: <kind>`
marker (unit/db_isolated/db_django/db_shared/e2e). A file with no marker is a
loud error — the runner never guesses a kind from file content.
Each DB test gets its own isolated database (created empty, dropped after).
All independent tests run concurrently via asyncio subprocess scheduling.
Ctrl-C kills all child processes and cleans up databases.
"""

import asyncio
import atexit
import contextlib
import datetime as _dt
import json
import os
import platform
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from hyperdjango.logging import logger

# ── Per-run session logging ───────────────────────────────────────────────
#
# Every `hyper-test` invocation AUTOMATICALLY writes a full mirror of its
# stdout + stderr to `logs/test_runs/<timestamp>_<slug>.log`. This lets
# operators and AI assistants (a) see live output, (b) inspect the full
# transcript after the run without manually `tee`-ing every invocation,
# and (c) have a durable artifact per run for CI debugging.
#
# The log filename is printed to stdout at the START of every run so
# follow-up inspection always knows where to look:
#
#     uv run hyper-test rest admin
#     → Session log: logs/test_runs/20260411_153045_rest_admin.log
#     ...
#
# Rules:
#  - NEVER pipe or tee `hyper-test` output manually. Just run it and
#    read the printed log path after.
#  - The log captures the complete session: classification output,
#    per-file results, failures, diagnostics, summary — everything.
#  - Log files are never rotated or deleted by the runner; clean up
#    `logs/test_runs/` periodically if you care about disk.


@dataclass(slots=True)
class _LogTee:
    """Minimal tee writer: every `.write()` fans out to N streams.

    Used to mirror stdout / stderr to a per-session log file while
    still flowing to the terminal. Flushed eagerly so the log file
    reflects the live state of the run (useful when a test hangs).

    `isatty()` returns False so the logging layer does not try to
    emit ANSI color codes into the log file.
    """

    _streams: tuple

    def write(self, data: str) -> int:
        n = 0
        for stream in self._streams:
            try:
                n = stream.write(data)
                stream.flush()
            # blind-except: one broken stream (closed pipe, full disk) must not kill tee'd output to the other streams.
            except Exception:
                # One broken stream must not kill output to the others.
                pass
        return n

    def flush(self) -> None:
        for stream in self._streams:
            with contextlib.suppress(Exception):
                stream.flush()

    def isatty(self) -> bool:
        return False


def _derive_session_slug(args: list[str]) -> str:
    """Derive a filesystem-safe slug for the session log filename.

    Examples:
      []                   → "all"
      ["--list"]           → "list"
      ["rest"]             → "rest"
      ["rest", "admin"]    → "rest_admin"
      ["rest", "--serial"] → "rest_serial"
    """
    cleaned: list[str] = []
    for a in args:
        token = a.lstrip("-").strip()
        if not token:
            continue
        # Keep only [a-zA-Z0-9_] to avoid path injection / weirdness.
        safe = re.sub(r"[^a-zA-Z0-9_]+", "_", token).strip("_")
        if safe:
            cleaned.append(safe)
    if not cleaned:
        return "all"
    slug = "_".join(cleaned)
    # Cap length — very long slugs are unhelpful filenames.
    return slug[:80]


def _open_session_log(args: list[str]) -> tuple[Path, object]:
    """Create and open the per-session log file.

    Returns (path, file handle). The caller is responsible for
    installing a `_LogTee` over sys.stdout / sys.stderr.
    """
    # Anchor at the project root (same convention as the rest of the
    # test runner machinery).
    project_dir = Path(__file__).parent.parent
    logs_dir = project_dir / "logs" / "test_runs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    timestamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _derive_session_slug(args)
    log_path = logs_dir / f"{timestamp}_{slug}.log"
    log_file = log_path.open("w", buffering=1)  # noqa: SIM115 — returned to caller; line-buffered
    return log_path, log_file


# Per-test timeouts. E2E tests need more time: they start servers, seed data,
# and run HTTP/WebSocket checks which compete for CPU/DB under parallel load.
# Per-file ceilings. Under parallel CPU contention with 24+ test
# workers running simultaneously (free-threaded Python 3.14t), heavy
# Hypothesis fuzz suites and large RBAC CRUD tests legitimately take
# 90+ seconds even after example-count halving — the OS scheduler is
# the bottleneck, not the test logic. Lifted from 90 → 180 so the
# ceiling never trips on a test that would otherwise pass; serial
# runs are unaffected because fast tests still finish fast.
TEST_TIMEOUT_SECONDS = 180
E2E_TIMEOUT_SECONDS = 240

# Concurrency cap for files marked `# hyper-test-concurrency: low`. Such files
# acquire this dedicated semaphore INSTEAD of their kind's semaphore, so a
# starvation-sensitive stress file runs against at most this many peers
# regardless of how wide its kind is otherwise scheduled.
LOW_CONCURRENCY_LIMIT = 2
# The only accepted value for the `# hyper-test-concurrency:` marker.
_CONCURRENCY_LOW = "low"

# ── Database connection boundary ──────────────────────────────────────────

# Sanctioned env boundary: test tooling. The runner reads PG*/HYPER_TEST_* to
# configure its OWN behavior (which DB to spin per-suite databases in, CI
# presets, log toggles) and injects env into the test subprocesses it spawns —
# it is not the framework's runtime config path. Allowlisted in
# scripts/check_no_os_environ.py.
_DB_USER = os.environ.get("PGUSER") or os.environ.get("USER", "postgres")
_DB_PASSWORD = os.environ.get("PGPASSWORD", "")
_DB_HOST = os.environ.get("PGHOST", "localhost")
_DB_PORT = os.environ.get("PGPORT", "")
_DB_AUTH = f"{_DB_USER}:{_DB_PASSWORD}@" if _DB_PASSWORD else f"{_DB_USER}@"
_DB_HOST_PORT = f"{_DB_HOST}:{_DB_PORT}" if _DB_PORT else _DB_HOST

# The fixed server ports every e2e test binds (scripts/e2e_helper.TEST_PORTS
# plus bench harness ports all live here).
_TEST_PORT_RANGE = (18000, 19999)


# Lines of faulthandler output to keep from a hung child. Enough for several
# thread stacks; bounded so one wedged file cannot bury the run summary.
_HANG_MARKER = "── where it hung (Python frames at the timeout) ──"
_HANG_TRACE_LINES = 60


def _hang_excerpt(stderr_text: str) -> str:
    """The PYTHON frames faulthandler wrote when a hung child was aborted.

    Order matters and cost a cycle to learn: faulthandler prints the Python
    stack FIRST and a long binary C stack LAST, so keeping the tail keeps
    ``Binary file ... [0x1c5c26c]`` and discards the only thing that names the
    hung test. This anchors on the Python section and drops the C trace, which
    says nothing a Python-level hang needs.
    """
    lines = [ln.rstrip() for ln in stderr_text.splitlines() if ln.strip()]
    if not lines:
        return ""
    start = 0
    for i, line in enumerate(lines):
        if line.startswith("Stack (most recent call first)") or line.startswith(
            "Thread 0x"
        ):
            start = i
            break
    kept: list[str] = []
    for line in lines[start:]:
        if line.startswith("Current thread's C stack trace"):
            break  # binary frames from here down
        kept.append(line)
        if len(kept) >= _HANG_TRACE_LINES:
            break
    if not kept:
        kept = lines[-_HANG_TRACE_LINES:]
    body = "\n".join(f"    │ {ln}" for ln in kept)
    return f"    {_HANG_MARKER}\n{body}"


def _warn_ephemeral_port_overlap() -> None:
    """Suite preflight: fail LOUDLY (as a warning) when the kernel's ephemeral
    port range covers the fixed test-server ports without reserving them.

    Benchmark tuning widens `ip_local_port_range` to `1024 65535` for
    load-generator port capacity; on such a box the kernel hands out
    18xxx/19xxx as ephemeral SOURCE ports for the suite's own outbound
    connections (HTTP clients, DB connects), and e2e servers then randomly
    fail to bind with EADDRINUSE — a nondeterministic set of files failing
    every parallel run, invisible on dev machines whose stock range
    (32768-60999) never overlaps. `ip_local_reserved_ports` is the designed
    fix: reserved ports are skipped for ephemeral allocation but stay
    explicitly bindable."""
    if platform.system() != "Linux":
        return
    try:
        lo_s, hi_s = Path("/proc/sys/net/ipv4/ip_local_port_range").read_text().split()
        reserved = (
            Path("/proc/sys/net/ipv4/ip_local_reserved_ports").read_text().strip()
        )
    except OSError, ValueError:
        return
    t_lo, t_hi = _TEST_PORT_RANGE
    if int(hi_s) < t_lo or int(lo_s) > t_hi:
        return  # ephemeral range does not touch the test ports
    # Accept any reservation that names the test range's bounds; the common
    # correct value is exactly "18000-19999".
    if reserved and f"{t_lo}-{t_hi}" in reserved:
        return
    logger.warning(
        "ip_local_port_range ({lo}-{hi}) covers the fixed test-server ports "
        "({tlo}-{thi}) and ip_local_reserved_ports does not reserve them — "
        "e2e servers WILL randomly fail to bind under parallel load. Fix:\n"
        "    sudo sysctl -w net.ipv4.ip_local_reserved_ports={tlo}-{thi}\n"
        "(and persist it next to the range override in /etc/sysctl.d/)",
        lo=lo_s,
        hi=hi_s,
        tlo=t_lo,
        thi=t_hi,
    )


@dataclass
class TestMeta:
    path: Path
    kind: str  # unit, db_isolated, db_django, db_shared, e2e
    port: int
    app_db: str = ""  # e2e: which app database (for serialization)
    timeout: int = (
        0  # per-file override from `# hyper-test-timeout: N`; 0 = category default
    )
    concurrency: str = (
        ""  # "low" from `# hyper-test-concurrency: low`; "" = kind default
    )
    flaky_reason: str = (
        ""  # non-empty from `# hyper-test-flaky: <reason>`; enables one retry
    )


@dataclass
class TestResult:
    name: str
    passed: int = 0
    failed: int = 0
    total: int = 0
    elapsed_ms: float = 0.0
    error: str = ""
    exit_code: int = 0
    output_log: str = ""  # path to per-subprocess output file
    retries: int = 0  # 1 if a flaky-marked file was rerun (see `# hyper-test-flaky`)
    # True when the file exited 0 but printed no parseable pass/fail counts —
    # it is tallied as ONE passed unit (the file) instead of vanishing behind
    # a "?" row, and listed at the end as a testkit-migration candidate.
    uncounted: bool = False


# ── Classification ────────────────────────────────────────────────────────


_VALID_KINDS = frozenset({"unit", "db_isolated", "db_django", "db_shared", "e2e"})
# Markers users have written that we treat as aliases for canonical
# kinds. `pure` is an older synonym for `unit` (no DB, no server) so we
# honor it. Adding new aliases here is preferred over breaking existing
# test files.
_KIND_ALIASES = {
    "pure": "unit",
}


def classify_test(test_path: Path) -> TestMeta:
    content = test_path.read_text(errors="replace")
    test_name = test_path.stem

    # Optional per-file timeout override, independent of the kind marker:
    # `# hyper-test-timeout: N` (seconds). For a genuinely long-running file
    # (e.g. reconnect/backoff suites that stretch under CI CPU starvation) so it
    # gets more budget than the category default instead of false-timing-out.
    tmo_m = re.search(r"#\s*hyper-test-timeout:\s*(\d+)", content)
    file_timeout = int(tmo_m.group(1)) if tmo_m else 0

    # Optional concurrency cap: `# hyper-test-concurrency: low`. Only `low` is
    # accepted — any other value is a typo that would otherwise be silently
    # ignored and let a starvation-sensitive stress file run at full width, so
    # we fail loudly (same philosophy as an invalid kind marker).
    conc_m = re.search(r"#\s*hyper-test-concurrency:\s*(\w+)", content)
    concurrency = ""
    if conc_m:
        conc_value = conc_m.group(1)
        if conc_value != _CONCURRENCY_LOW:
            raise ValueError(
                f"{test_path.name}: invalid '# hyper-test-concurrency: "
                f"{conc_value}' marker — only '{_CONCURRENCY_LOW}' is supported"
            )
        concurrency = conc_value

    # Optional flaky quarantine: `# hyper-test-flaky: <reason>`. A non-empty
    # reason is mandatory so the marker documents WHY the file is quarantined;
    # an empty/whitespace reason fails loudly rather than silently arming a
    # retry for an undocumented reason. The reason is captured on the marker's
    # own line only — `[^\n]*` never crosses the newline, so an empty reason
    # can't silently absorb the following source line.
    flaky_m = re.search(r"#\s*hyper-test-flaky:([^\n]*)", content)
    flaky_reason = ""
    if flaky_m:
        reason = flaky_m.group(1).strip()
        if not reason:
            raise ValueError(
                f"{test_path.name}: '# hyper-test-flaky:' marker requires a "
                f"non-empty reason"
            )
        flaky_reason = reason

    def _meta(kind: str, port: int = 0, app_db: str = "") -> TestMeta:
        if kind == "db_shared" and concurrency == _CONCURRENCY_LOW:
            # The shared-DB lane is SERIAL (width 1) by design; letting `low`
            # substitute its width-2 semaphore would WIDEN that lane and race
            # shared-DB tests against each other. Reject the combination
            # rather than silently picking one marker over the other.
            raise ValueError(
                f"{test_path.name}: '# hyper-test-concurrency: low' cannot be "
                f"combined with kind 'db_shared' — the shared-DB lane is "
                f"already serial, which is stricter than 'low'"
            )
        return TestMeta(
            path=test_path,
            kind=kind,
            port=port,
            app_db=app_db,
            timeout=file_timeout,
            concurrency=concurrency,
            flaky_reason=flaky_reason,
        )

    # The declared classification marker is the ONLY source of a file's kind:
    # # hyper-test: unit|db_isolated|db_django|db_shared|e2e (aliases resolved).
    override_m = re.search(r"#\s*hyper-test:\s*(\w+)", content)
    if not override_m:
        # No content heuristics: a file's kind is never guessed from what it
        # imports or calls. Every scripts/test_*.py must declare its kind so it
        # is scheduled into the right resource lane — an unmarked file would
        # otherwise silently land in the wrong lane and race in CI. The
        # scripts/check_test_markers.py gate enforces this; fail loudly here so
        # the same rule holds for any caller that reaches classify_test.
        raise ValueError(
            f"{test_path.name}: no '# hyper-test: <kind>' marker — every "
            f"scripts/test_*.py must declare its kind (one of "
            f"{sorted(_VALID_KINDS)} or an alias in {sorted(_KIND_ALIASES)}). "
            f"Add a '# hyper-test: <kind>' line, or run "
            f"`uv run python scripts/check_test_markers.py --fix`."
        )

    kind = override_m.group(1)
    kind = _KIND_ALIASES.get(kind, kind)
    if kind not in _VALID_KINDS:
        # Reject typos and unknown values rather than falling through to the
        # default `sem_unit` semaphore: an unrecognized marker like
        # `# hyper-test: db` would otherwise run with high parallelism on the
        # shared DB and deadlock against other shared-DB tests. Fail the test
        # classification loudly so the developer fixes the marker instead of
        # silently racing in CI.
        raise ValueError(
            f"{test_path.name}: invalid '# hyper-test: {kind}' marker — "
            f"must be one of {sorted(_VALID_KINDS)} or an alias in "
            f"{sorted(_KIND_ALIASES)}"
        )
    if kind == "e2e":
        return _meta("e2e", _extract_port(content), _extract_app_db(content, test_name))
    return _meta(kind)


def _extract_port(content: str) -> int:
    m = re.search(r"port\s*=\s*(\d{4,5})", content)
    return int(m.group(1)) if m else 0


def _extract_app_db(content: str, test_name: str = "") -> str:
    """Return a database group for this E2E test.

    Tests sharing the same app group use the same database and are
    serialized via a semaphore to prevent DDL conflicts.

    Isolated groups get their own fresh database; tests within the same
    isolated group are serialized so the first one (alphabetically) can
    run ``hyper setup --drop --seed`` and the rest reuse those tables.
    """
    # Isolated groups — each gets its own database
    # Check test name first for exact isolation (e.g. "e2e_security" gets its own DB)
    for app in (
        "security",
        "content_hub",
        "forms_demo",
        "full_stack",
        "semantic_search",
        "deployment",
        "bookstore",
        "task_queue",
        "multi_tenant",
        "hypernews",
        "hyperticket",
        "hyperai",
        "rest_api",
        "websocket",
        "blog_platform",
        "cms_lite",
        "metering",
        "notes_api",
        "live_config",
        "hypersecret",
        "hypermanager",
    ):
        if app in test_name:
            return f"isolated:{app}"

    # Content-based app detection for tests that don't have the app name
    # in their filename (e.g., test_e2e_performance uses hypernews)
    for app, markers in (
        ("hypernews", ("services.hypernews", "hypernews.app")),
        ("hyperticket", ("services.hyperticket", "hyperticket.app")),
        ("hyperai", ("services.hyperai", "hyperai.app")),
        ("bookstore", ("services.bookstore_api", "bookstore_api.app")),
        ("blog_platform", ("services.blog_platform", "blog_platform.app")),
        ("cms_lite", ("services.cms_lite", "cms_lite.app")),
        ("metering", ("services.metering_api", "metering_api.app")),
        ("notes_api", ("services.notes_api", "notes_api.app")),
        ("live_config", ("services.live_config", "live_config.app")),
        ("hypersecret", ("services.hypersecret", "hypersecret.app")),
        ("hypermanager", ("services.hypermanager", "hypermanager.app")),
    ):
        if any(m in content for m in markers):
            return f"isolated:{app}"

    # Fall back to content matching
    for app in (
        "content_hub",
        "forms_demo",
        "full_stack",
        "semantic_search",
        "deployment",
        "bookstore",
        "task_queue",
        "multi_tenant",
        "hypernews",
        "hyperticket",
        "hyperai",
        "rest_api",
        "websocket",
        "blog_platform",
        "cms_lite",
        "metering",
        "notes_api",
        "hypersecret",
        "hypermanager",
    ):
        if app in content:
            return f"isolated:{app}"

    # Shared groups — serialized on the default database
    for app in (
        "hello",
        "benchmark",
    ):
        if app in content or app in test_name:
            return app

    return test_name or ""


# ── Database lifecycle ────────────────────────────────────────────────────

_created_dbs: set[str] = set()


def _create_dbs_batch(names: list[str]) -> None:
    """Create all test databases in parallel using threads."""
    import concurrent.futures

    failures: list[tuple[str, str]] = []

    def _create_one(name: str) -> bool:
        r = subprocess.run(["createdb", name], capture_output=True, timeout=30)
        if r.returncode == 0:
            _created_dbs.add(name)
            return True
        failures.append((name, r.stderr.decode("utf-8", "replace").strip()))
        return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(_create_one, names))

    if failures:
        # Print so the failure shows up in CI / hyper-test session log even
        # though tests will continue and individually fail at connect time.
        print(f"\n⚠ createdb failed for {len(failures)} database(s):", flush=True)
        for name, err in failures[:5]:
            print(f"  - {name}: {err or '(no stderr)'}", flush=True)
        if len(failures) > 5:
            print(f"  ... and {len(failures) - 5} more", flush=True)
        print(
            "  Diagnose the PostgreSQL environment: uv run hyper db doctor",
            flush=True,
        )


def _drop_all_test_dbs() -> None:
    """Drop all created test databases. Best-effort cleanup."""
    import concurrent.futures

    def _drop_one(name: str) -> None:
        subprocess.run(["dropdb", "--if-exists", name], capture_output=True, timeout=30)

    names = list(_created_dbs)
    if not names:
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(_drop_one, names))
    _created_dbs.clear()


# ── Subprocess execution ──────────────────────────────────────────────────

_active_procs: set[asyncio.subprocess.Process] = set()


def _parse_result_line(output: str) -> tuple[int, int]:
    for line in reversed(output.splitlines()):
        line = line.strip()
        if "passed" in line and "failed" in line:
            try:
                parts = line.replace(",", " ").split()
                pi = next(i for i, p in enumerate(parts) if p == "passed")
                fi = next(i for i, p in enumerate(parts) if p == "failed")
                return int(parts[pi - 1].split("/")[0]), int(parts[fi - 1])
            except StopIteration, ValueError, IndexError:
                continue
        if "passed" in line and "failed" not in line:
            # `N/M passed` implies M-N failures — deriving them keeps a
            # hand-rolled summary format from silently reporting a failing
            # file as "0 failures (exit 1)" with the real count lost.
            m = re.search(r"(\d+)/(\d+)\s+passed", line)
            if m:
                passed, total = int(m.group(1)), int(m.group(2))
                return passed, max(total - passed, 0)
            # Allow a noun between the count and "passed" ("12 checks passed",
            # "34 tests passed") — hand-rolled summaries predating the testkit
            # harness use these shapes, and missing them turns a fully-counted
            # file into an uncounted "?" row.
            m = re.search(r"(\d+)\s+(?:\w+\s+)?passed", line)
            if m:
                return int(m.group(1)), 0
    return 0, 0


async def _exec_subprocess(
    name: str,
    cmd: list[str],
    cwd: str,
    env: dict[str, str],
    sem: asyncio.Semaphore | None = None,
    error_extract_fn=None,
    timeout: int = TEST_TIMEOUT_SECONDS,
) -> TestResult:
    """Run a subprocess, handle timeout/cleanup, parse results.

    Shared by both script tests and pytest suites. On timeout, sends
    SIGTERM first (allows atexit handlers to clean up server subprocesses),
    then SIGKILL after 3s.
    """
    if sem:
        await sem.acquire()

    start = time.perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
        _active_procs.add(proc)
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except TimeoutError:
            # SIGABRT first, not SIGTERM. Children run under -X faulthandler,
            # whose default handler dumps EVERY thread's stack to stderr before
            # the process dies. A timed-out file is the one case where the
            # subprocess log is never finalized and is simply absent from a CI
            # artifact, so without this the only evidence a timeout leaves is
            # the word TIMEOUT — which says a file hung, not where. The stacks
            # are captured below and reported with the failure.
            hang_trace = ""
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(signal.SIGABRT)
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=10
                )
                hang_trace = _hang_excerpt(stderr_b.decode("utf-8", "replace"))
            except TimeoutError:
                # Wedged past even the abort handler — nothing to collect.
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
            elapsed = (time.perf_counter() - start) * 1000
            return TestResult(
                name=name,
                failed=1,
                total=1,
                elapsed_ms=elapsed,
                exit_code=-1,
                error="TIMEOUT" + (f"\n{hang_trace}" if hang_trace else ""),
            )
        finally:
            # On the normal and timeout paths the child has already exited
            # (returncode is set). On CancelledError (Ctrl-C mid-run) the
            # wait_for raises straight through this finally while the child is
            # still alive — kill it BEFORE untracking, otherwise it orphans
            # (holding ports/DBs) and, being discarded from _active_procs,
            # _kill_all_children can never reap it.
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            _active_procs.discard(proc)

        elapsed = (time.perf_counter() - start) * 1000
        stdout_s = (stdout_b or b"").decode(errors="replace")
        stderr_s = (stderr_b or b"").decode(errors="replace")
        output = stdout_s + stderr_s
        passed, failed = _parse_result_line(output)
        rc = proc.returncode or 0

        # ── Per-subprocess output log ──────────────────────────────
        # Write FULL stdout + stderr to an individual file for every
        # test, not just failures. This is the authoritative record
        # for crash diagnosis (SIGABRT/SIGSEGV produce zero Python
        # output, so the session log's summary is useless without this).
        output_log_path = ""
        try:
            subproc_dir = Path(cwd) / "logs" / "test_runs" / "subprocess"
            subproc_dir.mkdir(parents=True, exist_ok=True)
            # Sanitize the log filename — colons (from pytest test names like
            # "pytest:db") are forbidden in GitHub Actions artifact uploads
            # (and on Windows filesystems generally). Translate any
            # filesystem-hostile char to '__'.
            safe_name = (
                name.replace(":", "__")
                .replace("/", "__")
                .replace("\\", "__")
                .replace("|", "__")
                .replace("*", "__")
                .replace("?", "__")
                .replace('"', "__")
                .replace("<", "__")
                .replace(">", "__")
            )
            output_log_file = subproc_dir / f"{safe_name}.log"
            with output_log_file.open("w") as f:
                f.write(f"# Test: {name}\n")
                f.write(f"# Command: {' '.join(cmd)}\n")
                f.write(f"# Exit code: {rc}")
                if rc < 0:
                    import signal as _sig

                    try:
                        sig_name = _sig.Signals(-rc).name
                    except ValueError, AttributeError:
                        sig_name = f"signal {-rc}"
                    f.write(f" ({sig_name})")
                f.write(f"\n# Elapsed: {elapsed:.0f}ms\n")
                f.write(f"# Passed: {passed}, Failed: {failed}\n\n")
                f.write("=== STDOUT ===\n")
                f.write(stdout_s or "(empty)\n")
                f.write("\n=== STDERR ===\n")
                f.write(stderr_s or "(empty)\n")
            output_log_path = str(output_log_file)
        # blind-except: writing the per-test output log is best-effort; a log-write failure must not crash the runner or lose the result.
        except Exception:
            pass  # Best-effort — don't crash the runner

        # ── Error text for session log summary ────────────────────
        error_text = ""
        if error_extract_fn:
            error_text = error_extract_fn(output, stdout_b, stderr_b, rc, failed)
        elif rc != 0 and failed == 0:
            # Crash or unexpected exit. Include the full combined
            # output (up to 2000 chars) so markers, Zig trace, and
            # faulthandler output are all visible in the session log.
            # Also include the signal name for clarity.
            parts: list[str] = []
            if rc < 0:
                import signal as _sig

                try:
                    sig_name = _sig.Signals(-rc).name
                except ValueError, AttributeError:
                    sig_name = f"signal {-rc}"
                parts.append(f"Killed by {sig_name} (exit {rc})")
            if output_log_path:
                parts.append(f"Full output: {output_log_path}")
            combined = output.strip()
            if combined:
                if len(combined) > 2000:
                    combined = combined[-2000:]
                parts.append(combined)
            error_text = "\n".join(parts)

        # A clean exit with no parseable counts is still a real, passing test
        # file (assert-style scripts crash on failure). Tally it as ONE passed
        # unit so the totals include it, and mark it uncounted so the summary
        # can list it as a testkit-migration candidate — a "?" row that
        # silently contributes zero to every total reads as suspicious and
        # hides the file from the aggregate entirely.
        uncounted = rc == 0 and passed == 0 and failed == 0
        if uncounted:
            passed = 1

        return TestResult(
            name=name,
            passed=passed,
            failed=failed,
            total=passed + failed,
            elapsed_ms=elapsed,
            exit_code=rc,
            error=error_text,
            output_log=output_log_path,
            uncounted=uncounted,
        )
    # blind-except: an unexpected failure running one test subprocess is reported as a failed TestResult so the runner keeps going across the suite.
    except Exception as e:
        elapsed = (time.perf_counter() - start) * 1000
        return TestResult(
            name=name, failed=1, total=1, elapsed_ms=elapsed, exit_code=-1, error=str(e)
        )
    finally:
        if sem:
            sem.release()


def _extract_fail_lines(output, _stdout_b, stderr_b, returncode, failed):
    """Extract FAIL lines from test script or pytest output."""
    if failed > 0:
        # Script tests use "FAIL", pytest uses "FAILED"
        fail_lines = [
            l for l in output.splitlines() if "FAIL" in l and "passed" not in l
        ]
        return "\n".join(fail_lines[-5:])
    if returncode != 0:
        return (stderr_b or b"").decode(errors="replace")[-500:]
    return ""


def _scrub_ambient_database(env: dict[str, str]) -> None:
    """Remove any ambient database locator from a test subprocess's env.

    Tests with no explicitly-assigned database (unit, db_shared) must see the
    SAME clean environment a developer has locally — no ambient locator.
    Otherwise a CI job that exports PGDATABASE/DATABASE_URL for its own setup
    steps leaks a live-but-foreign database into every such subprocess: a
    "unit" test that unexpectedly reaches get_db() then silently connects to
    it (with whatever schema that DB happens to have) instead of taking the
    deterministic "no database configured" path. db_shared tests are
    unaffected — they fall back to their hardcoded ``hyperdjango_test`` when
    PGDATABASE is unset, exactly as they do on a developer machine.
    """
    for locator in ("DATABASE_URL", "HYPER_DATABASE_URL", "PGDATABASE"):
        env.pop(locator, None)


async def _run_test(
    test_path: Path,
    python: str,
    env_override: dict[str, str] | None = None,
    sem: asyncio.Semaphore | None = None,
    timeout: int = TEST_TIMEOUT_SECONDS,
) -> TestResult:
    """Run a single test script as an async subprocess."""
    name = test_path.stem.replace("test_", "")
    env = os.environ.copy()
    env["HYPER_TEST_PARALLEL"] = "1"
    env.setdefault("HYPER_DEBUG", "1")  # Tests run in debug mode
    # Bounded server capacity for every test subprocess. Servers self-scale
    # workers + DB pool to the machine's usable cores; the parallel suite runs
    # dozens of servers at once (AppRunner subprocesses AND in-process
    # app.listen threads), so on a many-core box each would size itself as if
    # it owned the whole machine — observed on a 256-core box as thousands of
    # threads + hundreds of DB connections per app and a nondeterministic set
    # of ~40 e2e servers failing startup every run; all green when capped.
    # Budget 8 resolves to the historic 24-worker floor. Tests that measure
    # capacity/scaling set their own HYPER_THREAD_POOL_SIZE (an explicit pin
    # always beats the budget) or override HYPER_CPU_BUDGET.
    env.setdefault("HYPER_CPU_BUDGET", "8")
    # Stable secrets for test subprocesses (overrides random per-session defaults)
    env.setdefault("HYPER_API_KEY", "test-api-key")
    env.setdefault("HYPER_ADMIN_SECRET", "test-admin-secret")
    env.setdefault("HYPER_CSRF_SECRET", "test-csrf-secret")
    env.setdefault("HYPER_SESSION_SECRET", "test-session-secret")
    env.setdefault(
        "HYPER_SESSION_SIGNING_KEY", "test-session-signing-key-for-tests-only"
    )
    env.setdefault("HYPER_SEED_PASSWORD", "test-seed-password")
    env.setdefault("HYPER_ADMIN_PASSWORD", "test-admin-password")
    if env_override:
        # Tests that get an explicit database (db_isolated / db_django /
        # isolated e2e) receive it here — leave their locator untouched.
        env.update(env_override)
    else:
        _scrub_ambient_database(env)

    return await _exec_subprocess(
        name=name,
        # `-X faulthandler`: on a NATIVE fatal fault (SIGSEGV/SIGABRT/SIGBUS/
        # SIGFPE from the Zig extension) Python dumps the traceback of EVERY
        # thread — including the offload/executor thread that was inside the
        # crashing native call — instead of the subprocess vanishing with empty
        # output. This is the one observability handle that turns an untraceable
        # native crash into "which test, which _db_/native call, which thread".
        cmd=[python, "-X", "faulthandler", str(test_path)],
        cwd=str(test_path.parent.parent),
        env=env,
        sem=sem,
        error_extract_fn=_extract_fail_lines,
        timeout=timeout,
    )


async def _run_pytest_subprocess(
    suite: dict[str, object],
    project_dir: Path,
    python: str,
    env_override: dict[str, str] | None = None,
    sem: asyncio.Semaphore | None = None,
) -> TestResult:
    """Run a pytest suite as an async subprocess."""
    name = str(suite["name"])
    suite_path = project_dir / str(suite["path"])
    if not suite_path.exists():
        return TestResult(name=name, error=f"Path not found: {suite['path']}")

    env = os.environ.copy()
    env["HYPER_TEST_PARALLEL"] = "1"
    env.setdefault("HYPER_DEBUG", "1")  # Tests run in debug mode
    # Bounded server capacity for every test subprocess. Servers self-scale
    # workers + DB pool to the machine's usable cores; the parallel suite runs
    # dozens of servers at once (AppRunner subprocesses AND in-process
    # app.listen threads), so on a many-core box each would size itself as if
    # it owned the whole machine — observed on a 256-core box as thousands of
    # threads + hundreds of DB connections per app and a nondeterministic set
    # of ~40 e2e servers failing startup every run; all green when capped.
    # Budget 8 resolves to the historic 24-worker floor. Tests that measure
    # capacity/scaling set their own HYPER_THREAD_POOL_SIZE (an explicit pin
    # always beats the budget) or override HYPER_CPU_BUDGET.
    env.setdefault("HYPER_CPU_BUDGET", "8")
    env.setdefault("HYPER_API_KEY", "test-api-key")
    env.setdefault("HYPER_ADMIN_SECRET", "test-admin-secret")
    env.setdefault("HYPER_CSRF_SECRET", "test-csrf-secret")
    env.setdefault("HYPER_SESSION_SECRET", "test-session-secret")
    env.setdefault(
        "HYPER_SESSION_SIGNING_KEY", "test-session-signing-key-for-tests-only"
    )
    env.setdefault("HYPER_SEED_PASSWORD", "test-seed-password")
    env.setdefault("HYPER_ADMIN_PASSWORD", "test-admin-password")
    if env_override:
        env.update(env_override)
    else:
        _scrub_ambient_database(env)

    # -X faulthandler: dump every thread's traceback on a native fatal fault
    # (see _run_test) so a Zig-extension crash under pytest is never silent.
    cmd = [
        python,
        "-X",
        "faulthandler",
        "-m",
        "pytest",
        str(suite_path),
        "-q",
        "--tb=short",
        "--no-header",
    ]
    cmd += [str(a) for a in suite.get("extra_args", [])]

    return await _exec_subprocess(
        name=name,
        cmd=cmd,
        cwd=str(project_dir),
        env=env,
        sem=sem,
        error_extract_fn=_extract_fail_lines,
    )


# ── Signal handling ───────────────────────────────────────────────────────


def _kill_all_children() -> None:
    """Kill all tracked child processes."""
    for proc in list(_active_procs):
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
    _drop_all_test_dbs()


# ── Discovery and matching ────────────────────────────────────────────────


def _git_tracked_test_names(project_dir: Path) -> set[str] | None:
    """Basenames of git-tracked scripts/test_*.py, or None when git can't
    answer (no repo / no git binary — e.g. a release tarball), in which case
    discovery falls back to trusting the filesystem."""
    try:
        r = subprocess.run(
            ["git", "ls-files", "scripts/test_*.py"],
            capture_output=True,
            text=True,
            cwd=project_dir,
            timeout=10,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if r.returncode != 0:
        return None
    return {Path(line).name for line in r.stdout.splitlines() if line}


def discover_tests(project_dir: Path) -> list[Path]:
    """All runnable script tests: git-tracked scripts/test_*.py.

    Untracked test files are LOCAL SCRATCH — a developer's work-in-progress
    debugging file must never abort the whole suite at the marker gate (and
    must never be scheduled into a resource lane it never declared). They are
    skipped with a loud per-name notice so a genuinely new test that was
    simply never `git add`-ed is visible, not silently missing.
    """
    found = sorted((project_dir / "scripts").glob("test_*.py"))
    tracked = _git_tracked_test_names(project_dir)
    if tracked is None:
        return found
    scratch = [p.name for p in found if p.name not in tracked]
    if scratch:
        logger.warning(
            "Skipping {n} untracked scripts/test_*.py (local scratch): {names} "
            "— `git add` a file to include it in the suite",
            n=len(scratch),
            names=", ".join(scratch),
        )
    return [p for p in found if p.name in tracked]


def match_tests(tests: list[Path], patterns: list[str]) -> list[Path]:
    if not patterns:
        return tests
    return [t for t in tests if any(p in t.stem for p in patterns)]


PYTEST_SUITES = [
    {
        "name": "pytest:standalone",
        "path": "tests/test_standalone",
        "extra_args": ["--override-ini=DJANGO_SETTINGS_MODULE=", "-p", "no:django"],
    },
    {
        "name": "pytest:serving",
        "path": "tests/test_serving",
        "extra_args": ["--override-ini=DJANGO_SETTINGS_MODULE=", "-p", "no:django"],
    },
    {"name": "pytest:db", "path": "tests/test_db", "extra_args": []},
    {"name": "pytest:validation", "path": "tests/test_validation", "extra_args": []},
    {"name": "pytest:routing", "path": "tests/test_routing", "extra_args": []},
    {"name": "pytest:integration", "path": "tests/test_integration", "extra_args": []},
]


# ── Output ────────────────────────────────────────────────────────────────


def _print_result(r: TestResult) -> None:
    if r.uncounted:
        count_str = "ok*"  # exit-0, no counts printed — see end-of-run note
    elif r.total > 0:
        count_str = f"{r.passed}/{r.total}"
    else:
        count_str = "?"
    elapsed_str = f"{r.elapsed_ms:.0f}ms"
    retry_tag = "  (flaky: retried)" if r.retries > 0 else ""
    if r.exit_code != 0 or r.failed > 0:
        logger.opt(raw=True).info(
            f"  FAIL  {r.name:<40} {count_str:>10}  {elapsed_str:>8}{retry_tag}\n"
        )
    else:
        logger.opt(raw=True).info(
            f"  pass  {r.name:<40} {count_str:>10}  {elapsed_str:>8}{retry_tag}\n"
        )


# ── Async main ────────────────────────────────────────────────────────────


async def _async_main(
    scripts: list[Path],
    metas: dict[str, TestMeta],
    pytest_suites: list[dict[str, object]],
    project_dir: Path,
    python: str,
    force_serial: bool,
) -> tuple[list[TestResult], dict[str, dict[str, object]]]:
    """Run all tests with maximum parallelism via asyncio.

    Returns the per-file results plus a `run_info` map (keyed by the same
    name each `TestResult` carries) recording the kind, concurrency, and the
    EFFECTIVE timeout each file ran under — used to build the machine-readable
    run summary.
    """

    # Semaphores control concurrency per category. Defaults are tuned for
    # workstations / dev servers with high somaxconn + listen_backlog. CI
    # environments often have small TCP accept queues — the postgres image
    # ships with listen_backlog=128 and a single GHA runner trying to open
    # ~166 simultaneous connections (50+20+13 procs × pool_size=2) overflows
    # it and gets the kernel ECONNREFUSED.
    #
    # Override modes (most → least specific):
    #   HYPER_TEST_{UNIT,DB,E2E}_CONCURRENCY=N   per-category cap
    #   HYPER_TEST_PROFILE=ci                    apply the CI preset below
    _ci = os.environ.get("HYPER_TEST_PROFILE") == "ci"
    # CI preset is conservative (6/3/2) because the postgres image's
    # accept queue + per-connection fork() rate can't keep up with the
    # workstation defaults. Even small bursts trigger ECONNREFUSED.
    _defaults = (6, 3, 2) if _ci else (50, 20, 13)

    def _sem(env_var: str, default: int) -> asyncio.Semaphore:
        raw = os.environ.get(env_var)
        if raw and raw.isdigit() and int(raw) > 0:
            return asyncio.Semaphore(int(raw))
        return asyncio.Semaphore(default)

    sem_unit = _sem("HYPER_TEST_UNIT_CONCURRENCY", _defaults[0])
    sem_db = _sem("HYPER_TEST_DB_CONCURRENCY", _defaults[1])
    sem_e2e = _sem("HYPER_TEST_E2E_CONCURRENCY", _defaults[2])
    sem_shared = asyncio.Semaphore(1)  # Sequential

    # E2E tests sharing the same app database must be serialized
    # to avoid DDL races (CREATE TABLE, ALTER TABLE, CREATE INDEX).
    app_db_sems: dict[str, asyncio.Semaphore] = {}

    sem_map = {
        "unit": sem_unit,
        "db_isolated": sem_db,
        "db_django": sem_db,
        "db_shared": sem_shared,
        "e2e": sem_e2e,
    }

    if force_serial:
        for k in sem_map:
            sem_map[k] = asyncio.Semaphore(1)

    # Dedicated cap for files marked `# hyper-test-concurrency: low` — they
    # acquire this INSTEAD of their kind's semaphore. Under --serial this
    # collapses to 1 like every other lane.
    sem_low = asyncio.Semaphore(1 if force_serial else LOW_CONCURRENCY_LIMIT)

    # Per-file run metadata for the machine-readable summary, keyed by the
    # name each TestResult carries.
    run_info: dict[str, dict[str, object]] = {}

    # Assign unique DB names for DB tests AND isolated E2E tests
    db_names: dict[str, str] = {}  # test_stem -> db_name
    e2e_db_names: dict[str, str] = {}  # "isolated:app" group -> db_name
    pid = os.getpid()
    idx = 0
    for name in sorted(metas):
        meta = metas[name]
        if meta.kind in ("db_isolated", "db_django"):
            idx += 1
            db_names[name] = f"hd_t{idx}_{pid}"
        elif meta.kind == "e2e" and meta.app_db.startswith("isolated:"):
            # Only create isolated DBs for tests that manage their own setup
            if meta.app_db not in e2e_db_names:
                idx += 1
                app_tag = meta.app_db.split(":", 1)[1]
                e2e_db_names[meta.app_db] = f"hd_e2e_{app_tag}_{pid}"

    # Batch-create all isolated databases upfront
    all_db_names = list(db_names.values()) + list(e2e_db_names.values())
    if all_db_names:
        t0 = time.perf_counter()
        _create_dbs_batch(all_db_names)
        logger.info(
            "Created {created}/{total} test databases in {elapsed_ms:.0f}ms",
            created=len(_created_dbs),
            total=len(all_db_names),
            elapsed_ms=(time.perf_counter() - t0) * 1000,
        )

    # Build async tasks
    tasks: list[asyncio.Task[TestResult]] = []

    async def _run_e2e_serialized(
        path: Path,
        py: str,
        env: dict[str, str] | None,
        app_db: str,
        sem: asyncio.Semaphore,
        timeout: int,
    ) -> TestResult:
        """Run an e2e test, serialized with other tests using the same app DB."""
        if app_db:
            if app_db not in app_db_sems:
                app_db_sems[app_db] = asyncio.Semaphore(1)
            async with app_db_sems[app_db]:
                return await _run_test(
                    path, py, env_override=env, sem=sem, timeout=timeout
                )
        return await _run_test(path, py, env_override=env, sem=sem, timeout=timeout)

    async def _run_file(
        meta: TestMeta, env: dict[str, str] | None, sem: asyncio.Semaphore
    ) -> TestResult:
        """Run one script file, retrying once if it is flaky-quarantined.

        A file marked `# hyper-test-flaky` that fails is rerun exactly once in
        a fresh subprocess with the same env/timeout. If the retry passes the
        file counts as passed; a retry that also fails is a normal failure.
        Either way the returned result records that a retry occurred.
        """

        async def _once() -> TestResult:
            if meta.kind == "e2e":
                return await _run_e2e_serialized(
                    meta.path,
                    python,
                    env,
                    meta.app_db,
                    sem,
                    timeout=meta.timeout or E2E_TIMEOUT_SECONDS,
                )
            return await _run_test(
                meta.path,
                python,
                env_override=env,
                sem=sem,
                timeout=meta.timeout or TEST_TIMEOUT_SECONDS,
            )

        result = await _once()
        if meta.flaky_reason and (result.exit_code != 0 or result.failed > 0):
            retry = await _once()
            retry.retries = 1
            return retry
        return result

    for name in sorted(metas):
        meta = metas[name]
        # A `low` file is capped by the dedicated low semaphore INSTEAD of its
        # kind's; everything else about its kind is unchanged.
        if meta.concurrency == _CONCURRENCY_LOW:
            sem = sem_low
        else:
            sem = sem_map.get(meta.kind, sem_unit)
        env: dict[str, str] | None = None

        if meta.kind == "db_isolated" and name in db_names:
            _db_url = f"postgres://{_DB_AUTH}{_DB_HOST_PORT}/{db_names[name]}"
            env = {
                "DATABASE_URL": _db_url,
                "HYPER_DATABASE_URL": _db_url,
                "PGDATABASE": db_names[name],
            }
        elif meta.kind == "db_django" and name in db_names:
            env = {"PGDATABASE": db_names[name]}
        elif (
            meta.kind == "e2e"
            and meta.app_db.startswith("isolated:")
            and meta.app_db in e2e_db_names
        ):
            e2e_db = e2e_db_names[meta.app_db]
            _e2e_url = f"postgres://{_DB_AUTH}{_DB_HOST_PORT}/{e2e_db}"
            env = {
                "DATABASE_URL": _e2e_url,
                "HYPER_DATABASE_URL": _e2e_url,
                "PGDATABASE": e2e_db,
            }

        effective_timeout = meta.timeout or (
            E2E_TIMEOUT_SECONDS if meta.kind == "e2e" else TEST_TIMEOUT_SECONDS
        )
        run_info[meta.path.stem.replace("test_", "")] = {
            "kind": meta.kind,
            "concurrency": meta.concurrency,
            "timeout_s": effective_timeout,
        }

        tasks.append(asyncio.create_task(_run_file(meta, env, sem), name=name))

    # Pytest suites as async subprocess tasks
    for suite in pytest_suites:
        sname = str(suite["name"])
        # Standalone + validation + routing + integration have no DB — use unit semaphore
        # DB suite gets its own isolated database
        if "db" in sname:
            idx += 1
            db_name = f"hd_t{idx}_{pid}"
            _create_dbs_batch([db_name])
            env = {"PGDATABASE": db_name}
            sem = sem_db
            suite_kind = "db_isolated"
        else:
            env = None
            sem = sem_unit
            suite_kind = "unit"

        run_info[sname] = {
            "kind": suite_kind,
            "concurrency": "",
            "timeout_s": TEST_TIMEOUT_SECONDS,
        }

        tasks.append(
            asyncio.create_task(
                _run_pytest_subprocess(
                    suite, project_dir, python, env_override=env, sem=sem
                ),
                name=sname,
            )
        )

    # Gather all results
    results: list[TestResult] = []
    for coro in asyncio.as_completed(tasks):
        r = await coro
        results.append(r)
        _print_result(r)

    return results, run_info


# ── Main ──────────────────────────────────────────────────────────────────


def main() -> int:
    atexit.register(_kill_all_children)

    project_dir = Path(__file__).parent.parent
    python = sys.executable
    raw_args = sys.argv[1:]

    # ── Install session-log tee BEFORE any output ────────────────────────
    # Every run gets its own timestamped transcript in
    # `logs/test_runs/`. This avoids the need for callers to manually
    # pipe or tee output to a file — the runner is self-documenting.
    # Skip log installation when `--no-log` is passed (e.g. when
    # running under another test harness that captures its own output)
    # or when `HYPER_TEST_NO_LOG=1` is set in the environment.
    _no_log = "--no-log" in raw_args or os.environ.get("HYPER_TEST_NO_LOG") == "1"
    session_log_file = None
    session_log_path: Path | None = None
    session_log_sink_id: int | None = None
    old_logger_sinks: list[int] = []
    if not _no_log:
        session_log_path, session_log_file = _open_session_log(raw_args)
        # Single-writer discipline: the tee is the ONLY writer to the
        # session log file. It fans out every write to both (a) the
        # real terminal stdout/stderr AND (b) the session log file.
        # We then re-point the logger's stderr sink at our tee'd
        # sys.stderr so `logger.info(...)` calls also flow through
        # the tee — preventing the race we'd see if the logger had
        # its OWN file handle to the same path.
        sys.stdout = _LogTee(_streams=(sys.__stdout__, session_log_file))
        sys.stderr = _LogTee(_streams=(sys.__stderr__, session_log_file))

        # Drop the default ConsoleSink (captured at import time, still
        # pointing at the original terminal stderr) and add a fresh
        # one that evaluates `sys.stderr` NOW — which is our tee.
        old_logger_sinks = list(logger._core.handlers.keys())
        for hid in old_logger_sinks:
            with contextlib.suppress(Exception):
                logger.remove(hid)
        session_log_sink_id = logger.add(
            sys.stderr,
            level="DEBUG",
            enqueue=False,
        )

        # Announce the log path on the very first line so operators
        # and AI assistants always know where to look for the full
        # transcript after the run completes.
        print(f"→ Session log: {session_log_path}")

        def _close_session_log(f=session_log_file) -> None:
            with contextlib.suppress(Exception):
                if not f.closed:
                    f.close()

        atexit.register(_close_session_log)

    args = raw_args

    list_mode = "--list" in args
    force_serial = "--serial" in args
    scripts_only = "--scripts-only" in args
    pytest_only = "--pytest-only" in args
    args = [a for a in args if not a.startswith("--")]

    all_scripts = discover_tests(project_dir)
    run_scripts = not pytest_only
    run_pytest = not scripts_only

    scripts = match_tests(all_scripts, args) if run_scripts else []

    # Classify
    metas: dict[str, TestMeta] = {s.stem: classify_test(s) for s in scripts}

    if args and run_pytest:
        pytest_suites = [s for s in PYTEST_SUITES if any(p in s["name"] for p in args)]
    elif run_pytest:
        pytest_suites = PYTEST_SUITES
    else:
        pytest_suites = []

    if list_mode:
        kind_labels = {
            "unit": "UNIT",
            "db_isolated": "DB‖ ",
            "db_django": "DJG‖",
            "db_shared": "DB→ ",
            "e2e": "E2E ",
        }
        counts: dict[str, int] = {}
        for m in metas.values():
            counts[m.kind] = counts.get(m.kind, 0) + 1
        n_par = sum(v for k, v in counts.items() if k != "db_shared")
        n_seq = counts.get("db_shared", 0)
        logger.info(
            "Script tests ({count}):  {n_par} parallel + {n_seq} sequential",
            count=len(metas),
            n_par=n_par,
            n_seq=n_seq,
        )
        for k, v in sorted(counts.items()):
            logger.info("  {kind}: {count}", kind=k, count=v)
        logger.opt(raw=True).info("\n")
        for name in sorted(metas):
            m = metas[name]
            tag = kind_labels.get(m.kind, m.kind)
            port_str = f":{m.port}" if m.port else ""
            logger.opt(raw=True).info(f"  {name:<45} [{tag}]{port_str}\n")
        if run_pytest:
            logger.info("Pytest suites ({count}):", count=len(pytest_suites))
            for s in pytest_suites:
                logger.opt(raw=True).info(f"  {s['name']:<30} {s['path']}\n")
        return 0

    total_items = len(scripts) + len(pytest_suites)
    if total_items == 0:
        logger.info("No tests matching: {patterns}", patterns=args)
        return 1

    counts = {}
    for m in metas.values():
        counts[m.kind] = counts.get(m.kind, 0) + 1
    n_par = sum(v for k, v in counts.items() if k != "db_shared")
    n_seq = counts.get("db_shared", 0)

    logger.opt(raw=True).info(f"{'=' * 70}\n")
    logger.info(
        "HyperDjango Test Runner — {scripts} scripts + {suites} pytest suites",
        scripts=len(scripts),
        suites=len(pytest_suites),
    )
    _warn_ephemeral_port_overlap()
    if not force_serial:
        parts = [f"{v} {k}" for k, v in sorted(counts.items())]
        logger.info(
            "  {n_par} parallel + {n_seq} sequential  |  {breakdown}",
            n_par=n_par,
            n_seq=n_seq,
            breakdown=", ".join(parts),
        )
    logger.opt(raw=True).info(f"{'=' * 70}\n")

    run_started = _dt.datetime.now()
    start_all = time.perf_counter()

    try:
        results, run_info = asyncio.run(
            _async_main(
                scripts, metas, pytest_suites, project_dir, python, force_serial
            )
        )
    except KeyboardInterrupt:
        logger.info("Interrupted. Cleaning up...")
        _kill_all_children()
        return 130

    # Cleanup test databases
    _drop_all_test_dbs()

    total_elapsed = (time.perf_counter() - start_all) * 1000

    # Sort results by name for final summary
    results.sort(key=lambda r: r.name)

    total_passed = sum(r.passed for r in results)
    total_failed = sum(r.failed for r in results)
    total_tests = sum(r.total for r in results)
    files_passed = sum(1 for r in results if r.exit_code == 0 and r.failed == 0)
    files_failed = sum(1 for r in results if r.exit_code != 0 or r.failed > 0)

    logger.opt(raw=True).info(f"\n{'=' * 70}\n")
    logger.info(
        "Total: {passed}/{total} tests passed, {failed} failed",
        passed=total_passed,
        total=total_tests,
        failed=total_failed,
    )
    logger.info(
        "Files: {passed}/{total} passed, {failed} failed",
        passed=files_passed,
        total=len(results),
        failed=files_failed,
    )
    logger.info("Time:  {elapsed:.1f}s", elapsed=total_elapsed / 1000)

    # Counting health: files that passed but printed no parseable tally (the
    # `ok*` rows). Each counts as ONE passed unit above; migrating them to
    # the testkit harness (check()/finish()) makes their real check counts
    # land in the totals.
    uncounted = [r.name for r in results if r.uncounted]
    if uncounted:
        logger.info(
            "Uncounted: {n} file(s) passed without a parseable tally (shown "
            "as ok*, each tallied as 1) — migrate to hyperdjango.testkit "
            "check()/finish() for real counts: {names}",
            n=len(uncounted),
            names=", ".join(uncounted[:12]) + (" …" if len(uncounted) > 12 else ""),
        )

    # Flaky quarantine health: files that only went green because of a retry.
    flaky_recovered = [
        r for r in results if r.retries > 0 and r.exit_code == 0 and r.failed == 0
    ]
    if flaky_recovered:
        logger.info(
            "Flaky: {n} file(s) passed on retry (quarantined — see "
            "# hyper-test-flaky markers)",
            n=len(flaky_recovered),
        )

    # Slowest files — the ten highest wall-clock times, with their kinds.
    slowest = sorted(results, key=lambda r: r.elapsed_ms, reverse=True)[:10]
    if slowest:
        logger.info("Slowest:")
        for r in slowest:
            kind = str(run_info.get(r.name, {}).get("kind", "?"))
            logger.opt(raw=True).info(
                f"  {r.name:<40} [{kind:<11}]  {r.elapsed_ms:>8.0f}ms\n"
            )

    # ── Machine-readable run summary (CI-trendable, quarantine-inspectable) ──
    # Written next to the session log with the same stem and a `.json`
    # extension. Skipped when there is no session log (--no-log / env toggle).
    json_summary_path: Path | None = None
    if session_log_path is not None:
        json_summary_path = session_log_path.with_suffix(".json")
        files_payload = []
        for r in sorted(results, key=lambda r: r.elapsed_ms, reverse=True):
            info = run_info.get(r.name, {})
            files_payload.append(
                {
                    "name": r.name,
                    "kind": str(info.get("kind", "")),
                    "status": "pass"
                    if (r.exit_code == 0 and r.failed == 0)
                    else "fail",
                    "passed": r.passed,
                    "failed": r.failed,
                    "elapsed_ms": round(r.elapsed_ms, 3),
                    "retries": r.retries,
                    "timeout_s": info.get("timeout_s", TEST_TIMEOUT_SECONDS),
                    "concurrency": str(info.get("concurrency", "")),
                }
            )
        summary_payload = {
            "run": {
                "started": run_started.isoformat(),
                "duration_s": round(total_elapsed / 1000, 3),
                "profile": "ci"
                if os.environ.get("HYPER_TEST_PROFILE") == "ci"
                else "local",
                "totals": {
                    "files_passed": files_passed,
                    "files_failed": files_failed,
                    "tests_passed": total_passed,
                    "tests_failed": total_failed,
                    "flaky_retried": len(flaky_recovered),
                },
            },
            "files": files_payload,
        }
        try:
            with json_summary_path.open("w") as jf:
                json.dump(summary_payload, jf, indent=2)
        # blind-except: the JSON summary is a best-effort CI artifact; a
        # write failure must not mask the run's real exit status.
        except Exception:
            json_summary_path = None

    if files_failed > 0:
        logger.info("Failed files:")
        for r in results:
            if r.exit_code != 0 or r.failed > 0:
                logger.info(
                    "  {name}: {failures} failures (exit {exit_code})",
                    name=r.name,
                    failures=r.failed,
                    exit_code=r.exit_code,
                )
                if r.output_log:
                    logger.opt(raw=True).info(f"    → subprocess log: {r.output_log}\n")
                if r.error:
                    # A hang dump is already excerpted and ordered by
                    # _hang_excerpt; print it whole. Tailing it would keep the
                    # C stack (which faulthandler prints LAST and which says
                    # nothing) and discard the Python frames at its head —
                    # exactly the half that names the hung test. Everything
                    # else stays tail-truncated, where the last lines are the
                    # error.
                    lines = r.error.strip().splitlines()
                    shown = lines if _HANG_MARKER in r.error else lines[-5:]
                    for line in shown:
                        logger.opt(raw=True).info(f"    {line}\n")

    # Re-print the session log path at the END so it's visible both
    # at the top AND bottom of the scrollback — makes follow-up
    # inspection trivial regardless of how long the run was.
    if session_log_path is not None:
        logger.opt(raw=True).info(f"{'=' * 70}\n")
        print(f"→ Session log: {session_log_path}")
    if json_summary_path is not None:
        print(f"→ Run summary: {json_summary_path}")

    logger.opt(raw=True).info(f"{'=' * 70}\n")

    exit_code = 0 if files_failed == 0 else 1

    # On Python 3.14t free-threaded, asyncio subprocess pipe reader threads
    # may hold the stdout BufferedWriter lock during interpreter shutdown,
    # causing SIGABRT. Flush logger and I/O, then use os._exit() to skip
    # finalization — all subprocesses are already terminated and databases dropped.
    logger.complete()
    import sys as _sys

    with contextlib.suppress(Exception):
        _sys.stdout.flush()
    with contextlib.suppress(Exception):
        _sys.stderr.flush()
    os._exit(exit_code)
