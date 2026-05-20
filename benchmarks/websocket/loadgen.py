"""Multi-process WebSocket load generator.

A single asyncio client process does the same per-message work as a
single-threaded server, so it *cannot* saturate a server that uses more
than one core — the client becomes the bottleneck and the server's true
throughput is undercounted. This was a real methodology bug in the
first version of this suite: it made hyperdjango's native server (which
uses one OS thread per connection across many cores under free-threaded
Python) look slower than the single-loop `websockets` reference, when in
fact the *client* was the limiter and both were being measured at the
client's ceiling.

This module removes that bottleneck by spreading the offered load across
N independent OS processes, each with its own interpreter and event
loop, so the aggregate client capacity scales past a single core. It is
both:

  * a library — `run_multiprocess_throughput(...)` orchestrates the
    worker processes and aggregates their results; and
  * an entry point — `python -m benchmarks.websocket.loadgen <uri>
    <conns> <duration_s> <warmup>` runs a single worker (this is what
    the orchestrator spawns; it's also runnable by hand for debugging).

Workers synchronize their timed window against a shared wall-clock start
deadline passed on the command line, so every process measures the same
interval rather than drifting by process-spawn latency.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field

from websockets.asyncio.client import connect

MAX_SIZE = 20 * 1024 * 1024
CONNECT_TIMEOUT_S = 8.0


# ── Worker side ─────────────────────────────────────────────────────────────


def _make_payload(size: int, frame_type: str) -> str | bytes:
    return (b"x" * size) if frame_type == "binary" else ("x" * size)


async def _worker_connection(
    uri: str,
    payload: str | bytes,
    warmup: int,
    start_at: float,
    end_at: float,
    counts: list[int],
    idx: int,
) -> None:
    # Retry connect a few times: when a fresh cell opens exactly pool-size
    # connections while the previous cell's are still tearing down, a
    # straggler can transiently hit a full accept backlog. Retrying avoids
    # a worker silently reporting zero (which would corrupt the aggregate).
    ws = None
    for attempt in range(5):
        try:
            ws = await asyncio.wait_for(
                connect(
                    uri,
                    compression=None,
                    max_size=MAX_SIZE,
                    ping_interval=None,
                    open_timeout=CONNECT_TIMEOUT_S,
                ).__aenter__(),
                timeout=CONNECT_TIMEOUT_S,
            )
            break
        except Exception:
            await asyncio.sleep(0.1 * (attempt + 1))
    if ws is None:
        counts[
            idx
        ] = -1  # sentinel: connection never established (surfaced by orchestrator)
        return
    try:
        # Warm up before the shared window opens: excludes TCP slow-start,
        # first-call lazy init, and connection setup from the measurement.
        # A bounded recv keeps one unresponsive connection from hanging the
        # whole worker (and, via communicate(), the orchestrator).
        for _ in range(warmup):
            await ws.send(payload)
            await asyncio.wait_for(ws.recv(), timeout=10.0)
        # Align to the shared start deadline so every worker/connection
        # counts the same wall-clock interval.
        now = time.time()
        if now < start_at:
            await asyncio.sleep(start_at - now)
        n = 0
        while time.time() < end_at:
            await ws.send(payload)
            await ws.recv()
            n += 1
        counts[idx] = n
    finally:
        with __import__("contextlib").suppress(Exception):
            await ws.close()


async def _worker_main(
    uri: str,
    conns: int,
    duration_s: float,
    warmup: int,
    start_at: float,
    payload_size: int,
    frame_type: str,
) -> tuple[int, int]:
    payload = _make_payload(payload_size, frame_type)
    end_at = start_at + duration_s
    counts = [0] * conns
    await asyncio.gather(
        *(
            _worker_connection(uri, payload, warmup, start_at, end_at, counts, i)
            for i in range(conns)
        )
    )
    messages = sum(c for c in counts if c >= 0)
    failed = sum(1 for c in counts if c < 0)
    return messages, failed


def _run_worker(argv: list[str]) -> int:
    uri, conns, duration_s, warmup, start_at, payload_size, frame_type = (
        argv[0],
        int(argv[1]),
        float(argv[2]),
        int(argv[3]),
        float(argv[4]),
        int(argv[5]),
        argv[6],
    )
    messages, failed = asyncio.run(
        _worker_main(uri, conns, duration_s, warmup, start_at, payload_size, frame_type)
    )
    print(json.dumps({"messages": messages, "conns": conns, "failed_conns": failed}))
    return 0


# ── Orchestrator side ───────────────────────────────────────────────────────


@dataclass
class MultiProcResult:
    uri: str
    n_procs: int
    conns_per_proc: int
    total_conns: int
    duration_s: float
    messages: int
    payload_size: int
    frame_type: str
    failed_conns: int = 0
    crashed_procs: int = 0
    msgs_per_sec: float = field(init=False)
    mb_per_sec: float = field(init=False)

    def __post_init__(self):
        self.msgs_per_sec = self.messages / self.duration_s if self.duration_s else 0.0
        # ×2: each logical message is one send + one echo back over the wire.
        self.mb_per_sec = (
            (self.messages * self.payload_size * 2) / (1024 * 1024) / self.duration_s
            if self.duration_s
            else 0.0
        )


def run_multiprocess_throughput(
    uri: str,
    n_procs: int,
    conns_per_proc: int,
    duration_s: float,
    payload_size: int = 4096,
    frame_type: str = "text",
    warmup: int = 20,
    spawn_grace_s: float = 1.0,
) -> MultiProcResult:
    """Spawn `n_procs` worker processes, each opening `conns_per_proc`
    connections, all counting the same `duration_s` wall-clock window.

    `spawn_grace_s` is how long we give every worker to start and finish
    warmup before the shared timed window opens — workers that finish
    warmup early sleep until the deadline, so slow spawns don't skew the
    interval.
    """
    start_at = time.time() + spawn_grace_s
    procs: list[subprocess.Popen] = []
    for _ in range(n_procs):
        procs.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "benchmarks.websocket.loadgen",
                    uri,
                    str(conns_per_proc),
                    str(duration_s),
                    str(warmup),
                    str(start_at),
                    str(payload_size),
                    frame_type,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )
    total_messages = 0
    total_failed = 0
    crashed = 0
    for p in procs:
        out, _err = p.communicate(timeout=spawn_grace_s + duration_s + 30)
        parsed = False
        for line in out.decode().splitlines():
            line = line.strip()
            if line.startswith("{"):
                data = json.loads(line)
                total_messages += data["messages"]
                total_failed += data.get("failed_conns", 0)
                parsed = True
        if not parsed:
            # Worker produced no result line at all (crash / nonzero exit) —
            # count it so the aggregate is never silently undercounted.
            crashed += 1
    return MultiProcResult(
        uri=uri,
        n_procs=n_procs,
        conns_per_proc=conns_per_proc,
        total_conns=n_procs * conns_per_proc,
        duration_s=duration_s,
        messages=total_messages,
        payload_size=payload_size,
        frame_type=frame_type,
        failed_conns=total_failed,
        crashed_procs=crashed,
    )


if __name__ == "__main__":
    sys.exit(_run_worker(sys.argv[1:]))
