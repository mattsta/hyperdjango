"""Self-contained multi-process HTTP load generator (keep-alive, closed-loop).

No external tools (wrk/hey) required. `concurrency` independent keep-alive
connections each run a closed loop (send request → read full response → repeat),
so `concurrency` requests are genuinely in flight at once. Connections are spread
across processes (each running threads) so the client itself doesn't bottleneck.
An out-of-measurement warmup runs first; only the measurement window is counted.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import re
import shutil
import socket
import subprocess
import threading
import time

from benchmarks.http.affinity import core_count, wrap_command
from benchmarks.http.metrics import summarize

_WRK = shutil.which("wrk")


def wrk_available() -> bool:
    return _WRK is not None


def _wrk_ms(value: str, unit: str) -> float:
    return float(value) * {"us": 0.001, "ms": 1.0, "s": 1000.0, "m": 60000.0}[unit]


def run_load_wrk(
    host,
    port,
    path,
    concurrency,
    duration_s=2.0,
    warmup_s=0.5,
    threads=None,
    cpu_cores=None,
    numa=False,
) -> dict:
    """Drive load with `wrk` (C, kqueue — a fraction of the Python client's CPU,
    so on a single machine it doesn't starve the server for cores). Parses
    Requests/sec and the latency distribution. Falls back is the caller's job.

    The old default `min(concurrency, 4)` capped a big-machine run: 4 wrk
    threads can't saturate a many-core server, so throughput plateaus at the
    CLIENT's ceiling regardless of server config. When the client is pinned to
    a core set, default the thread count to that set's size; otherwise keep the
    conservative default so co-located small runs don't over-drive."""
    if not threads:
        pinned = core_count(cpu_cores)
        threads = pinned or min(concurrency, 4)
    threads = min(threads, concurrency)
    url = f"http://{host}:{port}{path}"

    def _run(dur: int) -> str:
        argv = wrap_command(
            [_WRK, f"-t{threads}", f"-c{concurrency}", f"-d{dur}s", "--latency", url],
            cpu_cores,
            numa,
        )
        r = subprocess.run(argv, capture_output=True, text=True, timeout=dur + 30)
        # Surface a launcher/wrk failure instead of returning empty output that
        # parses as 0 rps — otherwise a broken pin looks like a dead server.
        if r.returncode != 0 or "Requests/sec" not in r.stdout:
            detail = (r.stderr or r.stdout or "(no output)").strip().splitlines()
            print(
                f"[loadgen] wrk exited {r.returncode} for `{' '.join(argv[:3])} …` "
                f"— {detail[-1] if detail else '(no output)'}"
            )
        return r.stdout

    if warmup_s > 0:
        _run(max(1, int(round(warmup_s))))
    out = _run(max(1, int(round(duration_s))))

    raw_rps = 0.0
    m = re.search(r"Requests/sec:\s+([\d.]+)", out)
    if m:
        raw_rps = float(m.group(1))

    # Completed-request count + the ACTUAL measurement window wrk reports.
    completed, window_s = int(raw_rps * duration_s), duration_s
    m = re.search(r"(\d+) requests in ([\d.]+)(us|ms|s|m),", out)
    if m:
        completed = int(m.group(1))
        window_s = _wrk_ms(m.group(2), m.group(3)) / 1000.0

    # Error visibility: wrk folds 4xx/5xx responses INTO Requests/sec (a
    # load-shedding 503 storm reads as throughput) and reports starved/reset
    # connections only in these two lines. Ignoring them turns a shed or
    # starved cell into a fake number — parse both and make `throughput_rps`
    # the 2xx/3xx-only service rate. `raw_rps` keeps the wire rate.
    non2xx = 0
    m = re.search(r"Non-2xx or 3xx responses:\s+(\d+)", out)
    if m:
        non2xx = int(m.group(1))
    errs = {"connect": 0, "read": 0, "write": 0, "timeout": 0}
    m = re.search(
        r"Socket errors: connect (\d+), read (\d+), write (\d+), timeout (\d+)", out
    )
    if m:
        errs = {
            "connect": int(m.group(1)),
            "read": int(m.group(2)),
            "write": int(m.group(3)),
            "timeout": int(m.group(4)),
        }

    ok = max(completed - non2xx, 0)

    mean_ms = max_ms = 0.0
    m = re.search(
        r"Latency\s+([\d.]+)(us|ms|s|m)\s+[\d.]+(?:us|ms|s|m)\s+([\d.]+)(us|ms|s|m)",
        out,
    )
    if m:
        mean_ms = _wrk_ms(m.group(1), m.group(2))
        max_ms = _wrk_ms(m.group(3), m.group(4))

    # Little's law starvation detector: rps x mean latency = the number of
    # connections actually IN service. wrk reports latency only for requests
    # that completed, so a server serving 128 of 1024 connections (the rest
    # starved forever in an accept backlog) shows a beautiful p99 and a
    # `served_conns` of exactly 128 — while reporting ZERO socket errors.
    # This ratio is the only client-side signal that catches it.
    rps_all = (completed / window_s) if window_s > 0 else 0.0
    served_conns = rps_all * (mean_ms / 1000.0)
    served_frac = (served_conns / concurrency) if concurrency else 1.0

    def pct(p: int) -> float:
        mm = re.search(rf"\n\s+{p}%\s+([\d.]+)(us|ms|s|m)\b", out)
        return _wrk_ms(mm.group(1), mm.group(2)) if mm else 0.0

    return {
        "throughput_rps": (ok / window_s) if window_s > 0 else 0.0,
        "raw_rps": raw_rps,
        "non2xx": non2xx,
        "err_connect": errs["connect"],
        "err_read": errs["read"],
        "err_write": errs["write"],
        "err_timeout": errs["timeout"],
        "served_conns": served_conns,
        "served_frac": served_frac,
        "p50_ms": pct(50),
        "p90_ms": pct(90),
        "p99_ms": pct(99),
        "mean_ms": mean_ms,
        "max_ms": max_ms,
        "requests": completed,
        "concurrency": concurrency,
    }


def _read_response(sock: socket.socket) -> int:
    """Consume one full HTTP/1.1 response (headers + Content-Length body) so the
    keep-alive connection is clean for the next request. Returns the status
    code so callers can separate real service (2xx/3xx) from shed/error
    responses instead of counting both as throughput."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status_parts = lines[0].split(b" ", 2)
    status = int(status_parts[1]) if len(status_parts) >= 2 else 0
    cl = 0
    for line in lines[1:]:
        if line[:15].lower() == b"content-length:":
            cl = int(line.split(b":", 1)[1].strip())
            break
    body = rest
    while len(body) < cl:
        chunk = sock.recv(65536)
        if not chunk:
            break
        body += chunk
    return status


def _conn_client(host, port, req, warmup_deadline, stop_at, sink, non2xx_sink):
    try:
        s = socket.create_connection((host, port), timeout=30)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(30)
    except OSError:
        return
    non2xx = 0
    try:
        while time.monotonic() < warmup_deadline:  # warmup (uncounted)
            s.sendall(req)
            _read_response(s)
        lat: list[float] = []
        while time.monotonic() < stop_at:
            t0 = time.perf_counter()
            s.sendall(req)
            status = _read_response(s)
            # Only real service counts as throughput; a shed 503 (or any
            # error response) is tallied separately so a load-shedding server
            # can't inflate its rps with fast failure responses.
            if 200 <= status < 400:
                lat.append(time.perf_counter() - t0)
            else:
                non2xx += 1
        sink.append(lat)
    except OSError:
        # Connection starved (threaded ceiling) or reset — contributes 0
        # completed requests, which is exactly the signal we want to capture.
        pass
    finally:
        non2xx_sink.append(non2xx)
        s.close()


def _proc(host, port, path, n_conns, warmup_s, duration_s, q):
    req = (
        f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: keep-alive\r\n\r\n"
    ).encode()
    warmup_deadline = time.monotonic() + warmup_s
    stop_at = warmup_deadline + duration_s
    sink: list[list[float]] = []
    non2xx_sink: list[int] = []
    threads = [
        threading.Thread(
            target=_conn_client,
            args=(host, port, req, warmup_deadline, stop_at, sink, non2xx_sink),
        )
        for _ in range(n_conns)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    q.put(([x for lat in sink for x in lat], sum(non2xx_sink)))


def run_load(
    host: str,
    port: int,
    path: str,
    concurrency: int,
    duration_s: float = 3.0,
    warmup_s: float = 1.0,
    procs: int | None = None,
) -> dict:
    """Drive `concurrency` closed-loop keep-alive connections and return a
    metrics dict (throughput + latency percentiles) over the measurement window."""
    procs = procs or min(concurrency, max(1, os.cpu_count() or 4))
    per = [concurrency // procs] * procs
    for i in range(concurrency % procs):
        per[i] += 1

    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    processes = [
        ctx.Process(
            target=_proc, args=(host, port, path, per[i], warmup_s, duration_s, q)
        )
        for i in range(procs)
        if per[i] > 0
    ]
    for p in processes:
        p.start()
    latencies: list[float] = []
    non2xx_total = 0
    for _ in processes:
        lats, non2xx = q.get()
        latencies.extend(lats)
        non2xx_total += non2xx
    for p in processes:
        p.join()

    out = summarize(latencies, duration_s)
    out["concurrency"] = concurrency
    out["non2xx"] = non2xx_total
    out["raw_rps"] = out["throughput_rps"] + (
        non2xx_total / duration_s if duration_s > 0 else 0.0
    )
    return out
