"""
Test helper assertions for HyperDjango telemetry (v0.15.0+).

Pairs with `InMemorySink` to give tests fluent, targeted assertions
over what was recorded during a request or block of code. Tests that
need to verify "this handler produced a span named X with these attrs"
or "this action bumped the cache_miss counter" should use these
helpers instead of reaching into `sink.spans` / Prometheus text
manually.

Usage:

    from hyperdjango.telemetry import InMemorySink, TelemetryMiddleware
    from hyperdjango.telemetry.assertions import TelemetryAssertions

    sink = InMemorySink()
    mw = TelemetryMiddleware(sinks=[sink], ...)
    app.use(mw)

    # ...drive the app, then drain...
    mw.drain_now()
    asserts = TelemetryAssertions(sink)

    asserts.assert_span_count(3)
    asserts.assert_has_span("GET /api/books")
    asserts.assert_span_attr("GET /api/books", "http.status_code", "200")
    asserts.assert_no_error_spans()
    asserts.assert_metric_present("hyperdjango_http_requests_total")
    asserts.assert_metric_has_label(
        "hyperdjango_http_requests_total", "method", "GET"
    )

All assertions raise `AssertionError` with a readable diff on
failure — no need to format your own message.

Design:

  * Every public method is a dataclass method on `TelemetryAssertions`
    (slots=True, frozen=False so tests can mutate for ergonomics).
  * `InMemorySink` is the only accepted source — these helpers never
    reach into the native ring directly, so they're safe to call
    from any thread after `drain_now()`.
  * No global mutation. The caller owns the sink lifetime.
  * Metric queries parse the Prometheus exposition text exactly
    once per assertion (tests typically call a handful of asserts
    per test case, so caching the parse isn't worth the complexity).
"""

from dataclasses import dataclass

from hyperdjango.telemetry.sinks.memory import InMemorySink
from hyperdjango.telemetry.tracing import STATUS_ERROR


@dataclass(slots=True)
class TelemetryAssertions:
    """Fluent assertion wrapper around an `InMemorySink` buffer.

    Construct with a sink that was populated by a `TelemetryMiddleware`
    (or manually via `sink.export_spans`). Each assertion method
    raises `AssertionError` on failure with a targeted message — no
    need to write `assert ..., f"..."` boilerplate in every test.
    """

    sink: InMemorySink

    # ── Span assertions ───────────────────────────────────────────────

    def assert_span_count(self, expected: int) -> None:
        actual = len(self.sink.spans)
        if actual != expected:
            raise AssertionError(
                f"expected {expected} span(s) in InMemorySink, got {actual}. "
                f"Spans: {[s['name'] for s in self.sink.spans]}"
            )

    def assert_span_count_at_least(self, minimum: int) -> None:
        actual = len(self.sink.spans)
        if actual < minimum:
            raise AssertionError(
                f"expected at least {minimum} span(s), got {actual}. "
                f"Spans: {[s['name'] for s in self.sink.spans]}"
            )

    def assert_has_span(self, name: str) -> None:
        """Assert at least one span with the exact given name exists."""
        found = [s for s in self.sink.spans if s["name"] == name]
        if not found:
            names = [s["name"] for s in self.sink.spans]
            raise AssertionError(
                f"no span named {name!r} in InMemorySink. Available: {names}"
            )

    def assert_span_attr(self, span_name: str, key: str, value) -> None:
        """Assert the (first) span with `span_name` has attr `key == value`."""
        span = self._find_span(span_name)
        attrs = span["attributes"]
        if key not in attrs:
            raise AssertionError(
                f"span {span_name!r} missing attr {key!r}. "
                f"Available attrs: {sorted(attrs.keys())}"
            )
        actual = attrs[key]
        # Accept str coercion for ints/bools so callers don't need to
        # remember whether native attrs are stringified
        if actual != value and str(actual) != str(value):
            raise AssertionError(
                f"span {span_name!r} attr {key!r}: expected {value!r}, got {actual!r}"
            )

    def assert_span_attr_contains(self, span_name: str, key: str, needle: str) -> None:
        """Assert span attr value contains `needle` as a substring."""
        span = self._find_span(span_name)
        attrs = span["attributes"]
        actual = str(attrs.get(key, ""))
        if needle not in actual:
            raise AssertionError(
                f"span {span_name!r} attr {key!r}: "
                f"expected to contain {needle!r}, got {actual!r}"
            )

    def assert_span_status(self, span_name: str, code: int) -> None:
        """Assert the span's status.code matches. Use STATUS_OK/ERROR."""
        span = self._find_span(span_name)
        actual = span["status"]["code"]
        if actual != code:
            raise AssertionError(
                f"span {span_name!r} status.code: expected {code}, got {actual}"
            )

    def assert_no_error_spans(self) -> None:
        """Assert zero spans have status.code == STATUS_ERROR."""
        errors = [s for s in self.sink.spans if s["status"]["code"] == STATUS_ERROR]
        if errors:
            names = [s["name"] for s in errors]
            raise AssertionError(
                f"expected no error spans, found {len(errors)}: {names}"
            )

    def assert_span_chain(self, names: list[str]) -> None:
        """Assert a parent/child chain: each name in `names` is a span
        whose parent_id matches the previous name's span_id.

        Useful for verifying nested trace context propagation.
        """
        if not names:
            return
        chain: list[dict] = []
        for name in names:
            chain.append(self._find_span(name))
        for i in range(1, len(chain)):
            parent_of_child = chain[i].get("parent_id", "")
            parent_id = chain[i - 1]["span_id"]
            if parent_of_child != parent_id:
                raise AssertionError(
                    f"span chain broken: {names[i]!r} parent_id={parent_of_child!r} "
                    f"does not match {names[i - 1]!r} span_id={parent_id!r}"
                )

    # ── Metric assertions (Prometheus text-format parsing) ────────────

    def assert_metric_present(self, name: str) -> None:
        """Assert the metric name appears in the latest Prometheus scrape."""
        text = self.sink.latest_metrics.decode("utf-8", errors="replace")
        if name not in text:
            raise AssertionError(
                f"metric {name!r} not in Prometheus exposition. "
                f"Exposition snippet: {text[:500]!r}"
            )

    def assert_metric_value(
        self, name: str, expected: float, tolerance: float = 0.001
    ) -> None:
        """Assert the metric's value matches. Only works for non-labeled
        Counter/Gauge — use `assert_metric_label_value` for vecs.
        """
        actual = self._get_metric_value(name)
        if actual is None:
            raise AssertionError(f"metric {name!r} not found in Prometheus exposition")
        if abs(actual - expected) > tolerance:
            raise AssertionError(
                f"metric {name!r} value: expected {expected}, got {actual}"
            )

    def assert_metric_has_label(
        self, name: str, label_key: str, label_value: str
    ) -> None:
        """Assert at least one series for `name` has `label_key="label_value"`."""
        text = self.sink.latest_metrics.decode("utf-8", errors="replace")
        target = f'{label_key}="{label_value}"'
        for line in text.splitlines():
            if line.startswith(name) and target in line:
                return
        raise AssertionError(
            f"metric {name!r} has no series with {label_key}={label_value!r}. "
            f"Available lines: {[l for l in text.splitlines() if l.startswith(name)][:5]}"
        )

    def assert_metric_label_value(
        self,
        name: str,
        labels: dict[str, str],
        expected: float,
        tolerance: float = 0.001,
    ) -> None:
        """Assert the labeled series for `name{labels=...}` has value."""
        actual = self._get_labeled_metric_value(name, labels)
        if actual is None:
            raise AssertionError(f"metric {name!r} with labels {labels!r} not found")
        if abs(actual - expected) > tolerance:
            raise AssertionError(
                f"metric {name!r}{labels!r}: expected {expected}, got {actual}"
            )

    # ── Internal helpers ──────────────────────────────────────────────

    def _find_span(self, name: str) -> dict:
        for span in self.sink.spans:
            if span["name"] == name:
                return span
        names = [s["name"] for s in self.sink.spans]
        raise AssertionError(
            f"no span named {name!r} in InMemorySink. Available: {names}"
        )

    def _get_metric_value(self, name: str) -> float | None:
        """Parse the latest Prometheus exposition for a single-value metric.

        Returns None if the metric is missing. Raises AssertionError if
        the metric exists but has multiple labeled series (use the
        labeled variant instead).
        """
        text = self.sink.latest_metrics.decode("utf-8", errors="replace")
        candidates: list[tuple[str, float]] = []
        for line in text.splitlines():
            if line.startswith("#"):
                continue
            if not line.startswith(name):
                continue
            # Lines are either `name value` or `name{labels} value`
            rest = line[len(name) :]
            if rest.startswith("{"):
                continue  # labeled — skip in this path
            # rest is ` value` or `_suffix value` (e.g. _count, _sum)
            parts = line.split()
            if len(parts) != 2:
                continue
            if parts[0] != name:
                # Could be histogram _count / _sum / _bucket — ignore here
                continue
            try:
                candidates.append((parts[0], float(parts[1])))
            except ValueError:
                continue
        if not candidates:
            return None
        return candidates[0][1]

    def _get_labeled_metric_value(
        self, name: str, labels: dict[str, str]
    ) -> float | None:
        """Parse the latest Prometheus exposition for a specific labeled series."""
        text = self.sink.latest_metrics.decode("utf-8", errors="replace")
        for line in text.splitlines():
            if line.startswith("#"):
                continue
            if not line.startswith(name + "{"):
                continue
            if not all(f'{k}="{v}"' in line for k, v in labels.items()):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                return float(parts[-1])
            except ValueError:
                continue
        return None
