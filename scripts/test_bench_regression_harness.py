"""
Unit tests for scripts/bench_regression_harness.py.

# hyper-test: unit

Covers the pure data-transform surface of the harness — aggregation,
baseline loading, regression detection, report serialization — without
touching wrk, the Zig HTTP server, or PostgreSQL. The real end-to-end
multi-session loop is validated manually by running the script against
a live app (see README in the script's docstring).
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _wrk_bench import EndpointResult  # noqa: E402
from bench_regression_harness import (  # noqa: E402
    AggregatedEndpoint,
    RegressionReport,
    aggregate_sessions,
    apply_baseline,
    dump_report,
    load_baseline,
)

passed = 0
failed = 0
errors: list[str] = []


def check(name: str, condition: bool, msg: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
    else:
        failed += 1
        err = f"FAIL: {name}"
        if msg:
            err += f" — {msg}"
        errors.append(err)
        print(f"  {err}")


def _mk_endpoint(
    slug: str,
    median_rps: float,
    *,
    label: str | None = None,
    url: str | None = None,
    p99_ms: float = 5.0,
    jitter_pct: float = 1.0,
) -> EndpointResult:
    return EndpointResult(
        slug=slug,
        url=url or f"/api/{slug}",
        label=label or f"GET /api/{slug}",
        median_rps=median_rps,
        per_run_rps=[median_rps * 0.98, median_rps, median_rps * 1.02],
        jitter_pct=jitter_pct,
        avg_ms=4.0,
        p50_ms=3.5,
        p99_ms=p99_ms,
        errors=0,
        total_requests=int(median_rps * 8),
    )


def test_aggregate_single_session() -> None:
    print("\n── aggregate_sessions: single session ──")
    session_a = [
        _mk_endpoint("list", 1000.0),
        _mk_endpoint("detail", 2000.0),
    ]
    aggregated = aggregate_sessions([session_a])
    check("single session produces 2 endpoints", len(aggregated) == 2)
    by_slug = {e.slug: e for e in aggregated}
    check(
        "list MoM equals single session median",
        by_slug["list"].median_of_medians == 1000.0,
    )
    check(
        "detail MoM equals single session median",
        by_slug["detail"].median_of_medians == 2000.0,
    )
    check("single session mom_jitter is 0", by_slug["list"].mom_jitter_pct == 0.0)


def test_aggregate_multiple_sessions_median_of_medians() -> None:
    print("\n── aggregate_sessions: multi-session median-of-medians ──")
    sessions = [
        [_mk_endpoint("list", 900.0)],
        [_mk_endpoint("list", 1000.0)],
        [_mk_endpoint("list", 1100.0)],
        [_mk_endpoint("list", 1050.0)],
        [_mk_endpoint("list", 950.0)],
    ]
    aggregated = aggregate_sessions(sessions)
    check("5-session aggregation has 1 endpoint", len(aggregated) == 1)
    endpoint = aggregated[0]
    # Per-session medians sorted: [900, 950, 1000, 1050, 1100] → median 1000
    check(
        "MoM is the middle of the sorted per-session medians",
        endpoint.median_of_medians == 1000.0,
        f"got {endpoint.median_of_medians}",
    )
    check(
        "per_session_medians captures all 5 values",
        len(endpoint.per_session_medians) == 5,
    )
    # Jitter: (max - min) / mom * 100 / 2 = (1100 - 900) / 1000 * 100 / 2 = 10%
    check(
        "mom_jitter reflects spread across sessions",
        abs(endpoint.mom_jitter_pct - 10.0) < 0.01,
        f"got {endpoint.mom_jitter_pct}",
    )


def test_aggregate_groups_by_slug_not_url() -> None:
    print("\n── aggregate_sessions: groups by slug even when URL differs ──")
    # Mimics the HyperNews post_detail case where the signed URL rotates
    # between sessions but the slug is stable.
    sessions = [
        [_mk_endpoint("post_detail", 5000.0, url="/post/abc123")],
        [_mk_endpoint("post_detail", 5100.0, url="/post/def456")],
        [_mk_endpoint("post_detail", 4950.0, url="/post/ghi789")],
    ]
    aggregated = aggregate_sessions(sessions)
    check("slug-rotation grouped into 1 row", len(aggregated) == 1)
    check(
        "first session URL retained for reporting",
        aggregated[0].url == "/post/abc123",
    )
    check(
        "MoM ignores URL variation",
        aggregated[0].median_of_medians == 5000.0,
    )


def test_aggregate_multi_endpoint_multi_session() -> None:
    print("\n── aggregate_sessions: realistic bookstore-shape run ──")
    sessions = [
        [
            _mk_endpoint("list", 1000.0),
            _mk_endpoint("detail", 3000.0),
            _mk_endpoint("search", 800.0),
        ],
        [
            _mk_endpoint("list", 1020.0),
            _mk_endpoint("detail", 2980.0),
            _mk_endpoint("search", 810.0),
        ],
        [
            _mk_endpoint("list", 990.0),
            _mk_endpoint("detail", 3050.0),
            _mk_endpoint("search", 795.0),
        ],
    ]
    aggregated = aggregate_sessions(sessions)
    check("3 endpoints aggregated", len(aggregated) == 3)
    by_slug = {e.slug: e for e in aggregated}
    check(
        "list MoM = 1000 (median of 990/1000/1020)",
        by_slug["list"].median_of_medians == 1000.0,
    )
    check(
        "detail MoM = 3000 (median of 2980/3000/3050)",
        by_slug["detail"].median_of_medians == 3000.0,
    )
    check(
        "search MoM = 800 (median of 795/800/810)",
        by_slug["search"].median_of_medians == 800.0,
    )


def test_aggregate_empty_sessions() -> None:
    print("\n── aggregate_sessions: empty input ──")
    aggregated = aggregate_sessions([])
    check("empty input produces empty output", aggregated == [])


def test_baseline_comparison_no_regression() -> None:
    print("\n── apply_baseline: no regression within threshold ──")
    aggregated = [
        AggregatedEndpoint(
            slug="list",
            label="GET /list",
            url="/list",
            per_session_medians=[1000.0],
            median_of_medians=1000.0,
            mom_jitter_pct=0.0,
            p99_of_medians=5.0,
        ),
    ]
    baseline = {"list": 1050.0}  # -4.76% delta, within 10% threshold
    regressed_count = apply_baseline(aggregated, baseline, threshold_pct=10.0)
    check("no regression at -4.76% vs 10% threshold", regressed_count == 0)
    check("baseline_mom set", aggregated[0].baseline_mom == 1050.0)
    check(
        "delta_pct reflects real delta",
        abs(aggregated[0].delta_pct - (-4.761904761904762)) < 0.001,
    )
    check("not marked regressed", aggregated[0].regressed is False)


def test_baseline_comparison_triggers_regression() -> None:
    print("\n── apply_baseline: regression exceeds threshold ──")
    aggregated = [
        AggregatedEndpoint(
            slug="list",
            label="GET /list",
            url="/list",
            per_session_medians=[900.0],
            median_of_medians=900.0,
            mom_jitter_pct=0.0,
            p99_of_medians=5.0,
        ),
        AggregatedEndpoint(
            slug="detail",
            label="GET /detail",
            url="/detail",
            per_session_medians=[3000.0],
            median_of_medians=3000.0,
            mom_jitter_pct=0.0,
            p99_of_medians=5.0,
        ),
    ]
    baseline = {"list": 1000.0, "detail": 3050.0}  # list: -10%, detail: -1.64%
    regressed_count = apply_baseline(aggregated, baseline, threshold_pct=5.0)
    check("1 regression (list only) at 5% threshold", regressed_count == 1)
    check("list marked regressed", aggregated[0].regressed is True)
    check("detail not marked regressed", aggregated[1].regressed is False)


def test_baseline_comparison_threshold_is_strict() -> None:
    print("\n── apply_baseline: exactly at threshold is NOT regressed ──")
    aggregated = [
        AggregatedEndpoint(
            slug="list",
            label="GET /list",
            url="/list",
            per_session_medians=[900.0],
            median_of_medians=900.0,
            mom_jitter_pct=0.0,
            p99_of_medians=5.0,
        ),
    ]
    baseline = {"list": 1000.0}  # exactly -10%
    regressed_count = apply_baseline(aggregated, baseline, threshold_pct=10.0)
    # delta_pct = -10.0, threshold = 10.0, condition is `< -threshold_pct`
    # so -10.0 is NOT < -10.0 → not regressed (threshold is inclusive)
    check("exactly-at-threshold not marked regressed", regressed_count == 0)


def test_baseline_comparison_ignores_missing_slugs() -> None:
    print("\n── apply_baseline: missing slug in baseline is skipped ──")
    aggregated = [
        AggregatedEndpoint(
            slug="new_endpoint",
            label="GET /new",
            url="/new",
            per_session_medians=[500.0],
            median_of_medians=500.0,
            mom_jitter_pct=0.0,
            p99_of_medians=5.0,
        ),
        AggregatedEndpoint(
            slug="list",
            label="GET /list",
            url="/list",
            per_session_medians=[800.0],
            median_of_medians=800.0,
            mom_jitter_pct=0.0,
            p99_of_medians=5.0,
        ),
    ]
    baseline = {"list": 1000.0}  # missing "new_endpoint"
    regressed_count = apply_baseline(aggregated, baseline, threshold_pct=10.0)
    check("list regressed", regressed_count == 1)
    check(
        "new_endpoint has no baseline fields set",
        aggregated[0].baseline_mom is None and aggregated[0].delta_pct is None,
    )
    check(
        "list baseline fields populated",
        aggregated[1].baseline_mom == 1000.0,
    )


def test_baseline_improvement_not_regression() -> None:
    print("\n── apply_baseline: improvement vs baseline is not a regression ──")
    aggregated = [
        AggregatedEndpoint(
            slug="list",
            label="GET /list",
            url="/list",
            per_session_medians=[1500.0],
            median_of_medians=1500.0,
            mom_jitter_pct=0.0,
            p99_of_medians=5.0,
        ),
    ]
    baseline = {"list": 1000.0}  # +50%
    regressed_count = apply_baseline(aggregated, baseline, threshold_pct=10.0)
    check("+50% improvement not counted as regression", regressed_count == 0)
    check("delta_pct positive", aggregated[0].delta_pct > 0)


def test_report_roundtrip_json_load_baseline() -> None:
    print("\n── dump_report + load_baseline roundtrip ──")
    aggregated = [
        AggregatedEndpoint(
            slug="list",
            label="GET /list",
            url="/list",
            per_session_medians=[1000.0, 1020.0, 980.0],
            median_of_medians=1000.0,
            mom_jitter_pct=2.0,
            p99_of_medians=5.5,
        ),
        AggregatedEndpoint(
            slug="detail",
            label="GET /detail",
            url="/detail",
            per_session_medians=[3000.0, 3050.0, 2980.0],
            median_of_medians=3000.0,
            mom_jitter_pct=1.17,
            p99_of_medians=3.2,
        ),
    ]
    report = RegressionReport(
        name="Test App",
        output_slug="test",
        sessions=3,
        inter_session_gap_s=30.0,
        threshold_pct=10.0,
        baseline_path=None,
        endpoints=aggregated,
        regressed_count=0,
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test_report.json"
        dump_report(report, path)
        check("report JSON file exists", path.exists())
        parsed = json.loads(path.read_text())
        check("name preserved", parsed["name"] == "Test App")
        check("sessions preserved", parsed["sessions"] == 3)
        check("2 endpoints in JSON", len(parsed["endpoints"]) == 2)
        check(
            "list MoM preserved",
            parsed["endpoints"][0]["median_of_medians"] == 1000.0,
        )
        # Now round-trip through load_baseline
        baseline = load_baseline(path)
        check(
            "load_baseline reads slugs correctly",
            set(baseline.keys()) == {"list", "detail"},
        )
        check("load_baseline reads list MoM", baseline["list"] == 1000.0)
        check("load_baseline reads detail MoM", baseline["detail"] == 3000.0)


def test_report_serialization_includes_baseline_fields() -> None:
    print("\n── RegressionReport serialization with baseline annotations ──")
    aggregated = [
        AggregatedEndpoint(
            slug="list",
            label="GET /list",
            url="/list",
            per_session_medians=[900.0],
            median_of_medians=900.0,
            mom_jitter_pct=0.0,
            p99_of_medians=5.0,
            baseline_mom=1000.0,
            delta_pct=-10.0,
            regressed=True,
        ),
    ]
    report = RegressionReport(
        name="Test",
        output_slug="test",
        sessions=1,
        inter_session_gap_s=0.0,
        threshold_pct=5.0,
        baseline_path="logs/test_baseline.json",
        endpoints=aggregated,
        regressed_count=1,
    )
    d = report.to_dict()
    check(
        "baseline_path in top-level dict",
        d["baseline_path"] == "logs/test_baseline.json",
    )
    check("regressed_count in top-level dict", d["regressed_count"] == 1)
    endpoint_d = d["endpoints"][0]
    check("endpoint baseline_mom in dict", endpoint_d["baseline_mom"] == 1000.0)
    check("endpoint delta_pct in dict", endpoint_d["delta_pct"] == -10.0)
    check("endpoint regressed True in dict", endpoint_d["regressed"] is True)


def main() -> int:
    print("=" * 60)
    print("bench_regression_harness unit tests")
    print("=" * 60)

    test_aggregate_single_session()
    test_aggregate_multiple_sessions_median_of_medians()
    test_aggregate_groups_by_slug_not_url()
    test_aggregate_multi_endpoint_multi_session()
    test_aggregate_empty_sessions()
    test_baseline_comparison_no_regression()
    test_baseline_comparison_triggers_regression()
    test_baseline_comparison_threshold_is_strict()
    test_baseline_comparison_ignores_missing_slugs()
    test_baseline_improvement_not_regression()
    test_report_roundtrip_json_load_baseline()
    test_report_serialization_includes_baseline_fields()

    print("\n" + "=" * 60)
    total = passed + failed
    print(f"Results: {passed}/{total} passed, {failed} failed")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
