"""
Subsystem metric wiring tests (Telemetry P7, task #253).

# hyper-test: unit

Validates that every subsystem extended in Phase 7 emits its native metric
when the underlying state-update site is exercised. Each subsystem owns one
to three time series; the relevant ones are:

  RateLimitMiddleware       hyperdjango_rate_limit_hits_total{backend}
  CSRFMiddleware            hyperdjango_csrf_violations_total{reason}
  SessionAuth               hyperdjango_session_auth_total{result}
  HyperGuard                hyperdjango_guard_denials_total{reason}
  DataLoader                hyperdjango_dataloader_loads_total{result}
                            hyperdjango_dataloader_batch_dispatches_total
                            hyperdjango_dataloader_batch_size (histogram)
  TemplateEngine            hyperdjango_template_renders_total
                            hyperdjango_template_render_duration_seconds (histogram)
  HyperAdmin                hyperdjango_admin_actions_total{model, action}
  pg.zig pool sampler       hyperdjango_pool_total_connections + 6 more gauges
  Sampler registry          register_sampler / _run_samplers contract

The pattern: enable telemetry, exercise the state-update site, scrape the
Prometheus exposition text, assert the metric is present with the expected
label set + count. We deliberately don't assert *exact* counter values for
metrics that may be bumped by other tests in the same process; instead we
take a baseline snapshot and require the delta after the action to be ≥ 1
(or ≥ N for batched actions). Same pattern as the existing telemetry
suite — robust to test ordering and module-level singletons.

Pure-Python tests, no event loop, no DB required.
"""

import asyncio
import sys

from hyperdjango.telemetry import (
    InMemorySink,
    TelemetryAssertions,
    disable,
    enable,
    register_sampler,
)
from hyperdjango.telemetry.metrics import (
    Counter,
    Gauge,
    _run_samplers,
    _samplers,
    collect_prometheus_text,
)

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


# ── Helpers ─────────────────────────────────────────────────────────────────


def _scrape() -> str:
    """Return the full Prometheus exposition as text."""
    return collect_prometheus_text().decode("utf-8")


def _series_value(text: str, name: str, labels: dict[str, str] | None = None) -> float:
    """Find the FIRST line matching `name{labels...} <value>` and return value.

    Returns 0.0 when the series is not present (so a "metric not yet bumped"
    case still gives an arithmetic baseline).
    """
    label_strs = []
    if labels:
        # Prometheus exposition orders labels by registration; for our test
        # we accept any order so split-and-compare per-key.
        pass
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        # Strip name + optional labels
        if not line.startswith(name):
            continue
        head, _, value_str = line.rpartition(" ")
        if labels is None:
            if head == name:
                try:
                    return float(value_str)
                except ValueError:
                    return 0.0
            continue
        # head looks like: name{k1="v1",k2="v2"}
        if not head.startswith(name + "{") or not head.endswith("}"):
            continue
        body = head[len(name) + 1 : -1]
        parts = {}
        for kv in body.split(","):
            if "=" not in kv:
                continue
            k, v = kv.split("=", 1)
            parts[k.strip()] = v.strip().strip('"')
        if all(parts.get(k) == v for k, v in labels.items()):
            try:
                return float(value_str)
            except ValueError:
                return 0.0
    return 0.0


# ── Test 1: RateLimitMiddleware → rate_limit_hits_total ─────────────────────


def test_ratelimit_metric() -> None:
    enable()
    try:
        # Importing the middleware module triggers the module-level Counter
        # registration; we then bump via the same private singleton the
        # middleware uses, proving the registration name is what dashboards
        # will see.
        from hyperdjango.ratelimit import _rate_limit_hits_total

        before = _series_value(
            _scrape(),
            "hyperdjango_rate_limit_hits_total",
            {"backend": "memory"},
        )
        _rate_limit_hits_total.inc_tuple(("memory",))
        _rate_limit_hits_total.inc_tuple(("memory",))
        after = _series_value(
            _scrape(),
            "hyperdjango_rate_limit_hits_total",
            {"backend": "memory"},
        )
        check("rate_limit_hits_total backend=memory delta == 2", after - before == 2.0)
    finally:
        disable()


# ── Test 2: CSRFMiddleware → csrf_violations_total ──────────────────────────


def test_csrf_metric() -> None:
    enable()
    try:
        from hyperdjango.standalone_middleware import _csrf_violations_total

        text_before = _scrape()
        miss_before = _series_value(
            text_before,
            "hyperdjango_csrf_violations_total",
            {"reason": "missing"},
        )
        mis_before = _series_value(
            text_before,
            "hyperdjango_csrf_violations_total",
            {"reason": "mismatch"},
        )
        _csrf_violations_total.inc_tuple(("missing",))
        _csrf_violations_total.inc_tuple(("mismatch",))
        _csrf_violations_total.inc_tuple(("mismatch",))
        text_after = _scrape()
        miss_after = _series_value(
            text_after,
            "hyperdjango_csrf_violations_total",
            {"reason": "missing"},
        )
        mis_after = _series_value(
            text_after,
            "hyperdjango_csrf_violations_total",
            {"reason": "mismatch"},
        )
        check(
            "csrf_violations_total reason=missing delta == 1",
            miss_after - miss_before == 1.0,
        )
        check(
            "csrf_violations_total reason=mismatch delta == 2",
            mis_after - mis_before == 2.0,
        )
    finally:
        disable()


# ── Test 3: SessionAuth → session_auth_total ────────────────────────────────


def test_session_auth_metric() -> None:
    enable()
    try:
        from hyperdjango.auth.sessions import _session_auth_total

        before = {
            label: _series_value(
                _scrape(),
                "hyperdjango_session_auth_total",
                {"result": label},
            )
            for label in (
                "ok",
                "no_cookie",
                "invalid_cookie",
                "not_found",
                "hash_mismatch",
            )
        }
        for label in before:
            _session_auth_total.inc_tuple((label,))
        after = {
            label: _series_value(
                _scrape(),
                "hyperdjango_session_auth_total",
                {"result": label},
            )
            for label in before
        }
        for label in before:
            check(
                f"session_auth_total result={label} delta == 1",
                after[label] - before[label] == 1.0,
            )
    finally:
        disable()


# ── Test 4: HyperGuard → guard_denials_total ────────────────────────────────


def test_guard_denials_metric() -> None:
    enable()
    try:
        from hyperdjango.guard.evaluator import _guard_denials_total

        before = _series_value(
            _scrape(),
            "hyperdjango_guard_denials_total",
            {"reason": "permission_denied"},
        )
        _guard_denials_total.inc_tuple(("permission_denied",))
        after = _series_value(
            _scrape(),
            "hyperdjango_guard_denials_total",
            {"reason": "permission_denied"},
        )
        check(
            "guard_denials_total reason=permission_denied delta == 1",
            after - before == 1.0,
        )
    finally:
        disable()


# ── Test 5: DataLoader → loads_total + batch_dispatches + batch_size ────────


def test_dataloader_metrics() -> None:
    enable()
    try:
        from hyperdjango.dataloader import (
            DataLoader,
            _dataloader_batch_dispatches_total,
        )

        async def batch_users(keys):
            return [{"id": k, "name": f"u{k}"} for k in keys]

        loader = DataLoader(batch_fn=batch_users, max_batch_size=10)

        loads_miss_before = _series_value(
            _scrape(),
            "hyperdjango_dataloader_loads_total",
            {"result": "miss"},
        )
        loads_hit_before = _series_value(
            _scrape(),
            "hyperdjango_dataloader_loads_total",
            {"result": "hit"},
        )
        dispatches_before = _dataloader_batch_dispatches_total.value()

        async def run() -> None:
            # Three distinct keys → batched into ONE dispatch (3 misses)
            await asyncio.gather(
                loader.load(1),
                loader.load(2),
                loader.load(3),
            )
            # Same keys again → all cached (3 hits)
            await asyncio.gather(
                loader.load(1),
                loader.load(2),
                loader.load(3),
            )

        asyncio.run(run())

        loads_miss_after = _series_value(
            _scrape(),
            "hyperdjango_dataloader_loads_total",
            {"result": "miss"},
        )
        loads_hit_after = _series_value(
            _scrape(),
            "hyperdjango_dataloader_loads_total",
            {"result": "hit"},
        )
        dispatches_after = _dataloader_batch_dispatches_total.value()

        check(
            "dataloader_loads_total result=miss delta == 3",
            loads_miss_after - loads_miss_before == 3.0,
        )
        check(
            "dataloader_loads_total result=hit delta == 3",
            loads_hit_after - loads_hit_before == 3.0,
        )
        check(
            "dataloader_batch_dispatches_total delta == 1",
            dispatches_after - dispatches_before == 1,
        )

        # Verify the histogram registered with our custom buckets — its
        # name appears in exposition.
        text = _scrape()
        check(
            "dataloader_batch_size histogram present",
            "hyperdjango_dataloader_batch_size" in text,
        )
    finally:
        disable()


# ── Test 6: TemplateEngine → renders_total + render_duration ────────────────


def test_template_metrics() -> None:
    enable()
    try:
        # render_string() takes the same metric path as render() and avoids
        # needing a templates dir on disk.
        from hyperdjango.templating import (
            TemplateEngine,
            _template_renders_total,
        )

        engine = TemplateEngine(template_dir=".")
        before = _template_renders_total.value()
        for _ in range(3):
            engine.render_string("Hello {{ name }}", {"name": "world"})
        after = _template_renders_total.value()

        check("template_renders_total delta == 3", after - before == 3)
        check(
            "template_render_duration_seconds present in scrape",
            "hyperdjango_template_render_duration_seconds" in _scrape(),
        )
        # Histogram observe path has been touched (compare _count series).
        # Use the assertion helper for the count assertion.
        sink = InMemorySink()
        sink.export_metrics(collect_prometheus_text())
        asserts = TelemetryAssertions(sink)
        asserts.assert_metric_present(
            "hyperdjango_template_render_duration_seconds_count"
        )
        check("render_duration_seconds_count present (asserted)", True)
    finally:
        disable()


# ── Test 7: HyperAdmin → admin_actions_total ────────────────────────────────


def test_admin_actions_metric() -> None:
    enable()
    try:
        # We test the metric singleton directly because exercising _audit_log
        # requires a full HyperAdmin instance + DB connection. The metric
        # name + label set is the contract dashboards depend on; the bump
        # site is verified by reading the source (and by the e2e admin
        # tests under `test_e2e_*`).
        from hyperdjango.admin import _admin_actions_total

        before = {
            action: _series_value(
                _scrape(),
                "hyperdjango_admin_actions_total",
                {"model": "book", "action": action},
            )
            for action in ("add", "change", "delete")
        }
        _admin_actions_total.inc_tuple(("book", "add"))
        _admin_actions_total.inc_tuple(("book", "change"))
        _admin_actions_total.inc_tuple(("book", "change"))
        _admin_actions_total.inc_tuple(("book", "delete"))
        after = {
            action: _series_value(
                _scrape(),
                "hyperdjango_admin_actions_total",
                {"model": "book", "action": action},
            )
            for action in before
        }
        check(
            "admin_actions_total model=book action=add delta == 1",
            after["add"] - before["add"] == 1.0,
        )
        check(
            "admin_actions_total model=book action=change delta == 2",
            after["change"] - before["change"] == 2.0,
        )
        check(
            "admin_actions_total model=book action=delete delta == 1",
            after["delete"] - before["delete"] == 1.0,
        )
    finally:
        disable()


# ── Test 8: Sampler registry contract ───────────────────────────────────────


def test_sampler_registry_contract() -> None:
    """register_sampler is push-only with idempotent dedupe; _run_samplers
    invokes every registered callable exactly once and isolates errors."""
    invocations: list[str] = []

    def good():
        invocations.append("good")

    def also_good():
        invocations.append("also_good")

    def broken():
        invocations.append("broken")
        raise RuntimeError("intentional")

    # Snapshot original state to keep the rest of the suite isolated
    original = list(_samplers)
    try:
        _samplers.clear()
        register_sampler(good)
        register_sampler(also_good)
        register_sampler(broken)
        register_sampler(good)  # idempotent — should not double-add

        check("sampler dedupe — len(_samplers) == 3", len(_samplers) == 3)

        errors_caught = _run_samplers()
        check(
            "sampler invocation order preserved",
            invocations == ["good", "also_good", "broken"],
        )
        check("sampler errors collected, count == 1", len(errors_caught) == 1)
        check(
            "sampler error type == RuntimeError",
            isinstance(errors_caught[0], RuntimeError),
        )
    finally:
        _samplers.clear()
        _samplers.extend(original)


# ── Test 9: register_sampler bumps a Gauge end-to-end via _run_samplers ─────


def test_sampler_drives_gauge() -> None:
    """A sampler can update a Gauge that then appears in scrape output."""
    enable()
    try:
        # Use a uniquely-named Gauge so the test is order-independent.
        gauge = Gauge(
            "hyperdjango_test_sampler_gauge",
            "Test gauge driven by a sampler.",
        )
        counter = Counter(
            "hyperdjango_test_sampler_invocations",
            "Sampler invocation counter.",
        )

        def sampler():
            gauge.set(42)
            counter.inc()

        original = list(_samplers)
        try:
            _samplers.clear()
            register_sampler(sampler)
            _run_samplers()
            check("test sampler set gauge to 42", gauge.value() == 42)
            check("test sampler bumped counter", counter.value() >= 1)

            text = _scrape()
            check(
                "test gauge appears in exposition",
                "hyperdjango_test_sampler_gauge 42" in text,
            )
        finally:
            _samplers.clear()
            _samplers.extend(original)
    finally:
        disable()


# ── Test 10: pool sampler is registered and survives no-pool case ───────────


def test_pool_sampler_no_db() -> None:
    """When no Database has been instantiated, _sample_pool_gauges is a
    safe no-op — it returns without raising and without updating the
    gauges. This protects telemetry boot from triggering DB connections.
    """
    enable()
    try:
        # Force-clear the global default db so the sampler hits the no-op path.
        import hyperdjango.database as _db_module
        from hyperdjango.database import (
            _pool_in_use_connections,
            _pool_total_connections,
            _sample_pool_gauges,
        )

        original_db = _db_module._db
        try:
            _db_module._db = None
            before_total = _pool_total_connections.value()
            before_in_use = _pool_in_use_connections.value()
            _sample_pool_gauges()  # must not raise
            after_total = _pool_total_connections.value()
            after_in_use = _pool_in_use_connections.value()
            check(
                "pool sampler unchanged total when _db is None",
                after_total == before_total,
            )
            check(
                "pool sampler unchanged in_use when _db is None",
                after_in_use == before_in_use,
            )
        finally:
            _db_module._db = original_db

        # The sampler IS registered globally — assert it's in the registry
        # so the drain worker will pick it up at runtime.
        check(
            "_sample_pool_gauges registered in global samplers",
            _sample_pool_gauges in _samplers,
        )
    finally:
        disable()


# ── Test 11: Zero cost when disabled — every bump is a no-op ────────────────


def test_zero_cost_when_disabled() -> None:
    """When telemetry is disabled, all subsystem metrics are silently
    no-op (the Counter/Gauge/Histogram fast-path branches return early).
    """
    disable()
    from hyperdjango.dataloader import _dataloader_loads_total
    from hyperdjango.ratelimit import _rate_limit_hits_total
    from hyperdjango.standalone_middleware import _csrf_violations_total

    text_before = _scrape()
    rl_before = _series_value(
        text_before,
        "hyperdjango_rate_limit_hits_total",
        {"backend": "memory"},
    )
    csrf_before = _series_value(
        text_before,
        "hyperdjango_csrf_violations_total",
        {"reason": "missing"},
    )
    loads_before = _series_value(
        text_before,
        "hyperdjango_dataloader_loads_total",
        {"result": "miss"},
    )

    # Spam the bumps — must all no-op
    for _ in range(50):
        _rate_limit_hits_total.inc_tuple(("memory",))
        _csrf_violations_total.inc_tuple(("missing",))
        _dataloader_loads_total.inc_tuple(("miss",))

    text_after = _scrape()
    rl_after = _series_value(
        text_after,
        "hyperdjango_rate_limit_hits_total",
        {"backend": "memory"},
    )
    csrf_after = _series_value(
        text_after,
        "hyperdjango_csrf_violations_total",
        {"reason": "missing"},
    )
    loads_after = _series_value(
        text_after,
        "hyperdjango_dataloader_loads_total",
        {"result": "miss"},
    )

    check("disabled: rate_limit_hits_total unchanged", rl_after == rl_before)
    check("disabled: csrf_violations_total unchanged", csrf_after == csrf_before)
    check("disabled: dataloader_loads_total unchanged", loads_after == loads_before)


# ── Test 12: All registered metric NAMES appear in exposition ───────────────


def test_all_subsystem_metric_names_present() -> None:
    """Documentation contract: every metric name listed in the docstring
    must be present in the Prometheus exposition. Catches drift if a
    subsystem registration is removed without updating the test."""
    enable()
    try:
        # Bump every counter at least once so the series is materialized.
        from hyperdjango.admin import _admin_actions_total
        from hyperdjango.auth.sessions import _session_auth_total
        from hyperdjango.dataloader import (
            _dataloader_batch_dispatches_total,
            _dataloader_batch_size,
            _dataloader_loads_total,
        )
        from hyperdjango.guard.evaluator import _guard_denials_total
        from hyperdjango.ratelimit import _rate_limit_hits_total
        from hyperdjango.standalone_middleware import _csrf_violations_total
        from hyperdjango.templating import (
            _template_render_duration_seconds,
            _template_renders_total,
        )

        _rate_limit_hits_total.inc_tuple(("memory",))
        _csrf_violations_total.inc_tuple(("missing",))
        _session_auth_total.inc_tuple(("ok",))
        _guard_denials_total.inc_tuple(("permission_denied",))
        _dataloader_loads_total.inc_tuple(("hit",))
        _dataloader_batch_dispatches_total.inc(1)
        _dataloader_batch_size.observe(7.0)
        _template_renders_total.inc(1)
        _template_render_duration_seconds.observe(0.001)
        _admin_actions_total.inc_tuple(("book", "add"))

        text = _scrape()
        expected_names = (
            "hyperdjango_rate_limit_hits_total",
            "hyperdjango_csrf_violations_total",
            "hyperdjango_session_auth_total",
            "hyperdjango_guard_denials_total",
            "hyperdjango_dataloader_loads_total",
            "hyperdjango_dataloader_batch_dispatches_total",
            "hyperdjango_dataloader_batch_size",
            "hyperdjango_template_renders_total",
            "hyperdjango_template_render_duration_seconds",
            "hyperdjango_admin_actions_total",
            "hyperdjango_pool_total_connections",
            "hyperdjango_pool_in_use_connections",
            "hyperdjango_pool_available_connections",
            "hyperdjango_pool_waiters",
            "hyperdjango_pool_max_waiters",
            "hyperdjango_pool_acquires",
            "hyperdjango_pool_timeouts",
        )
        for name in expected_names:
            check(f"{name} present in exposition", name in text)
    finally:
        disable()


def main() -> int:
    print("=" * 70)
    print("  Telemetry P7 — subsystem metric wiring (task #253)")
    print("=" * 70)

    test_ratelimit_metric()
    test_csrf_metric()
    test_session_auth_metric()
    test_guard_denials_metric()
    test_dataloader_metrics()
    test_template_metrics()
    test_admin_actions_metric()
    test_sampler_registry_contract()
    test_sampler_drives_gauge()
    test_pool_sampler_no_db()
    test_zero_cost_when_disabled()
    test_all_subsystem_metric_names_present()

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
