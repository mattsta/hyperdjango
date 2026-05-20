"""Benchmark client drivers.

Both servers are driven by the exact same client — the `websockets`
PyPI library's asyncio client — so every measured difference is
attributable to the server side, not to client implementation quirks.

Note on concurrency vs. hyperdjango's native architecture: the native
server dedicates one OS thread (from a fixed-size pool, see
fixtures.native_fixture) to each *live* WebSocket connection for its
entire lifetime. A throughput/connection-scaling run that holds N
connections open simultaneously with N > pool size will find that only
`pool size` connections ever complete their handshake — the rest queue
indefinitely, since no thread frees up until an existing connection
closes. That's an expected, real architectural ceiling, not a bug, so
connection attempts here use a bounded `open_timeout` and report
successes/timeouts separately rather than hanging the whole run.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

MAX_SIZE = 20 * 1024 * 1024  # comfortably above the largest payload we test
CONNECT_TIMEOUT_S = 8.0


def _payload_bytes(payload: str | bytes) -> int:
    return len(payload) if isinstance(payload, bytes) else len(payload.encode())


async def _connect(uri: str) -> ClientConnection | None:
    try:
        return await asyncio.wait_for(
            connect(
                uri,
                compression=None,
                max_size=MAX_SIZE,
                ping_interval=None,
                open_timeout=CONNECT_TIMEOUT_S,
            ).__aenter__(),
            timeout=CONNECT_TIMEOUT_S,
        )
    except TimeoutError, OSError, ConnectionClosed, Exception:
        return None


@dataclass
class ThroughputResult:
    requested_concurrency: int
    connected: int
    connect_timeouts: int
    messages: int
    errors: int
    duration_s: float
    msgs_per_sec: float = field(init=False)
    mb_per_sec: float = field(init=False)

    def __post_init__(self):
        self.msgs_per_sec = self.messages / self.duration_s if self.duration_s else 0.0


WARMUP_MESSAGES = 20


async def throughput_test(
    uri: str,
    payload: str | bytes,
    concurrency: int,
    duration_s: float,
    warmup_messages: int = WARMUP_MESSAGES,
) -> ThroughputResult:
    """Concurrency connections, each sending/receiving for duration_s.

    Every connection runs `warmup_messages` round trips *before* the timer
    starts, and all connections synchronize (via gather) at that boundary —
    so the timed window excludes connection-setup effects, first-call
    lazy-init costs (executor creation, reader registration, JIT-ish
    interpreter warm-up), and TCP slow-start, and every connection starts
    the real measurement from the same clean state at the same instant.
    """
    conns = await asyncio.gather(*(_connect(uri) for _ in range(concurrency)))
    live = [c for c in conns if c is not None]
    timeouts = concurrency - len(live)

    payload_size = _payload_bytes(payload)
    counts = [0] * len(live)
    errors = [0] * len(live)

    async def warmup(ws: ClientConnection) -> None:
        with contextlib.suppress(Exception):
            for _ in range(warmup_messages):
                await ws.send(payload)
                await ws.recv()

    if warmup_messages and live:
        await asyncio.gather(*(warmup(ws) for ws in live))

    async def worker(i: int, ws: ClientConnection) -> None:
        end = time.perf_counter() + duration_s
        try:
            while time.perf_counter() < end:
                await ws.send(payload)
                await ws.recv()
                counts[i] += 1
        except Exception:
            errors[i] += 1

    start = time.perf_counter()
    await asyncio.gather(*(worker(i, ws) for i, ws in enumerate(live)))
    elapsed = time.perf_counter() - start

    await asyncio.gather(*(ws.close() for ws in live), return_exceptions=True)

    total_messages = sum(counts)
    result = ThroughputResult(
        requested_concurrency=concurrency,
        connected=len(live),
        connect_timeouts=timeouts,
        messages=total_messages,
        errors=sum(errors),
        duration_s=elapsed,
    )
    result.mb_per_sec = (
        (total_messages * payload_size * 2) / (1024 * 1024) / elapsed
        if elapsed
        else 0.0
    )
    return result


@dataclass
class LatencyResult:
    samples: int
    rtts_us: list[float]

    @property
    def mean_us(self) -> float:
        return sum(self.rtts_us) / len(self.rtts_us) if self.rtts_us else 0.0

    def percentile_us(self, pct: float) -> float:
        if not self.rtts_us:
            return 0.0
        s = sorted(self.rtts_us)
        idx = min(int(len(s) * pct), len(s) - 1)
        return s[idx]


async def latency_test(
    uri: str, payload: str | bytes, samples: int
) -> LatencyResult | None:
    """Serial (unpipelined) request/response RTT over a single connection."""
    ws = await _connect(uri)
    if ws is None:
        return None
    rtts_us: list[float] = []
    try:
        for _ in range(10):  # warmup, excluded from measurement
            await ws.send(payload)
            await ws.recv()
        for _ in range(samples):
            t0 = time.perf_counter_ns()
            await ws.send(payload)
            await ws.recv()
            rtts_us.append((time.perf_counter_ns() - t0) / 1000)
    finally:
        await ws.close()
    return LatencyResult(samples=len(rtts_us), rtts_us=rtts_us)


@dataclass
class ConnectionScalingResult:
    target: int
    connected: int
    timed_out: int
    connect_time_s: float
    teardown_time_s: float


async def connection_scaling_test(uri: str, target: int) -> ConnectionScalingResult:
    """Measure handshake success rate and setup/teardown time at a given concurrency."""
    start = time.perf_counter()
    conns = await asyncio.gather(*(_connect(uri) for _ in range(target)))
    connect_time = time.perf_counter() - start
    live = [c for c in conns if c is not None]

    start = time.perf_counter()
    await asyncio.gather(*(ws.close() for ws in live), return_exceptions=True)
    teardown_time = time.perf_counter() - start

    return ConnectionScalingResult(
        target=target,
        connected=len(live),
        timed_out=target - len(live),
        connect_time_s=connect_time,
        teardown_time_s=teardown_time,
    )
