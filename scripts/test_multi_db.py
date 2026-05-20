#!/usr/bin/env python3
"""
Tests for multi-database routing.

Unit tests (connection manager, router logic, QuerySet.using()) +
integration tests (live PostgreSQL with two named connections).

Usage:
    uv run hyper-test multi_db
"""

# hyper-test: db_isolated

import asyncio
import inspect
import os
import sys
import traceback

from hyperdjango.database import Database, get_db, pool_registry_stats, set_db
from hyperdjango.models import Field, Model
from hyperdjango.multi_db import (
    ConnectionManager,
    DatabaseRouter,
    PrimaryReplicaRouter,
    set_connections,
)

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

RESULTS = {"passed": 0, "failed": 0, "errors": []}
DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/hyperdjango_test")


def test(name):
    def decorator(func):
        async def wrapper():
            try:
                if inspect.iscoroutinefunction(func):
                    await func()
                else:
                    func()
                RESULTS["passed"] += 1
                print(f"  ✓ {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  ✗ {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Test models
# ---------------------------------------------------------------------------


class MultiUser(Model):
    class Meta:
        table = "test_multi_users"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=200)


class BoundModel(Model):
    class Meta:
        table = "test_bound_items"
        database = "secondary"

    id: int = Field(primary_key=True, auto=True)
    label: str = Field(max_length=100)


# ═══════════════════════════════════════════════════════════════════════════
# UNIT TESTS
# ═══════════════════════════════════════════════════════════════════════════


@test("ConnectionManager: configure and lookup")
async def test_cm_configure():
    cm = ConnectionManager()
    await cm.configure(
        {
            "default": DB_URL,
            "secondary": DB_URL,  # Same DB for testing, different logical name
        }
    )
    assert "default" in cm
    assert "secondary" in cm
    assert "nonexistent" not in cm
    assert isinstance(cm["default"], Database)
    assert isinstance(cm["secondary"], Database)
    await cm.close_all()


@test("ConnectionManager: partial configure failure closes opened pools")
async def test_cm_configure_partial_failure():
    # A bad database that fails at connect() time (missing DB → auth error).
    bad_url = "postgres://localhost:5432/hyperdjango_missing_db_xyz"

    baseline = pool_registry_stats()["total_refs"]

    cm = ConnectionManager()
    raised = False
    try:
        # "default" opens successfully; "zbroken" then fails. The already-open
        # "default" pool must be closed, not leaked.
        await cm.configure({"default": DB_URL, "zbroken": bad_url})
    except Exception:
        raised = True

    assert raised, "configure with a bad database must raise"
    # All-or-nothing: nothing registered on partial failure.
    assert len(cm._databases) == 0, f"leaked registrations: {list(cm._databases)}"
    # No pool ref leaked — the opened "default" pool was released.
    after = pool_registry_stats()["total_refs"]
    assert after == baseline, f"pool refs leaked: baseline={baseline} after={after}"


@test("ConnectionManager: KeyError for unknown database")
async def test_cm_key_error():
    cm = ConnectionManager()
    await cm.configure({"default": DB_URL})
    try:
        _ = cm["nonexistent"]
        assert False, "Should raise KeyError"
    except KeyError as e:
        assert "nonexistent" in str(e)
    await cm.close_all()


@test("ConnectionManager: get() with default")
async def test_cm_get():
    cm = ConnectionManager()
    await cm.configure({"default": DB_URL})
    assert cm.get("default") is not None
    assert cm.get("missing") is None
    assert cm.get("missing", "fallback") == "fallback"
    await cm.close_all()


@test("ConnectionManager: databases property")
async def test_cm_databases():
    cm = ConnectionManager()
    await cm.configure({"default": DB_URL, "replica": DB_URL})
    dbs = cm.databases
    assert set(dbs.keys()) == {"default", "replica"}
    await cm.close_all()


@test("DatabaseRouter: default returns None")
def test_router_default():
    router = DatabaseRouter()
    assert router.db_for_read(MultiUser) is None
    assert router.db_for_write(MultiUser) is None
    assert router.allow_relation(None, None) is None
    assert router.allow_migrate("default", MultiUser) is None


@test("PrimaryReplicaRouter: routes reads to replica, writes to primary")
def test_primary_replica_router():
    router = PrimaryReplicaRouter(replica="ro_replica", primary="rw_primary")
    assert router.db_for_read(MultiUser) == "ro_replica"
    assert router.db_for_write(MultiUser) == "rw_primary"


@test("ConnectionManager: resolve_for_read with router")
async def test_cm_resolve_read_router():
    cm = ConnectionManager()
    await cm.configure({"default": DB_URL, "replica": DB_URL})
    cm.router = PrimaryReplicaRouter()
    db = cm.resolve_for_read(MultiUser)
    assert db is cm["replica"]
    await cm.close_all()


@test("ConnectionManager: resolve_for_write with router")
async def test_cm_resolve_write_router():
    cm = ConnectionManager()
    await cm.configure({"default": DB_URL, "replica": DB_URL})
    cm.router = PrimaryReplicaRouter()
    db = cm.resolve_for_write(MultiUser)
    assert db is cm["default"]
    await cm.close_all()


@test("ConnectionManager: resolve_for_read with per-model binding")
async def test_cm_resolve_model_binding():
    cm = ConnectionManager()
    await cm.configure({"default": DB_URL, "secondary": DB_URL})
    # BoundModel has Meta.database = "secondary"
    db = cm.resolve_for_read(BoundModel)
    assert db is cm["secondary"]
    await cm.close_all()


@test("ConnectionManager: per-model binding overrides router")
async def test_cm_model_overrides_router():
    cm = ConnectionManager()
    await cm.configure({"default": DB_URL, "secondary": DB_URL, "replica": DB_URL})
    cm.router = PrimaryReplicaRouter()
    # BoundModel.Meta.database = "secondary" should override router's "replica"
    db = cm.resolve_for_read(BoundModel)
    assert db is cm["secondary"]
    await cm.close_all()


@test("QuerySet.using() clones with database alias")
def test_qs_using():
    qs = MultiUser.objects.filter(name="test")
    qs2 = qs.using("replica")
    assert qs._using is None
    assert qs2._using == "replica"


@test("QuerySet.using() with Database instance")
async def test_qs_using_instance():
    db = Database(DB_URL)
    await db.connect()
    qs = MultiUser.objects.using(db)
    assert qs._using is db
    await db.disconnect()


@test("QuerySet._using preserved through chaining")
def test_qs_using_chain():
    qs = MultiUser.objects.using("replica").filter(name="test").order_by("id").limit(10)
    assert qs._using == "replica"


# ═══════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════════════


@test("DB: setup multi-db test tables")
async def test_db_setup():
    db = get_db()
    await db.execute("DROP TABLE IF EXISTS test_multi_users CASCADE")
    await db.execute("DROP TABLE IF EXISTS test_bound_items CASCADE")
    await db.execute("""
        CREATE TABLE test_multi_users (
            id SERIAL PRIMARY KEY,
            name VARCHAR(200) NOT NULL
        )
    """)
    await db.execute("""
        CREATE TABLE test_bound_items (
            id SERIAL PRIMARY KEY,
            label VARCHAR(100) NOT NULL
        )
    """)
    await db.execute("INSERT INTO test_multi_users (name) VALUES ($1)", "Alice")
    await db.execute("INSERT INTO test_multi_users (name) VALUES ($1)", "Bob")
    await db.execute("INSERT INTO test_bound_items (label) VALUES ($1)", "item1")


@test("DB: using() with Database instance queries correctly")
async def test_db_using_instance():
    db = get_db()
    users = await MultiUser.objects.using(db).filter(name="Alice").all()
    assert len(users) == 1
    assert users[0].name == "Alice"


@test("DB: using() with named connection")
async def test_db_using_named():
    cm = ConnectionManager()
    await cm.configure({"default": DB_URL, "secondary": DB_URL})
    set_connections(cm)

    users = await MultiUser.objects.using("secondary").all()
    assert len(users) == 2

    await cm.close_all()
    set_connections(ConnectionManager())  # Reset


@test("DB: per-model database binding")
async def test_db_model_binding():
    cm = ConnectionManager()
    await cm.configure({"default": DB_URL, "secondary": DB_URL})
    set_connections(cm)

    # BoundModel has Meta.database = "secondary"
    items = await BoundModel.objects.all()
    assert len(items) == 1
    assert items[0].label == "item1"

    await cm.close_all()
    set_connections(ConnectionManager())


@test("DB: router-based read/write splitting")
async def test_db_router():
    cm = ConnectionManager()
    await cm.configure({"default": DB_URL, "replica": DB_URL})
    cm.router = PrimaryReplicaRouter()
    set_connections(cm)

    # Read goes to "replica" (same DB in tests, but different logical path)
    users = await MultiUser.objects.all()
    assert len(users) == 2

    await cm.close_all()
    set_connections(ConnectionManager())


@test("DB: cleanup")
async def test_db_cleanup():
    db = get_db()
    await db.execute("DROP TABLE IF EXISTS test_multi_users CASCADE")
    await db.execute("DROP TABLE IF EXISTS test_bound_items CASCADE")


# ═══════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════


async def main():
    tests = [
        obj
        for name, obj in list(globals().items())
        if callable(obj) and getattr(obj, "_is_test", False)
    ]

    unit_tests = [t for t in tests if "DB:" not in t.__name__]
    db_tests = [t for t in tests if "DB:" in t.__name__]

    # Main database connection (kept alive throughout)
    main_db = Database(DB_URL)
    set_db(main_db)
    await main_db.connect()

    print("\n═══ Unit Tests: Multi-Database Routing ═══")
    for t in unit_tests:
        await t()
        # Restore main db after unit tests that may have clobbered it
        set_db(main_db)

    print("\n═══ Integration Tests: Live PostgreSQL ═══")
    set_db(main_db)
    for t in db_tests:
        await t()
        set_db(main_db)

    total = RESULTS["passed"] + RESULTS["failed"]
    print(f"\n{'═' * 60}")
    print(f"Results: {RESULTS['passed']}/{total} passed, {RESULTS['failed']} failed")
    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n--- {name} ---")
            print(tb)

    return RESULTS["failed"] == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
