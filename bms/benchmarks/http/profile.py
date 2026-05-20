"""OS-level contention profilers wrapped around a load window.

LINUX: if `perf` is present, `perf stat -p <server-pid> -e
context-switches,cache-misses,cpu-migrations -- sleep <window>` runs alongside
the load and its counts are parsed. These are the direct evidence for the two
suspected collapse causes: a convoy on a single reactor work-queue mutex shows as
exploding context-switches / cpu-migrations, and the single global
`active_requests` atomic ping-ponging one cache line shows as cache-misses.

macOS: `perf` does not exist. If `py-spy` is installed a short flamegraph sample
of the server is taken instead; otherwise profiling is skipped with a clear note.
A missing profiler NEVER fails the run — the throughput/latency/native-counter
signal stands on its own.
"""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

PERF_EVENTS = ("context-switches", "cache-misses", "cpu-migrations")
# Extra tail so perf's own `sleep` covers the whole load window even if the load
# driver's process spin-up lags a touch.
_PERF_TAIL_S = 0.5
_PERF_KILL_GRACE_S = 5.0
_PYSPY_RATE_HZ = 250


@dataclass(slots=True)
class ProfileResult:
    """What a profiler captured around one window (any field may be absent)."""

    tool: str = "none"
    note: str = ""
    counters: dict[str, float] = field(default_factory=dict)
    artifact_path: str | None = None

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "note": self.note,
            "counters": self.counters,
            "artifact_path": self.artifact_path,
        }


def perf_available() -> bool:
    return platform.system() == "Linux" and shutil.which("perf") is not None


def pyspy_available() -> bool:
    return shutil.which("py-spy") is not None


def _parse_perf_stat(stderr: str) -> dict[str, float]:
    """Pull the event counts out of `perf stat` stderr.

    Lines look like `        12,345      context-switches` (locale grouping in
    the count, event name after). `<not counted>` / `<not supported>` are
    tolerated and simply omitted.
    """
    out: dict[str, float] = {}
    for ev in PERF_EVENTS:
        m = re.search(rf"^\s*([\d,]+)\s+{re.escape(ev)}\b", stderr, re.MULTILINE)
        if m:
            try:
                out[ev] = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
    return out


def start_perf_stat(pid: int, window_s: float) -> subprocess.Popen | None:
    """Launch `perf stat` targeting `pid` for the window. Returns the process
    (parse it with `finish_perf_stat`) or None if perf isn't usable."""
    if not perf_available():
        return None
    dur = max(1, int(round(window_s + _PERF_TAIL_S)))
    argv = [
        "perf",
        "stat",
        "-p",
        str(pid),
        "-e",
        ",".join(PERF_EVENTS),
        "--",
        "sleep",
        str(dur),
    ]
    try:
        return subprocess.Popen(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
        )
    except Exception:  # noqa: BLE001
        return None


def finish_perf_stat(proc: subprocess.Popen | None) -> ProfileResult:
    if proc is None:
        return ProfileResult(tool="none", note="perf unavailable")
    try:
        _, stderr = proc.communicate(timeout=_PERF_KILL_GRACE_S + 30)
    except Exception:  # noqa: BLE001
        proc.kill()
        return ProfileResult(tool="perf", note="perf stat timed out")
    counters = _parse_perf_stat(stderr or "")
    note = "" if counters else "perf stat produced no parseable counters"
    return ProfileResult(tool="perf", note=note, counters=counters)


def pyspy_sample(pid: int, window_s: float, outdir: str, tag: str) -> ProfileResult:
    """Record a py-spy flamegraph of `pid` for the window (macOS fallback)."""
    if not pyspy_available():
        return ProfileResult(
            tool="none",
            note="no profiler: perf is Linux-only and py-spy is not installed",
        )
    Path(outdir).mkdir(parents=True, exist_ok=True)
    svg = Path(outdir) / f"pyspy_{tag}.svg"
    dur = max(1, int(round(window_s)))
    argv = [
        "py-spy",
        "record",
        "-p",
        str(pid),
        "-o",
        str(svg),
        "-f",
        "flamegraph",
        "-d",
        str(dur),
        "-r",
        str(_PYSPY_RATE_HZ),
        "--nonblocking",
    ]
    try:
        subprocess.run(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=dur + 30
        )
    except Exception as exc:  # noqa: BLE001
        return ProfileResult(tool="py-spy", note=f"py-spy failed: {exc}")
    if svg.exists():
        return ProfileResult(
            tool="py-spy", note="flamegraph sampled", artifact_path=str(svg)
        )
    return ProfileResult(tool="py-spy", note="py-spy produced no output")


@dataclass(slots=True)
class WindowProfiler:
    """Context that profiles the server pid across the caller's load window.

    Usage:
        with WindowProfiler(pid, window_s, outdir, tag) as prof:
            ...run the load...
        prof.result  # ProfileResult
    """

    pid: int
    window_s: float
    outdir: str
    tag: str
    _perf: subprocess.Popen | None = field(default=None, init=False)
    _t0: float = field(default=0.0, init=False)
    result: ProfileResult = field(default_factory=ProfileResult, init=False)

    def __enter__(self) -> WindowProfiler:
        self._t0 = time.monotonic()
        if perf_available():
            self._perf = start_perf_stat(self.pid, self.window_s)
        return self

    def __exit__(self, *exc) -> None:
        if self._perf is not None:
            self.result = finish_perf_stat(self._perf)
        elif pyspy_available():
            # py-spy records for its own duration; since the load already ran, a
            # short tail sample still captures the steady-state stack mix.
            remaining = max(1.0, self.window_s - (time.monotonic() - self._t0))
            self.result = pyspy_sample(
                self.pid, min(remaining, self.window_s), self.outdir, self.tag
            )
        else:
            self.result = ProfileResult(
                tool="none",
                note="no profiler available (perf is Linux-only, py-spy not installed) — contention inferred from rps/latency/native gauge",
            )
