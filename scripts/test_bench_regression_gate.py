#!/usr/bin/env python3
"""Benchmark regression gate: peak folding, run comparison, and the
RESULT-verified completeness classification.

Pure-Python coverage of benchmarks.http.report's gate functions —
peak_cells (per-series peak folding), compare_runs (tolerance verdicts,
missing/new series handling) and verify_completeness (which runs may enter
the comparison history at all) — so `hyper-bench --check-against` behavior is
pinned without needing a live benchmark run.

Usage:
    uv run hyper-test bench_regression_gate
"""

# hyper-test: unit

import json
import tempfile
from pathlib import Path

from benchmarks.http.report import (
    compare_runs,
    find_run_by_label,
    load_history,
    peak_cells,
    save_run,
    verify_completeness,
)
from benchmarks.http.run import ALL_FRAMEWORKS, PAYLOADS
from hyperdjango.testkit import check, finish, run_main

PAYLOAD_NAMES = [name for name, _ in PAYLOADS]


def _full_matrix(
    skip: set[tuple[str, str, str]] | None = None,
    drop_sweeps: set[str] | None = None,
) -> dict:
    """A synthetic archived entry carrying the COMPLETE matrix, minus any
    (sweep, framework, payload) cells in `skip` and any sweeps in
    `drop_sweeps` — the exact shapes a mid-run skip leaves behind."""
    skip, drop_sweeps = skip or set(), drop_sweeps or set()
    sweeps: dict = {}
    for skey in ("workers", "concurrency"):
        if skey in drop_sweeps:
            continue
        rows = [
            {"framework": fw, "payload": p, "workers": 8, "throughput_rps": 100_000.0}
            for fw in ALL_FRAMEWORKS
            for p in PAYLOAD_NAMES
            if (skey, fw, p) not in skip
        ]
        sweeps[skey] = {"meta": {}, "results": rows}
    for skey in ("bounded", "connscaling"):
        if skey in drop_sweeps:
            continue
        sweeps[skey] = {
            "meta": {},
            "results": [
                {
                    "framework": "hyperdjango-reactor",
                    "payload": "plaintext",
                    "throughput_rps": 1.0,
                }
            ],
        }
    return {"sweeps": sweeps}


def _completeness_checks() -> None:
    """Flags gate the INTENT to run the full matrix; these results gate the
    CLASSIFICATION. A run with holes must never enter the comparison history."""
    full = verify_completeness(_full_matrix(), ALL_FRAMEWORKS, PAYLOAD_NAMES)
    check("full synthetic matrix classifies complete", full.complete)
    check("complete verdict names nothing missing", full.missing == [])

    # A framework skipped mid-run (missing optional deps) — every one of its
    # cells is gone from both payload sweeps.
    gone = {("workers", "flask", p) for p in PAYLOAD_NAMES}
    gone |= {("concurrency", "flask", p) for p in PAYLOAD_NAMES}
    v = verify_completeness(_full_matrix(skip=gone), ALL_FRAMEWORKS, PAYLOAD_NAMES)
    check("missing framework classifies diagnostic", not v.complete)
    check(
        "missing framework named in both sweeps",
        sum(1 for m in v.missing if "flask" in m and "absent" in m) == 2,
    )

    # One payload lost in one sweep (server died at that cell).
    v = verify_completeness(
        _full_matrix(skip={("workers", "fastapi", "64KiB")}),
        ALL_FRAMEWORKS,
        PAYLOAD_NAMES,
    )
    check("missing payload classifies diagnostic", not v.complete)
    check(
        "missing payload cell named exactly",
        any("workers" in m and "fastapi" in m and "64KiB" in m for m in v.missing),
    )

    # A regime sweep that never ran.
    for skey in ("bounded", "connscaling", "workers", "concurrency"):
        v = verify_completeness(
            _full_matrix(drop_sweeps={skey}), ALL_FRAMEWORKS, PAYLOAD_NAMES
        )
        check(f"missing {skey} sweep classifies diagnostic", not v.complete)
        check(f"missing {skey} sweep named", any(skey in m for m in v.missing))

    # An empty regime sweep is the same hole as an absent one.
    entry = _full_matrix()
    entry["sweeps"]["connscaling"]["results"] = []
    v = verify_completeness(entry, ALL_FRAMEWORKS, PAYLOAD_NAMES)
    check("empty regime sweep classifies diagnostic", not v.complete)

    # A cell that ran but measured 0 rps is a dead server, not a data point.
    entry = _full_matrix()
    entry["sweeps"]["workers"]["results"][0]["throughput_rps"] = 0.0
    v = verify_completeness(entry, ALL_FRAMEWORKS, PAYLOAD_NAMES)
    check("zero-throughput cell classifies diagnostic", not v.complete)

    check(
        "empty entry classifies diagnostic",
        not verify_completeness({}, ALL_FRAMEWORKS, PAYLOAD_NAMES).complete,
    )


def _run(cells: dict[tuple[str, str, str, int], float]) -> dict:
    """Build an archived-run-shaped dict from {(sweep, fw, payload, x): rps}."""
    sweeps: dict = {}
    for (skey, fw, payload, x), rps in cells.items():
        sweeps.setdefault(skey, {"results": []})["results"].append(
            {"framework": fw, "payload": payload, "workers": x, "throughput_rps": rps}
        )
    return {"sweeps": sweeps}


def main() -> bool:
    base = _run(
        {
            ("workers", "reactor", "plaintext", 8): 150_000,
            ("workers", "reactor", "plaintext", 64): 550_000,
            ("workers", "flask", "plaintext", 64): 180_000,
            ("bounded", "threaded (c=2W)", "plaintext", 64): 560_000,
        }
    )

    peaks = peak_cells(base)
    check(
        "peak folding takes the series max",
        peaks[("workers", "reactor", "plaintext")] == 550_000,
    )
    check("peak folding keys every series", len(peaks) == 3)

    # Within tolerance: -10% on one series, +5% on another.
    cur_ok = _run(
        {
            ("workers", "reactor", "plaintext", 64): 495_000,
            ("workers", "flask", "plaintext", 64): 189_000,
            ("bounded", "threaded (c=2W)", "plaintext", 64): 560_000,
        }
    )
    lines, ok = compare_runs(cur_ok, base, tolerance=0.15)
    check("within-tolerance run passes", ok)
    check("every baseline series reported", len([l for l in lines if "/" in l]) >= 3)

    # Beyond tolerance: -40% regression must fail.
    cur_bad = _run(
        {
            ("workers", "reactor", "plaintext", 64): 330_000,
            ("workers", "flask", "plaintext", 64): 180_000,
            ("bounded", "threaded (c=2W)", "plaintext", 64): 560_000,
        }
    )
    lines, ok = compare_runs(cur_bad, base, tolerance=0.15)
    check("regression beyond tolerance fails", not ok)
    check(
        "regressed series named",
        any("REGRESSION" in l and "reactor" in l for l in lines),
    )

    # Missing series: reported, never fails the gate.
    cur_missing = _run({("workers", "flask", "plaintext", 64): 180_000})
    lines, ok = compare_runs(cur_missing, base, tolerance=0.15)
    check("missing series does not fail the gate", ok)
    check(
        "missing series reported",
        any("not measured" in l for l in lines),
    )

    # New series: reported as new, never fails.
    cur_new = _run(
        {
            ("workers", "reactor", "plaintext", 64): 560_000,
            ("workers", "flask", "plaintext", 64): 181_000,
            ("bounded", "threaded (c=2W)", "plaintext", 64): 561_000,
            ("connscaling", "reactor", "plaintext", 1024): 40_000,
        }
    )
    lines, ok = compare_runs(cur_new, base, tolerance=0.15)
    check("new series does not fail the gate", ok)
    check("new series reported", any("new series" in l for l in lines))

    # Improvement is never a failure regardless of magnitude.
    cur_up = _run(
        {
            ("workers", "reactor", "plaintext", 64): 900_000,
            ("workers", "flask", "plaintext", 64): 400_000,
            ("bounded", "threaded (c=2W)", "plaintext", 64): 900_000,
        }
    )
    _, ok = compare_runs(cur_up, base, tolerance=0.15)
    check("improvement passes", ok)

    # find_run_by_label against a synthetic history dir: newest-with-label
    # wins, self-comparison excluded, unknown label -> None.
    with tempfile.TemporaryDirectory() as td:
        hist = Path(td) / "history"
        hist.mkdir()
        runs = [
            {"id": "r1", "ts": "2026-07-01T00:00:00", "label": "baseline"},
            {"id": "r2", "ts": "2026-07-02T00:00:00", "label": "baseline"},
            {"id": "r3", "ts": "2026-07-03T00:00:00", "label": "other"},
        ]
        for r in runs:
            (hist / f"{r['id']}.json").write_text(json.dumps(r))
        (hist / "index.json").write_text(json.dumps([{"id": r["id"]} for r in runs]))

        found = find_run_by_label(td, "baseline")
        check("newest run with label wins", found is not None and found["id"] == "r2")
        found = find_run_by_label(td, "baseline", exclude_id="r2")
        check("exclude_id skips self", found is not None and found["id"] == "r1")
        check("unknown label -> None", find_run_by_label(td, "nope") is None)
        check(
            "empty history dir -> None",
            find_run_by_label(str(Path(td) / "missing"), "baseline") is None,
        )

    # Diagnostic quarantine: partial runs archive under diagnostics/ and are
    # invisible to every comparison surface (load_history reads history/ only).
    with tempfile.TemporaryDirectory() as td:
        rid_d = save_run(
            td,
            work=[{"framework": "reactor"}],
            work_meta={"cores": 1},
            label="probe",
            diagnostic=True,
        )
        check(
            "diagnostic run lands in diagnostics/",
            (Path(td) / "diagnostics" / f"{rid_d}.json").exists(),
        )
        check("diagnostic run invisible to load_history", load_history(td) == [])
        check(
            "diagnostic run invisible to gate baseline lookup",
            find_run_by_label(td, "probe") is None,
        )
        rid_c = save_run(
            td,
            work=[{"framework": "reactor"}],
            work_meta={"cores": 1},
            label="full",
            diagnostic=False,
        )
        check(
            "complete run lands in history/",
            (Path(td) / "history" / f"{rid_c}.json").exists(),
        )
        hist = load_history(td)
        check(
            "complete run visible to comparison surfaces",
            len(hist) == 1 and hist[0]["id"] == rid_c,
        )

    _completeness_checks()
    return finish()


if __name__ == "__main__":
    run_main(main)
