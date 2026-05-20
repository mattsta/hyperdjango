"""Tests for multi-dimensional usage metering engine.

Tests MeterEngine: define_meter, record, query, query_multi,
query_hierarchy, quotas, hooks, middleware, aggregation, cleanup.
"""

# hyper-test: db_isolated

import asyncio
import os
import sys
from datetime import UTC, datetime

from hyperdjango.database import Database, set_db
from hyperdjango.metering import (
    AggregateResult,
    AlertHook,
    DimensionSpec,
    MeterEngine,
    MeterHook,
    MeterHookContext,
    PeriodExport,
    get_meter_engine,
    set_meter_engine,
)

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost:5432/hyperdjango_test")


def run_async(coro):
    return asyncio.run(coro)


async def setup_engine():
    db = Database(DB_URL, max_size=3)
    await db.connect()
    set_db(db)
    engine = MeterEngine(db)
    await engine.ensure_tables()
    # Clean previous test data
    for table in (
        "hyper_meter_event_values",
        "hyper_meter_events",
        "hyper_meter_aggregates",
        "hyper_meter_quotas",
        "hyper_meter_accounts",
        "hyper_meter_dimensions",
        "hyper_meters",
    ):
        await db.execute(f"DELETE FROM {table}")
    return db, engine


async def teardown(db):
    await db.disconnect()


# ── DimensionSpec tests ───────────────────────────────────────────────────


def test_dimension_spec():
    """DimensionSpec is frozen and stores all fields."""
    spec = DimensionSpec("tokens_in", "counter", "tokens", "sum")
    assert spec.name == "tokens_in"
    assert spec.dimension_type == "counter"
    assert spec.unit == "tokens"
    assert spec.default_agg == "sum"
    try:
        spec.name = "changed"
        assert False, "Should be frozen"
    except AttributeError:
        pass
    print("  PASS: DimensionSpec frozen dataclass")


def test_aggregate_result_avg():
    """AggregateResult computes avg from sum/count."""
    r = AggregateResult("requests", "req", 100.0, 20, 1.0, 10.0, 5.0)
    assert r.value_avg == 5.0
    r0 = AggregateResult("requests", "req", 0.0, 0, None, None, None)
    assert r0.value_avg == 0.0
    print("  PASS: AggregateResult avg computation")


# ── MeterEngine: define + record + query ──────────────────────────────────


def test_define_meter():
    """define_meter creates meter + dimensions in DB."""

    async def run():
        db, engine = await setup_engine()
        try:
            meter_id = await engine.define_meter(
                "test_api",
                [
                    DimensionSpec("requests", "counter", "requests", "sum"),
                    DimensionSpec("bytes_in", "counter", "bytes", "sum"),
                    DimensionSpec("duration_ms", "gauge", "ms", "avg"),
                ],
                description="Test API meter",
            )

            assert meter_id > 0

            # Verify in DB
            meter = await db.query_one(
                "SELECT * FROM hyper_meters WHERE name = $1", "test_api"
            )
            assert meter["description"] == "Test API meter"

            dims = await db.query(
                "SELECT * FROM hyper_meter_dimensions WHERE meter_id = $1 ORDER BY sort_order",
                meter_id,
            )
            assert len(dims) == 3
            assert dims[0]["name"] == "requests"
            assert dims[1]["name"] == "bytes_in"
            assert dims[2]["name"] == "duration_ms"

            print("  PASS: define_meter creates meter + dimensions")
        finally:
            await teardown(db)

    run_async(run())


def test_define_meter_idempotent():
    """define_meter is idempotent — second call updates."""

    async def run():
        db, engine = await setup_engine()
        try:
            id1 = await engine.define_meter(
                "test_idem",
                [
                    DimensionSpec("x", "counter", "events", "sum"),
                ],
            )
            id2 = await engine.define_meter(
                "test_idem",
                [
                    DimensionSpec("x", "counter", "events", "sum"),
                    DimensionSpec("y", "gauge", "bytes", "last"),
                ],
                description="updated",
            )
            assert id1 == id2
            dims = await db.query(
                "SELECT * FROM hyper_meter_dimensions WHERE meter_id = $1", id1
            )
            assert len(dims) == 2
            print("  PASS: define_meter idempotent")
        finally:
            await teardown(db)

    run_async(run())


def test_record_single_event():
    """record() inserts event + values + aggregates."""

    async def run():
        db, engine = await setup_engine()
        try:
            await engine.define_meter(
                "rec_test",
                [
                    DimensionSpec("requests", "counter", "requests", "sum"),
                    DimensionSpec("bytes", "counter", "bytes", "sum"),
                ],
            )

            event_id = await engine.record(
                "rec_test",
                "acme",
                {
                    "requests": 1,
                    "bytes": 1024,
                },
            )
            assert event_id > 0

            # Verify event
            event = await db.query_one(
                "SELECT * FROM hyper_meter_events WHERE id = $1", event_id
            )
            assert event["account_id"] == "acme"

            # Verify values
            values = await db.query(
                "SELECT * FROM hyper_meter_event_values WHERE event_id = $1", event_id
            )
            assert len(values) == 2

            # Verify aggregates (3 bucket sizes × 2 dimensions = 6 rows)
            aggs = await db.query(
                "SELECT * FROM hyper_meter_aggregates WHERE account_id = $1", "acme"
            )
            assert len(aggs) == 6

            print("  PASS: record() creates event + values + aggregates")
        finally:
            await teardown(db)

    run_async(run())


def test_record_idempotency():
    """Duplicate idempotency_key is a no-op."""

    async def run():
        db, engine = await setup_engine()
        try:
            await engine.define_meter(
                "idem_test",
                [
                    DimensionSpec("x", "counter", "events", "sum"),
                ],
            )

            id1 = await engine.record(
                "idem_test", "acme", {"x": 1}, idempotency_key="key-1"
            )
            id2 = await engine.record(
                "idem_test", "acme", {"x": 1}, idempotency_key="key-1"
            )
            assert id1 > 0
            assert id2 is None  # Duplicate idempotency key — no-op

            events = await db.query(
                "SELECT * FROM hyper_meter_events WHERE account_id = $1", "acme"
            )
            assert len(events) == 1

            print("  PASS: Idempotency key prevents duplicates")
        finally:
            await teardown(db)

    run_async(run())


def test_record_incremental_aggregation():
    """Multiple records incrementally update aggregates."""

    async def run():
        db, engine = await setup_engine()
        try:
            await engine.define_meter(
                "incr_test",
                [
                    DimensionSpec("requests", "counter", "requests", "sum"),
                ],
            )

            await engine.record("incr_test", "acme", {"requests": 5})
            await engine.record("incr_test", "acme", {"requests": 3})
            await engine.record("incr_test", "acme", {"requests": 7})

            # Monthly aggregate should have sum=15, count=3, min=3, max=7
            agg = await db.query_one(
                "SELECT * FROM hyper_meter_aggregates WHERE account_id = $1 AND bucket_size = 'monthly'",
                "acme",
            )
            assert agg["value_sum"] == 15.0
            assert agg["value_count"] == 3
            assert agg["value_min"] == 3.0
            assert agg["value_max"] == 7.0
            assert agg["value_last"] == 7.0

            print("  PASS: Incremental aggregation correct")
        finally:
            await teardown(db)

    run_async(run())


def test_concurrent_record_no_deadlock():
    """Two threads recording the same dims in REVERSED order must not deadlock.

    The aggregate upsert locks hyper_meter_aggregates conflict rows in unnest
    order. Without a deterministic (dimension_id, bucket_size) sort, reversed
    dimension orderings acquire those row locks in opposite orders and can
    deadlock (40P01). Regression for F5 — each thread uses its own pool
    connection so the two transactions run genuinely concurrently.
    """
    import threading

    async def prepare():
        db, engine = await setup_engine()
        await engine.define_meter(
            "concurrent",
            [
                DimensionSpec("a", "counter", "u", "sum"),
                DimensionSpec("b", "counter", "u", "sum"),
                DimensionSpec("c", "counter", "u", "sum"),
                DimensionSpec("d", "counter", "u", "sum"),
            ],
        )
        await db.disconnect()

    run_async(prepare())

    ITERS = 40
    errors: list[str] = []

    def worker(order):
        async def run():
            db = Database(DB_URL, max_size=3)
            await db.connect()
            engine = MeterEngine(db)
            for _ in range(ITERS):
                # Same account + dims → maximal aggregate-row contention;
                # reversed key order is what used to flip the lock order.
                await engine.record(
                    "concurrent", "acct:shared", {k: 1.0 for k in order}
                )
            await db.disconnect()

        try:
            run_async(run())
        except Exception as e:  # 40P01 deadlock surfaces here
            errors.append(repr(e))

    t1 = threading.Thread(target=worker, args=(["a", "b", "c", "d"],))
    t2 = threading.Thread(target=worker, args=(["d", "c", "b", "a"],))
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    assert not errors, f"deadlock/errors during concurrent record(): {errors}"

    # Both threads' events landed: 2 * ITERS aggregated into each bucket.
    async def verify():
        db = Database(DB_URL, max_size=2)
        await db.connect()
        try:
            agg = await db.query_one(
                "SELECT value_count FROM hyper_meter_aggregates "
                "WHERE account_id = $1 AND bucket_size = 'monthly' "
                "ORDER BY value_count DESC LIMIT 1",
                "acct:shared",
            )
            assert agg is not None and agg["value_count"] == 2 * ITERS, (
                f"expected {2 * ITERS} counted, got "
                f"{agg['value_count'] if agg else None}"
            )
        finally:
            await db.disconnect()

    run_async(verify())
    print("  PASS: concurrent reversed-order record() — no deadlock, all counted")


def test_record_deterministic_lock_order():
    """record() emits aggregate rows sorted by (dimension_id, bucket_size)
    regardless of the caller's dimension order.

    This is the deterministic guarantee behind F5's deadlock fix: two concurrent
    record() calls that pass dimensions in different orders still lock
    hyper_meter_aggregates conflict rows in identical order, so they can never
    form a lock cycle. Reliable (non-probabilistic) guard for that ordering.
    """

    async def run():
        db, engine = await setup_engine()
        try:
            await engine.define_meter(
                "order_test",
                [
                    DimensionSpec("a", "counter", "u", "sum"),
                    DimensionSpec("b", "counter", "u", "sum"),
                    DimensionSpec("c", "counter", "u", "sum"),
                ],
            )

            captured: list[tuple[list, list]] = []
            orig_execute = engine.db.execute

            async def spy(sql, *args):
                if "hyper_meter_aggregates" in sql:
                    # args[3] = agg_dim_ids, args[4] = agg_bucket_sizes
                    captured.append((list(args[3]), list(args[4])))
                return await orig_execute(sql, *args)

            engine.db.execute = spy
            try:
                await engine.record("order_test", "acct", {"a": 1, "b": 1, "c": 1})
                await engine.record("order_test", "acct", {"c": 1, "b": 1, "a": 1})
            finally:
                engine.db.execute = orig_execute

            assert len(captured) == 2, (
                f"expected 2 aggregate upserts, got {len(captured)}"
            )
            (dims1, buckets1), (dims2, buckets2) = captured

            # Dimension ids are non-decreasing (sorted).
            assert dims1 == sorted(dims1), f"dim ids not sorted: {dims1}"
            # (dimension_id, bucket_size) pairs are fully sorted.
            pairs1 = list(zip(dims1, buckets1))
            assert pairs1 == sorted(pairs1), f"(dim,bucket) not sorted: {pairs1}"
            # Identical lock order despite reversed input dimension order.
            assert (dims1, buckets1) == (dims2, buckets2), (
                "lock order differs with input order — deadlock window open"
            )

            print("  PASS: deterministic (dimension_id, bucket_size) lock order")
        finally:
            await teardown(db)

    run_async(run())


def test_query_single_dimension():
    """query() returns correct AggregateResult."""

    async def run():
        db, engine = await setup_engine()
        try:
            await engine.define_meter(
                "query_test",
                [
                    DimensionSpec("requests", "counter", "requests", "sum"),
                    DimensionSpec("bytes", "counter", "bytes", "sum"),
                ],
            )

            await engine.record("query_test", "acme", {"requests": 10, "bytes": 2048})
            await engine.record("query_test", "acme", {"requests": 20, "bytes": 4096})

            result = await engine.query(
                "query_test",
                "acme",
                "requests",
                "monthly",
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2027, 1, 1, tzinfo=UTC),
            )

            assert result.dimension_name == "requests"
            assert result.unit == "requests"
            assert result.value_sum == 30.0
            assert result.value_count == 2
            assert result.value_avg == 15.0

            print("  PASS: query() returns correct result")
        finally:
            await teardown(db)

    run_async(run())


def test_query_multi():
    """query_multi() returns dict of results."""

    async def run():
        db, engine = await setup_engine()
        try:
            await engine.define_meter(
                "multi_test",
                [
                    DimensionSpec("a", "counter", "units", "sum"),
                    DimensionSpec("b", "counter", "units", "sum"),
                ],
            )
            await engine.record("multi_test", "acme", {"a": 100, "b": 200})

            results = await engine.query_multi(
                "multi_test",
                "acme",
                ["a", "b"],
                "monthly",
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2027, 1, 1, tzinfo=UTC),
            )

            assert "a" in results
            assert "b" in results
            assert results["a"].value_sum == 100.0
            assert results["b"].value_sum == 200.0

            print("  PASS: query_multi() returns all dimensions")
        finally:
            await teardown(db)

    run_async(run())


# ── Quotas ────────────────────────────────────────────────────────────────


def test_quota_set_and_check():
    """set_quota + check_quota works correctly."""

    async def run():
        db, engine = await setup_engine()
        try:
            await engine.define_meter(
                "quota_test",
                [
                    DimensionSpec("requests", "counter", "requests", "sum"),
                ],
            )

            await engine.set_quota(
                "acme",
                "quota_test",
                "requests",
                period="monthly",
                limit_value=100,
                action="reject",
            )

            # Record 50 requests
            await engine.record("quota_test", "acme", {"requests": 50})

            decision = await engine.check_quota(
                "acme", "quota_test", "requests", "monthly"
            )
            assert decision.allowed is True
            assert decision.remaining == 50.0
            assert decision.limit_value == 100.0

            # Record 60 more (total 110, over limit)
            await engine.record("quota_test", "acme", {"requests": 60})

            decision2 = await engine.check_quota(
                "acme", "quota_test", "requests", "monthly"
            )
            assert decision2.allowed is False
            assert decision2.remaining == 0.0
            assert decision2.action == "reject"

            print("  PASS: Quota enforcement works")
        finally:
            await teardown(db)

    run_async(run())


def test_quota_no_quota_set():
    """check_quota with no quota returns allowed=True."""

    async def run():
        db, engine = await setup_engine()
        try:
            await engine.define_meter(
                "noquota",
                [
                    DimensionSpec("x", "counter", "events", "sum"),
                ],
            )
            decision = await engine.check_quota("acme", "noquota", "x", "monthly")
            assert decision.allowed is True
            assert decision.remaining == float("inf")

            print("  PASS: No quota = always allowed")
        finally:
            await teardown(db)

    run_async(run())


# ── Hooks ─────────────────────────────────────────────────────────────────


def test_hook_on_event():
    """Hooks receive on_event for every record()."""

    events_received: list[MeterHookContext] = []

    class TrackingHook(MeterHook):
        async def on_event(self, ctx: MeterHookContext) -> None:
            events_received.append(ctx)

    async def run():
        db, engine = await setup_engine()
        try:
            engine.register_hook(TrackingHook())
            await engine.define_meter(
                "hook_test",
                [
                    DimensionSpec("x", "counter", "events", "sum"),
                ],
            )
            await engine.record("hook_test", "acme", {"x": 42})

            assert len(events_received) == 1
            assert events_received[0].meter_name == "hook_test"
            assert events_received[0].account_id == "acme"
            assert events_received[0].dimensions == {"x": 42}

            print("  PASS: Hook receives on_event")
        finally:
            await teardown(db)

    run_async(run())


def test_alert_hook():
    """AlertHook fires when value exceeds threshold."""

    alerts: list[tuple[str, float]] = []

    def on_alert(ctx, dim_name, value, threshold):
        alerts.append((dim_name, value))

    async def run():
        db, engine = await setup_engine()
        try:
            engine.register_hook(AlertHook(thresholds={"x": 100}, callback=on_alert))
            await engine.define_meter(
                "alert_test",
                [
                    DimensionSpec("x", "counter", "events", "sum"),
                ],
            )

            await engine.record("alert_test", "acme", {"x": 50})  # Under threshold
            assert len(alerts) == 0

            await engine.record("alert_test", "acme", {"x": 150})  # Over threshold
            assert len(alerts) == 1
            assert alerts[0] == ("x", 150)

            print("  PASS: AlertHook fires on threshold")
        finally:
            await teardown(db)

    run_async(run())


# ── Export ────────────────────────────────────────────────────────────────


def test_export_period():
    """export_period returns all dimensions aggregated."""

    async def run():
        db, engine = await setup_engine()
        try:
            await engine.define_meter(
                "export_test",
                [
                    DimensionSpec("a", "counter", "units", "sum"),
                    DimensionSpec("b", "counter", "bytes", "sum"),
                ],
            )
            await engine.record("export_test", "acme", {"a": 10, "b": 2048})
            await engine.record("export_test", "acme", {"a": 5, "b": 1024})

            export = await engine.export_period(
                "export_test",
                "acme",
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2027, 1, 1, tzinfo=UTC),
            )

            assert isinstance(export, PeriodExport)
            assert export.meter_name == "export_test"
            assert "a" in export.dimensions
            assert "b" in export.dimensions
            assert export.dimensions["a"].value_sum == 15.0
            assert export.dimensions["b"].value_sum == 3072.0

            print("  PASS: export_period returns all dimensions")
        finally:
            await teardown(db)

    run_async(run())


# ── Hierarchy ─────────────────────────────────────────────────────────────


def test_query_hierarchy():
    """query_hierarchy aggregates across sub-accounts."""

    async def run():
        db, engine = await setup_engine()
        try:
            await engine.define_meter(
                "hier_test",
                [
                    DimensionSpec("requests", "counter", "requests", "sum"),
                ],
            )

            # Create account hierarchy: org → team1, team2
            await db.execute(
                "INSERT INTO hyper_meter_accounts (account_id, display_name, account_type) "
                "VALUES ($1, $2, $3)",
                "org1",
                "Org 1",
                "org",
            )
            await db.execute(
                "INSERT INTO hyper_meter_accounts (account_id, display_name, account_type, parent_account_id) "
                "VALUES ($1, $2, $3, $4)",
                "team1",
                "Team 1",
                "team",
                "org1",
            )
            await db.execute(
                "INSERT INTO hyper_meter_accounts (account_id, display_name, account_type, parent_account_id) "
                "VALUES ($1, $2, $3, $4)",
                "team2",
                "Team 2",
                "team",
                "org1",
            )

            # Record usage for each
            await engine.record("hier_test", "org1", {"requests": 10})
            await engine.record("hier_test", "team1", {"requests": 30})
            await engine.record("hier_test", "team2", {"requests": 60})

            # Hierarchy query should sum all: 10 + 30 + 60 = 100
            result = await engine.query_hierarchy(
                "hier_test",
                "org1",
                "requests",
                "monthly",
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2027, 1, 1, tzinfo=UTC),
            )
            assert result.value_sum == 100.0

            print("  PASS: Hierarchy query aggregates sub-accounts")
        finally:
            await teardown(db)

    run_async(run())


# ── Global singleton ──────────────────────────────────────────────────────


def test_global_singleton():
    """get/set_meter_engine work."""
    assert get_meter_engine() is None or isinstance(get_meter_engine(), MeterEngine)

    async def run():
        db, engine = await setup_engine()
        try:
            set_meter_engine(engine)
            assert get_meter_engine() is engine
            set_meter_engine(None)
            print("  PASS: Global singleton")
        finally:
            await teardown(db)

    run_async(run())


# ── Multi-dimensional real scenario ───────────────────────────────────────


def test_llm_usage_scenario():
    """Real-world: LLM API with tokens, bytes, duration, cost."""

    async def run():
        db, engine = await setup_engine()
        try:
            await engine.define_meter(
                "llm",
                [
                    DimensionSpec("requests", "counter", "requests", "sum"),
                    DimensionSpec("tokens_in", "counter", "tokens", "sum"),
                    DimensionSpec("tokens_out", "counter", "tokens", "sum"),
                    DimensionSpec("cost_units", "counter", "units", "sum"),
                    DimensionSpec("duration_ms", "gauge", "ms", "avg"),
                ],
            )

            # Simulate 3 API calls
            await engine.record(
                "llm",
                "acme",
                {
                    "requests": 1,
                    "tokens_in": 500_000,
                    "tokens_out": 1_000_000,
                    "cost_units": 7.5,
                    "duration_ms": 3200,
                },
            )
            await engine.record(
                "llm",
                "acme",
                {
                    "requests": 1,
                    "tokens_in": 200_000,
                    "tokens_out": 800_000,
                    "cost_units": 5.0,
                    "duration_ms": 1800,
                },
            )
            await engine.record(
                "llm",
                "acme",
                {
                    "requests": 1,
                    "tokens_in": 1_000_000,
                    "tokens_out": 2_000_000,
                    "cost_units": 15.0,
                    "duration_ms": 6500,
                },
            )

            report = await engine.query_multi(
                "llm",
                "acme",
                ["requests", "tokens_in", "tokens_out", "cost_units", "duration_ms"],
                "monthly",
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2027, 1, 1, tzinfo=UTC),
            )

            assert report["requests"].value_sum == 3.0
            assert report["tokens_in"].value_sum == 1_700_000.0
            assert report["tokens_out"].value_sum == 3_800_000.0
            assert report["cost_units"].value_sum == 27.5
            assert report["duration_ms"].value_count == 3
            assert report["duration_ms"].value_min == 1800.0
            assert report["duration_ms"].value_max == 6500.0

            print("  PASS: LLM usage scenario (5 dimensions, 3 events)")
        finally:
            await teardown(db)

    run_async(run())


def main():
    tests = [
        # Dataclasses
        test_dimension_spec,
        test_aggregate_result_avg,
        # Define + record
        test_define_meter,
        test_define_meter_idempotent,
        test_record_single_event,
        test_record_idempotency,
        test_record_incremental_aggregation,
        test_concurrent_record_no_deadlock,
        test_record_deterministic_lock_order,
        # Query
        test_query_single_dimension,
        test_query_multi,
        # Quotas
        test_quota_set_and_check,
        test_quota_no_quota_set,
        # Hooks
        test_hook_on_event,
        test_alert_hook,
        # Export
        test_export_period,
        # Hierarchy
        test_query_hierarchy,
        # Singleton
        test_global_singleton,
        # Real scenario
        test_llm_usage_scenario,
    ]

    passed = 0
    failed = 0
    errors = []

    print(f"\n{'=' * 60}")
    print("Multi-Dimensional Metering Engine Tests")
    print(f"{'=' * 60}\n")

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            import traceback

            failed += 1
            errors.append((test.__name__, str(e)))
            traceback.print_exc()
            print(f"  FAIL: {test.__name__}: {e}")

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    if errors:
        print("\nFailures:")
        for name, err in errors:
            print(f"  - {name}: {err}")
    print(f"{'=' * 60}\n")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
