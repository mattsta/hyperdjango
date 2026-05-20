"""Server fixtures: launch each framework's server, await readiness, sample
memory (RSS incl. children), and tear down cleanly.

Fairness note: all servers get the same parallelism budget W and run on the same
free-threaded CPython 3.14t build (hyperdjango's required interpreter), single
machine over loopback. FastAPI = uvicorn, W worker PROCESSES (async loop each);
Flask = gunicorn, W worker PROCESSES × 4 gthread threads (sync WSGI); hyperdjango
= native Zig core + W-thread pool (threaded or reactor via HYPER_HTTP_SERVER_MODEL).
Flask/FastAPI are not usually deployed on a free-threaded interpreter, so their
absolute numbers are conservative; sync WSGI (Flask) is also inherently slower
per request. Each framework's exact launch is `config_summary()`, shown in the
report's setup panel. Documented in the report.
"""

from __future__ import annotations

import contextlib
import http.client
import os
import signal
import socket
import subprocess
import sys
import time

from benchmarks.http.affinity import wrap_command

HD_APP = "benchmarks.http.apps.hyperdjango_app"


def _kill_port(port: int) -> None:
    """Best-effort: free the port from a stale process."""
    with contextlib.suppress(Exception):
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True, timeout=5
        ).stdout
        for pid in out.split():
            with contextlib.suppress(Exception):
                os.kill(int(pid), signal.SIGKILL)


def config_summary(framework: str) -> str:
    """One-line description of how `framework` is launched — surfaced in the
    dashboard so every number is traceable to its exact server configuration.
    W is the swept parallelism budget (threads for the native pool, worker
    processes for uvicorn/gunicorn)."""
    return {
        "hyperdjango-threaded": "native Zig HTTP core · W-thread worker pool, thread-per-connection",
        "hyperdjango-reactor": "native Zig HTTP core · kqueue/epoll reactor multiplexing + W-thread pool",
        "fastapi": "uvicorn ASGI · W worker processes · async event loop per process",
        "flask": "gunicorn WSGI · W worker processes × 4 gthread threads · sync",
    }.get(framework, framework)


def _command(
    framework: str, host: str, port: int, workers: int, reactor_count: int | None = None
):
    """Return (argv, extra_env) to launch `framework`'s server."""
    py = sys.executable
    if framework == "fastapi":
        # uvicorn is single-loop per process, so W cores of parallelism = W
        # worker PROCESSES (the same parallelism budget as W threads elsewhere).
        # Launched via uvicorn_main (NOT `-m uvicorn`): uvicorn's own multi-
        # worker path binds a proto-0 listen socket, which silently disables
        # TCP_NODELAY on every accepted connection and turns the benchmark into
        # a ~40 ms delayed-ACK measurement — see uvicorn_main's docstring.
        return (
            [
                py,
                "-m",
                "benchmarks.http.apps.uvicorn_main",
                "benchmarks.http.apps.fastapi_app:app",
                host,
                str(port),
                str(workers),
            ],
            {},
        )
    if framework == "flask":
        # Fair, production-standard deployment: W gunicorn worker PROCESSES (the
        # same W-way parallelism budget as fastapi's `uvicorn --workers W`), each
        # a gthread worker with a small thread pool so keep-alive concurrency
        # isn't starved. `-w 1 --threads W` (one process) pins sync WSGI to ~one
        # core and badly understates Flask — see benchmarks/http/README.
        return (
            [
                py,
                "-m",
                "gunicorn",
                "benchmarks.http.apps.flask_app:app",
                "-b",
                f"{host}:{port}",
                "-k",
                "gthread",
                "-w",
                str(workers),
                "--threads",
                "4",
                "--log-level",
                "warning",
            ],
            {},
        )
    if framework in ("hyperdjango-threaded", "hyperdjango-reactor"):
        model = "reactor" if framework.endswith("reactor") else "threaded"
        script = f"import {HD_APP} as m; m.app.run(host='{host}', port={port})"
        env = {
            "HYPER_HTTP_SERVER_MODEL": model,
            "HYPER_THREAD_POOL_SIZE": str(workers),
            "HYPER_DEBUG": "0",
        }
        # Optional reactor-shard override: pin HYPER_HTTP_REACTOR_COUNT to sweep
        # the reactor's queue-sharding as a benchmark axis. Left unset = the
        # server's own capacity-scaled default (auto).
        if reactor_count is not None and model == "reactor":
            env["HYPER_HTTP_REACTOR_COUNT"] = str(reactor_count)
        return ([py, "-c", script], env)
    raise ValueError(f"unknown framework: {framework}")


class ServerFixture:
    def __init__(
        self,
        framework: str,
        host: str,
        port: int,
        workers: int,
        reactor_count: int | None = None,
        cpu_cores: str | None = None,
        numa: bool = False,
    ):
        self.framework = framework
        self.host = host
        self.port = port
        self.workers = workers
        self.reactor_count = reactor_count
        self.cpu_cores = cpu_cores
        self.numa = numa
        self.proc: subprocess.Popen | None = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def start(self) -> None:
        _kill_port(self.port)
        argv, extra_env = _command(
            self.framework, self.host, self.port, self.workers, self.reactor_count
        )
        # Pin the whole server process (and its worker threads) to a core set,
        # disjoint from the load generator's, so client and server don't steal
        # cores and workers don't migrate across NUMA nodes.
        argv = wrap_command(argv, self.cpu_cores, self.numa)
        env = os.environ.copy()
        env.update(extra_env)
        self.proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
        self._await_ready()

    def _await_ready(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc and self.proc.poll() is not None:
                raise RuntimeError(f"{self.framework} server exited during startup")
            try:
                with socket.create_connection((self.host, self.port), timeout=0.5):
                    pass
                conn = http.client.HTTPConnection(self.host, self.port, timeout=2)
                conn.request("GET", "/health")
                if conn.getresponse().status == 200:
                    conn.close()
                    return
                conn.close()
            except OSError:
                time.sleep(0.1)
        raise RuntimeError(f"{self.framework} server not ready within {timeout}s")

    def rss_mb(self) -> float:
        """Resident memory of the server process tree, in MiB.

        psutil is imported lazily so the fixture (and any harness that reuses it)
        loads even when the optional `benchmark-comparison` group isn't synced;
        without psutil this cleanly reports 0.0 rather than failing at import.
        """
        if not self.proc:
            return 0.0
        try:
            import psutil

            p = psutil.Process(self.proc.pid)
            rss = p.memory_info().rss
            for child in p.children(recursive=True):
                with contextlib.suppress(Exception):
                    rss += child.memory_info().rss
            return rss / (1024 * 1024)
        except Exception:
            return 0.0

    def stop(self) -> None:
        if not self.proc:
            return
        with contextlib.suppress(Exception):
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        with contextlib.suppress(Exception):
            self.proc.wait(timeout=5)
        if self.proc.poll() is None:
            with contextlib.suppress(Exception):
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        _kill_port(self.port)
        self.proc = None
