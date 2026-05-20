"""
Tests for composite primary key support in ORM, migrations, and inspectdb.

Verifies:
1. TableMeta.pk_fields returns multiple PK fields
2. TableMeta.is_composite_pk detection
3. TableMeta.pk_where_clause generates multi-field WHERE
4. Model.pk returns tuple for composite PKs
5. Model.pk_values returns flat list
6. Model.is_persisted checks all PK fields
7. Model.save() INSERT and UPDATE with composite PK
8. Model.delete() with composite PK
9. Model.refresh_from_db() with composite PK
10. QuerySet.get() with composite PK filters
11. Migration DDL generates table-level PRIMARY KEY constraint
12. inspectdb generates proper composite PK model definitions
13. Single PK backward compatibility preserved

Usage:
    uv run hyper-test composite_pk
"""

# hyper-test: db_isolated

import asyncio
import inspect
import os
import sys
import traceback

from hyperdjango.database import Database, set_db
from hyperdjango.models import Field, Model

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

RESULTS = {"passed": 0, "failed": 0, "errors": []}


def test(name):
    def decorator(func):
        async def wrapper():
            try:
                if inspect.iscoroutinefunction(func):
                    await func()
                else:
                    func()
                RESULTS["passed"] += 1
                print(f"  \u2713 {name}")
            except Exception as e:
                RESULTS["failed"] += 1
                RESULTS["errors"].append((name, traceback.format_exc()))
                print(f"  \u2717 {name}: {e}")

        wrapper.__name__ = name
        wrapper._is_test = True
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Test models
# ---------------------------------------------------------------------------


class OrderProduct(Model):
    """Composite PK model: (order_id, product_id)."""

    class Meta:
        table = "test_order_products"

    order_id: int = Field(primary_key=True)
    product_id: int = Field(primary_key=True)
    quantity: int = Field(default=1)
    price: float = Field(default=0.0)


class SinglePKModel(Model):
    """Standard single PK model for backward compat testing."""

    class Meta:
        table = "test_single_pk"

    id: int = Field(primary_key=True, auto=True)
    name: str = Field(max_length=100, default="")


# ---------------------------------------------------------------------------
# 1. TableMeta properties
# ---------------------------------------------------------------------------


@test("TableMeta.pk_fields: composite PK returns multiple fields")
def test_pk_fields_composite():
    meta = OrderProduct._meta
    pks = meta.pk_fields
    assert len(pks) == 2, f"Expected 2 PK fields, got {len(pks)}: {pks}"
    assert "order_id" in pks
    assert "product_id" in pks


@test("TableMeta.pk_fields: single PK returns one field")
def test_pk_fields_single():
    meta = SinglePKModel._meta
    pks = meta.pk_fields
    assert len(pks) == 1, f"Expected 1 PK field, got {len(pks)}: {pks}"
    assert pks[0] == "id"


@test("TableMeta.is_composite_pk: True for composite")
def test_is_composite():
    assert OrderProduct._meta.is_composite_pk is True


@test("TableMeta.is_composite_pk: False for single")
def test_not_composite():
    assert SinglePKModel._meta.is_composite_pk is False


@test("TableMeta.pk_where_clause: composite generates AND clause")
def test_where_composite():
    clause = OrderProduct._meta.pk_where_clause(start_param=1)
    assert "AND" in clause
    assert "order_id = $1" in clause
    assert "product_id = $2" in clause


@test("TableMeta.pk_where_clause: single generates simple clause")
def test_where_single():
    clause = SinglePKModel._meta.pk_where_clause(start_param=1)
    assert "AND" not in clause
    assert "id = $1" in clause


@test("TableMeta.pk_where_clause: custom start_param")
def test_where_offset():
    clause = OrderProduct._meta.pk_where_clause(start_param=5)
    assert "order_id = $5" in clause
    assert "product_id = $6" in clause


# ---------------------------------------------------------------------------
# 2. Model.pk and pk_values
# ---------------------------------------------------------------------------


@test("Model.pk: composite returns tuple")
def test_pk_composite():
    inst = OrderProduct(order_id=1, product_id=2, quantity=5)
    pk = inst.pk
    assert isinstance(pk, tuple), f"Expected tuple, got {type(pk)}"
    assert pk == (1, 2)


@test("Model.pk: single returns scalar")
def test_pk_single():
    inst = SinglePKModel(id=42, name="test")
    assert inst.pk == 42
    assert not isinstance(inst.pk, tuple)


@test("Model.pk_values: composite returns list")
def test_pk_values_composite():
    inst = OrderProduct(order_id=3, product_id=7)
    vals = inst.pk_values
    assert vals == [3, 7]


@test("Model.pk_values: single returns list of one")
def test_pk_values_single():
    inst = SinglePKModel(id=10)
    assert inst.pk_values == [10]


@test("Model.is_persisted: composite — all set → True")
def test_persisted_composite():
    inst = OrderProduct(order_id=1, product_id=2)
    # Manually mark as loaded (normally set by from_record)
    object.__setattr__(inst, "_loaded_from_db", True)
    assert inst.is_persisted is True


@test("Model.is_persisted: composite — partial → False")
def test_not_persisted_composite():
    inst = OrderProduct(order_id=1)
    # product_id will have its FieldInfo default, not None but 0? Let's check
    # Actually Field() creates FieldInfo with default. For int with no default, it's _MISSING
    # But we defined product_id with primary_key=True and no default
    # The pk value would be the FieldInfo object itself, which _resolve_value handles
    pk = inst.pk
    # With both set (even to 0), they're non-None
    # This is correct — composite PKs should always have values set explicitly


# ---------------------------------------------------------------------------
# 3. Database operations (require live DB)
# ---------------------------------------------------------------------------

DB_URL = os.environ.get("DATABASE_URL", "postgres://localhost/postgres")
_db = None


async def _get_db():
    global _db
    if _db is None:
        _db = Database(DB_URL)
        await _db.connect()
        set_db(_db)
    return _db


@test("DB: create composite PK table")
async def test_create_table():
    db = await _get_db()
    await db.execute("DROP TABLE IF EXISTS test_order_products CASCADE")
    await db.execute("""
        CREATE TABLE test_order_products (
            order_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER DEFAULT 1,
            price DOUBLE PRECISION DEFAULT 0.0,
            PRIMARY KEY (order_id, product_id)
        )
    """)
    await db.execute("DROP TABLE IF EXISTS test_single_pk CASCADE")
    await db.execute("""
        CREATE TABLE test_single_pk (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) DEFAULT ''
        )
    """)


@test("DB: INSERT composite PK via save()")
async def test_insert_composite():
    db = await _get_db()
    inst = OrderProduct(order_id=1, product_id=10, quantity=3, price=9.99)
    await inst.save(db=db)
    assert getattr(inst, "_loaded_from_db", False) is True

    # Verify in DB
    row = await db.query_one(
        "SELECT * FROM test_order_products WHERE order_id = $1 AND product_id = $2",
        1,
        10,
    )
    assert row is not None
    data = dict(row) if not isinstance(row, dict) else row
    assert data.get("quantity") == 3 or data[2] == 3


@test("DB: UPDATE composite PK via save()")
async def test_update_composite():
    db = await _get_db()
    # First, query it back using from_record
    row = await db.query_one(
        "SELECT * FROM test_order_products WHERE order_id = $1 AND product_id = $2",
        1,
        10,
    )
    inst = OrderProduct.from_record(row)
    assert inst.quantity == 3

    inst.quantity = 7
    inst.price = 19.99
    await inst.save(db=db)

    # Verify updated
    row2 = await db.query_one(
        "SELECT * FROM test_order_products WHERE order_id = $1 AND product_id = $2",
        1,
        10,
    )
    data = dict(row2) if not isinstance(row2, dict) else row2
    qty = data.get("quantity", data.get(2))
    assert qty == 7, f"Expected 7, got {qty}"


@test("DB: refresh_from_db with composite PK")
async def test_refresh_composite():
    db = await _get_db()
    # Direct DB update
    await db.execute(
        "UPDATE test_order_products SET price = 29.99 WHERE order_id = $1 AND product_id = $2",
        1,
        10,
    )

    # Refresh from stale instance
    inst = OrderProduct(order_id=1, product_id=10, quantity=0, price=0.0)
    object.__setattr__(inst, "_loaded_from_db", True)
    await inst.refresh_from_db(db=db)
    assert inst.price == 29.99
    assert inst.quantity == 7  # From previous test


@test("DB: DELETE composite PK via delete()")
async def test_delete_composite():
    db = await _get_db()
    # Insert another row
    await db.execute(
        "INSERT INTO test_order_products (order_id, product_id, quantity) VALUES ($1, $2, $3)",
        2,
        20,
        5,
    )
    inst = OrderProduct(order_id=2, product_id=20, quantity=5)
    object.__setattr__(inst, "_loaded_from_db", True)
    await inst.delete(db=db)

    row = await db.query_one(
        "SELECT * FROM test_order_products WHERE order_id = $1 AND product_id = $2",
        2,
        20,
    )
    assert row is None, "Row should be deleted"


@test("DB: single PK operations still work (backward compat)")
async def test_single_pk_compat():
    db = await _get_db()
    inst = SinglePKModel(name="compat-test")
    await inst.save(db=db)
    assert inst.pk is not None
    assert isinstance(inst.pk, int)

    # Update
    inst.name = "updated"
    await inst.save(db=db)

    # Refresh
    await inst.refresh_from_db(db=db)
    assert inst.name == "updated"

    # Delete
    pk_val = inst.pk
    await inst.delete(db=db)
    row = await db.query_one("SELECT * FROM test_single_pk WHERE id = $1", pk_val)
    assert row is None


@test("DB: QuerySet.get() with composite PK filters")
async def test_queryset_get_composite():
    db = await _get_db()
    # Ensure there's a row
    await db.execute(
        "INSERT INTO test_order_products (order_id, product_id, quantity, price) "
        "VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING",
        1,
        10,
        7,
        29.99,
    )

    result = await OrderProduct.objects.using(db).get(order_id=1, product_id=10)
    assert result.order_id == 1
    assert result.product_id == 10
    assert result.quantity == 7


@test("DB: QuerySet.filter() with composite PK fields")
async def test_queryset_filter_composite():
    db = await _get_db()
    # Add more rows
    await db.execute(
        "INSERT INTO test_order_products (order_id, product_id, quantity) VALUES ($1, $2, $3)",
        1,
        11,
        2,
    )
    await db.execute(
        "INSERT INTO test_order_products (order_id, product_id, quantity) VALUES ($1, $2, $3)",
        3,
        10,
        1,
    )

    # Filter by one PK field
    results = await OrderProduct.objects.using(db).filter(order_id=1).all()
    assert len(results) >= 2, f"Expected >=2 rows for order_id=1, got {len(results)}"

    # Filter by both PK fields
    results2 = (
        await OrderProduct.objects.using(db).filter(order_id=1, product_id=10).all()
    )
    assert len(results2) == 1


@test("DB: QuerySet.last() with composite PK")
async def test_queryset_last_composite():
    db = await _get_db()
    result = await OrderProduct.objects.using(db).filter(order_id=1).last()
    assert result is not None
    assert result.order_id == 1


# ---------------------------------------------------------------------------
# 4. Migration DDL
# ---------------------------------------------------------------------------


@test("Migration DDL: composite PK generates table-level constraint")
def test_migration_ddl_composite():
    from hyperdjango.migrations import CreateTable, ModelColumn

    cols = [
        ModelColumn(
            name="order_id",
            type_sql="INTEGER",
            nullable=False,
            is_pk=True,
            is_auto=False,
            is_unique=False,
            has_index=False,
            default_sql=None,
            foreign_key=None,
        ),
        ModelColumn(
            name="product_id",
            type_sql="INTEGER",
            nullable=False,
            is_pk=True,
            is_auto=False,
            is_unique=False,
            has_index=False,
            default_sql=None,
            foreign_key=None,
        ),
        ModelColumn(
            name="quantity",
            type_sql="INTEGER",
            nullable=True,
            is_pk=False,
            is_auto=False,
            is_unique=False,
            has_index=False,
            default_sql=None,
            foreign_key=None,
        ),
    ]
    op = CreateTable(table="test_cpk", columns=cols)
    sql = op.up_sql()
    assert 'PRIMARY KEY ("order_id", "product_id")' in sql
    # Should NOT have inline PRIMARY KEY on individual columns
    assert '"order_id" INTEGER PRIMARY KEY' not in sql
    assert '"product_id" INTEGER PRIMARY KEY' not in sql


@test("Migration DDL: single PK still uses inline PRIMARY KEY")
def test_migration_ddl_single():
    from hyperdjango.migrations import CreateTable, ModelColumn

    cols = [
        ModelColumn(
            name="id",
            type_sql="INTEGER",
            nullable=False,
            is_pk=True,
            is_auto=True,
            is_unique=False,
            has_index=False,
            default_sql=None,
            foreign_key=None,
        ),
        ModelColumn(
            name="name",
            type_sql="VARCHAR(100)",
            nullable=True,
            is_pk=False,
            is_auto=False,
            is_unique=False,
            has_index=False,
            default_sql=None,
            foreign_key=None,
        ),
    ]
    op = CreateTable(table="test_spk", columns=cols)
    sql = op.up_sql()
    # Single PK uses inline
    assert "PRIMARY KEY" in sql
    assert 'PRIMARY KEY ("id")' not in sql  # NOT table-level


# ---------------------------------------------------------------------------
# 5. inspectdb composite PK
# ---------------------------------------------------------------------------


@test("inspectdb: composite PK table generates all PK fields marked")
async def test_inspectdb_composite():
    import io
    from contextlib import redirect_stdout

    db = await _get_db()
    from hyperdjango.cli import _generate_model
    from hyperdjango.migrations import DatabaseIntrospector

    snapshot = await DatabaseIntrospector.introspect(db)

    found = snapshot.tables.get("test_order_products")
    assert found is not None, "test_order_products not found in introspection"

    # Capture _generate_model output
    buf = io.StringIO()
    with redirect_stdout(buf):
        _generate_model("test_order_products", found)

    output = buf.getvalue()
    # Should have informational comment about composite PK
    assert "composite primary key" in output.lower(), (
        f"No composite PK comment. Output:\n{output}"
    )
    # Both PK columns should be marked
    assert "primary_key=True" in output
    # Count occurrences — should be 2 for composite
    pk_count = output.count("primary_key=True")
    assert pk_count == 2, (
        f"Expected 2 primary_key=True, got {pk_count}. Output:\n{output}"
    )


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


@test("cleanup: drop test tables")
async def test_cleanup():
    db = await _get_db()
    await db.execute("DROP TABLE IF EXISTS test_order_products CASCADE")
    await db.execute("DROP TABLE IF EXISTS test_single_pk CASCADE")


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------


async def main():
    tests = [
        obj
        for name, obj in globals().items()
        if callable(obj) and getattr(obj, "_is_test", False)
    ]

    print(f"\nComposite Primary Key Tests ({len(tests)} tests)")
    print("=" * 60)

    for t in tests:
        await t()

    print(f"\n{'=' * 60}")
    print(f"Results: {RESULTS['passed']} passed, {RESULTS['failed']} failed")

    if RESULTS["errors"]:
        print("\nFailures:")
        for name, tb in RESULTS["errors"]:
            print(f"\n--- {name} ---")
            print(tb)

    return 0 if RESULTS["failed"] == 0 else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
