#!/usr/bin/env python3
"""WebSocket benchmark suite adapter + unified-record feed.

Pure-Python coverage of `benchmarks.websocket.suite` (the adapter that turns a
measured WebSocket ``results.json`` into a `benchmarks.core` suite block) and of
`benchmarks.websocket.refeed` (the entry point that feeds an EXISTING result set
into the shared cross-suite record without measuring anything). No servers, no
network, no box — every input here is synthetic.

Usage:
    uv run hyper-test bench_websocket_suite
"""

# hyper-test: unit

import importlib.util
import json
import tempfile
from pathlib import Path

from benchmarks.core.results import load_history, save_run
from benchmarks.http.suite import _HTTP_METRICS
from benchmarks.websocket import refeed
from benchmarks.websocket.suite import (
    _NV,
    _RV,
    _TV,
    SUITE_KEY,
    build_websocket_suite,
)
from hyperdjango.testkit import check, finish, run_main

_SWEEP_KEYS = {
    "throughput",
    "concurrency",
    "latency",
    "connection_scaling",
    "connection_model",
}


def _mp(payload: int, frame_type: str, mps: float) -> dict:
    """A `loadgen.MultiProcResult` as it lands in results.json."""
    duration = 5.0
    return {
        "uri": "ws://127.0.0.1:19901/ws",
        "n_procs": 6,
        "conns_per_proc": 4,
        "total_conns": 24,
        "duration_s": duration,
        "messages": int(mps * duration),
        "payload_size": payload,
        "frame_type": frame_type,
        "failed_conns": 0,
        "crashed_procs": 0,
        "msgs_per_sec": mps,
        "mb_per_sec": mps * payload * 2 / (1024 * 1024),
    }


def _conn(target: int, connected: int, connect_s: float) -> dict:
    return {
        "target": target,
        "connected": connected,
        "timed_out": target - connected,
        "connect_time_s": connect_s,
        "teardown_time_s": connect_s / 2,
    }


def _results() -> dict:
    """A synthetic results.json carrying every section the adapter reads."""
    return {
        "methodology": "synthetic fixture",
        "throughput": [
            {
                "payload_size": 64,
                "frame_type": "text",
                "concurrency": 24,
                "native": _mp(64, "text", 400_000.0),
                "reference": _mp(64, "text", 100_000.0),
            },
            {
                "payload_size": 4096,
                "frame_type": "text",
                "concurrency": 24,
                "native": _mp(4096, "text", 200_000.0),
                "reference": _mp(4096, "text", 50_000.0),
            },
            {
                "payload_size": 64,
                "frame_type": "binary",
                "concurrency": 24,
                "native": _mp(64, "binary", 390_000.0),
                "reference": _mp(64, "binary", 95_000.0),
            },
            {
                "payload_size": 4096,
                "frame_type": "binary",
                "concurrency": 24,
                "native": _mp(4096, "binary", 190_000.0),
                "reference": _mp(4096, "binary", 45_000.0),
            },
        ],
        "concurrency_scaling_throughput": [
            {
                "concurrency": 8,
                "native": _mp(4096, "text", 150_000.0),
                "reference": _mp(4096, "text", 40_000.0),
            },
            {
                "concurrency": 64,
                "native": _mp(4096, "text", 300_000.0),
                "reference": _mp(4096, "text", 45_000.0),
            },
        ],
        "latency": [
            {
                "payload_size": 64,
                "native": {"mean_us": 90.0, "p50_us": 85.0, "p99_us": 140.0},
                "reference": {"mean_us": 210.0, "p50_us": 200.0, "p99_us": 480.0},
            },
            {
                "payload_size": 4096,
                "native": {"mean_us": 120.0, "p50_us": 110.0, "p99_us": 260.0},
                "reference": {"mean_us": 260.0, "p50_us": 250.0, "p99_us": 700.0},
            },
        ],
        "connection_scaling": [
            {
                "target": 16,
                "native": _conn(16, 16, 0.02),
                "reference": _conn(16, 16, 0.05),
            },
            {
                "target": 96,
                "native": _conn(96, 96, 0.14),
                "reference": _conn(96, 90, 0.61),
            },
        ],
        "connection_model": {
            "shared": {
                "target_conns": 96,
                "connected": 96,
                "timed_out": 0,
                "msgs_per_sec": 310_000.0,
                "peak_rss_mb": 96.5,
                "peak_threads": 12,
            },
            "thread": {
                "target_conns": 96,
                "connected": 24,
                "timed_out": 72,
                "msgs_per_sec": 120_000.0,
                "peak_rss_mb": 180.25,
                "peak_threads": 30,
            },
        },
    }


def _suite_shape_checks() -> None:
    su = build_websocket_suite(_results())

    check("suite key is the websocket suite", su["key"] == SUITE_KEY)
    check("suite label names the subsystem", su["label"] == "WebSocket servers")
    check(
        "variants are native, reference and the thread-per-conn model",
        su["variants"] == [_NV, _RV, _TV],
        str(su["variants"]),
    )
    check(
        "every variant has a color",
        all(v in su["colors"] for v in su["variants"]),
        str(su["colors"]),
    )
    check(
        "every variant has a launch config",
        all(su["configs"].get(v) for v in su["variants"]),
        str(list(su["configs"])),
    )

    mkeys = [m["key"] for m in su["metrics"]]
    check(
        "headline metrics declared (throughput, bandwidth, latency, connections, memory)",
        set(mkeys)
        == {
            "mps",
            "gbps",
            "p50_us",
            "p99_us",
            "mean_us",
            "connect_ms",
            "connected",
            "rss",
            "threads",
        },
        str(mkeys),
    )
    by_key = {m["key"]: m for m in su["metrics"]}
    check("throughput unit is msgs/s", by_key["mps"]["unit"] == "msgs/s")
    http_gbps = next(m for m in _HTTP_METRICS if m["key"] == "gbps")
    check(
        "gbps declared exactly like the HTTP suite's bandwidth metric",
        by_key["gbps"]["unit"] == http_gbps["unit"] == "GB/s"
        and by_key["gbps"]["lower_is_better"] is False,
        f"{by_key['gbps']} vs {http_gbps}",
    )
    check("latency units are microseconds", by_key["p99_us"]["unit"] == "µs")
    check(
        "cost metrics are lower-is-better",
        all(
            by_key[k]["lower_is_better"]
            for k in ("p50_us", "p99_us", "mean_us", "connect_ms", "rss", "threads")
        ),
    )
    check(
        "capacity metrics are higher-is-better",
        not any(by_key[k]["lower_is_better"] for k in ("mps", "gbps", "connected")),
    )

    check(
        "all five headline sweeps present",
        set(su["sweeps"]) == _SWEEP_KEYS,
        str(sorted(su["sweeps"])),
    )

    # ── throughput: native vs reference msgs/sec per payload, faceted by frame
    tp = su["sweeps"]["throughput"]
    check("throughput x-axis is payload size, ascending", tp["xs"] == [64, 4096])
    check("throughput x-axis is log-scaled", tp["xlog"] is True)
    check(
        "throughput facets by frame type",
        [g["key"] for g in tp["groups"]] == ["text", "binary"],
        str(tp["groups"]),
    )
    check(
        "throughput carries a series per variant per frame type",
        set(tp["data"])
        == {f"{_NV}|text", f"{_RV}|text", f"{_NV}|binary", f"{_RV}|binary"},
        str(sorted(tp["data"])),
    )
    check(
        "native msgs/sec per payload preserved",
        tp["data"][f"{_NV}|text"]["mps"] == [400_000.0, 200_000.0],
    )
    check(
        "reference msgs/sec per payload preserved",
        tp["data"][f"{_RV}|text"]["mps"] == [100_000.0, 50_000.0],
    )
    check(
        "text and binary rows are NOT collapsed onto each other",
        tp["data"][f"{_NV}|binary"]["mps"] == [390_000.0, 190_000.0],
        str(tp["data"][f"{_NV}|binary"]["mps"]),
    )
    check(
        "gbps = msgs/sec x payload x2 / 1e9 (send + echo)",
        tp["data"][f"{_NV}|text"]["gbps"]
        == [400_000.0 * 64 * 2 / 1e9, 200_000.0 * 4096 * 2 / 1e9],
        str(tp["data"][f"{_NV}|text"]["gbps"]),
    )
    check("throughput declares its own metric set", tp["metrics"] == ["mps", "gbps"])
    check(
        "throughput compares native against the reference only",
        tp["variants"] == [_NV, _RV],
    )

    # ── concurrency
    co = su["sweeps"]["concurrency"]
    check("concurrency x-axis is connection count", co["xs"] == [8, 64])
    check(
        "concurrency records both variants' throughput",
        co["data"][f"{_NV}|"]["mps"] == [150_000.0, 300_000.0]
        and co["data"][f"{_RV}|"]["mps"] == [40_000.0, 45_000.0],
    )
    check("concurrency also carries bandwidth", co["metrics"] == ["mps", "gbps"])

    # ── latency percentiles
    la = su["sweeps"]["latency"]
    check(
        "latency exposes p50/p99/mean",
        la["metrics"] == ["p50_us", "p99_us", "mean_us"],
    )
    check(
        "native latency percentiles preserved",
        la["data"][f"{_NV}|"]["p50_us"] == [85.0, 110.0]
        and la["data"][f"{_NV}|"]["p99_us"] == [140.0, 260.0]
        and la["data"][f"{_NV}|"]["mean_us"] == [90.0, 120.0],
    )
    check(
        "reference latency percentiles preserved",
        la["data"][f"{_RV}|"]["p99_us"] == [480.0, 700.0],
    )

    # ── connection scaling
    cs = su["sweeps"]["connection_scaling"]
    check(
        "connect time converted to milliseconds",
        cs["data"][f"{_NV}|"]["connect_ms"] == [20.0, 140.0],
        str(cs["data"][f"{_NV}|"]["connect_ms"]),
    )
    check(
        "connections established travel with connect time",
        cs["data"][f"{_RV}|"]["connected"] == [16, 90],
    )
    check(
        "connection scaling declares both its metrics",
        cs["metrics"] == ["connect_ms", "connected"],
    )

    # ── connection model (shared default vs thread opt-out)
    cm = su["sweeps"]["connection_model"]
    check(
        "connection model compares the two NATIVE models",
        cm["variants"] == [_NV, _TV],
        str(cm["variants"]),
    )
    check(
        "connection model does not draw the reference server",
        not any(k.startswith(_RV) for k in cm["data"]),
        str(sorted(cm["data"])),
    )
    check("connection model pinned at the measured connection count", cm["xs"] == [96])
    check(
        "shared model values carried",
        cm["data"][f"{_NV}|"]["mps"] == [310_000.0]
        and cm["data"][f"{_NV}|"]["connected"] == [96]
        and cm["data"][f"{_NV}|"]["rss"] == [96.5]
        and cm["data"][f"{_NV}|"]["threads"] == [12],
    )
    check(
        "thread model values carried",
        cm["data"][f"{_TV}|"]["mps"] == [120_000.0]
        and cm["data"][f"{_TV}|"]["connected"] == [24]
        and cm["data"][f"{_TV}|"]["threads"] == [30],
    )
    check(
        "connection model declares throughput + capacity + cost metrics",
        cm["metrics"] == ["mps", "connected", "rss", "threads"],
    )


def _schema_conformance_checks() -> None:
    """Everything the generic core schema/dashboard assumes must hold."""
    su = build_websocket_suite(_results())
    mkeys = {m["key"] for m in su["metrics"]}

    bad_metrics = {
        k: [m for m in sw["metrics"] if m not in mkeys]
        for k, sw in su["sweeps"].items()
    }
    check(
        "every sweep metric is declared on the suite",
        not any(bad_metrics.values()),
        str(bad_metrics),
    )

    bad_keys, bad_len = [], []
    for k, sw in su["sweeps"].items():
        gkeys = {g["key"] for g in sw["groups"]}
        for dkey, arrays in sw["data"].items():
            variant, _, gkey = dkey.partition("|")
            if variant not in sw["variants"] or gkey not in gkeys:
                bad_keys.append(f"{k}:{dkey}")
            for mkey, arr in arrays.items():
                if len(arr) != len(sw["xs"]):
                    bad_len.append(f"{k}:{dkey}:{mkey}")
    check(
        "every data key is '<declared variant>|<declared group>'",
        not bad_keys,
        str(bad_keys),
    )
    check("every metric array aligns to the sweep's xs", not bad_len, str(bad_len))
    check(
        "sweep keys match their dict keys",
        all(k == sw["key"] for k, sw in su["sweeps"].items()),
    )
    check(
        "every sweep carries a purpose description",
        all(sw["desc"] for sw in su["sweeps"].values()),
    )


def _partial_results_checks() -> None:
    empty = build_websocket_suite({})
    check("an empty result set yields no sweeps", empty["sweeps"] == {})
    check(
        "an empty result set still declares the compared servers",
        empty["variants"] == [_NV, _RV],
    )
    check(
        "the thread variant appears only when its comparison was measured",
        _TV not in empty["variants"] and _TV not in empty["configs"],
    )

    res = _results()
    res.pop("connection_model")
    no_cm = build_websocket_suite(res)
    check(
        "a run without the connection-model comparison drops that sweep",
        "connection_model" not in no_cm["sweeps"]
        and set(no_cm["sweeps"]) == _SWEEP_KEYS - {"connection_model"},
    )

    res = _results()
    res["connection_model"] = {"shared": res["connection_model"]["shared"]}
    half = build_websocket_suite(res)
    cm = half["sweeps"]["connection_model"]
    check(
        "a half-measured connection model keeps the measured side",
        cm["data"][f"{_NV}|"]["mps"] == [310_000.0],
    )
    check(
        "a half-measured connection model leaves the missing side as a gap",
        cm["data"][f"{_TV}|"]["mps"] == [None],
        str(cm["data"][f"{_TV}|"]),
    )


def _refeed_checks() -> None:
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "results.json"
        payload = json.dumps(_results())
        src.write_text(payload)
        out = str(Path(td) / "unified")

        rc = refeed.main(
            [
                "--results",
                str(src),
                "--outdir",
                out,
                "--label",
                "ws-refeed",
                "--no-render",
            ]
        )
        check("refeed of an existing results.json succeeds", rc == 0, f"rc={rc}")
        check(
            "refeed measures nothing — the source is untouched",
            src.read_text() == payload,
        )

        hist = list((Path(out) / "history").glob("*.json"))
        entries = [p for p in hist if p.name != "index.json"]
        check("refeed archives exactly one run", len(entries) == 1, str(hist))
        entry = json.loads(entries[0].read_text())
        check("archived run carries the websocket suite", SUITE_KEY in entry["suites"])
        check("archived run carries the run label", entry["label"] == "ws-refeed")
        check(
            "archived suite is the adapter's output",
            set(entry["suites"][SUITE_KEY]["sweeps"]) == _SWEEP_KEYS,
        )
        index = json.loads((Path(out) / "history" / "index.json").read_text())
        check(
            "index metadata states this record's suite coverage",
            len(index) == 1 and index[0]["suites"] == [SUITE_KEY],
            str(index),
        )

        rc = refeed.main(["--results", str(Path(td) / "nope.json"), "--outdir", out])
        check("refeed of a missing results.json fails loudly", rc == 2, f"rc={rc}")


def _both_suites_checks() -> None:
    """The unified record must be able to hold BOTH suites, and the dashboard
    must render from that history."""
    with tempfile.TemporaryDirectory() as td:
        http_suite = {
            "key": "http",
            "label": "HTTP frameworks",
            "variants": ["reactor"],
            "metrics": [{"key": "t", "label": "Throughput", "unit": "req/s"}],
            "sweeps": {
                "concurrency": {
                    "key": "concurrency",
                    "label": "Concurrency",
                    "xtitle": "connections",
                    "xs": [1, 2],
                    "xlog": False,
                    "variants": ["reactor"],
                    "groups": [{"key": "", "label": ""}],
                    "refs": [],
                    "note": "",
                    "desc": "",
                    "data": {"reactor|": {"t": [1.0, 2.0]}},
                }
            },
            "colors": {},
            "configs": {},
            "interpreter": "",
            "note": "",
        }
        save_run(td, {"http": http_suite}, label="http-only")
        save_run(
            td,
            {"http": http_suite, SUITE_KEY: build_websocket_suite(_results())},
            label="canonical",
        )

        runs = load_history(td)
        check("both records archived non-destructively", len(runs) == 2)
        check(
            "the canonical record contains BOTH suites",
            set(runs[-1]["suites"]) == {"http", SUITE_KEY},
            str(sorted(runs[-1]["suites"])),
        )
        check(
            "the single-suite record is distinguishable from it",
            set(runs[0]["suites"]) == {"http"},
        )

        if importlib.util.find_spec("plotly") is None:
            print("  (plotly not installed — dashboard render check skipped)")
            return
        from benchmarks.core.dashboard import write_dashboard

        html = write_dashboard(td).read_text()
        check(
            "rendered dashboard embeds both suite keys",
            f'"{SUITE_KEY}"' in html and '"http"' in html,
        )
        check(
            "rendered dashboard labels each record's suite coverage",
            "coverageTag" in html and "not in this run" in html,
        )
        check(
            "rendered dashboard honors sweep-level variants",
            "varsOf" in html,
        )


def main() -> bool:
    _suite_shape_checks()
    _schema_conformance_checks()
    _partial_results_checks()
    _refeed_checks()
    _both_suites_checks()
    return finish()


if __name__ == "__main__":
    run_main(main)
