"""CPU-affinity wrapping for benchmark subprocesses (server and load generator).

On a big many-core / NUMA box, letting the server, the load generator, and the
profiler all float across the same logical CPUs produces the two artifacts the
worker sweep keeps hitting: the client and server steal cores from each other
(so the server's real ceiling is hidden), and worker threads migrate across
NUMA nodes (remote-memory penalties → chaotic, non-monotonic collapse). Pinning
each subprocess to a DISJOINT set of cores removes both.

This is opt-in and topology-explicit by design: the caller passes the exact
core list for the server and for the client (e.g. `0-63` and `64-127`), rather
than the harness guessing a topology it can't verify. `taskset` pins the CPUs;
`numactl --localalloc` additionally keeps each process's memory on the node of
the CPU it runs on (the NUMA win) when `numa=True` and numactl is present.

Absent on non-Linux or when the tool isn't installed: the wrap is a no-op with
a single warning, so a run never fails just because it can't pin.
"""

from __future__ import annotations

import platform
import shutil
import subprocess

_warned: set[str] = set()


def _warn_once(key: str, msg: str) -> None:
    if key not in _warned:
        _warned.add(key)
        print(f"[affinity] {msg}")


def core_count(cores: str | None) -> int:
    """Number of CPUs described by a taskset-style list like '0-63,96' (0 when
    unset). Used to size the load generator's thread count to its core budget."""
    if not cores:
        return 0
    total = 0
    for part in cores.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            total += int(hi) - int(lo) + 1
        else:
            total += 1
    return total


def describe(cores: str | None, numa: bool) -> str:
    """Human description of the pin that WILL be applied (for startup logging)."""
    if not cores:
        return "none (unpinned)"
    probe = wrap_command(["CMD"], cores, numa)
    return (
        " ".join(probe[:-1])
        if probe[0] in ("taskset", "numactl")
        else "none (unpinned — tool unavailable)"
    )


def preflight(cores: str | None, numa: bool) -> str | None:
    """Validate that the pin can actually be applied before the sweep runs.
    Returns None on success, or the launcher's error text on failure — so the
    caller can abort with an actionable message instead of the sweep silently
    reporting 0 rps for every cell (a wrapped command that fails leaves empty
    stdout, which parses as zero throughput). No-op / always-OK where pinning
    is a no-op (non-Linux, tool absent, or no cores requested)."""
    if not cores or platform.system() != "Linux":
        return None
    wrapped = wrap_command(["true"], cores, numa)
    if wrapped[0] not in ("taskset", "numactl"):
        return None  # wrap_command already warned that no tool is available
    try:
        r = subprocess.run(wrapped, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{wrapped[0]} could not run: {exc}"
    if r.returncode != 0:
        return (r.stderr or r.stdout or f"{wrapped[0]} exited {r.returncode}").strip()
    return None


def wrap_command(argv: list[str], cores: str | None, numa: bool = False) -> list[str]:
    """Prefix `argv` with a CPU-pinning launcher for `cores`, or return it
    unchanged when pinning is unavailable / not requested."""
    if not cores:
        return argv
    if platform.system() != "Linux":
        _warn_once(
            "platform",
            f"core pinning requested ({cores}) but taskset/numactl are "
            "Linux-only — running unpinned",
        )
        return argv
    if numa and shutil.which("numactl"):
        # --physcpubind pins the CPUs; --localalloc keeps memory on the running
        # CPU's NUMA node (the locality win on a multi-socket box).
        return ["numactl", f"--physcpubind={cores}", "--localalloc", *argv]
    if shutil.which("taskset"):
        return ["taskset", "-c", cores, *argv]
    _warn_once(
        "tool",
        f"core pinning requested ({cores}) but neither taskset nor numactl is "
        "installed — running unpinned (apt-get install util-linux numactl)",
    )
    return argv
