"""Subprocess lifecycle management for the two servers under comparison.

Mirrors the AppRunner pattern in scripts/e2e_helper.py (spawn as a real
OS subprocess for fair isolation — separate thread pools, no import
contamination — poll for readiness, clean SIGTERM/SIGKILL teardown),
generalized to cover both the hyperdjango native app and the plain
`websockets`-library reference server.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import psutil

_APPS_DIR = Path(__file__).resolve().parent / "apps"
_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class ServerFixture:
    """Starts one comparison server as a subprocess and tracks it for metrics."""

    name: str
    script: str  # filename under benchmarks/websocket/apps/
    host: str = "127.0.0.1"
    port: int = 0
    env: dict[str, str] = field(default_factory=dict)
    startup_timeout: float = 15.0
    # 100ms is fine for the main benchmark (readiness is a one-time cost
    # amortized over a whole run), but too coarse to *measure* startup
    # latency itself — quantization error would swamp exactly the kind of
    # double-digit-millisecond improvement we care about there. Tightened
    # by measure_startup_latency() below.
    poll_interval: float = 0.1

    _proc: subprocess.Popen | None = field(default=None, init=False, repr=False)
    _ps: psutil.Process | None = field(default=None, init=False, repr=False)

    @property
    def ws_url(self) -> str:
        path = "/ws/echo" if "native" in self.script else ""
        return f"ws://{self.host}:{self.port}{path}"

    @property
    def health_url(self) -> str:
        return f"http://{self.host}:{self.port}/health"

    def start(self) -> None:
        script_path = _APPS_DIR / self.script
        proc_env = os.environ.copy()
        proc_env.update(self.env)
        self._proc = subprocess.Popen(
            [sys.executable, str(script_path), self.host, str(self.port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(_REPO_ROOT),
            env=proc_env,
            start_new_session=True,
        )
        self._ps = psutil.Process(self._proc.pid)
        self._wait_ready()
        # Prime CPU% measurement (first call always returns 0.0 as baseline).
        with contextlib.suppress(psutil.Error):
            self._ps.cpu_percent(interval=None)

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + self.startup_timeout
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                out, err = self._proc.communicate(timeout=2)
                raise RuntimeError(
                    f"{self.name} exited during startup (code {self._proc.returncode}):\n"
                    f"stdout: {out.decode(errors='replace')[-2000:]}\n"
                    f"stderr: {err.decode(errors='replace')[-2000:]}"
                )
            try:
                urllib.request.urlopen(self.health_url, timeout=1)
                return
            except (urllib.error.URLError, ConnectionError, OSError) as e:
                last_err = e
                time.sleep(self.poll_interval)
        raise TimeoutError(f"{self.name} did not become ready in time: {last_err}")

    def resource_snapshot(self) -> dict[str, float]:
        """Point-in-time RSS (MB), CPU%, and thread count for this server process."""
        if self._ps is None:
            return {"rss_mb": 0.0, "cpu_percent": 0.0, "num_threads": 0}
        with contextlib.suppress(psutil.Error):
            mem = self._ps.memory_info()
            return {
                "rss_mb": mem.rss / (1024 * 1024),
                "cpu_percent": self._ps.cpu_percent(interval=None),
                "num_threads": self._ps.num_threads(),
            }
        return {"rss_mb": 0.0, "cpu_percent": 0.0, "num_threads": 0}

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    def stop(self) -> None:
        if self._proc is None:
            return
        with contextlib.suppress(ProcessLookupError):
            self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()
            self._proc.wait(timeout=5)
        self._proc = None
        self._ps = None

    def __enter__(self) -> ServerFixture:
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()


def native_fixture(
    port: int,
    pool_size: int | None = None,
    shared_loops: int | None = None,
    concurrency: str | None = None,
) -> ServerFixture:
    """Start the native echo server.

    concurrency: "shared" (framework default) or "thread" to force the
    one-OS-thread-per-connection model. Left None → use the framework default
    (shared). `shared_loops` also selects shared mode and sets the loop count.
    """
    env = {}
    if pool_size is not None:
        env["HYPER_THREAD_POOL_SIZE"] = str(pool_size)
    tag = ""
    if concurrency == "thread":
        env["HYPER_WEBSOCKET_CONCURRENCY"] = "thread"
        tag = f" (thread{f', pool={pool_size}' if pool_size else ''})"
    elif shared_loops is not None:
        env["HYPER_WEBSOCKET_CONCURRENCY"] = "shared"
        env["HYPER_WS_LOOP_COUNT"] = str(shared_loops)
        tag = f" (shared×{shared_loops})"
    elif concurrency == "shared":
        env["HYPER_WEBSOCKET_CONCURRENCY"] = "shared"
        tag = " (shared)"
    return ServerFixture(
        name=f"hyperdjango-native{tag}",
        script="native_echo.py",
        port=port,
        env=env,
    )


def reference_fixture(port: int) -> ServerFixture:
    return ServerFixture(
        name="websockets-reference", script="reference_echo.py", port=port
    )


def measure_startup_latency(make_fixture, trials: int = 5) -> dict[str, float]:
    """Median/mean/max wall-clock time from process spawn to /health responding.

    `make_fixture` is a zero-arg callable returning a fresh ServerFixture
    (fresh instance per trial — a ServerFixture is single-use). Uses a
    tight poll interval so the measurement itself doesn't dominate the
    thing being measured.
    """
    samples_s: list[float] = []
    for _ in range(trials):
        fixture = make_fixture()
        fixture.poll_interval = 0.005
        start = time.monotonic()
        fixture.start()
        samples_s.append(time.monotonic() - start)
        fixture.stop()
    samples_s.sort()
    n = len(samples_s)
    return {
        "median_s": samples_s[n // 2],
        "mean_s": sum(samples_s) / n,
        "min_s": samples_s[0],
        "max_s": samples_s[-1],
        "samples": samples_s,
    }
