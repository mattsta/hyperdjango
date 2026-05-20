"""
Tests for CLI enhancements: shell, dbshell, inspectdb.

Tests shell auto-imports namespace, dbshell arg building, inspectdb model
generation from live database tables with various column types, PKs, FKs,
unique constraints, nullable fields, and defaults.

Usage:
    uv run hyper-test cli_enhanced
"""

# hyper-test: db_isolated

import asyncio
import inspect
import os
import shutil
import sys
import traceback

from hyperdjango.cli import (
    _PG_TYPE_MAP,
    _generate_model,
    _pg_to_python_type,
    _table_to_class_name,
)
from hyperdjango.database import Database, set_db

# Every env var the unified connection-URL resolver consults. A test that
# asserts a command "refuses without an explicit DSN" must clear ALL of them —
# resolution now honors HYPER_DATABASE_URL and the libpq PG* set, not just
# DATABASE_URL — otherwise an ambient PG* / HYPER_DATABASE_URL supplies a DSN.
_DSN_ENV_KEYS = (
    "DATABASE_URL",
    "HYPER_DATABASE_URL",
    "PGDATABASE",
    "PGHOST",
    "PGPORT",
    "PGUSER",
    "PGPASSWORD",
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
# Tests: _table_to_class_name
# ---------------------------------------------------------------------------


@test("table_to_class: simple name")
def test_class_name_simple():
    assert _table_to_class_name("users") == "Users"


@test("table_to_class: underscored name")
def test_class_name_underscored():
    assert _table_to_class_name("user_profiles") == "UserProfiles"


@test("table_to_class: prefixed name")
def test_class_name_prefixed():
    assert _table_to_class_name("hyper_audit_log") == "HyperAuditLog"


@test("table_to_class: hyphenated name")
def test_class_name_hyphen():
    assert _table_to_class_name("my-table") == "MyTable"


@test("table_to_class: single word")
def test_class_name_single():
    assert _table_to_class_name("products") == "Products"


# ---------------------------------------------------------------------------
# Tests: _pg_to_python_type
# ---------------------------------------------------------------------------


@test("pg_type: integer types")
def test_pg_type_int():
    assert _pg_to_python_type("int4") == "int"
    assert _pg_to_python_type("int8") == "int"
    assert _pg_to_python_type("int2") == "int"
    assert _pg_to_python_type("serial") == "int"
    assert _pg_to_python_type("bigserial") == "int"


@test("pg_type: float types")
def test_pg_type_float():
    assert _pg_to_python_type("float4") == "float"
    assert _pg_to_python_type("float8") == "float"
    assert _pg_to_python_type("numeric") == "Decimal"


@test("pg_type: string types")
def test_pg_type_str():
    assert _pg_to_python_type("text") == "str"
    assert _pg_to_python_type("varchar") == "str"
    assert _pg_to_python_type("uuid") == "str"
    assert _pg_to_python_type("jsonb") == "str"


@test("pg_type: bool")
def test_pg_type_bool():
    assert _pg_to_python_type("bool") == "bool"


@test("pg_type: timestamp types")
def test_pg_type_timestamp():
    assert _pg_to_python_type("timestamptz") == "datetime"
    assert _pg_to_python_type("timestamp") == "datetime"


@test("pg_type: bytes")
def test_pg_type_bytes():
    assert _pg_to_python_type("bytea") == "bytes"


@test("pg_type: unknown type defaults to str")
def test_pg_type_unknown():
    assert _pg_to_python_type("weird_custom_type") == "str"


# ---------------------------------------------------------------------------
# Tests: _generate_model (capture stdout)
# ---------------------------------------------------------------------------


@test("generate_model: basic table")
def test_generate_basic():
    from hyperdjango.migrations import DbColumn, DbConstraint, DbTable

    table = DbTable(name="products")
    table.columns["id"] = DbColumn(
        name="id",
        type_name="int4",
        type_display="INTEGER",
        nullable=False,
        has_default=True,
        default_expr="nextval('products_id_seq'::regclass)",
        is_serial=True,
        char_max_length=None,
    )
    table.columns["name"] = DbColumn(
        name="name",
        type_name="varchar",
        type_display="VARCHAR(100)",
        nullable=False,
        has_default=False,
        default_expr=None,
        is_serial=False,
        char_max_length=100,
    )
    table.columns["price"] = DbColumn(
        name="price",
        type_name="numeric",
        type_display="NUMERIC",
        nullable=True,
        has_default=False,
        default_expr=None,
        is_serial=False,
        char_max_length=None,
    )
    table.constraints.append(
        DbConstraint(
            name="products_pkey",
            type="p",
            columns=["id"],
        )
    )

    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        _generate_model("products", table)

    output = buf.getvalue()
    assert "class Products(Model):" in output
    assert 'table = "products"' in output
    assert "id: int = Field(primary_key=True, auto=True)" in output
    assert "name: str = Field(max_length=100)" in output
    assert "price: Decimal | None = Field(default=None)" in output


@test("generate_model: foreign key")
def test_generate_fk():
    from hyperdjango.migrations import DbColumn, DbConstraint, DbTable

    table = DbTable(name="orders")
    table.columns["id"] = DbColumn(
        name="id",
        type_name="int4",
        type_display="INTEGER",
        nullable=False,
        has_default=True,
        default_expr="nextval('orders_id_seq'::regclass)",
        is_serial=True,
        char_max_length=None,
    )
    table.columns["user_id"] = DbColumn(
        name="user_id",
        type_name="int4",
        type_display="INTEGER",
        nullable=False,
        has_default=False,
        default_expr=None,
        is_serial=False,
        char_max_length=None,
    )
    table.constraints.append(
        DbConstraint(
            name="orders_pkey",
            type="p",
            columns=["id"],
        )
    )
    table.constraints.append(
        DbConstraint(
            name="orders_user_fkey",
            type="f",
            columns=["user_id"],
            fk_table="users",
            fk_columns=["id"],
        )
    )

    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        _generate_model("orders", table)

    output = buf.getvalue()
    assert 'foreign_key="users"' in output


@test("generate_model: unique constraint")
def test_generate_unique():
    from hyperdjango.migrations import DbColumn, DbConstraint, DbTable

    table = DbTable(name="emails")
    table.columns["id"] = DbColumn(
        name="id",
        type_name="int4",
        type_display="INTEGER",
        nullable=False,
        has_default=True,
        default_expr="nextval('emails_id_seq'::regclass)",
        is_serial=True,
        char_max_length=None,
    )
    table.columns["email"] = DbColumn(
        name="email",
        type_name="varchar",
        type_display="VARCHAR(254)",
        nullable=False,
        has_default=False,
        default_expr=None,
        is_serial=False,
        char_max_length=254,
    )
    table.constraints.append(DbConstraint(name="emails_pkey", type="p", columns=["id"]))
    table.constraints.append(
        DbConstraint(name="emails_email_key", type="u", columns=["email"])
    )

    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        _generate_model("emails", table)

    output = buf.getvalue()
    assert "unique=True" in output


@test("generate_model: boolean default")
def test_generate_bool_default():
    from hyperdjango.migrations import DbColumn, DbConstraint, DbTable

    table = DbTable(name="flags")
    table.columns["id"] = DbColumn(
        name="id",
        type_name="int4",
        type_display="INTEGER",
        nullable=False,
        has_default=True,
        default_expr="nextval('flags_id_seq'::regclass)",
        is_serial=True,
        char_max_length=None,
    )
    table.columns["is_active"] = DbColumn(
        name="is_active",
        type_name="bool",
        type_display="BOOLEAN",
        nullable=False,
        has_default=True,
        default_expr="true",
        is_serial=False,
        char_max_length=None,
    )
    table.constraints.append(DbConstraint(name="flags_pkey", type="p", columns=["id"]))

    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        _generate_model("flags", table)

    output = buf.getvalue()
    assert "is_active: bool = Field(default=True)" in output


# ---------------------------------------------------------------------------
# Tests: inspectdb with live database
# ---------------------------------------------------------------------------


@test("inspectdb: introspects real table")
async def test_inspectdb_live():
    from hyperdjango.migrations import DatabaseIntrospector

    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    # Create test table
    await db.execute("DROP TABLE IF EXISTS inspectdb_test CASCADE")
    await db.execute("""
        CREATE TABLE inspectdb_test (
            id SERIAL PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            email VARCHAR(254) UNIQUE,
            age INTEGER DEFAULT 0,
            bio TEXT,
            is_active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    snapshot = await DatabaseIntrospector.introspect(db)
    table = snapshot.tables.get("inspectdb_test")
    assert table is not None

    # Check columns
    assert "id" in table.columns
    assert "name" in table.columns
    assert "email" in table.columns
    assert "age" in table.columns
    assert "bio" in table.columns
    assert "is_active" in table.columns
    assert "created_at" in table.columns

    # Check types
    assert table.columns["id"].is_serial is True
    assert table.columns["name"].type_name == "varchar"
    assert table.columns["name"].char_max_length == 100
    assert table.columns["email"].char_max_length == 254
    assert table.columns["age"].type_name == "int4"
    assert table.columns["bio"].nullable is True
    assert table.columns["is_active"].type_name == "bool"

    # Check PK
    pk_cols = table.get_pk_columns()
    assert pk_cols == ["id"]

    # Generate model output
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        _generate_model("inspectdb_test", table)

    output = buf.getvalue()
    assert "class InspectdbTest(Model):" in output
    assert "primary_key=True" in output
    assert "auto=True" in output
    assert "max_length=100" in output

    # Cleanup
    await db.execute("DROP TABLE IF EXISTS inspectdb_test CASCADE")
    await db.disconnect()


@test("inspectdb: introspects foreign key")
async def test_inspectdb_fk():
    from hyperdjango.migrations import DatabaseIntrospector

    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    await db.execute("DROP TABLE IF EXISTS inspectdb_child CASCADE")
    await db.execute("DROP TABLE IF EXISTS inspectdb_parent CASCADE")

    await db.execute("""
        CREATE TABLE inspectdb_parent (
            id SERIAL PRIMARY KEY,
            name VARCHAR(50) NOT NULL
        )
    """)
    await db.execute("""
        CREATE TABLE inspectdb_child (
            id SERIAL PRIMARY KEY,
            parent_id INTEGER NOT NULL REFERENCES inspectdb_parent(id) ON DELETE CASCADE,
            value TEXT
        )
    """)

    snapshot = await DatabaseIntrospector.introspect(db)
    child = snapshot.tables.get("inspectdb_child")
    assert child is not None

    # Check FK constraint
    fk_cons = [c for c in child.constraints if c.type == "f"]
    assert len(fk_cons) == 1
    assert fk_cons[0].fk_table == "inspectdb_parent"
    assert fk_cons[0].columns == ["parent_id"]

    # Generate and check output
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        _generate_model("inspectdb_child", child)

    output = buf.getvalue()
    assert 'foreign_key="inspectdb_parent"' in output

    # Cleanup
    await db.execute("DROP TABLE IF EXISTS inspectdb_child CASCADE")
    await db.execute("DROP TABLE IF EXISTS inspectdb_parent CASCADE")
    await db.disconnect()


@test("inspectdb: introspects multiple tables")
async def test_inspectdb_multi():
    from hyperdjango.migrations import DatabaseIntrospector

    db = Database(DB_URL)
    await db.connect()
    set_db(db)

    await db.execute("DROP TABLE IF EXISTS inspectdb_a CASCADE")
    await db.execute("DROP TABLE IF EXISTS inspectdb_b CASCADE")
    await db.execute("CREATE TABLE inspectdb_a (id SERIAL PRIMARY KEY, name TEXT)")
    await db.execute("CREATE TABLE inspectdb_b (id SERIAL PRIMARY KEY, tag TEXT)")

    snapshot = await DatabaseIntrospector.introspect(db)
    assert "inspectdb_a" in snapshot.tables
    assert "inspectdb_b" in snapshot.tables

    # Cleanup
    await db.execute("DROP TABLE IF EXISTS inspectdb_a CASCADE")
    await db.execute("DROP TABLE IF EXISTS inspectdb_b CASCADE")
    await db.disconnect()


# ---------------------------------------------------------------------------
# Tests: dbshell arg parsing
# ---------------------------------------------------------------------------


@test("dbshell: psql available check")
def test_dbshell_psql():
    psql = shutil.which("psql")
    # psql should be available (PostgreSQL is installed)
    assert psql is not None, "psql not found — PostgreSQL client tools required"


@test("dbshell: URL parsing for psql args")
def test_dbshell_url_parse():
    from urllib.parse import urlparse

    parsed = urlparse("postgres://myuser:mypass@dbhost:5433/mydb")

    assert parsed.hostname == "dbhost"
    assert parsed.port == 5433
    assert parsed.username == "myuser"
    assert parsed.password == "mypass"
    assert parsed.path == "/mydb"


# ---------------------------------------------------------------------------
# Tests: shell namespace
# ---------------------------------------------------------------------------


@test("shell: core imports available")
def test_shell_namespace():
    import hyperdjango

    assert hasattr(hyperdjango, "HyperApp")
    assert hasattr(hyperdjango, "Request")
    assert hasattr(hyperdjango, "Response")


@test("shell: model imports available")
def test_shell_model_imports():
    from hyperdjango.models import Field, Model

    assert Model is not None
    assert Field is not None


@test("shell: expression imports available")
def test_shell_expression_imports():
    from hyperdjango.expressions import Count, F

    assert F is not None
    assert Count is not None


# ---------------------------------------------------------------------------
# Tests: type map completeness
# ---------------------------------------------------------------------------


@test("type_map: covers all common PostgreSQL types")
def test_type_map_coverage():
    common_types = [
        "int2",
        "int4",
        "int8",
        "float4",
        "float8",
        "numeric",
        "bool",
        "text",
        "varchar",
        "uuid",
        "jsonb",
        "json",
        "timestamptz",
        "timestamp",
        "date",
        "time",
        "bytea",
        "inet",
        "cidr",
        "xml",
    ]
    for t in common_types:
        assert t in _PG_TYPE_MAP, f"Missing type mapping for {t}"


# ---------------------------------------------------------------------------
# Tests: custom command dispatch + explicit-DSN safety (finding #4)
# ---------------------------------------------------------------------------


@test("cli: custom @command is dispatchable via `hyper <name>` (end-to-end)")
def test_custom_command_dispatch():
    # Full end-to-end through main(): a non-builtin token must route to the
    # command registry, parse args, and run. Uses a subprocess because the
    # dispatch calls asyncio.run() (can't nest in this async test harness).
    import subprocess
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "commands.py").write_text(
            "from hyperdjango.commands import command\n"
            "@command(name='ws13_demo', help='demo')\n"
            "def _demo(x: int = 1, shout: bool = False):\n"
            "    print(('X=%d' % x) + ('!' if shout else ''))\n"
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hyperdjango.cli",
                "ws13_demo",
                "--x",
                "7",
                "--shout",
            ],
            cwd=tmp,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"exit {result.returncode}: {result.stderr}"
        assert "X=7!" in result.stdout, f"command output missing: {result.stdout!r}"


@test("cli: unknown command exits non-zero (not a traceback)")
def test_unknown_command_exits():
    from hyperdjango import cli

    try:
        cli._dispatch_custom_command("definitely_not_a_command", [])
        raise AssertionError("expected SystemExit")
    except SystemExit as e:
        assert e.code == 2, f"unexpected exit code {e.code}"


@test("cli: dumpdata refuses to run without an explicit DSN")
def test_dumpdata_requires_dsn():
    from hyperdjango import cli

    args = type(
        "A",
        (),
        {
            "database": None,
            "models": [],
            "output": None,
            "indent": 2,
            "natural_key": None,
        },
    )()
    saved = {k: os.environ.pop(k, None) for k in _DSN_ENV_KEYS}
    try:
        try:
            cli.cmd_dumpdata(args)
            raise AssertionError("expected SystemExit — must not default to a DSN")
        except SystemExit as e:
            assert e.code == 1, f"unexpected exit code {e.code}"
    finally:
        for _k, _v in saved.items():
            if _v is not None:
                os.environ[_k] = _v


@test("cli: loaddata refuses to run without an explicit DSN")
def test_loaddata_requires_dsn():
    import tempfile
    from pathlib import Path

    from hyperdjango import cli

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        fh.write("[]")
        fixture_path = fh.name

    args = type("A", (), {"database": None, "fixture": fixture_path})()
    saved = {k: os.environ.pop(k, None) for k in _DSN_ENV_KEYS}
    try:
        try:
            cli.cmd_loaddata(args)
            raise AssertionError("expected SystemExit — must not default to a DSN")
        except SystemExit as e:
            assert e.code == 1, f"unexpected exit code {e.code}"
    finally:
        for _k, _v in saved.items():
            if _v is not None:
                os.environ[_k] = _v
        Path(fixture_path).unlink()


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------


async def main():
    tests = [
        obj
        for name, obj in globals().items()
        if callable(obj) and getattr(obj, "_is_test", False)
    ]

    print(f"\nCLI Enhancements Tests ({len(tests)} tests)")
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
