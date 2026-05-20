#!/usr/bin/env python3
"""Connection-scaling capacity headline, client-ceiling annotation, and the
sharded think-time client's aggregation arithmetic.

The conn-scaling regime's question is CAPACITY (`max_held` — the largest
connection count a server held with essentially every connection in service
inside a stated p99 bound), not peak rps: rps there is ~(connections held) x
(1 / think-time), so it restates an architectural cap AND inherits the shared
load generator's ceiling. These tests pin the three pieces that make that
reading trustworthy:

  - max_held: the cap is found at the right ladder step, the p99 bound is
    enforced, and the served-fraction threshold is exact at its boundary;
  - the client-ceiling annotation: it fires on a plateau that keeps full
    service and healthy latency, and does NOT fire when the flat rps is
    explained by the server (starved connections, blown latency, collapse);
  - shard aggregation: K worker processes fold into ONE cell with the same
    recorded field names, rates summed and percentiles taken from the MERGED
    sample (per-shard percentiles do not average).

Usage:
    uv run hyper-test bench_conn_capacity
"""

# hyper-test: unit

from benchmarks.http.connscaling import (
    aggregate_shards,
    default_shard_count,
    split_conns,
)
from benchmarks.http.report import (
    CS_P99_BOUND_MS,
    conn_scaling_capacity,
    conn_scaling_verdict,
    render_conn_scaling_markdown,
)
from hyperdjango.testkit import check, finish, run_main

LADDER = [8, 64, 256, 512, 1024, 2048, 4096]


def _cell(fw: str, n: int, rps: float, p99: float, served: float = 100.0) -> dict:
    """One archived conn-scaling row (served_frac is a PERCENT, as recorded)."""
    return {
        "framework": fw,
        "payload": "plaintext",
        "conns": n,
        "n_conns": n,
        "throughput_rps": rps,
        "p50_ms": p99 / 3,
        "p90_ms": p99 / 2,
        "p99_ms": p99,
        "served_frac": served,
        "shed_frac": 0.0,
    }


def _sweep(rows: list[dict], frameworks: list[str], bound: float | None = None) -> dict:
    meta = {"frameworks": frameworks, "conns": LADDER, "workers": 256, "think_ms": 25.0}
    if bound is not None:
        meta["cs_p99_bound_ms"] = bound
    return meta


def _cap(rows: list[dict], fw: str, bound: float | None = None):
    caps = conn_scaling_capacity(rows, _sweep(rows, [fw], bound))
    return caps[0]


def _max_held_checks() -> None:
    # A thread-per-connection cap: full service to W, then half the connections
    # starve. rps is FLAT across the whole cap (it restates ~W x 1/think-time) —
    # the capacity finding lives entirely in the served fraction.
    threaded = (
        [
            _cell("threaded", 8, 308.0, 0.6),
            _cell("threaded", 64, 2404.0, 1.6),
            _cell("threaded", 256, 9398.0, 2.6),
        ]
        + [_cell("threaded", 512, 9434.0, 2.4, served=50.0)]
        + [_cell("threaded", 1024, 9383.0, 2.5, served=25.0)]
        + [_cell("threaded", 2048, 9313.0, 3.0, served=12.5)]
        + [_cell("threaded", 4096, 9310.0, 3.3, served=6.2)]
    )
    c = _cap(threaded, "threaded")
    check("connection cap found at the last fully-served step", c.max_held == 256)
    check("cap is not ladder-limited", not c.ladder_limited)
    check("first unheld step named", c.first_fail == 512)
    check("cap reason is the served fraction", "served" in c.fail_reason)
    check("held label is the bare count", c.held_label == "256")
    check(
        "peak rps is reported but is NOT the capacity",
        c.peak_rps > c.held_rps and c.peak_rps_conns == 512,
    )

    # Full service the whole way, latency blows the bound partway up.
    slow = [
        _cell("slow", 8, 300.0, 1.0),
        _cell("slow", 64, 2400.0, 2.0),
        _cell("slow", 256, 9600.0, 2.0),
        _cell("slow", 512, 18000.0, 3.0),
        _cell("slow", 1024, 19400.0, 48.0),
        _cell("slow", 2048, 10200.0, 389.0),
        _cell("slow", 4096, 12500.0, 611.0),
    ]
    c = _cap(slow, "slow")
    check("p99 bound caps max_held", c.max_held == 1024)
    check(
        "p99 reason names the bound", "p99" in c.fail_reason and "250" in c.fail_reason
    )
    check("held p99 is the cell's own p99", abs(c.held_p99_ms - 48.0) < 1e-9)

    # Bound is configurable and travels with the run.
    c = _cap(slow, "slow", bound=400.0)
    check("looser bound from meta raises max_held", c.max_held == 2048)
    c = _cap(slow, "slow", bound=10.0)
    check("tighter bound from meta lowers max_held", c.max_held == 512)
    caps = conn_scaling_capacity(
        slow, _sweep(slow, ["slow"], bound=400.0), p99_bound_ms=10.0
    )
    check("explicit bound argument overrides meta", caps[0].max_held == 512)

    # served_frac threshold is exact at its boundary (percent units).
    boundary = [
        _cell("edge", 8, 300.0, 1.0),
        _cell("edge", 64, 2400.0, 1.0, served=99.0),
        _cell("edge", 256, 9600.0, 1.0, served=98.9),
        _cell("edge", 512, 18000.0, 1.0, served=100.0),
    ]
    c = _cap(boundary, "edge")
    check("served exactly at the threshold counts as held", c.max_held == 512)
    caps = conn_scaling_capacity(
        [r for r in boundary if r["conns"] <= 256], _sweep([], ["edge"])
    )
    check("served just under the threshold is not held", caps[0].max_held == 64)

    # Held the top of the ladder: the sweep never found the cap.
    scaling = [_cell("reactor", n, min(n * 37.0, 23000.0), 5.0) for n in LADDER]
    c = _cap(scaling, "reactor")
    check("holding the ladder top is ladder-limited", c.ladder_limited)
    check("ladder-limited max_held is the top step", c.max_held == 4096)
    check("ladder-limited label says at-least", c.held_label == ">=4096")
    check("ladder-limited names no failing step", c.first_fail == 0)

    # Nothing held anywhere.
    dead = [_cell("dead", n, 100.0, 1.0, served=10.0) for n in LADDER]
    c = _cap(dead, "dead")
    check("a series that holds nothing reports max_held 0", c.max_held == 0)
    check("a series that holds nothing labels 'none'", c.held_label == "none")

    # A 0-rps cell is a dead server, not a held connection count.
    zeroed = [_cell("z", 8, 300.0, 1.0), _cell("z", 64, 0.0, 0.0)]
    c = _cap(zeroed, "z")
    check("zero-throughput cell is never held", c.max_held == 8)

    check(
        "default p99 bound is the documented 250ms",
        CS_P99_BOUND_MS == 250.0,
    )


def _plateau_checks() -> None:
    # Flat rps, every connection served, p99 well inside the bound: the server
    # is not the thing that stopped scaling.
    flat = [
        _cell("a", 8, 300.0, 1.0),
        _cell("a", 64, 2400.0, 2.0),
        _cell("a", 256, 9600.0, 3.0),
        _cell("a", 512, 19000.0, 4.0),
        _cell("a", 1024, 20000.0, 12.0),
        _cell("a", 2048, 20200.0, 42.0),
        _cell("a", 4096, 19800.0, 105.0),
    ]
    c = _cap(flat, "a")
    check("plateau with full service + healthy p99 is flagged", c.plateau)
    check("plateau span starts where growth stopped", c.plateau_from == 1024)
    check("plateau span runs to the ladder top", c.plateau_to == 4096)
    check(
        "annotation names the generator ceiling and the lower bound",
        "load-generator ceiling" in c.note
        and "LOWER BOUND" in c.note
        and "max_held" in c.note,
    )

    # Same flat rps, but half the connections are starved — this flat curve IS
    # the server's cap, and must NOT be blamed on the client.
    starved = [
        _cell("b", 8, 300.0, 1.0),
        _cell("b", 64, 2400.0, 2.0),
        _cell("b", 256, 9400.0, 2.5),
        _cell("b", 512, 9434.0, 2.4, served=50.0),
        _cell("b", 1024, 9383.0, 2.5, served=25.0),
        _cell("b", 2048, 9313.0, 3.0, served=12.5),
    ]
    check(
        "flat rps with starved connections is not a client ceiling",
        not _cap(starved, "b").plateau,
    )

    # Flat rps but the latency bound is blown: the server is bending.
    laggy = [
        _cell("c", 256, 9600.0, 3.0),
        _cell("c", 512, 19000.0, 300.0),
        _cell("c", 1024, 19200.0, 480.0),
        _cell("c", 2048, 19300.0, 900.0),
    ]
    check(
        "flat rps outside the p99 bound is not a client ceiling",
        not _cap(laggy, "c").plateau,
    )

    # A steep fall is a collapse, a different finding with a different cause.
    collapse = [
        _cell("d", 256, 9600.0, 3.0),
        _cell("d", 512, 20000.0, 5.0),
        _cell("d", 1024, 6000.0, 40.0),
    ]
    check("a collapse is not read as a plateau", not _cap(collapse, "d").plateau)

    # A flat region far below the series' own best is not that series' ceiling.
    early_flat = [
        _cell("e", 8, 300.0, 1.0),
        _cell("e", 64, 305.0, 1.0),
        _cell("e", 256, 310.0, 1.0),
        _cell("e", 512, 9000.0, 2.0),
        _cell("e", 1024, 20000.0, 5.0),
    ]
    check(
        "a low flat region is not the series ceiling", not _cap(early_flat, "e").plateau
    )

    # Monotonic growth to the ladder top: nothing to annotate.
    growing = [_cell("f", n, n * 37.0, 3.0) for n in LADDER]
    check("a still-scaling series is not annotated", not _cap(growing, "f").plateau)


def _verdict_checks() -> None:
    def series(fw: str, top_rps: float) -> list[dict]:
        return [
            _cell(fw, 8, 300.0, 1.0),
            _cell(fw, 64, 2400.0, 2.0),
            _cell(fw, 256, 9600.0, 3.0),
            _cell(fw, 512, top_rps * 0.85, 5.0),
            _cell(fw, 1024, top_rps, 12.0),
            _cell(fw, 2048, top_rps * 1.01, 40.0),
            _cell(fw, 4096, top_rps * 0.98, 100.0),
        ]

    close = series("reactor", 23000.0) + series("flask", 21500.0)
    caps = conn_scaling_capacity(close, _sweep(close, ["reactor", "flask"]))
    lines = conn_scaling_verdict(caps)
    notes = [line for line in lines if line.startswith("SWEEP NOTE:")]
    check("clustered plateaus raise the shared-generator note", len(notes) == 1)
    check(
        "sweep note names the suspected shared generator",
        "load generator" in notes[0] and "max_held" in notes[0],
    )
    check(
        "sweep note names every clustered framework",
        "reactor" in notes[0] and "flask" in notes[0],
    )
    check(
        "every framework gets a capacity line",
        sum(1 for line in lines if line.startswith(("reactor:", "flask:"))) == 2,
    )
    check(
        "capacity lines lead with max_held",
        all("max_held=" in line for line in lines if line.startswith("reactor:")),
    )

    far = series("reactor", 60000.0) + series("flask", 21500.0)
    caps = conn_scaling_capacity(far, _sweep(far, ["reactor", "flask"]))
    check(
        "plateaus far apart raise no shared-generator note",
        not [
            line
            for line in conn_scaling_verdict(caps)
            if line.startswith("SWEEP NOTE:")
        ],
    )

    lone = series("reactor", 23000.0)
    caps = conn_scaling_capacity(lone, _sweep(lone, ["reactor"]))
    check(
        "a single plateau is not a shared-generator claim",
        not [
            line
            for line in conn_scaling_verdict(caps)
            if line.startswith("SWEEP NOTE:")
        ],
    )

    # Verdict ordering: the highest capacity leads.
    ordered = [line for line in conn_scaling_verdict(caps) if not line.startswith(" ")]
    check("verdict is non-empty for a measured sweep", bool(ordered))

    # Markdown headline renders from archive-shaped rows alone.
    md = render_conn_scaling_markdown(close, _sweep(close, ["reactor", "flask"]))
    check(
        "markdown headlines max_held", "max_held" in md and "Capacity (headline)" in md
    )
    check("markdown keeps the rps curve beneath", "Throughput (req/s)" in md)
    check("markdown carries the sweep note", "SWEEP NOTE:" in md)
    check("markdown states the p99 bound", "250" in md)


def _shard_checks() -> None:
    check("connections split evenly across shards", split_conns(1024, 4) == [256] * 4)
    check("remainder spreads one per shard", split_conns(10, 4) == [3, 3, 2, 2])
    check(
        "every connection is assigned exactly once", sum(split_conns(4095, 7)) == 4095
    )
    check(
        "shard sizes differ by at most one",
        max(split_conns(4095, 7)) - min(split_conns(4095, 7)) <= 1,
    )
    check("more shards than connections clamps to N", split_conns(3, 8) == [1, 1, 1])
    check("a single shard takes everything", split_conns(512, 1) == [512])

    check("shard count floors at 2 on a small client", default_shard_count("0-3") == 2)
    check("shard count is one per 8 client cores", default_shard_count("0-63") == 8)
    check("shard count reads a comma list", default_shard_count("0-31,64-95") == 8)

    # Aggregation arithmetic: rates sum, counters sum, percentiles come from the
    # MERGED sample (a per-shard p99 averaged across shards is not the cell's).
    fast = (
        [1.0] * 100,
        {"served_conns": 50, "shed_conns": 0, "failed_conn": 0, "errors": 0},
    )
    slow = (
        [100.0] * 100,
        {"served_conns": 50, "shed_conns": 2, "failed_conn": 1, "errors": 3},
    )
    cell = aggregate_shards(100, [fast, slow], duration_s=2.0)
    check("throughput sums across shards", cell["throughput_rps"] == 100.0)
    check("served connections sum across shards", cell["served_conns"] == 100)
    check("served fraction is a percent of the cell's N", cell["served_frac"] == 100.0)
    check("shed fraction is a percent of the cell's N", cell["shed_frac"] == 2.0)
    check("errors sum across shards", cell["errors"] == 3)
    check("failed connections sum across shards", cell["failed_conn"] == 1)
    check("p99 comes from the merged sample", cell["p99_ms"] == 100.0)
    check("p50 comes from the merged sample", cell["p50_ms"] == 100.0)

    # One shard carrying the same total sample records the same cell.
    merged = aggregate_shards(
        100,
        [
            (
                [1.0] * 100 + [100.0] * 100,
                {"served_conns": 100, "shed_conns": 2, "failed_conn": 1, "errors": 3},
            )
        ],
        duration_s=2.0,
    )
    check("K shards and 1 shard agree on the same sample", merged == cell)

    # The recorded field names must not depend on the shard count.
    single = aggregate_shards(8, [([1.0] * 8, {"served_conns": 8})], duration_s=1.0)
    expected = {
        "n_conns",
        "throughput_rps",
        "p50_ms",
        "p90_ms",
        "p99_ms",
        "served_conns",
        "served_frac",
        "shed_frac",
        "errors",
        "failed_conn",
    }
    check("recorded field names are shard-count independent", set(single) == expected)
    check("field names match the multi-shard cell", set(cell) == expected)
    check("a missing shard counter defaults to zero", single["errors"] == 0)

    empty = aggregate_shards(16, [([], {"failed_conn": 16})], duration_s=2.0)
    check("an all-failed cell records zero throughput", empty["throughput_rps"] == 0.0)
    check("an all-failed cell records zero percentiles", empty["p99_ms"] == 0.0)
    check(
        "an all-failed cell records zero served fraction", empty["served_frac"] == 0.0
    )


def main() -> bool:
    _max_held_checks()
    _plateau_checks()
    _verdict_checks()
    _shard_checks()
    return finish()


if __name__ == "__main__":
    run_main(main)
