"""
Multi-session wrk regression harness — stable before/after throughput
deltas for any per-app benchmark declared in ``_wrk_bench.py``.

Single-session wrk on a dev laptop carries ±20% variance (thermal state,
scheduler, background noise). That makes any wire-speed delta below 20%
impossible to attribute in a single run. This harness lifts the signal
out by:

1. Running the full drop+seed+AppRunner+wrk pipeline **N times** per app.
2. Sleeping a configurable **inter-session gap** between sessions so
   thermal state and kernel scheduler decorrelate.
3. Taking the **median-of-medians** of per-session medians as the
   ground-truth throughput number for each endpoint.
4. Optionally comparing against a saved baseline and exiting non-zero
   if any endpoint drops below a regression threshold.

Usage::

    # Capture a baseline (first time, or after a known-good refactor):
    uv run python scripts/bench_regression_harness.py \\
        --app bookstore --sessions 5 --gap 30 --save-baseline

    # Regression check against the saved baseline:
    uv run python scripts/bench_regression_harness.py \\
        --app bookstore --sessions 5 --gap 30 \\
        --baseline logs/bench_regression_bookstore_baseline.json

    # Quick sanity check with fewer sessions:
    uv run python scripts/bench_regression_harness.py \\
        --app hypernews --sessions 3 --gap 10 --duration 5s

The per-session wrk output is still printed live via the existing
``_wrk_bench.run_wrk_benchmark`` orchestrator — this script only wraps
that call in a timed outer loop and aggregates the results.

Exit codes: 0 = no regression / no baseline, 1 = regression detected.
"""

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench_bookstore_wrk  # noqa: E402
import bench_hypernews_wrk  # noqa: E402
from _wrk_bench import (  # noqa: E402
    EndpointResult,
    WrkBenchmark,
    WrkConfig,
    run_wrk_benchmark,
)

LOGS = Path(__file__).resolve().parent.parent / "logs"


# ── Typed result dataclasses ─────────────────────────────────────────────


@dataclass(slots=True)
class AggregatedEndpoint:
    """Per-endpoint aggregation across N sessions.

    `median_of_medians` is the score we compare against a baseline —
    it cancels out both within-session wrk jitter (via the median
    inside each session) AND between-session thermal drift (via the
    outer median across sessions).
    """

    slug: str
    label: str
    url: str
    per_session_medians: list[float]
    median_of_medians: float
    mom_jitter_pct: float  # jitter across per_session_medians
    p99_of_medians: float
    baseline_mom: float | None = None
    delta_pct: float | None = None
    regressed: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "slug": self.slug,
            "label": self.label,
            "url": self.url,
            "per_session_medians": [round(v, 1) for v in self.per_session_medians],
            "median_of_medians": round(self.median_of_medians, 1),
            "mom_jitter_pct": round(self.mom_jitter_pct, 2),
            "p99_of_medians": round(self.p99_of_medians, 3),
            "baseline_mom": (
                round(self.baseline_mom, 1) if self.baseline_mom is not None else None
            ),
            "delta_pct": (
                round(self.delta_pct, 2) if self.delta_pct is not None else None
            ),
            "regressed": self.regressed,
        }


@dataclass(slots=True)
class RegressionReport:
    """Full harness output — serialized to JSON + pretty-printed."""

    name: str
    output_slug: str
    sessions: int
    inter_session_gap_s: float
    threshold_pct: float
    baseline_path: str | None
    endpoints: list[AggregatedEndpoint] = field(default_factory=list)
    regressed_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "output_slug": self.output_slug,
            "sessions": self.sessions,
            "inter_session_gap_s": self.inter_session_gap_s,
            "threshold_pct": self.threshold_pct,
            "baseline_path": self.baseline_path,
            "regressed_count": self.regressed_count,
            "endpoints": [e.to_dict() for e in self.endpoints],
        }


# ── App benchmark factories ──────────────────────────────────────────────


def _bookstore_benchmark(cfg: WrkConfig) -> WrkBenchmark:
    return WrkBenchmark(
        name="Bookstore API",
        app_spec="services.bookstore_api.app:app",
        port=18802,
        targets=list(bench_bookstore_wrk._TARGETS),
        setup_args=[
            "--drop",
            "--seed",
            "services.bookstore_api.seed:run",
        ],
        output_slug="bookstore",
        config=cfg,
    )


def _hypernews_benchmark(cfg: WrkConfig) -> WrkBenchmark:
    return WrkBenchmark(
        name="HyperNews",
        app_spec="services.hypernews.app:app",
        port=18801,
        targets=list(bench_hypernews_wrk._TARGETS),
        setup_args=[
            "--drop",
            "--seed",
            "services.hypernews.setup:seed",
        ],
        output_slug="hypernews",
        config=cfg,
        pre_hook=bench_hypernews_wrk._pre_hook,
    )


_APP_FACTORIES: dict[str, callable] = {
    "bookstore": _bookstore_benchmark,
    "hypernews": _hypernews_benchmark,
}


# ── Aggregation ──────────────────────────────────────────────────────────


def aggregate_sessions(
    sessions: list[list[EndpointResult]],
) -> list[AggregatedEndpoint]:
    """Take per-session ``EndpointResult`` lists and compute the
    median-of-medians for each unique endpoint slug.

    Endpoints are grouped by slug (not URL) so a slug whose URL gets
    rewritten between sessions — e.g. the HyperNews ``post_detail``
    slug whose URL contains a freshly-signed external ID per session
    — is still aggregated into a single row. The first session's URL
    is retained for reporting purposes.
    """
    if not sessions:
        return []

    grouped: dict[str, list[EndpointResult]] = {}
    for session_results in sessions:
        for result in session_results:
            grouped.setdefault(result.slug, []).append(result)

    aggregated: list[AggregatedEndpoint] = []
    for slug, results in grouped.items():
        per_session_medians = [r.median_rps for r in results]
        mom = statistics.median(per_session_medians)
        if len(per_session_medians) > 1 and mom > 0:
            mom_jitter = (
                (max(per_session_medians) - min(per_session_medians)) / mom * 100 / 2
            )
        else:
            mom_jitter = 0.0
        p99_of_medians = statistics.median([r.p99_ms for r in results])
        aggregated.append(
            AggregatedEndpoint(
                slug=slug,
                label=results[0].label,
                url=results[0].url,
                per_session_medians=per_session_medians,
                median_of_medians=mom,
                mom_jitter_pct=mom_jitter,
                p99_of_medians=p99_of_medians,
            )
        )
    return aggregated


def load_baseline(path: Path) -> dict[str, float]:
    """Load a previously saved baseline JSON and return
    ``{slug: median_of_medians}`` for fast lookup."""
    data = json.loads(path.read_text())
    return {
        endpoint["slug"]: float(endpoint["median_of_medians"])
        for endpoint in data["endpoints"]
    }


def apply_baseline(
    aggregated: list[AggregatedEndpoint],
    baseline: dict[str, float],
    threshold_pct: float,
) -> int:
    """Annotate each aggregated endpoint with baseline comparison.

    Sets ``baseline_mom``, ``delta_pct``, and ``regressed`` fields.
    An endpoint is regressed when ``delta_pct < -threshold_pct``
    (e.g. a 10% drop vs the baseline with ``threshold_pct=10.0``).
    Returns the number of regressed endpoints.
    """
    regressed_count = 0
    for endpoint in aggregated:
        if endpoint.slug not in baseline:
            continue
        endpoint.baseline_mom = baseline[endpoint.slug]
        if endpoint.baseline_mom > 0:
            endpoint.delta_pct = (
                (endpoint.median_of_medians - endpoint.baseline_mom)
                / endpoint.baseline_mom
                * 100
            )
        else:
            endpoint.delta_pct = 0.0
        if endpoint.delta_pct < -threshold_pct:
            endpoint.regressed = True
            regressed_count += 1
    return regressed_count


def dump_report(report: RegressionReport, output_path: Path) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report.to_dict(), indent=2))


def print_summary(report: RegressionReport) -> None:
    print("\n" + "=" * 90)
    print(f"  {report.name} — Multi-session regression summary")
    print(
        f"  {report.sessions} sessions × inter-session gap "
        f"{report.inter_session_gap_s:.0f}s"
    )
    print(f"  Regression threshold: -{report.threshold_pct:.1f}%")
    if report.baseline_path:
        print(f"  Baseline: {report.baseline_path}")
    print("=" * 90)

    has_baseline = report.baseline_path is not None
    header = f"{'Endpoint':<55} {'MoM rps':>10} {'jitter':>8} {'p99':>10}"
    if has_baseline:
        header += f" {'Baseline':>10} {'Δ%':>8}  {'Status':<8}"
    print(header)
    print("-" * len(header))

    for endpoint in report.endpoints:
        line = (
            f"{endpoint.label:<55} "
            f"{endpoint.median_of_medians:>10,.0f} "
            f"±{endpoint.mom_jitter_pct:>6.1f}% "
            f"{endpoint.p99_of_medians:>8.2f}ms"
        )
        if has_baseline and endpoint.baseline_mom is not None:
            status = "REGRESS" if endpoint.regressed else "OK"
            line += (
                f" {endpoint.baseline_mom:>10,.0f} "
                f"{endpoint.delta_pct:>+7.2f}%  {status:<8}"
            )
        print(line)

    print("=" * 90)
    if has_baseline:
        if report.regressed_count:
            print(
                f"  WARN {report.regressed_count} endpoint(s) regressed below "
                f"-{report.threshold_pct:.1f}%"
            )
        else:
            print("  OK  No regressions detected")


# ── Harness entry point ──────────────────────────────────────────────────


def run_multi_session(
    benchmark: WrkBenchmark,
    sessions: int,
    inter_session_gap_s: float,
) -> list[list[EndpointResult]]:
    """Run the full drop+seed+wrk pipeline ``sessions`` times.

    Each session reuses the same ``WrkBenchmark`` config but re-runs
    ``run_wrk_benchmark`` which does its own fresh ``hyper setup --drop``
    and ``AppRunner`` boot, so DB state, prepared statement caches, and
    OS page cache are all reset between sessions.
    """
    all_sessions: list[list[EndpointResult]] = []
    for i in range(sessions):
        print("\n" + "#" * 90)
        print(f"# Session {i + 1}/{sessions}")
        print("#" * 90)
        session = run_wrk_benchmark(benchmark)
        all_sessions.append(session)
        if i < sessions - 1 and inter_session_gap_s > 0:
            print(
                f"\n  Inter-session gap: sleeping {inter_session_gap_s:.0f}s "
                f"for thermal decorrelation..."
            )
            time.sleep(inter_session_gap_s)
    return all_sessions


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Multi-session wrk regression harness",
    )
    parser.add_argument(
        "--app",
        choices=sorted(_APP_FACTORIES.keys()),
        required=True,
        help="Which app to benchmark (bookstore | hypernews)",
    )
    parser.add_argument(
        "--sessions",
        type=int,
        default=5,
        help="Number of full benchmark sessions (default: 5)",
    )
    parser.add_argument(
        "--gap",
        type=float,
        default=30.0,
        help="Inter-session gap in seconds (default: 30)",
    )
    parser.add_argument(
        "--duration",
        default="8s",
        help="wrk duration per run (default: 8s — stability rule ≥ 5s)",
    )
    parser.add_argument(
        "--multi-run",
        type=int,
        default=3,
        help="wrk runs per endpoint per session (default: 3)",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=4,
        help="wrk -t threads (default: 4)",
    )
    parser.add_argument(
        "--connections",
        type=int,
        default=20,
        help="wrk -c connections (default: 20)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=10.0,
        help="Regression threshold %% below baseline (default: 10.0)",
    )
    parser.add_argument(
        "--save-baseline",
        action="store_true",
        help="Save this run as the baseline for the given --app",
    )
    parser.add_argument(
        "--baseline",
        type=str,
        default=None,
        help=(
            "Compare against this baseline JSON. If omitted but "
            "logs/bench_regression_<app>_baseline.json exists, "
            "compare against it automatically."
        ),
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output JSON path (default: logs/bench_regression_<app>.json)",
    )
    args = parser.parse_args()

    if args.sessions < 1:
        print("--sessions must be >= 1", file=sys.stderr)
        return 2
    if args.gap < 0:
        print("--gap must be >= 0", file=sys.stderr)
        return 2
    if args.threshold <= 0:
        print("--threshold must be > 0", file=sys.stderr)
        return 2

    cfg = WrkConfig(
        duration=args.duration,
        threads=args.threads,
        connections=args.connections,
        multi_run=args.multi_run,
    )
    benchmark = _APP_FACTORIES[args.app](cfg)

    start = time.perf_counter()
    all_sessions = run_multi_session(
        benchmark=benchmark,
        sessions=args.sessions,
        inter_session_gap_s=args.gap,
    )
    elapsed = time.perf_counter() - start

    aggregated = aggregate_sessions(all_sessions)

    # Baseline comparison — explicit path takes precedence, else
    # fall back to the conventional baseline file for this app.
    baseline_path: Path | None = None
    if args.baseline:
        baseline_path = Path(args.baseline)
    else:
        default_baseline = LOGS / f"bench_regression_{args.app}_baseline.json"
        if default_baseline.exists():
            baseline_path = default_baseline

    regressed_count = 0
    if baseline_path and baseline_path.exists():
        baseline = load_baseline(baseline_path)
        regressed_count = apply_baseline(aggregated, baseline, args.threshold)

    output_path = (
        Path(args.output) if args.output else LOGS / f"bench_regression_{args.app}.json"
    )

    report = RegressionReport(
        name=benchmark.name,
        output_slug=args.app,
        sessions=args.sessions,
        inter_session_gap_s=args.gap,
        threshold_pct=args.threshold,
        baseline_path=str(baseline_path) if baseline_path else None,
        endpoints=aggregated,
        regressed_count=regressed_count,
    )

    dump_report(report, output_path)
    print_summary(report)

    print(f"\n  Total wall clock: {elapsed:.1f}s")
    print(f"  Report:   {output_path}")

    if args.save_baseline:
        baseline_output = LOGS / f"bench_regression_{args.app}_baseline.json"
        # Strip any baseline/delta annotations so the saved baseline is
        # clean — otherwise a compared-then-saved run would embed its
        # own prior baseline, which is confusing on the next comparison.
        for endpoint in aggregated:
            endpoint.baseline_mom = None
            endpoint.delta_pct = None
            endpoint.regressed = False
        report.baseline_path = None
        report.regressed_count = 0
        dump_report(report, baseline_output)
        print(f"  Baseline: {baseline_output}  (saved)")

    return 1 if regressed_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
