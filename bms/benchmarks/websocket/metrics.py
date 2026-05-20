"""Resource metrics: background RSS/CPU sampling, in-process object-size
introspection (pympler), and optional py-spy flamegraph capture.

All of this is best-effort instrumentation around the actual benchmark —
none of it should ever abort a run. py-spy in particular commonly needs
elevated privileges to attach to another process (SIP/ptrace
restrictions on macOS, ptrace_scope on Linux); when it's unavailable we
record why and continue rather than failing the suite.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import psutil


@dataclass
class ResourceSampler:
    """Samples RSS/CPU/thread-count for a PID on a background thread."""

    pid: int
    interval_s: float = 0.1
    samples: list[dict[str, float]] = field(default_factory=list, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _thread: threading.Thread | None = field(default=None, init=False)

    def _run(self) -> None:
        try:
            proc = psutil.Process(self.pid)
        except psutil.NoSuchProcess:
            return
        while not self._stop.is_set():
            with contextlib.suppress(psutil.Error):
                mem = proc.memory_info()
                self.samples.append(
                    {
                        "t": time.monotonic(),
                        "rss_mb": mem.rss / (1024 * 1024),
                        "cpu_percent": proc.cpu_percent(interval=None),
                        "num_threads": proc.num_threads(),
                    }
                )
            self._stop.wait(self.interval_s)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> dict[str, float]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if not self.samples:
            return {
                "peak_rss_mb": 0.0,
                "mean_rss_mb": 0.0,
                "mean_cpu_percent": 0.0,
                "peak_threads": 0,
            }
        rss = [s["rss_mb"] for s in self.samples]
        cpu = [s["cpu_percent"] for s in self.samples[1:]] or [
            0.0
        ]  # first sample's cpu% is meaningless
        threads = [s["num_threads"] for s in self.samples]
        return {
            "peak_rss_mb": max(rss),
            "mean_rss_mb": sum(rss) / len(rss),
            "mean_cpu_percent": sum(cpu) / len(cpu),
            "peak_threads": max(threads),
        }

    def __enter__(self) -> ResourceSampler:
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()


def object_overhead_report() -> dict[str, object]:
    """In-process per-connection object size, via pympler.asizeof.

    This is intentionally *not* a subprocess/network measurement — RSS
    under load conflates connection objects with buffers, GC overhead,
    and interpreter noise. asizeof walks the actual object graph to
    answer a narrower question: how many bytes does each library's
    connection-side wrapper object cost, independent of I/O.
    """
    from pympler import asizeof

    report: dict[str, object] = {}

    try:
        from hyperdjango.websocket import ZigWebSocket

        # Construct without a real Zig connection — accept()/send/recv aren't
        # invoked, we're only sizing the wrapper object's own footprint.
        native_obj = ZigWebSocket.__new__(ZigWebSocket)
        native_obj._conn_id = 1
        native_obj.headers = {"host": "127.0.0.1"}
        native_obj.path = "/ws/echo"
        native_obj.query_string = ""
        native_obj._accepted = True
        report["hyperdjango_ZigWebSocket_bytes"] = asizeof.asizeof(native_obj)
    except Exception as e:
        report["hyperdjango_ZigWebSocket_bytes"] = f"error: {e}"

    try:
        # The sans-I/O protocol object holds the same kind of per-connection
        # parsing/framing state as ZigWebSocket, without requiring a live
        # socket — the fairest like-for-like comparison available.
        from websockets.client import ClientProtocol
        from websockets.uri import parse_uri

        uri = parse_uri("ws://127.0.0.1:8000/ws/echo")
        proto = ClientProtocol(uri)
        report["websockets_ClientProtocol_bytes"] = asizeof.asizeof(proto)
    except Exception as e:
        report["websockets_ClientProtocol_bytes"] = f"error: {e}"

    return report


@dataclass
class FlamegraphResult:
    ok: bool
    path: str | None = None
    reason: str | None = None


def capture_flamegraph(pid: int, duration_s: float, out_svg: Path) -> FlamegraphResult:
    """Best-effort py-spy record over `pid` for `duration_s` seconds."""
    py_spy = shutil.which("py-spy")
    if py_spy is None:
        return FlamegraphResult(ok=False, reason="py-spy not found on PATH")
    out_svg.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        py_spy,
        "record",
        "-o",
        str(out_svg),
        "--pid",
        str(pid),
        "--duration",
        str(int(duration_s)),
        "--nonblocking",
        "--subprocesses",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=duration_s + 15
        )
    except Exception as e:
        return FlamegraphResult(ok=False, reason=f"py-spy invocation failed: {e}")
    if result.returncode != 0 or not out_svg.exists():
        detail = (result.stderr or result.stdout or "").strip()[-500:]
        return FlamegraphResult(
            ok=False,
            reason=f"py-spy exited {result.returncode} (often needs elevated privileges on this "
            f"OS to attach to another process): {detail}",
        )
    return FlamegraphResult(ok=True, path=str(out_svg))


async def measure_executor_thread_hop_overhead(
    pool_size: int = 24, iterations: int = 5000
) -> dict[str, float]:
    """Isolate the pure cost of bridging a blocking call into asyncio via
    ThreadPoolExecutor — no I/O, just the submit/context-switch/callback
    machinery. This is the mechanism `ZigWebSocket.receive_text()`/
    `iter_text()` use on every message (see hyperdjango/websocket.py),
    so this number is a direct proxy for the native receive path's
    architectural floor, independent of anything the Zig side does.
    """

    def _noop(x: int) -> int:
        return x

    executor = ThreadPoolExecutor(max_workers=pool_size)
    loop = asyncio.get_running_loop()
    try:
        for _ in range(100):  # warmup
            await loop.run_in_executor(executor, _noop, 1)

        start = time.perf_counter_ns()
        for _ in range(iterations):
            await loop.run_in_executor(executor, _noop, 1)
        hop_ns = (time.perf_counter_ns() - start) / iterations

        start = time.perf_counter_ns()
        for _ in range(iterations):
            _noop(1)
        direct_ns = (time.perf_counter_ns() - start) / iterations
    finally:
        executor.shutdown(wait=True)

    return {
        "thread_hop_us": hop_ns / 1000,
        "direct_call_us": direct_ns / 1000,
        "iterations": iterations,
    }
