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
TRACKED = (GAUGE_ACTIVE, COUNTER_RESPONSES, COUNTER_RESP_5XX, *POOL_KEYS)


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
    pool_exposed: bool = False

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
            "pool_exposed": self.pool_exposed,
            "pool_deltas": pool,
        }
