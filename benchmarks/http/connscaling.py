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

CLIENT HEADROOM. An asyncio client multiplexes all N connections without a
thread-per-connection ceiling — but it is still ONE Python process on ONE core,
so above a few tens of thousands of requests/sec the GENERATOR becomes the
slowest term in the loop. That failure is silent and looks like a result: every
connection served, a healthy p99, and a flat rps that several architecturally
different servers all agree on. So the connections are SHARDED across K worker
processes (`procs=`), each running its own event loop over N/K connections, with
the per-process counts aggregated afterwards. The shards align on a barrier so
they share one measurement window. K=1 keeps the original single-process,
no-IPC path.

Capacity is reported as `max_held` (see report.py) rather than peak rps, for
the same reason: rps here is ~(connections held) x (1 / think-time), so it
restates the connection cap and inherits the generator's ceiling, while the
count of connections actually held does neither.
"""

from __future__ import annotations

import asyncio
import contextlib
import multiprocessing as mp
import os
import time

from benchmarks.http.affinity import core_count

# One shard per this many client cores. Each shard is an event loop pushing a
# few tens of thousands of requests/sec on one core; more shards than cores just
# adds context switching to the measurement.
CLIENT_CORES_PER_SHARD = 8
# Shards rendezvous here before warmup so every process measures the same
# window. Bounded so one dead shard can never hang the sweep.
SHARD_BARRIER_TIMEOUT_S = 60.0


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


async def _drive(
    host, port, path, n_conns, think_s, duration_s, warmup_s, req_timeout
) -> tuple[list[float], dict[str, int]]:
    """Run one shard's connections and return its raw (latencies_ms, counters)."""
    req = (
        f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: keep-alive\r\n\r\n"
    ).encode()
    ctr: dict[str, int] = {
        "served_conns": 0,
        "shed_conns": 0,
        "failed_conn": 0,
        "errors": 0,
    }
    lat: list[float] = []
    warmup_end = time.monotonic() + warmup_s
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
    return lat, ctr


def aggregate_shards(
    n_conns: int, shards: list[tuple[list[float], dict[str, int]]], duration_s: float
) -> dict:
    """Fold every shard's raw (latencies, counters) into ONE cell.

    The recorded field names are identical whether the cell was driven by one
    process or by K: rates sum, connection counts sum, and the percentiles come
    from the merged latency sample (percentiles do NOT average — a per-shard
    p99 averaged across shards is not the cell's p99)."""
    lat: list[float] = []
    ctr: dict[str, int] = {
        "served_conns": 0,
        "shed_conns": 0,
        "failed_conn": 0,
        "errors": 0,
    }
    for shard_lat, shard_ctr in shards:
        lat.extend(shard_lat)
        for k in ctr:
            ctr[k] += shard_ctr.get(k, 0)
    lat.sort()

    def pct(p: float) -> float:
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


def split_conns(n_conns: int, procs: int) -> list[int]:
    """Connections per shard — every connection assigned exactly once, sizes
    within one of each other, and never more shards than connections."""
    procs = max(1, min(procs, n_conns))
    per = [n_conns // procs] * procs
    for i in range(n_conns % procs):
        per[i] += 1
    return per


def default_shard_count(client_cores: str | None = None) -> int:
    """Shards for the think-time client: one per `CLIENT_CORES_PER_SHARD` client
    cores, floor 2. The floor is the point — a single Python process is the one
    ceiling this sweep cannot distinguish from a server result, so the default
    never leaves the generator undefended, even on a small box."""
    cores = core_count(client_cores) or os.cpu_count() or 2
    return max(2, cores // CLIENT_CORES_PER_SHARD)


def _shard_proc(
    host, port, path, n_conns, think_s, duration_s, warmup_s, req_timeout, barrier, q
) -> None:
    """One shard process: rendezvous, drive its connections, ship raw counts."""
    # A broken or late barrier must not lose a shard: measure anyway (the cell
    # is then slightly skewed) rather than drop N/K connections from the count.
    with contextlib.suppress(Exception):
        barrier.wait(timeout=SHARD_BARRIER_TIMEOUT_S)
    try:
        lat, ctr = asyncio.run(
            _drive(
                host, port, path, n_conns, think_s, duration_s, warmup_s, req_timeout
            )
        )
    except Exception:  # noqa: BLE001 — report an empty shard, never hang the parent
        lat, ctr = (
            [],
            {"served_conns": 0, "shed_conns": 0, "failed_conn": n_conns, "errors": 0},
        )
    q.put((lat, ctr))


def run_conn_scaling(
    host: str,
    port: int,
    path: str,
    n_conns: int,
    think_ms: float = 25.0,
    duration_s: float = 3.0,
    warmup_s: float = 0.5,
    req_timeout: float = 5.0,
    procs: int | None = None,
    client_cores: str | None = None,
) -> dict:
    """Open `n_conns` keep-alive connections, each doing request→think→repeat.
    Returns throughput / latency percentiles / served-connection fraction.

    `procs` shards the connections across worker processes so the generator
    itself is not the ceiling (default: `default_shard_count(client_cores)`).
    `procs=1` runs the original in-process path with no IPC."""
    think_s = think_ms / 1000.0
    k = procs or default_shard_count(client_cores)
    per = split_conns(n_conns, k)
    if len(per) == 1:
        lat, ctr = asyncio.run(
            _drive(
                host, port, path, n_conns, think_s, duration_s, warmup_s, req_timeout
            )
        )
        return aggregate_shards(n_conns, [(lat, ctr)], duration_s)

    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    barrier = ctx.Barrier(len(per))
    procs_list = [
        ctx.Process(
            target=_shard_proc,
            args=(
                host,
                port,
                path,
                n,
                think_s,
                duration_s,
                warmup_s,
                req_timeout,
                barrier,
                q,
            ),
        )
        for n in per
    ]
    for p in procs_list:
        p.start()
    shards = [q.get() for _ in procs_list]
    for p in procs_list:
        p.join()
    return aggregate_shards(n_conns, shards, duration_s)
