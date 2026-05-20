"""Scrape the server's OWN contention counters from its /metrics endpoint.

The native Zig core registers a small set of server metrics (see
`zig/src/server.zig`, `initServerMetrics`) that are the contention signals for a
scaling collapse:

  hyperdjango_native_connections_active   in-flight requests (the single global
                                          `active_requests` atomic mirrored into
                                          a gauge — the cache line every core
                                          fetchAdd/fetchSub's on every request)
  hyperdjango_native_responses_total      server-side completed responses; its
                                          before/after delta spans warmup + the
                                          measured window (the baseline scrape is
                                          taken before the driver's in-band
                                          warmup), so it is a server-truth
                                          sanity cross-check, not a window-exact
                                          request count

The DB pool contention gauges (`hyperdjango_pool_waiters`,
`hyperdjango_pool_max_waiters`, ...) are also parsed when present, but the echo
workload touches no database, so they stay absent here — recorded as null and
flagged "inferred" rather than fabricated. Note the pool exposes `waiters` /
`max_waiters` gauges but no cumulative `wait_total_ns` series (pool_stats carries
no such field), so that particular signal is not obtainable from /metrics.
"""

from __future__ import annotations

import re
import urllib.request
from dataclasses import dataclass, field

# Native server gauges/counters (zig/src/server.zig initServerMetrics).
GAUGE_ACTIVE = "hyperdjango_native_connections_active"
COUNTER_RESPONSES = "hyperdjango_native_responses_total"
COUNTER_RESP_5XX = "hyperdjango_native_responses_5xx_total"
# Accept-side truth: the kernel drops listen-queue overflow SILENTLY (no errno
# anywhere), so the accepted-count delta is the ONLY server-side way to split
# "connection never accepted" from "accepted but never serviced" when a load
# cell reports served_frac < 1 with zero errors.
COUNTER_ACCEPTED = "hyperdjango_native_accepted_connections_total"
GAUGE_ACCEPT_BURST = "hyperdjango_native_accept_burst_max"

# Post-accept connection state. `accepted_delta` proves a connection reached the
# server; these say what happened to it afterwards. A steady non-zero
# `parked_unserved` under load is the direct fingerprint of a starved keep-alive
# set: connections the reactor holds armed but never dispatches, which show up
# nowhere in throughput, latency or error counts.
GAUGE_PARKED = "hyperdjango_native_reactor_parked_connections"
GAUGE_PARKED_UNSERVED = "hyperdjango_native_reactor_parked_unserved"
GAUGE_QUEUE_DEPTH = "hyperdjango_native_reactor_queue_depth"
COUNTER_DISPATCHED = "hyperdjango_native_reactor_dispatched_total"
COUNTER_REARM = "hyperdjango_native_reactor_rearm_total"
COUNTER_REARM_FAIL = "hyperdjango_native_reactor_rearm_failures_total"
COUNTER_REQUEUE = "hyperdjango_native_reactor_requeue_total"

# Gauges whose peak DURING the load window is the signal (an after-the-fact
# scrape sees an idle server and reports zeros).
PEAK_GAUGES = (
    GAUGE_ACTIVE,
    GAUGE_PARKED,
    GAUGE_PARKED_UNSERVED,
    GAUGE_QUEUE_DEPTH,
)

# DB pool contention gauges (hyperdjango/database.py, exact registered names).
# Absent for the echo workload; parsed opportunistically so a DB-backed route
# would populate them. `max_waiters`/`waiters` are the pool contention signals;
# there is no cumulative wait_total_ns series to scrape.
POOL_KEYS = (
    "hyperdjango_pool_waiters",
    "hyperdjango_pool_max_waiters",
    "hyperdjango_pool_acquires",
    "hyperdjango_pool_timeouts",
    "hyperdjango_pool_in_use_connections",
)

_SCRAPE_TIMEOUT_S = 5.0
# A Prometheus sample line: `metric_name{labels} value` or `metric_name value`.
_SAMPLE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)(?:\{[^}]*\})?\s+(?P<val>[-+0-9.eEnaN]+)\s*$"
)

# The keys the harness reads out of a scrape, for stable JSON/report columns.
TRACKED = (
    GAUGE_ACTIVE,
    COUNTER_RESPONSES,
    COUNTER_RESP_5XX,
    COUNTER_ACCEPTED,
    GAUGE_ACCEPT_BURST,
    GAUGE_PARKED,
    GAUGE_PARKED_UNSERVED,
    GAUGE_QUEUE_DEPTH,
    COUNTER_DISPATCHED,
    COUNTER_REARM,
    COUNTER_REARM_FAIL,
    COUNTER_REQUEUE,
    *POOL_KEYS,
)


def parse_prometheus(text: str) -> dict[str, float]:
    """Parse Prometheus exposition text into {metric_name: value}.

    Only the tracked series are retained. Repeated (labeled) series of the same
    name are summed, which is the correct aggregate for the per-status counters.
    """
    out: dict[str, float] = {}
    tracked = set(TRACKED)
    for line in text.splitlines():
        if not line or line[0] == "#":
            continue
        m = _SAMPLE_RE.match(line)
        if not m:
            continue
        name = m.group("name")
        if name not in tracked:
            continue
        try:
            val = float(m.group("val"))
        except ValueError:
            continue
        out[name] = out.get(name, 0.0) + val
    return out


def scrape(host: str, port: int, path: str = "/metrics") -> dict[str, float]:
    """GET the /metrics endpoint and return the tracked series. Never raises —
    a scrape failure yields an empty dict so the load result still stands."""
    url = f"http://{host}:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=_SCRAPE_TIMEOUT_S) as resp:  # noqa: S310  (loopback bench)
            body = resp.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001  — best-effort telemetry, never fail the cell
        return {}
    return parse_prometheus(body)


@dataclass(slots=True)
class ContentionSample:
    """A before/after pair of /metrics scrapes around one load window, plus the
    peak in-flight gauge sampled DURING the window."""

    before: dict[str, float] = field(default_factory=dict)
    after: dict[str, float] = field(default_factory=dict)
    active_peak: float = 0.0
    # In-window peaks for every PEAK_GAUGES series, keyed by metric name.
    peaks: dict[str, float] = field(default_factory=dict)
    # Every in-window sample per PEAK_GAUGES series. A peak alone is misleading
    # here: the driver runs warmup and measurement as two separate client
    # processes, so between them every connection closes and reopens — a
    # "1024 connections parked, queue empty" peak can be that reconnect instant
    # rather than steady state. The mean over samples where the server is
    # actually working is the honest steady-state view.
    series: dict[str, list[float]] = field(default_factory=dict)
    pool_exposed: bool = False

    def mean(self, key: str) -> float | None:
        vals = self.series.get(key) or []
        return (sum(vals) / len(vals)) if vals else None

    def delta(self, key: str) -> float | None:
        """Post-minus-pre delta for a counter, or None if not exposed."""
        if key not in self.before and key not in self.after:
            return None
        return self.after.get(key, 0.0) - self.before.get(key, 0.0)

    def responses_delta(self) -> float | None:
        return self.delta(COUNTER_RESPONSES)

    def to_dict(self) -> dict:
        pool = {k: self.delta(k) for k in POOL_KEYS}
        return {
            "active_peak": self.active_peak,
            "responses_delta": self.responses_delta(),
            "responses_5xx_delta": self.delta(COUNTER_RESP_5XX),
            # Delta spans the same warmup+measure window as responses_delta —
            # compare against total connections the driver opened, not rps.
            "accepted_delta": self.delta(COUNTER_ACCEPTED),
            # Point-in-time high-water mark, not a delta: largest single-wakeup
            # accept drain since server start.
            "accept_burst_max": self.after.get(GAUGE_ACCEPT_BURST),
            # Post-accept connection state — peaks sampled DURING the window.
            "parked_peak": self.peaks.get(GAUGE_PARKED),
            "parked_mean": self.mean(GAUGE_PARKED),
            # Steady-state, not the peak: every measurement window opens with a
            # fresh set of connections that are armed but have not been served
            # yet, so the PEAK of this series always counts the whole reconnect
            # front. Only the in-window mean says whether a set stays unserved.
            "parked_unserved_mean": self.mean(GAUGE_PARKED_UNSERVED),
            "queue_depth_mean": self.mean(GAUGE_QUEUE_DEPTH),
            "active_mean": self.mean(GAUGE_ACTIVE),
            "parked_unserved_peak": self.peaks.get(GAUGE_PARKED_UNSERVED),
            "queue_depth_peak": self.peaks.get(GAUGE_QUEUE_DEPTH),
            "dispatched_delta": self.delta(COUNTER_DISPATCHED),
            "rearm_delta": self.delta(COUNTER_REARM),
            "rearm_fail_delta": self.delta(COUNTER_REARM_FAIL),
            "requeue_delta": self.delta(COUNTER_REQUEUE),
            "pool_exposed": self.pool_exposed,
            "pool_deltas": pool,
        }
