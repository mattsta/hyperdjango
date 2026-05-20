#!/usr/bin/env python3
"""Unified benchmark record: schema declarations, coverage intent, and the
diagnostic quarantine.

Four properties the cross-suite record depends on, each pinned here because each
was previously expressed by patching a half-built dict after the fact, or not at
all:

- a SWEEP declares its own metric set + facet label THROUGH `sweep()`, and both
  suite builders use that path, so what the dashboard reads is what the builder
  said (no post-construction attachment);
- a RUN declares the coverage it was supposed to reach (`expected_suites`), so a
  record missing a suite it promised reads as incomplete instead of having its
  coverage reverse-engineered from whatever landed — while a record archived
  BEFORE that declaration existed keeps rendering exactly as it always did;
- a RESTRICTED run archives under `diagnostics/`, invisible to the dashboard,
  the index and the merge-target lookup;
- `group()`'s size parameter no longer shadows the `bytes` builtin.

Everything here is synthetic and local — no servers, no network, no box.

Usage:
    uv run hyper-test bench_core_schema
"""

# hyper-test: unit

import importlib.util
import inspect
import json
import tempfile
from pathlib import Path

from benchmarks.core import results as R
from benchmarks.core.dashboard import _DASH_JS, run_blocks, write_dashboard
from benchmarks.core.merge import merge_runs
from benchmarks.core.results import (
    declared_suites,
    diagnostics_index,
    find_entry,
    load_history,
    read_entry,
    read_index,
    save_run,
    summarize,
)
from benchmarks.http.suite import build_http_suite
from benchmarks.websocket import refeed
from benchmarks.websocket.suite import (
    SUITE_KEY,
    build_websocket_suite,
    feed_unified,
    websocket_completeness,
)
from hyperdjango.testkit import check, finish, run_main

_DATA_OPEN = "<script>const DATA="
_DATA_CLOSE = ";</script>"

# The exact key set (and order) a run block has carried since before
# `expected_suites` existed. A legacy entry must still render precisely this.
_LEGACY_RUN_KEYS = [
    "id",
    "label",
    "ts",
    "sha",
    "branch",
    "subject",
    "cores",
    "suites",
    "provenance",
    "merged_from",
]


def _min_suite(key: str, marker: float = 1.0) -> dict:
    return R.suite(
        key=key,
        label=key.upper(),
        variants=["a"],
        metrics=[R.metric("t", "Throughput", "req/s")],
        sweeps={
            "s": R.sweep(
                key="s",
                label="S",
                xtitle="x",
                xs=[1],
                variants=["a"],
                data={"a|": {"t": [marker]}},
                desc="d",
            )
        },
    )


def _dashboard_data(outdir: str) -> dict:
    """The DATA payload the dashboard embeds. Falls back to the history it is
    built from when plotly is absent (only the RENDER needs it), so the check
    count never changes."""
    if importlib.util.find_spec("plotly") is None:
        return {"runs": run_blocks(load_history(outdir))}
    doc = write_dashboard(outdir).read_text()
    i = doc.index(_DATA_OPEN) + len(_DATA_OPEN)
    return json.loads(doc[i : doc.index(_DATA_CLOSE, i)])


# ── 1. A sweep declares its own metrics + facet label through the signature ──


def _sweep_declaration_checks() -> None:
    params = inspect.signature(R.sweep).parameters
    check(
        "sweep() takes its metric list in the signature",
        "metrics" in params and params["metrics"].default is None,
        str(list(params)),
    )
    check(
        "sweep() takes its facet label in the signature",
        "groups_label" in params and params["groups_label"].default == "",
        str(list(params)),
    )

    bare = R.sweep(
        key="s", label="S", xtitle="x", xs=[1], variants=["a"], data={"a|": {"t": [1]}}
    )
    check(
        "an undeclared sweep means 'every metric the suite declares'",
        bare["metrics"] == [],
        str(bare["metrics"]),
    )
    check("an undeclared sweep has no facet label", bare["groupsLabel"] == "")

    declared = R.sweep(
        key="s",
        label="S",
        xtitle="x",
        xs=[1],
        variants=["a"],
        data={"a|": {"t": [1]}},
        metrics=["t", "p99"],
        groups_label="payload",
    )
    check(
        "declared metrics land on the wire key the dashboard reads",
        declared["metrics"] == ["t", "p99"],
        str(declared["metrics"]),
    )
    check(
        "declared facet label lands on the wire key `groupsLabel`",
        declared["groupsLabel"] == "payload",
        str(declared["groupsLabel"]),
    )
    check(
        "the metric list is copied, not aliased to the caller's list",
        declared["metrics"] is not None
        and R.sweep(
            key="s",
            label="S",
            xtitle="x",
            xs=[1],
            variants=["a"],
            data={},
            metrics=(m := ["t"]),
        )["metrics"]
        is not m,
    )


def _builder_declaration_checks() -> None:
    """Both suite builders must go through the signature — no sweep may be
    patched into shape after construction."""
    ws = build_websocket_suite(_WS_RESULTS)
    check(
        "every WebSocket sweep declares its metrics",
        all(sw["metrics"] for sw in ws["sweeps"].values()),
        str({k: sw["metrics"] for k, sw in ws["sweeps"].items()}),
    )
    check(
        "the WebSocket throughput sweep declares its facet dimension",
        ws["sweeps"]["throughput"]["groupsLabel"] == "frame type",
        str(ws["sweeps"]["throughput"]["groupsLabel"]),
    )

    http = build_http_suite(_HTTP_ENTRY)
    check(
        "every HTTP sweep declares its metrics",
        all(sw["metrics"] for sw in http["sweeps"].values()),
        str({k: sw["metrics"] for k, sw in http["sweeps"].items()}),
    )
    check(
        "the HTTP payload sweep declares 'payload' as its facet dimension",
        http["sweeps"]["concurrency"]["groupsLabel"] == "payload",
        str(http["sweeps"]["concurrency"]["groupsLabel"]),
    )
    check(
        "the HTTP conn-scaling sweep declares the served/shed metrics only it records",
        http["sweeps"]["conn_scaling"]["metrics"] == ["t", "p99", "served", "shed"],
        str(http["sweeps"]["conn_scaling"]["metrics"]),
    )
    check(
        "no sweep offers a metric its suite does not declare",
        not [
            (k, m)
            for su in (ws, http)
            for k, sw in su["sweeps"].items()
            for m in sw["metrics"]
            if m not in {d["key"] for d in su["metrics"]}
        ],
    )


def _declaration_reaches_dashboard_checks() -> None:
    with tempfile.TemporaryDirectory() as out:
        save_run(out, {SUITE_KEY: build_websocket_suite(_WS_RESULTS)}, label="decl")
        block = _dashboard_data(out)["runs"][-1]["suites"][SUITE_KEY]["sweeps"]
        check(
            "a signature-declared metric list reaches the dashboard data",
            block["throughput"]["metrics"] == ["mps", "gbps"],
            str(block["throughput"]["metrics"]),
        )
        check(
            "a signature-declared facet label reaches the dashboard data",
            block["throughput"]["groupsLabel"] == "frame type",
            str(block["throughput"]["groupsLabel"]),
        )


# ── 2. A run DECLARES the coverage it was supposed to reach ──────────────────


def _expected_suites_checks() -> None:
    with tempfile.TemporaryDirectory() as out:
        rid = save_run(out, {"http": _min_suite("http")}, label="solo")
        entry = read_entry(out, rid)
        check(
            "a run with no explicit declaration declares what it fed",
            entry["expected_suites"] == ["http"],
            str(entry.get("expected_suites")),
        )

        rid = save_run(
            out,
            {"http": _min_suite("http")},
            label="battery",
            expected_suites=["http", "websocket"],
        )
        entry = read_entry(out, rid)
        check(
            "a bench-all half declares BOTH suites before the second arrives",
            entry["expected_suites"] == ["http", "websocket"],
            str(entry["expected_suites"]),
        )
        check(
            "and carries only the one it actually measured",
            list(entry["suites"]) == ["http"],
            str(list(entry["suites"])),
        )
        check(
            "the index row states the declaration too",
            summarize(entry)["expected_suites"] == ["http", "websocket"],
        )

        merged = save_run(out, {"websocket": _min_suite("websocket")}, label="battery")
        entry = read_entry(out, merged)
        check("the second suite merges into the declaring record", merged == rid)
        check(
            "the declaration survives the merge",
            entry["expected_suites"] == ["http", "websocket"],
            str(entry["expected_suites"]),
        )
        check(
            "the record now carries everything it declared",
            sorted(entry["suites"]) == entry["expected_suites"],
        )

    with tempfile.TemporaryDirectory() as out:
        base = save_run(out, {"http": _min_suite("http")}, label="u")
        save_run(
            out,
            {"websocket": _min_suite("websocket")},
            label="u",
            expected_suites=["websocket", "startup"],
        )
        check(
            "merging UNIONS both sides' declarations",
            read_entry(out, base)["expected_suites"]
            == ["http", "startup", "websocket"],
            str(read_entry(out, base)["expected_suites"]),
        )


def _legacy_entry_checks() -> None:
    """An entry archived before `expected_suites` existed is tolerated on read
    and must never be rewritten to acquire one."""
    with tempfile.TemporaryDirectory() as out:
        rid = save_run(out, {"http": _min_suite("http")}, label="legacy")
        path = Path(out) / "history" / f"{rid}.json"
        entry = json.loads(path.read_text())
        entry.pop("expected_suites")
        path.write_text(json.dumps(entry, indent=2))
        index = read_index(out)
        for row in index:
            row.pop("expected_suites", None)
        (Path(out) / "history" / "index.json").write_text(json.dumps(index, indent=2))

        legacy = read_entry(out, rid)
        check(
            "a legacy entry declares nothing on disk",
            "expected_suites" not in legacy,
            str(sorted(legacy)),
        )
        check(
            "declared_suites() falls back to what a legacy entry carries",
            declared_suites(legacy) == ["http"],
            str(declared_suites(legacy)),
        )
        check(
            "summarize() omits the declaration for a legacy entry",
            "expected_suites" not in summarize(legacy),
        )

        runs = _dashboard_data(out)["runs"]
        check(
            "the dashboard omits the key entirely for a legacy entry — "
            "migrate on read, never rewrite",
            "expected_suites" not in runs[0],
            str(sorted(runs[0])),
        )
        check(
            "a legacy run block keeps its exact historical shape",
            list(runs[0]) == _LEGACY_RUN_KEYS,
            str(list(runs[0])),
        )
        check(
            "reading the archive did not rewrite it",
            json.loads(path.read_text()) == entry,
        )

        # A NEW record alongside it does carry the declaration.
        save_run(out, {"websocket": _min_suite("websocket")}, label="fresh")
        runs = _dashboard_data(out)["runs"]
        check(
            "a legacy entry and a declaring entry coexist in one render",
            "expected_suites" not in runs[0]
            and runs[1]["expected_suites"] == ["websocket"],
            str([sorted(r) for r in runs]),
        )


def _coverage_labeling_checks() -> None:
    """The dashboard must state coverage against the DECLARATION where there is
    one, and against the history-wide suite set where there is not."""
    js = _DASH_JS
    check("the dashboard reads the declaration", "expected_suites" in js)
    check(
        "coverage falls back to the history-wide suite set when none is declared",
        "ALL_SUITES.forEach" in js and "yardstickOf" in js,
    )
    check(
        "a declared-but-absent suite is labeled distinctly from a merely-absent one",
        "missingDeclared" in js and "declared, not recorded" in js,
    )
    check(
        "the coverage line calls a record that broke its own promise INCOMPLETE",
        "INCOMPLETE record" in js,
    )


def _merge_declaration_checks() -> None:
    with tempfile.TemporaryDirectory() as out:
        save_run(
            out,
            {"http": _min_suite("http")},
            label="a",
            expected_suites=["http", "websocket"],
        )
        save_run(out, {"websocket": _min_suite("websocket")}, label="b")
        combined = merge_runs(out, ["a", "b"], quiet=True)
        entry = read_entry(out, combined)
        check(
            "an after-the-fact merge unions the sources' declarations",
            entry["expected_suites"] == ["http", "websocket"],
            str(entry["expected_suites"]),
        )
        check(
            "the combined record carries everything it declares",
            sorted(entry["suites"]) == entry["expected_suites"],
        )


# ── 3. Restricted runs are quarantined under diagnostics/ ────────────────────


def _core_diagnostic_checks() -> None:
    with tempfile.TemporaryDirectory() as out:
        rid = save_run(
            out, {SUITE_KEY: _min_suite(SUITE_KEY)}, label="probe", diagnostic=True
        )
        check(
            "a diagnostic run lands in diagnostics/",
            (Path(out) / "diagnostics" / f"{rid}.json").exists(),
        )
        check(
            "a diagnostic run is NOT written to history/",
            not (Path(out) / "history" / f"{rid}.json").exists(),
        )
        check(
            "the entry says so itself",
            read_entry(out, rid) is None
            and json.loads((Path(out) / "diagnostics" / f"{rid}.json").read_text())[
                "diagnostic"
            ]
            is True,
        )
        check("a diagnostic run is invisible to load_history", load_history(out) == [])
        check("a diagnostic run is invisible to the index", read_index(out) == [])
        check(
            "a diagnostic run is invisible to the merge-target lookup",
            find_entry(out, "probe") is None and find_entry(out, rid) is None,
        )
        check(
            "the quarantined run is still addressable in its own index",
            [e["id"] for e in diagnostics_index(out)] == [rid],
            str(diagnostics_index(out)),
        )

        # A later COMPLETE feed under the same label must not merge into it.
        full = save_run(out, {SUITE_KEY: _min_suite(SUITE_KEY, 2.0)}, label="probe")
        check("a complete run never merges into a quarantined one", full != rid)
        check(
            "the complete run is the only record any comparison surface sees",
            [r["id"] for r in load_history(out)] == [full],
        )
        check(
            "the dashboard renders the complete run only",
            [r["id"] for r in _dashboard_data(out)["runs"]] == [full],
        )

        try:
            save_run(
                out, {"http": _min_suite("http")}, diagnostic=True, merge_into=full
            )
            raised = False
        except ValueError:
            raised = True
        check("a diagnostic may not be aimed into a canonical record", raised)

        second = save_run(
            out, {"http": _min_suite("http")}, label="probe", diagnostic=True
        )
        check(
            "diagnostics accumulate in their own archive",
            sorted(e["id"] for e in diagnostics_index(out)) == sorted([rid, second]),
        )
        check(
            "and never leak into the comparison history",
            [r["id"] for r in load_history(out)] == [full],
        )


def _websocket_classification_checks() -> None:
    full = websocket_completeness(_WS_RESULTS, full_matrix=True)
    check(
        "a full, fully-measured WebSocket run is COMPLETE",
        full.complete and full.missing == (),
        str(full),
    )

    quick = websocket_completeness(_WS_RESULTS, full_matrix=False)
    check(
        "a --quick smoke matrix is a DIAGNOSTIC regardless of what it measured",
        not quick.complete and any("quick" in m for m in quick.missing),
        str(quick),
    )

    holed = dict(_WS_RESULTS)
    holed["latency"] = []
    verdict = websocket_completeness(holed, full_matrix=True)
    check(
        "a full run with an empty section is a DIAGNOSTIC — results gate the "
        "classification, not the flag",
        not verdict.complete and any("latency" in m for m in verdict.missing),
        str(verdict),
    )

    half = dict(_WS_RESULTS)
    half["connection_model"] = {"shared": _WS_RESULTS["connection_model"]["shared"]}
    verdict = websocket_completeness(half, full_matrix=True)
    check(
        "a half-measured connection-model comparison is a DIAGNOSTIC",
        not verdict.complete and any("thread" in m for m in verdict.missing),
        str(verdict),
    )
    check(
        "every missing reason is reported, not just the first",
        len(websocket_completeness({}, full_matrix=False).missing) == 7,
        str(websocket_completeness({}, full_matrix=False).missing),
    )


def _websocket_feed_checks() -> None:
    with tempfile.TemporaryDirectory() as out:
        fed = feed_unified(
            _WS_RESULTS, label="ws-quick", outdir=out, diagnostic=True, render=True
        )
        check(
            "a diagnostic feed reports the archive it used",
            fed.archive_dir == "diagnostics",
        )
        check(
            "a diagnostic feed lands in diagnostics/",
            (Path(out) / "diagnostics" / f"{fed.run_id}.json").exists(),
        )
        check(
            "a diagnostic feed renders no dashboard — it is absent from that page",
            fed.dashboard == "",
        )
        check("a diagnostic feed is invisible to the history", load_history(out) == [])

        fed = feed_unified(
            _WS_RESULTS,
            label="ws-full",
            outdir=out,
            expected_suites=["http", SUITE_KEY],
            render=False,
        )
        check(
            "a complete feed reports the history archive", fed.archive_dir == "history"
        )
        entry = read_entry(out, fed.run_id)
        check(
            "a complete feed carries the declared coverage",
            entry["expected_suites"] == ["http", SUITE_KEY],
            str(entry["expected_suites"]),
        )
        check(
            "the record is visibly short of the HTTP suite it declared",
            set(entry["expected_suites"]) - set(entry["suites"]) == {"http"},
        )


def _refeed_classification_checks() -> None:
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "results.json"
        src.write_text(json.dumps(_WS_RESULTS))
        out = str(Path(td) / "unified")

        rc = refeed.main(
            ["--results", str(src), "--outdir", out, "--no-render", "--quick-source"]
        )
        check("refeed of a quick-matrix result set succeeds", rc == 0, f"rc={rc}")
        check(
            "and quarantines it under diagnostics/",
            len(diagnostics_index(out)) == 1 and load_history(out) == [],
            str(diagnostics_index(out)),
        )

        rc = refeed.main(
            [
                "--results",
                str(src),
                "--outdir",
                out,
                "--no-render",
                "--expect-suites",
                "http,websocket",
            ]
        )
        check("refeed of a full result set succeeds", rc == 0, f"rc={rc}")
        runs = load_history(out)
        check("and enters the comparison history", len(runs) == 1, str(runs))
        check(
            "carrying the coverage the caller declared",
            runs[0]["expected_suites"] == ["http", "websocket"],
            str(runs[0].get("expected_suites")),
        )


# ── 4. group()'s size parameter no longer shadows a builtin ──────────────────


def _group_parameter_checks() -> None:
    params = list(inspect.signature(R.group).parameters)
    check(
        "group() no longer shadows the `bytes` builtin",
        "bytes" not in params,
        str(params),
    )
    check("the size parameter is spelled out", params == ["key", "label", "size_bytes"])
    check(
        "a sized facet still travels under the archived wire key",
        R.group("64B", "64 B", size_bytes=64)
        == {
            "key": "64B",
            "label": "64 B",
            "bytes": 64,
        },
    )
    check(
        "positional callers are unaffected",
        R.group("4KB", "4 KB", 4096)["bytes"] == 4096,
    )
    check("an unsized facet carries no size", "bytes" not in R.group("text"))
    check(
        "the HTTP builder's payload facets keep their byte sizes",
        [
            g.get("bytes")
            for g in build_http_suite(_HTTP_ENTRY)["sweeps"]["concurrency"]["groups"]
        ]
        == [64, 4096],
    )


# ── Synthetic fixtures ───────────────────────────────────────────────────────


def _mp(payload: int, mps: float) -> dict:
    return {
        "total_conns": 24,
        "duration_s": 5.0,
        "payload_size": payload,
        "msgs_per_sec": mps,
    }


_WS_RESULTS = {
    "throughput": [
        {
            "payload_size": 64,
            "frame_type": "text",
            "native": _mp(64, 400_000.0),
            "reference": _mp(64, 100_000.0),
        },
        {
            "payload_size": 64,
            "frame_type": "binary",
            "native": _mp(64, 390_000.0),
            "reference": _mp(64, 95_000.0),
        },
    ],
    "concurrency_scaling_throughput": [
        {
            "concurrency": 8,
            "native": _mp(4096, 150_000.0),
            "reference": _mp(4096, 40_000.0),
        }
    ],
    "latency": [
        {
            "payload_size": 64,
            "native": {"mean_us": 90.0, "p50_us": 85.0, "p99_us": 140.0},
            "reference": {"mean_us": 210.0, "p50_us": 200.0, "p99_us": 480.0},
        }
    ],
    "connection_scaling": [
        {
            "target": 16,
            "native": {"connected": 16, "connect_time_s": 0.02},
            "reference": {"connected": 16, "connect_time_s": 0.05},
        }
    ],
    "connection_model": {
        "shared": {
            "target_conns": 96,
            "connected": 96,
            "msgs_per_sec": 310_000.0,
            "peak_rss_mb": 96.5,
            "peak_threads": 12,
        },
        "thread": {
            "target_conns": 96,
            "connected": 24,
            "msgs_per_sec": 120_000.0,
            "peak_rss_mb": 180.25,
            "peak_threads": 30,
        },
    },
}

_HTTP_META = {
    "frameworks": ["reactor", "threaded"],
    "payloads": [["json_small", 64], ["json_large", 4096]],
    "concurrencies": [16, 64],
    "workers": 8,
    "duration_s": 2.0,
    "cores": 8,
    "client": "wrk",
    "configs": {"reactor": "native reactor"},
    "interpreter": "3.14t",
}


def _http_rows() -> list[dict]:
    return [
        {
            "framework": fw,
            "payload": p,
            "concurrency": c,
            "throughput_rps": 1000.0 + c,
            "p50_ms": 1.0,
            "p90_ms": 2.0,
            "p99_ms": 3.0,
            "rss_mb": 50.0,
            "body_gbps": 0.5,
        }
        for fw in _HTTP_META["frameworks"]
        for p, _ in _HTTP_META["payloads"]
        for c in _HTTP_META["concurrencies"]
    ]


_HTTP_ENTRY = {
    "sweeps": {"concurrency": {"meta": _HTTP_META, "results": _http_rows()}},
    "conn_scaling": {
        "meta": {
            **_HTTP_META,
            "conns": [64, 256],
            "think_ms": 25.0,
            "cs_p99_bound_ms": 50.0,
        },
        "results": [
            {
                "framework": fw,
                "conns": n,
                "throughput_rps": 900.0,
                "p99_ms": 10.0,
                "served_frac": 100.0,
                "shed_frac": 0.0,
            }
            for fw in _HTTP_META["frameworks"]
            for n in (64, 256)
        ],
    },
}


def main() -> bool:
    _sweep_declaration_checks()
    _builder_declaration_checks()
    _declaration_reaches_dashboard_checks()
    _expected_suites_checks()
    _legacy_entry_checks()
    _coverage_labeling_checks()
    _merge_declaration_checks()
    _core_diagnostic_checks()
    _websocket_classification_checks()
    _websocket_feed_checks()
    _refeed_classification_checks()
    _group_parameter_checks()
    return finish()


if __name__ == "__main__":
    run_main(main)
