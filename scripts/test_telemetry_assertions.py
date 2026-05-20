"""
Unit tests for `hyperdjango.telemetry.assertions.TelemetryAssertions`.

# hyper-test: unit

Coverage:

    Span assertions
      1. assert_span_count matches
      2. assert_span_count mismatch → AssertionError with diff
      3. assert_has_span finds by exact name
      4. assert_has_span missing → AssertionError lists available names
      5. assert_span_attr matches (string + numeric coercion)
      6. assert_span_attr missing key → AssertionError
      7. assert_span_attr_contains substring match
      8. assert_span_status matches
      9. assert_no_error_spans passes on clean run
      10. assert_no_error_spans fails on one 500
      11. assert_span_chain walks parent→child correctly
      12. assert_span_chain detects broken chain

    Metric assertions
      13. assert_metric_present finds by name
      14. assert_metric_present missing → AssertionError
      15. assert_metric_value parses non-labeled counter value
      16. assert_metric_has_label matches single series
      17. assert_metric_label_value parses specific labeled series
      18. assert_metric_value missing → AssertionError
"""

import sys

from hyperdjango.telemetry import (
    STATUS_ERROR,
    STATUS_OK,
    Counter,
    CounterVec,
    InMemorySink,
    TelemetryAssertions,
    disable,
    enable,
)
from hyperdjango.telemetry.metrics import collect_prometheus_text

passed = 0
failed = 0
errors: list[str] = []


def check(name: str, cond: bool, msg: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        err = f"FAIL: {name}"
        if msg:
            err += f" — {msg}"
        errors.append(err)
        print(f"  {err}")


def _expect_raises(name: str, fn) -> None:
    """Run fn, expect AssertionError. Emit check."""
    try:
        fn()
    except AssertionError:
        check(name, True)
        return
    check(name, False, "expected AssertionError")


# ── Span fixture ────────────────────────────────────────────────────────────


def _build_sink_with_spans() -> InMemorySink:
    sink = InMemorySink()
    sink.export_spans(
        [
            {
                "trace_id": "aaaa" * 8,
                "span_id": "a1" * 8,
                "parent_id": "",
                "name": "GET /books",
                "start_time_unix_nano": 100,
                "end_time_unix_nano": 200,
                "attributes": {
                    "http.method": "GET",
                    "http.status_code": "200",
                    "user.id": 42,
                },
                "status": {"code": STATUS_OK, "message": ""},
            },
            {
                "trace_id": "aaaa" * 8,
                "span_id": "b2" * 8,
                "parent_id": "a1" * 8,
                "name": "db.query",
                "start_time_unix_nano": 110,
                "end_time_unix_nano": 150,
                "attributes": {"sql": "SELECT * FROM books"},
                "status": {"code": STATUS_OK, "message": ""},
            },
            {
                "trace_id": "cccc" * 8,
                "span_id": "c3" * 8,
                "parent_id": "",
                "name": "POST /fail",
                "start_time_unix_nano": 300,
                "end_time_unix_nano": 350,
                "attributes": {
                    "http.method": "POST",
                    "http.status_code": "500",
                    "error.type": "ValueError",
                    "error.message": "boom",
                },
                "status": {"code": STATUS_ERROR, "message": ""},
            },
        ]
    )
    return sink


# ── Span assertion tests ───────────────────────────────────────────────────


def test_span_count_match() -> None:
    print("\n── assert_span_count: match ──")
    sink = _build_sink_with_spans()
    asserts = TelemetryAssertions(sink)
    asserts.assert_span_count(3)
    check("span count 3 matches", True)


def test_span_count_mismatch() -> None:
    print("\n── assert_span_count: mismatch ──")
    sink = _build_sink_with_spans()
    asserts = TelemetryAssertions(sink)
    _expect_raises("count mismatch raises", lambda: asserts.assert_span_count(5))


def test_span_count_at_least() -> None:
    print("\n── assert_span_count_at_least ──")
    sink = _build_sink_with_spans()
    asserts = TelemetryAssertions(sink)
    asserts.assert_span_count_at_least(2)
    check("≥2 passes (3 buffered)", True)
    _expect_raises("≥5 raises", lambda: asserts.assert_span_count_at_least(5))


def test_has_span() -> None:
    print("\n── assert_has_span ──")
    sink = _build_sink_with_spans()
    asserts = TelemetryAssertions(sink)
    asserts.assert_has_span("GET /books")
    check("existing name matches", True)
    _expect_raises(
        "missing name raises",
        lambda: asserts.assert_has_span("GET /nowhere"),
    )


def test_span_attr_match() -> None:
    print("\n── assert_span_attr match + coercion ──")
    sink = _build_sink_with_spans()
    asserts = TelemetryAssertions(sink)
    asserts.assert_span_attr("GET /books", "http.method", "GET")
    check("string attr matches", True)
    # Numeric coercion: user.id is int 42, test with str "42"
    asserts.assert_span_attr("GET /books", "user.id", "42")
    check("numeric coerced match", True)
    asserts.assert_span_attr("GET /books", "user.id", 42)
    check("numeric native match", True)


def test_span_attr_missing() -> None:
    print("\n── assert_span_attr missing key ──")
    sink = _build_sink_with_spans()
    asserts = TelemetryAssertions(sink)
    _expect_raises(
        "missing key raises",
        lambda: asserts.assert_span_attr("GET /books", "user.name", "alice"),
    )


def test_span_attr_contains() -> None:
    print("\n── assert_span_attr_contains ──")
    sink = _build_sink_with_spans()
    asserts = TelemetryAssertions(sink)
    asserts.assert_span_attr_contains("db.query", "sql", "FROM books")
    check("substring found", True)
    _expect_raises(
        "substring not found raises",
        lambda: asserts.assert_span_attr_contains("db.query", "sql", "MARS"),
    )


def test_span_status() -> None:
    print("\n── assert_span_status ──")
    sink = _build_sink_with_spans()
    asserts = TelemetryAssertions(sink)
    asserts.assert_span_status("GET /books", STATUS_OK)
    check("OK status matches", True)
    asserts.assert_span_status("POST /fail", STATUS_ERROR)
    check("ERROR status matches", True)
    _expect_raises(
        "wrong status raises",
        lambda: asserts.assert_span_status("POST /fail", STATUS_OK),
    )


def test_no_error_spans_passes_when_clean() -> None:
    print("\n── assert_no_error_spans: clean buffer ──")
    sink = InMemorySink()
    sink.export_spans(
        [
            {
                "trace_id": "aa",
                "span_id": "bb",
                "parent_id": "",
                "name": "clean",
                "start_time_unix_nano": 0,
                "end_time_unix_nano": 1,
                "attributes": {},
                "status": {"code": STATUS_OK, "message": ""},
            }
        ]
    )
    asserts = TelemetryAssertions(sink)
    asserts.assert_no_error_spans()
    check("no errors → passes", True)


def test_no_error_spans_fails_on_error() -> None:
    print("\n── assert_no_error_spans: one error ──")
    sink = _build_sink_with_spans()
    asserts = TelemetryAssertions(sink)
    _expect_raises("error span raises", lambda: asserts.assert_no_error_spans())


def test_span_chain_valid() -> None:
    print("\n── assert_span_chain: valid chain ──")
    sink = _build_sink_with_spans()
    asserts = TelemetryAssertions(sink)
    asserts.assert_span_chain(["GET /books", "db.query"])
    check("valid chain passes", True)


def test_span_chain_broken() -> None:
    print("\n── assert_span_chain: broken chain ──")
    sink = _build_sink_with_spans()
    asserts = TelemetryAssertions(sink)
    # POST /fail is a root (parent_id="") — not a child of GET /books
    _expect_raises(
        "broken chain raises",
        lambda: asserts.assert_span_chain(["GET /books", "POST /fail"]),
    )


# ── Metric assertion tests ─────────────────────────────────────────────────


def test_metric_assertions_roundtrip() -> None:
    print("\n── Metric assertions: full roundtrip ──")
    enable()
    try:
        counter = Counter("hyperdjango_assert_counter_total", "test counter")
        counter.inc(7)
        vec = CounterVec(
            "hyperdjango_assert_vec_total",
            "labeled test counter",
            label_names=("op", "status"),
        )
        vec.inc({"op": "read", "status": "ok"})
        vec.inc({"op": "read", "status": "ok"})
        vec.inc({"op": "write", "status": "fail"})

        sink = InMemorySink()
        sink.export_metrics(collect_prometheus_text())
        asserts = TelemetryAssertions(sink)

        asserts.assert_metric_present("hyperdjango_assert_counter_total")
        check("non-labeled present", True)
        asserts.assert_metric_present("hyperdjango_assert_vec_total")
        check("labeled present", True)

        asserts.assert_metric_value("hyperdjango_assert_counter_total", 7.0)
        check("non-labeled value correct", True)

        asserts.assert_metric_has_label("hyperdjango_assert_vec_total", "op", "read")
        check("labeled series found", True)

        asserts.assert_metric_label_value(
            "hyperdjango_assert_vec_total",
            {"op": "read", "status": "ok"},
            2.0,
        )
        check("labeled series value correct", True)

        asserts.assert_metric_label_value(
            "hyperdjango_assert_vec_total",
            {"op": "write", "status": "fail"},
            1.0,
        )
        check("second labeled series value correct", True)

        _expect_raises(
            "missing metric raises",
            lambda: asserts.assert_metric_present("hyperdjango_assert_nonexistent"),
        )
        _expect_raises(
            "wrong label raises",
            lambda: asserts.assert_metric_has_label(
                "hyperdjango_assert_vec_total", "op", "delete"
            ),
        )
    finally:
        disable()


def main() -> int:
    print("=" * 70)
    print("  TelemetryAssertions test helper API (P6.1)")
    print("=" * 70)

    test_span_count_match()
    test_span_count_mismatch()
    test_span_count_at_least()
    test_has_span()
    test_span_attr_match()
    test_span_attr_missing()
    test_span_attr_contains()
    test_span_status()
    test_no_error_spans_passes_when_clean()
    test_no_error_spans_fails_on_error()
    test_span_chain_valid()
    test_span_chain_broken()
    test_metric_assertions_roundtrip()

    print()
    print("=" * 70)
    total = passed + failed
    print(f"Results: {passed}/{total} passed, {failed} failed")
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  {e}")
    print("=" * 70)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
