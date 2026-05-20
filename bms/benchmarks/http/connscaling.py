"""Connection-scaling load: many keep-alive connections, each MOSTLY IDLE
(a request, then think-time, repeat). This is the workload that separates the
connection models — the one the busy closed-loop benchmark can't show:

- thread-per-connection (hyperdjango threaded, and Flask/gunicorn-gthread slots):
  a worker is pinned to a connection for its whole life, *including think-time*,
  so only ~W connections can ever be served; the rest starve.
- multiplexing (hyperdjango reactor, and FastAPI/uvicorn async): an idle
  connection costs only an fd in the event loop; a worker touches it only while a
  request is actually in flight — so N >> W idle connections are all served.

We measure, per connection-count N: aggregate throughput, latency percentiles of
served requests, and the fraction of connections that got served at all. The
served fraction + throughput collapse for the blocking models at N > slot-count,
while the multiplexing models keep scaling — which is exactly the reactor's point.

One asyncio client process multiplexes all N connections efficiently, so the
client itself doesn't impose a thread-per-connection ceiling.
"""

from __future__ import annotations

import asyncio
import contextlib
import time


async def _read_response(reader: asyncio.StreamReader) -> int:
    """Consume one full HTTP/1.1 response (headers + Content-Length body) and
    return its status code (0 if unparseable)."""
    header = await reader.readuntil(b"\r\n\r\n")
    status = 0
    first = header.split(b"\r\n", 1)[0].split(b" ")
    if len(first) >= 2:
        try:
            status = int(first[1])
        except ValueError:
            status = 0
    cl = 0
    for line in header.split(b"\r\n"):
        if line[:15].lower() == b"content-length:":
            try:
                cl = int(line.split(b":", 1)[1].strip())
            except ValueError:
                cl = 0
            break
    if cl:
        await reader.readexactly(cl)
    return status


async def _one_conn(
    host, port, req, think_s, warmup_end, stop_t, lat, ctr, req_timeout
):
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=req_timeout
        )
    except Exception:  # noqa: BLE001
        ctr["failed_conn"] += 1
        return
    got_ok = False  # got a real (2xx) response — actual service
    got_shed = False  # got a 503 — cleanly rejected (load-shed), not service
    try:
        while time.monotonic() < stop_t:
            t0 = time.monotonic()
            try:
                writer.write(req)
                await asyncio.wait_for(writer.drain(), timeout=req_timeout)
                status = await asyncio.wait_for(
                    _read_response(reader), timeout=req_timeout
                )
            except Exception:  # noqa: BLE001 — timeout/reset = this connection is starved
                ctr["errors"] += 1
                break
            if status == 503:
                got_shed = True
                break  # shed responses close the connection
            if 200 <= status < 300:
                if t0 >= warmup_end:  # count only the measurement window
                    lat.append((time.monotonic() - t0) * 1000.0)
                got_ok = True
            if think_s > 0:
                await asyncio.sleep(think_s)
    finally:
        if got_ok:
            ctr["served_conns"] += 1
        elif got_shed:
            ctr["shed_conns"] += 1
        with contextlib.suppress(Exception):
            writer.close()


async def _run(host, port, path, n_conns, think_s, duration_s, warmup_s, req_timeout):
    req = (
        f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: keep-alive\r\n\r\n"
    ).encode()
    ctr = {"served_conns": 0, "shed_conns": 0, "failed_conn": 0, "errors": 0}
    lat: list[float] = []
    now = time.monotonic()
    warmup_end = now + warmup_s
    stop_t = warmup_end + duration_s
    tasks = [
        asyncio.create_task(
            _one_conn(
                host, port, req, think_s, warmup_end, stop_t, lat, ctr, req_timeout
            )
        )
        for _ in range(n_conns)
    ]
    await asyncio.gather(*tasks, return_exceptions=True)

    lat.sort()

    def pct(p):
        if not lat:
            return 0.0
        return lat[min(len(lat) - 1, int(len(lat) * p))]

    return {
        "n_conns": n_conns,
        # throughput and latency count only real (2xx) requests, never shed 503s.
        "throughput_rps": len(lat) / duration_s if duration_s else 0.0,
        "p50_ms": pct(0.50),
        "p90_ms": pct(0.90),
        "p99_ms": pct(0.99),
        "served_conns": ctr["served_conns"],
        "served_frac": (ctr["served_conns"] / n_conns * 100.0) if n_conns else 0.0,
        "shed_frac": (ctr["shed_conns"] / n_conns * 100.0) if n_conns else 0.0,
        "errors": ctr["errors"],
        "failed_conn": ctr["failed_conn"],
    }


def run_conn_scaling(
    host: str,
    port: int,
    path: str,
    n_conns: int,
    think_ms: float = 25.0,
    duration_s: float = 3.0,
    warmup_s: float = 0.5,
    req_timeout: float = 5.0,
) -> dict:
    """Open `n_conns` keep-alive connections, each doing request→think→repeat.
    Returns throughput / latency percentiles / served-connection fraction."""
    return asyncio.run(
        _run(
            host,
            port,
            path,
            n_conns,
            think_ms / 1000.0,
            duration_s,
            warmup_s,
            req_timeout,
        )
    )
