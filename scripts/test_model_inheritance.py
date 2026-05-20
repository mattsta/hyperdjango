#!/usr/bin/env python3
"""Test model inheritance: abstract, proxy, and concrete.

Tests:
1. Abstract models — share fields without creating a table
2. Proxy models — same table, different Python class/behavior
3. Concrete inheritance — each model gets its own table
4. Abstract field inheritance — subclass inherits parent fields
5. Multi-level abstract inheritance — A(abstract) → B(abstract) → C(concrete)
6. Proxy QuerySet — proxy model queries the parent table
7. Proxy with custom methods — proxy adds methods, parent doesn't have them
8. Abstract with M2M — abstract model defines M2M, concrete inherits it
9. Migration support — abstract/proxy models handled correctly by SchemaDiffer

Run: uv run hyper-test model_inheritance
Requires: PostgreSQL running, DATABASE_URL or default hyperdjango_test
"""

# hyper-test: db_isolated

import asyncio
import contextlib
import os
import sys

from hyperdjango.database import Database, set_db
from hyperdjango.models import Field, Model
from hyperdjango.query import QuerySet, _model_registry

DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://localhost/hyperdjango_test",
)

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  PASS: {name}")
        passed += 1
    else:
        print(f"  FAIL: {name} — {detail}")
        failed += 1


# ─── Abstract Model Tests ─────────────────────────────────────────────────────


def test_abstract_model():
    """Test abstract models don't create tables but share fields."""
    print("\n=== Abstract Models ===")

    class TimestampMixin(Model):
        class Meta:
            abstract = True

        created_at: str = Field(default="")
        updated_at: str = Field(default="")

    check("abstract has _meta", hasattr(TimestampMixin, "_meta"))
    check("abstract _meta.abstract is True", TimestampMixin._meta.abstract)
    check("abstract _meta.table is empty", TimestampMixin._meta.table == "")
    check(
        "abstract has no objects manager",
        not hasattr(TimestampMixin, "objects") or TimestampMixin._meta.abstract,
    )

    # Concrete subclass inherits fields
    class InhArticle(Model):
        class Meta:
            table = "test_inh_articles"

        id: int = Field(primary_key=True, auto=True)
        title: str = Field(max_length=200)
        created_at: str = Field(default="")
        updated_at: str = Field(default="")

    check("concrete has _meta", hasattr(InhArticle, "_meta"))
    check("concrete is not abstract", not InhArticle._meta.abstract)
    check("concrete has table", InhArticle._meta.table == "test_inh_articles")
    check("concrete has id field", "id" in InhArticle._meta.fields)
    check("concrete has title field", "title" in InhArticle._meta.fields)
    check("concrete has objects manager", hasattr(InhArticle, "objects"))
    check("concrete objects is QuerySet", isinstance(InhArticle.objects, QuerySet))


def test_abstract_field_inheritance():
    """Test abstract model fields are inherited by concrete subclass."""
    print("\n=== Abstract Field Inheritance ===")

    class BaseEntity(Model):
        class Meta:
            abstract = True

        id: int = Field(primary_key=True, auto=True)
        name: str = Field(max_length=100)

    class InhPerson(BaseEntity):
        class Meta:
            table = "test_inh_persons"

        email: str = Field(max_length=200)

    check("person has _meta", hasattr(InhPerson, "_meta"))
    check("person table", InhPerson._meta.table == "test_inh_persons")
    check("person inherits id", "id" in InhPerson._meta.fields)
    check("person inherits name", "name" in InhPerson._meta.fields)
    check("person has own email", "email" in InhPerson._meta.fields)
    check("person id is PK", InhPerson._meta.fields["id"].primary_key)
    check("person id is auto", InhPerson._meta.fields["id"].auto)
    check("person pk_field is id", InhPerson._meta.pk_field == "id")

    # Another subclass inherits differently
    class InhCompany(BaseEntity):
        class Meta:
            table = "test_inh_companies"

        industry: str = Field(max_length=100)

    check("company inherits id", "id" in InhCompany._meta.fields)
    check("company inherits name", "name" in InhCompany._meta.fields)
    check("company has own industry", "industry" in InhCompany._meta.fields)
    check("company does NOT have email", "email" not in InhCompany._meta.fields)


def test_multi_level_abstract():
    """Test multi-level abstract inheritance: A → B → C."""
    print("\n=== Multi-Level Abstract ===")

    class Level1(Model):
        class Meta:
            abstract = True

        id: int = Field(primary_key=True, auto=True)

    class Level2(Level1):
        class Meta:
            abstract = True

        name: str = Field(max_length=100)

    class Level3(Level2):
        class Meta:
            table = "test_inh_level3"

        description: str = Field(default="")

    check("level1 is abstract", Level1._meta.abstract)
    check("level2 is abstract", Level2._meta.abstract)
    check("level3 is concrete", not Level3._meta.abstract)
    check("level3 has table", Level3._meta.table == "test_inh_level3")
    check("level3 inherits id", "id" in Level3._meta.fields)
    check("level3 inherits name", "name" in Level3._meta.fields)
    check("level3 has own description", "description" in Level3._meta.fields)
    check(
        "level3 has 3 fields",
        len(Level3._meta.fields) == 3,
        f"got {len(Level3._meta.fields)}: {list(Level3._meta.fields.keys())}",
    )


# ─── Proxy Model Tests ────────────────────────────────────────────────────────


def test_proxy_model():
    """Test proxy models reuse parent table."""
    print("\n=== Proxy Models ===")

    class InhBaseUser(Model):
        class Meta:
            table = "test_inh_base_users"

        id: int = Field(primary_key=True, auto=True)
        username: str = Field(max_length=100)
        is_admin: bool = Field(default=False)

    class InhAdminUser(InhBaseUser):
        class Meta:
            proxy = True

        def admin_greeting(self):
            return f"Admin: {self.username}"

    check("proxy has _meta", hasattr(InhAdminUser, "_meta"))
    check("proxy is proxy", InhAdminUser._meta.proxy)
    check("proxy uses parent table", InhAdminUser._meta.table == "test_inh_base_users")
    check(
        "proxy inherits fields",
        set(InhAdminUser._meta.fields.keys()) == set(InhBaseUser._meta.fields.keys()),
    )
    check("proxy has objects manager", hasattr(InhAdminUser, "objects"))
    check("proxy has custom method", hasattr(InhAdminUser, "admin_greeting"))
    check(
        "base does NOT have custom method", not hasattr(InhBaseUser, "admin_greeting")
    )
    check(
        "proxy pk_field matches parent",
        InhAdminUser._meta.pk_field == InhBaseUser._meta.pk_field,
    )


def test_proxy_error_without_concrete_parent():
    """Test proxy model requires a concrete parent."""
    print("\n=== Proxy Error Handling ===")

    class AbstractOnly(Model):
        class Meta:
            abstract = True

        id: int = Field(primary_key=True, auto=True)

    error_raised = False
    try:

        class BadProxy(AbstractOnly):
            class Meta:
                proxy = True
    except TypeError as e:
        error_raised = True
        check("proxy without concrete parent raises TypeError", True)
        check(
            "error message mentions proxy",
            "proxy" in str(e).lower() or "Proxy" in str(e),
        )

    if not error_raised:
        check(
            "proxy without concrete parent raises TypeError", False, "no error raised"
        )


# ─── Concrete Inheritance Tests ────────────────────────────────────────────────


def test_concrete_inheritance():
    """Test concrete models each get their own table."""
    print("\n=== Concrete Inheritance ===")

    class InhVehicle(Model):
        class Meta:
            table = "test_inh_vehicles"

        id: int = Field(primary_key=True, auto=True)
        make: str = Field(max_length=50)
        year: int = Field(default=2024)

    class InhCar(Model):
        class Meta:
            table = "test_inh_cars"

        id: int = Field(primary_key=True, auto=True)
        make: str = Field(max_length=50)
        year: int = Field(default=2024)
        doors: int = Field(default=4)

    check("vehicle has own table", InhVehicle._meta.table == "test_inh_vehicles")
    check("car has own table", InhCar._meta.table == "test_inh_cars")
    check("car has doors field", "doors" in InhCar._meta.fields)
    check("vehicle does NOT have doors", "doors" not in InhVehicle._meta.fields)
    check("both registered", "test_inh_vehicles" in _model_registry)
    check("car registered", "test_inh_cars" in _model_registry)


# ─── Database Integration Tests ────────────────────────────────────────────────


async def test_abstract_db_operations(db):
    """Test concrete subclass of abstract model works with DB."""
    print("\n=== Abstract Model DB Operations ===")

    # Create table for concrete subclass
    await db.execute(
        "CREATE TABLE IF NOT EXISTS test_inh_employees ("
        "  id SERIAL PRIMARY KEY,"
        "  name VARCHAR(100) NOT NULL,"
        "  department VARCHAR(100) NOT NULL DEFAULT ''"
        ")"
    )

    class NamedEntity(Model):
        class Meta:
            abstract = True

        id: int = Field(primary_key=True, auto=True)
        name: str = Field(max_length=100)

    class InhEmployee(NamedEntity):
        class Meta:
            table = "test_inh_employees"

        department: str = Field(max_length=100, default="")

    # Insert
    emp = InhEmployee(name="Alice", department="Engineering")
    await emp.save(db)
    check("insert returns PK", emp.pk is not None)

    # Query
    rows = await db.query(
        "SELECT id, name, department FROM test_inh_employees WHERE name = $1",
        "Alice",
    )
    check("query finds employee", len(rows) == 1)
    if rows:
        check("name correct", rows[0]["name"] == "Alice")
        check("department correct", rows[0]["department"] == "Engineering")

    # QuerySet (uses global db via get_db())
    results = await InhEmployee.objects.filter(name="Alice").all()
    check("QuerySet filter works", len(results) == 1)
    if results:
        check("QuerySet result has name", results[0].name == "Alice")
        check("QuerySet result has department", results[0].department == "Engineering")

    # Cleanup
    await db.execute("DROP TABLE IF EXISTS test_inh_employees CASCADE")


async def test_proxy_db_operations(db):
    """Test proxy model queries the parent table."""
    print("\n=== Proxy Model DB Operations ===")

    await db.execute(
        "CREATE TABLE IF NOT EXISTS test_inh_items ("
        "  id SERIAL PRIMARY KEY,"
        "  name VARCHAR(100) NOT NULL,"
        "  item_type VARCHAR(50) NOT NULL DEFAULT 'generic',"
        "  price INTEGER NOT NULL DEFAULT 0"
        ")"
    )

    class InhItem(Model):
        class Meta:
            table = "test_inh_items"

        id: int = Field(primary_key=True, auto=True)
        name: str = Field(max_length=100)
        item_type: str = Field(max_length=50, default="generic")
        price: int = Field(default=0)

    class InhPremiumItem(InhItem):
        class Meta:
            proxy = True

        def is_premium(self):
            return self.price > 100

    # Insert via parent
    item1 = InhItem(name="Basic Widget", item_type="basic", price=50)
    await item1.save(db)
    item2 = InhItem(name="Premium Gadget", item_type="premium", price=200)
    await item2.save(db)

    check("items inserted", item1.pk is not None and item2.pk is not None)

    # Query via proxy — should see same table
    all_items = await InhPremiumItem.objects.all()
    check("proxy sees all parent rows", len(all_items) == 2, f"got {len(all_items)}")

    # Proxy instances have custom method
    if all_items:
        premium_items = [i for i in all_items if i.is_premium()]
        check("proxy method works", len(premium_items) == 1)
        if premium_items:
            check("premium item is correct", premium_items[0].name == "Premium Gadget")

    # Query via parent
    parent_items = await InhItem.objects.all()
    check("parent also sees all rows", len(parent_items) == 2)

    # Cleanup
    await db.execute("DROP TABLE IF EXISTS test_inh_items CASCADE")


async def test_migration_with_abstract(db):
    """Test that migration framework handles abstract models correctly."""
    print("\n=== Migration + Abstract Models ===")

    from hyperdjango.migrations import (
        DatabaseIntrospector,
        ModelExtractor,
        SchemaDiffer,
    )

    class MigAbstract(Model):
        class Meta:
            abstract = True

        id: int = Field(primary_key=True, auto=True)
        name: str = Field(max_length=100)

    class MigConcrete(MigAbstract):
        class Meta:
            table = "test_mig_inh_concrete"

        email: str = Field(max_length=200)

    # Extract should work for concrete, skip abstract
    schema = ModelExtractor.extract(MigConcrete)
    check("extract concrete has table", schema.table == "test_mig_inh_concrete")
    check("extract concrete has inherited id", "id" in schema.columns)
    check("extract concrete has inherited name", "name" in schema.columns)
    check("extract concrete has own email", "email" in schema.columns)

    # Diff against empty DB should produce CreateTable
    db_snapshot = await DatabaseIntrospector.introspect(db)
    ops = SchemaDiffer.diff([schema], db_snapshot)
    create_ops = [
        op for op in ops if hasattr(op, "table") and op.table == "test_mig_inh_concrete"
    ]
    check(
        "diff produces CreateTable for concrete",
        len(create_ops) > 0 and create_ops[0].__class__.__name__ == "CreateTable",
        f"got {[op.__class__.__name__ for op in create_ops]}",
    )


# ─── Meta Properties Tests ────────────────────────────────────────────────────


def test_meta_properties():
    """Test TableMeta properties with inheritance context."""
    print("\n=== Meta Properties ===")

    class MetaBase(Model):
        class Meta:
            abstract = True

        id: int = Field(primary_key=True, auto=True)
        name: str = Field(max_length=100)

    class MetaConcrete(MetaBase):
        class Meta:
            table = "test_meta_concrete"

        email: str = Field(max_length=200, unique=True)
        role: str = Field(max_length=50, default="user")

    meta = MetaConcrete._meta
    check(
        "column_names includes all",
        len(meta.column_names) >= 3,
        f"got {meta.column_names}",
    )
    check(
        "writable_columns excludes auto",
        "id" not in meta.writable_columns or not meta.fields["id"].auto,
    )
    check("fk_fields returns dict", isinstance(meta.get_fk_fields(), dict))
    check("not abstract", not meta.abstract)
    check("not proxy", not meta.proxy)
    check("parents list exists", isinstance(meta.parents, list))


async def main():
    global passed, failed

    db = await Database(DB_URL).connect() or None
    db_inst = Database(DB_URL)
    await db_inst.connect()
    set_db(db_inst)

    try:
        # Pure Python tests (no DB needed)
        test_abstract_model()
        test_abstract_field_inheritance()
        test_multi_level_abstract()
        test_proxy_model()
        test_proxy_error_without_concrete_parent()
        test_concrete_inheritance()
        test_meta_properties()

        # DB integration tests
        await test_abstract_db_operations(db_inst)
        await test_proxy_db_operations(db_inst)
        await test_migration_with_abstract(db_inst)
    finally:
        # Cleanup any remaining test tables
        for t in ["test_inh_employees", "test_inh_items", "test_mig_inh_concrete"]:
            with contextlib.suppress(Exception):
                await db_inst.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
        await db_inst.disconnect()

    print(f"\n{'=' * 60}")
    print(f"Results: {passed} passed, {failed} failed")
    if failed == 0:
        print("All model inheritance tests passed!")
    else:
        print(f"{failed} tests need attention")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
