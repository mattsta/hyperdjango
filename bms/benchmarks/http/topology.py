"""CPU-topology detection for automatic benchmark core pinning.

The single-box methodology needs the server and the load generator on
DISJOINT physical cores (SMT siblings sharing a physical core measured ~3x
worse; a floating client steals server cores). Deriving the pin used to mean
reading `lscpu` and hand-writing `--server-cores 0-63 --client-cores 64-127`
per machine. This module reads sysfs directly and proposes the same split
automatically:

- 2+ NUMA nodes: server = node0's physical cores, client = node1's — whole
  sockets, no cross-socket worker migration, matching the validated manual
  layout on the 2×EPYC benchmark box.
- 1 node: the physical-core list split in half.
- SMT siblings are excluded everywhere (only the lowest CPU id of each
  sibling set is used), so both halves are whole physical cores and the
  siblings idle.

Linux-only (sysfs); callers get None elsewhere and fall back to unpinned.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_CPU_ROOT = Path("/sys/devices/system/cpu")
_NODE_ROOT = Path("/sys/devices/system/node")


def _parse_cpulist(text: str) -> list[int]:
    """Parse a sysfs cpulist ("0-3,8,10-11") into a sorted list of ints."""
    cpus: list[int] = []
    for part in text.strip().split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            cpus.extend(range(int(lo), int(hi) + 1))
        else:
            cpus.append(int(part))
    return sorted(cpus)


def to_cpulist(cpus: list[int]) -> str:
    """Compress a sorted CPU list back into taskset-style range syntax."""
    if not cpus:
        return ""
    runs: list[str] = []
    start = prev = cpus[0]
    for c in cpus[1:]:
        if c == prev + 1:
            prev = c
            continue
        runs.append(f"{start}-{prev}" if prev > start else f"{start}")
        start = prev = c
    runs.append(f"{start}-{prev}" if prev > start else f"{start}")
    return ",".join(runs)


def _physical_filter(cpus: list[int]) -> list[int]:
    """Keep one CPU per physical core (the lowest id of each sibling set)."""
    physical: list[int] = []
    for cpu in cpus:
        sib = _CPU_ROOT / f"cpu{cpu}" / "topology" / "thread_siblings_list"
        try:
            siblings = _parse_cpulist(sib.read_text())
        except OSError:
            siblings = [cpu]
        if cpu == min(siblings):
            physical.append(cpu)
    return physical


@dataclass(slots=True)
class AutoPin:
    """A proposed disjoint physical-core split for server and client."""

    server_cores: str
    client_cores: str
    server_count: int
    client_count: int
    numa_nodes: int
    description: str


def detect_auto_pin(min_physical: int = 8) -> AutoPin | None:
    """Propose disjoint physical-core pins for server and load generator.

    Returns None when the topology can't be read (non-Linux), or when the
    machine has fewer than `min_physical` physical cores — pinning halves of
    a small box hurts more than client/server interference does."""
    if not _CPU_ROOT.exists():
        return None
    try:
        node_dirs = sorted(
            (d for d in _NODE_ROOT.glob("node[0-9]*")),
            key=lambda d: int(d.name[4:]),
        )
    except OSError:
        node_dirs = []

    if len(node_dirs) >= 2:
        server = _physical_filter(
            _parse_cpulist((node_dirs[0] / "cpulist").read_text())
        )
        client = _physical_filter(
            _parse_cpulist((node_dirs[1] / "cpulist").read_text())
        )
        if len(server) + len(client) < min_physical:
            return None
        # Balance: benchmark headroom showed the client needs no more cores
        # than the server (84% idle at ~480k rps on an equal split).
        desc = (
            f"NUMA split: server=node0 ({len(server)} phys cores), "
            f"client=node1 ({len(client)} phys cores), SMT siblings idle"
        )
        return AutoPin(
            server_cores=to_cpulist(server),
            client_cores=to_cpulist(client),
            server_count=len(server),
            client_count=len(client),
            numa_nodes=len(node_dirs),
            description=desc,
        )

    try:
        present = _parse_cpulist((_CPU_ROOT / "present").read_text())
    except OSError:
        return None
    physical = _physical_filter(present)
    if len(physical) < min_physical:
        return None
    half = len(physical) // 2
    server, client = physical[:half], physical[half:]
    desc = (
        f"single-node split: server={len(server)} phys cores, "
        f"client={len(client)} phys cores, SMT siblings idle"
    )
    return AutoPin(
        server_cores=to_cpulist(server),
        client_cores=to_cpulist(client),
        server_count=len(server),
        client_count=len(client),
        numa_nodes=max(len(node_dirs), 1),
        description=desc,
    )
