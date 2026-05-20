# Migrations

Schema migration engine that diffs Model definitions against the live database. Combines Django's autodetection, Alembic's branching flexibility, and modern innovations: live introspection, mandatory reversibility, deployment safety analysis, and schema version pinning.

## How It Works

1. **Introspect** -- reads actual database schema from `pg_catalog` system catalogs (tables, columns, constraints, indexes)
2. **Extract** -- reads Model definitions from Python code via the model registry
3. **Diff** -- compares models vs live DB, produces a list of `Operation` objects
4. **Generate** -- writes SQL migration file with forward + reverse DDL (every operation is reversible)
5. **Apply** -- executes within a PostgreSQL transaction, records in `hyper_migrations`, acquires advisory lock to prevent concurrent runs
6. **Verify** -- introspects again after apply to confirm DDL succeeded

## CLI Commands

### `makemigrations`

Generate a migration by diffing models against the live database:

```bash
hyper makemigrations                       # Auto-detect changes, auto-name
hyper makemigrations --name add_email      # Custom migration name
hyper makemigrations --dry-run             # Show SQL without writing file
```

The `--dry-run` flag prints the exact DDL that would be generated, including safety warnings, without creating a migration file. This is useful for previewing changes before committing.

### `migrate`

Apply all pending migrations:

```bash
hyper migrate                              # Apply all pending migrations
hyper migrate --target 0003_add_index      # Apply up to specific migration
hyper migrate --fake 0001_initial          # Mark as applied without executing DDL
hyper migrate --dry-run                    # Show what would be applied
```

The `--fake` flag records the migration as applied in `hyper_migrations` without executing any SQL. Use this when you've manually applied schema changes and need to synchronize the migration state.

Migrations run under a PostgreSQL advisory lock (`pg_try_advisory_lock`) to prevent two processes from applying migrations simultaneously. If another migration is in progress, the command fails immediately with a clear message.

### `rollback`

Reverse migrations using the DOWN section of each migration file:

```bash
hyper rollback                             # Rollback most recent migration
hyper rollback --target 0003_add_index     # Rollback to specific point (exclusive)
```

Without `--target`, only the most recent migration is rolled back. With `--target`, all migrations after the target are reversed in reverse order.

### `showmigrations`

List all migrations and their applied status:

```bash
hyper showmigrations
```

Output:

```
[X] 0001_initial_20260322_143000
[X] 0002_add_email_20260322_150000
[ ] 0003_add_index_20260323_090000
```

### `db verify`

Check that your models match the actual database schema:

```bash
hyper db verify --database postgres://localhost/mydb --app myapp:app
```

Reports any drift: missing tables, extra columns, type mismatches, missing constraints, missing indexes. Returns exit code 0 if schema matches, 1 if drift detected.

### `db snapshot`

Save a complete schema checkpoint:

```bash
hyper db snapshot --database postgres://localhost/mydb
# Saves migrations/snapshots/0005_snapshot.json
```

### `db drift`

Detect schema drift from the expected state:

```bash
hyper db drift --database postgres://localhost/mydb
```

## Migration File Format

Migrations are generated as `.sql` files in the `migrations/` directory. Each file contains both UP (forward) and DOWN (reverse) SQL, separated by comment markers:

```sql
-- Migration 0001: initial
-- Operations: 3

-- UP
-- Create table products
CREATE TABLE IF NOT EXISTS "products" (
  "id" SERIAL PRIMARY KEY,
  "name" VARCHAR(200) NOT NULL,
  "price" DOUBLE PRECISION DEFAULT 0.0,
  "category_id" INTEGER REFERENCES "categories"("id")
);

-- Create index idx_products_name on products(name)
CREATE INDEX "idx_products_name" ON "products" ("name");

-- Add constraint uq_products_sku on products
ALTER TABLE "products" ADD CONSTRAINT "uq_products_sku" UNIQUE ("sku");

-- DOWN
-- Reverse: Add constraint uq_products_sku on products
ALTER TABLE "products" DROP CONSTRAINT IF EXISTS "uq_products_sku";

-- Reverse: Create index idx_products_name on products(name)
DROP INDEX IF EXISTS "idx_products_name";

-- Reverse: Create table products
DROP TABLE IF EXISTS "products" CASCADE;
```

File naming convention: `{number:04d}_{name}_{timestamp}.sql`, for example `0001_initial_20260322_143000.sql`.

The parser splits on `-- UP` and `-- DOWN` markers. Lines starting with `--` are treated as comments and skipped during execution. Statements are delimited by semicolons.

## Operations

Every operation generates both forward and reverse SQL. The `Operation` base class requires both `up_sql()` and `down_sql()` methods:

### DDL Operations

| Operation             | Forward SQL                                | Reverse SQL                                   |
| --------------------- | ------------------------------------------ | --------------------------------------------- |
| `CreateTable`         | `CREATE TABLE IF NOT EXISTS ...`           | `DROP TABLE IF EXISTS ... CASCADE`            |
| `DropTable`           | `DROP TABLE IF EXISTS ...`                 | `CREATE TABLE ...` (stored)                   |
| `AddColumn`           | `ALTER TABLE ADD COLUMN ...`               | `ALTER TABLE DROP COLUMN ...`                 |
| `DropColumn`          | `ALTER TABLE DROP COLUMN ...`              | `ALTER TABLE ADD COLUMN ...` (type preserved) |
| `AlterColumnType`     | `ALTER COLUMN TYPE ... USING ...::type`    | `ALTER COLUMN TYPE (old) USING ...::type`     |
| `AlterColumnNullable` | `SET NOT NULL` / `DROP NOT NULL`           | reverse                                       |
| `AlterColumnDefault`  | `SET DEFAULT ...` / `DROP DEFAULT`         | reverse (old default preserved)               |
| `AddConstraint`       | `ADD CONSTRAINT ...`                       | `DROP CONSTRAINT IF EXISTS ...`               |
| `DropConstraint`      | `DROP CONSTRAINT IF EXISTS ...`            | `ADD CONSTRAINT ...` (clause preserved)       |
| `CreateIndex`         | `CREATE [UNIQUE] INDEX [CONCURRENTLY] ...` | `DROP INDEX IF EXISTS ...`                    |
| `DropIndex`           | `DROP INDEX IF EXISTS ...`                 | `CREATE INDEX ...` (definition preserved)     |
| `RunSQL`              | custom forward SQL                         | custom reverse SQL                            |
| `RunPython`           | Python function with DB access             | Python reverse function                       |

### RunSQL -- Custom DDL

For operations the auto-detector cannot generate:

```python
from hyperdjango.migrations import RunSQL

op = RunSQL(
    forward="CREATE EXTENSION IF NOT EXISTS pg_trgm;",
    reverse="DROP EXTENSION IF EXISTS pg_trgm;",
)
```

Both forward and reverse SQL are required. This ensures every migration is fully reversible.

### RunPython -- Data Migrations

For data transformations that require Python logic:

```python
from hyperdjango.migrations import RunPython


async def forwards(db):
    rows = await db.query("SELECT id, first_name, last_name FROM users")
    for row in rows:
        slug = f"{row['first_name']}-{row['last_name']}".lower().replace(" ", "-")
        await db.execute(
            "UPDATE users SET slug = $1 WHERE id = $2",
            slug,
            row["id"],
        )


async def backwards(db):
    await db.execute("UPDATE users SET slug = NULL")


op = RunPython(
    forward_func=forwards,
    reverse_func=backwards,
    _description="Generate user slugs from names",
)
```

Both forward and reverse functions are required. Functions receive the database connection as their only argument and should use `db.execute()` / `db.query()`.

## Database Introspection

The `DatabaseIntrospector` reads the live PostgreSQL schema from system catalogs (`pg_catalog`). It returns a `SchemaSnapshot` containing all tables, columns, constraints, and indexes.

### Data Structures

```python
from hyperdjango.migrations import (
    DbColumn,
    DbConstraint,
    DbIndex,
    DbTable,
    SchemaSnapshot,
)
```

**DbColumn** -- a column as introspected from PostgreSQL:

| Field             | Type          | Description                                                      |
| ----------------- | ------------- | ---------------------------------------------------------------- |
| `name`            | `str`         | Column name                                                      |
| `type_name`       | `str`         | Raw pg_catalog type (e.g., `"int4"`, `"varchar"`)                |
| `type_display`    | `str`         | Normalized DDL type (e.g., `"INTEGER"`, `"VARCHAR(100)"`)        |
| `nullable`        | `bool`        | Whether the column allows NULL                                   |
| `has_default`     | `bool`        | Whether a default expression exists                              |
| `default_expr`    | `str \| None` | Default expression (e.g., `"nextval('users_id_seq'::regclass)"`) |
| `is_serial`       | `bool`        | True if default is `nextval(...)`                                |
| `char_max_length` | `int \| None` | Character length for varchar/char                                |

**DbConstraint** -- a constraint (PK, FK, UNIQUE, CHECK):

| Field        | Type                | Description                                           |
| ------------ | ------------------- | ----------------------------------------------------- |
| `name`       | `str`               | Constraint name                                       |
| `type`       | `str`               | `'p'` (PK), `'f'` (FK), `'u'` (UNIQUE), `'c'` (CHECK) |
| `columns`    | `list[str]`         | Columns involved                                      |
| `fk_table`   | `str \| None`       | Referenced table (FK only)                            |
| `fk_columns` | `list[str] \| None` | Referenced columns (FK only)                          |

**DbIndex** -- a non-constraint index:

| Field     | Type        | Description                    |
| --------- | ----------- | ------------------------------ |
| `name`    | `str`       | Index name                     |
| `columns` | `list[str]` | Indexed columns                |
| `unique`  | `bool`      | Whether this is a unique index |

**DbTable** -- complete table with helper methods:

```python
table.get_pk_columns()  # list[str] -- primary key column names
table.get_fk_constraints()  # list[DbConstraint] -- foreign key constraints
table.get_unique_constraints()  # list[DbConstraint] -- unique constraints
```

### Introspection API

```python
from hyperdjango.migrations import DatabaseIntrospector

# Full schema introspection
snapshot = await DatabaseIntrospector.introspect(db)

# With specific schema
snapshot = await DatabaseIntrospector.introspect(db, schema="myschema")

# Include views and materialized views
snapshot = await DatabaseIntrospector.introspect(db, include_views=True)

# Examine results
for table_name, table in snapshot.tables.items():
    print(f"\n{table_name}:")
    for col_name, col in table.columns.items():
        null = "NULL" if col.nullable else "NOT NULL"
        print(f"  {col_name}: {col.type_display} {null}")
    for con in table.constraints:
        print(f"  constraint {con.name}: {con.type} on {con.columns}")
    for idx in table.indexes:
        unique = "UNIQUE " if idx.unique else ""
        print(f"  index {idx.name}: {unique}({', '.join(idx.columns)})")
```

### Schema Snapshots

Snapshots are serializable to JSON for drift detection and squashing:

```python
# Save snapshot
data = snapshot.to_dict()
json.dumps(data, indent=2)

# Load snapshot
restored = SchemaSnapshot.from_dict(data)

# Compute deterministic checksum for drift detection
checksum = snapshot.compute_checksum()  # SHA-256 of canonical JSON, first 16 hex chars
```

## Schema Diffing

The `SchemaDiffer` compares Model definitions against a `SchemaSnapshot` and produces `Operation` objects:

```python
from hyperdjango.migrations import SchemaDiffer, ModelExtractor, DatabaseIntrospector

# Extract model schemas from registry
model_schemas = ModelExtractor.extract_all()

# Introspect live DB
db_snapshot = await DatabaseIntrospector.introspect(db)

# Diff
ops = SchemaDiffer.diff(model_schemas, db_snapshot)
for op in ops:
    print(op.description())
    print(op.up_sql())
```

The differ detects:

- **New tables** -- models not present in DB
- **Dropped tables** -- DB tables not present in models (excluding system tables)
- **New columns** -- model fields not in DB table
- **Dropped columns** -- DB columns not in model
- **Type changes** -- column type mismatches (with equivalence groups, e.g., INTEGER/INT/INT4 are equivalent)
- **Nullable changes** -- NOT NULL added or removed
- **Missing constraints** -- FK and UNIQUE constraints in model but not in DB
- **Missing indexes** -- indexed fields without corresponding DB indexes
- **M2M junction tables** -- creates junction tables for `ManyToManyField`

System tables (`hyper_migrations`, `hyper_users`, `hyper_groups`, etc.) are never suggested for dropping.

### Type Mapping

Python types map to PostgreSQL DDL types:

| Python Type     | PostgreSQL Type    |
| --------------- | ------------------ |
| `int`           | `INTEGER`          |
| `float`         | `DOUBLE PRECISION` |
| `str`           | `TEXT`             |
| `bool`          | `BOOLEAN`          |
| `bytes`         | `BYTEA`            |
| `datetime`      | `TIMESTAMPTZ`      |
| `date`          | `DATE`             |
| `time`          | `TIME`             |
| `timedelta`     | `INTERVAL`         |
| `uuid` / `UUID` | `UUID`             |
| `Decimal`       | `NUMERIC`          |
| `list`          | `JSONB`            |
| `dict`          | `JSONB`            |

An `Optional` / `X | None` annotation maps by its non-`None` member (e.g.
`list[str] | None` → `JSONB`, `int | None` → `INTEGER`) and makes the column
nullable. Fields with `max_length` produce `VARCHAR(N)` instead of `TEXT`.

## Deployment Safety

The `SafetyAnalyzer` examines operations and warns about dangerous DDL:

```python
from hyperdjango.migrations import SafetyAnalyzer

reports = await SafetyAnalyzer.analyze(ops, db)
for report in reports:
    print(f"Operation: {report['operation']}")
    print(f"Row count: {report['row_count']}")
    for warning in report["warnings"]:
        print(f"  WARNING: {warning}")
```

Warnings are generated for:

| Scenario                                           | Warning                                                             |
| -------------------------------------------------- | ------------------------------------------------------------------- |
| `NOT NULL` without `DEFAULT`                       | Requires table rewrite, locks table. Suggests 3-step approach.      |
| `CREATE INDEX` without `CONCURRENTLY` (>100K rows) | Blocks writes. Suggests `CREATE INDEX CONCURRENTLY`.                |
| `ALTER COLUMN TYPE` (>100K rows)                   | Requires table rewrite. Suggests add-backfill-drop-rename approach. |
| `SET NOT NULL`                                     | Requires scanning all rows for NULLs.                               |
| `DROP TABLE`                                       | Data loss warning with row count.                                   |
| `DROP COLUMN`                                      | Data loss warning.                                                  |

Row counts are fetched from `pg_class.reltuples` for accurate sizing.

## Migration Squashing

Collapse old migrations into a single initial migration:

```python
engine = MigrationEngine("migrations")
result = await engine.squash(db)
# result = {
#     "squashed_count": 15,
#     "removed_files": 14,
#     "new_migration": "0001_squashed_initial_20260322_143000.sql",
#     "snapshot_path": "migrations/snapshots/0015_add_search_snapshot.json",
# }
```

The squash process:

1. Introspects the current database schema
2. Saves a snapshot of the current state
3. Generates a single `CREATE TABLE` migration from the snapshot
4. Removes old migration files
5. Updates `hyper_migrations` to replace all squashed entries with the new one

Requires at least 2 applied migrations. Optionally squash up to a specific migration:

```python
result = await engine.squash(db, up_to="0010_add_search")
```

## Offline SQL Generation

Generate SQL without connecting to the database (like Alembic's `--sql` mode):

```python
engine = MigrationEngine("migrations")

# Generate SQL for all migrations
sql = engine.generate_sql()

# Generate SQL up to a specific migration
sql = engine.generate_sql(target="0005")

print(sql)
```

Output is a complete SQL script including `INSERT INTO hyper_migrations` statements for state tracking.

## Schema Version Pinning

Ensure the database meets a minimum schema version at startup:

```python
engine = MigrationEngine("migrations")

# Raises RuntimeError if schema version < 5
await engine.check_schema_version(db, min_version=5)
```

This is useful in deployment scripts to fail fast if migrations haven't been applied.

## Testing Migrations

Test that migrations apply and rollback cleanly:

```python
async def test_migration_roundtrip(db):
    engine = MigrationEngine("migrations")

    # Generate migration
    result = await engine.makemigrations(db, name="test")
    assert result["operations"]

    # Apply
    applied = await engine.migrate(db)
    assert len(applied) == 1

    # Verify schema matches
    verify = await engine.verify(db)
    assert verify["matches"]

    # Rollback
    rolled_back = await engine.rollback(db)
    assert len(rolled_back) == 1

    # Verify drift exists again
    verify = await engine.verify(db)
    assert not verify["matches"]
```

Test with dry-run to preview without side effects:

```python
async def test_dry_run(db):
    engine = MigrationEngine("migrations")

    # Preview migration without writing file
    result = await engine.makemigrations(db, name="test", dry_run=True)
    assert result["filepath"] is None  # No file written
    assert result["sql"]  # SQL preview available

    # Preview apply without executing
    applied = await engine.migrate(db, dry_run=True)
```

## Programmatic API

### MigrationEngine

The main entry point for all migration operations:

```python
from hyperdjango.migrations import MigrationEngine

engine = MigrationEngine("migrations")

# Generate migration
result = await engine.makemigrations(db, name="add_email")
# result = {
#     "operations": [AddColumn(...)],
#     "filepath": Path("migrations/0002_add_email_20260322_150000.sql"),
#     "sql": ["ALTER TABLE ..."],
#     "safety": [...],
#     "message": "Created migration ...",
# }

# Apply pending migrations
applied = await engine.migrate(db)  # Apply all
applied = await engine.migrate(db, target="0003...")  # Up to target
applied = await engine.migrate(db, fake=True)  # Fake-apply
applied = await engine.migrate(db, dry_run=True)  # Preview only

# Rollback
rolled_back = await engine.rollback(db)  # Most recent
rolled_back = await engine.rollback(db, target="0002")  # To target

# Verify schema
result = await engine.verify(db)
# result = {"matches": True/False, "drift": [...], "snapshot": SchemaSnapshot}

# Save snapshot
path = await engine.snapshot(db)

# List migrations
migrations = await engine.showmigrations(db)
# [{"name": "0001_initial...", "applied": True, "file": "..."}]

# Squash
result = await engine.squash(db)

# Offline SQL
sql = engine.generate_sql()

# Schema version check
await engine.check_schema_version(db, min_version=5)
```

### MigrationStateManager

Low-level tracking of applied migrations via the `hyper_migrations` table:

```python
from hyperdjango.migrations import MigrationStateManager

await MigrationStateManager.ensure_table(db)  # Create tracking table
applied = await MigrationStateManager.get_applied(db)  # Set of applied names
ordered = await MigrationStateManager.get_applied_ordered(db)  # List with timestamps
await MigrationStateManager.record_applied(db, "0001_initial", checksum="abc123")
await MigrationStateManager.record_unapplied(db, "0001_initial")
```

### MigrationFileManager

Low-level file operations:

```python
from hyperdjango.migrations import MigrationFileManager

files = MigrationFileManager("migrations")
files.ensure_dir()  # Create migrations/ and snapshots/
number = files.next_number()  # Next sequential number
path = files.write_migration(1, "initial", ops)  # Write SQL file
migrations = files.list_migrations()  # List all .sql files in order
up, down = files.parse_migration(path)  # Parse UP/DOWN statements
path = files.write_snapshot(snapshot, "0005_migration")  # Save snapshot JSON
snap = files.load_snapshot("0005_migration")  # Load snapshot
snap = files.latest_snapshot()  # Most recent snapshot
```

### ModelExtractor

Extract schema from Python Model classes:

```python
from hyperdjango.migrations import ModelExtractor

# Extract single model
schema = ModelExtractor.extract(MyModel)
# schema.table = "mymodel"
# schema.columns = {"id": ModelColumn(...), "name": ModelColumn(...)}
# schema.m2m_tables = [{"junction_table": "mymodel_tags", ...}]

# Extract all registered models
all_schemas = ModelExtractor.extract_all()
```

## Transactions

All migration operations run inside a PostgreSQL transaction by default. If any statement fails, the entire migration is rolled back:

```
Migration 0003_add_index failed: relation "products" does not exist
Database has been rolled back to pre-migration state.
```

PostgreSQL supports DDL transactions (unlike MySQL), so `CREATE TABLE`, `ALTER TABLE`, and `CREATE INDEX` are all transactional. The migration is recorded in `hyper_migrations` inside the same transaction, ensuring atomicity.

### Non-transactional (non-atomic) migrations

A few operations **cannot** run inside a transaction block — most importantly
`CREATE INDEX CONCURRENTLY` / `DROP INDEX CONCURRENTLY` (the zero-downtime index
pattern the safety analysis recommends), and things like `VACUUM`. Such a
migration is applied **without** a wrapping transaction, detected automatically:

- Any statement containing `CONCURRENTLY`, **or**
- An explicit header marker before `-- UP`:

```sql
-- hyper:atomic = false
-- UP
CREATE INDEX CONCURRENTLY idx_orders_customer ON orders (customer_id);
-- DOWN
DROP INDEX CONCURRENTLY IF EXISTS idx_orders_customer;
```

⚠️ A non-atomic migration is **not rolled back** on failure — if a statement
fails part-way, earlier statements stay applied and the migration is left
**unrecorded**. Recover manually (e.g. `DROP INDEX` any half-built `INVALID`
index) before re-running. Keep each non-atomic migration small (ideally one
`CONCURRENTLY` statement) to make recovery trivial.

## Advisory Locking

The `migrate` command acquires a PostgreSQL advisory lock to prevent concurrent migration runs across multiple processes or servers:

```sql
SELECT pg_try_advisory_lock(hashtext('hyper_migrations'))
```

If the lock cannot be acquired (another migration is running), the command fails immediately. The lock is released after migration completes (or on failure). To manually release a stuck lock:

```sql
SELECT pg_advisory_unlock(hashtext('hyper_migrations'));
```

## Async Migration Runner

The `AsyncMigrationRunner` wraps the migration system with per-migration timing, progress callbacks, destructive operation detection, and structured result reporting.

### Constructor

```python
from hyperdjango.migrations import (
    AsyncMigrationRunner,
    MigrationResult,
    MigrationRunReport,
)

runner = AsyncMigrationRunner(migration_system)
```

| Parameter          | Type                           | Description                   |
| ------------------ | ------------------------------ | ----------------------------- |
| `migration_system` | `HyperUltimateMigrationSystem` | The migration engine instance |

### run()

Apply migrations with progress reporting:

```python
report = await runner.run(
    db,
    target=None,  # Apply up to this migration name (None = all pending)
    fake=False,  # Record as applied without executing SQL
    dry_run=False,  # Preview SQL without applying
    on_progress=callback,  # Progress callback function
)
```

| Parameter     | Type               | Default  | Description                                   |
| ------------- | ------------------ | -------- | --------------------------------------------- |
| `db`          | `Database`         | required | Database connection                           |
| `target`      | `str \| None`      | `None`   | Apply up to this migration (inclusive)        |
| `fake`        | `bool`             | `False`  | Mark as applied without executing DDL         |
| `dry_run`     | `bool`             | `False`  | Preview SQL and warnings without side effects |
| `on_progress` | `Callable \| None` | `None`   | Progress callback (see below)                 |

Returns a `MigrationRunReport`.

The runner acquires a PostgreSQL advisory lock before applying (unless `dry_run=True`). If the lock cannot be acquired, the report contains a single failed result with an error message.

On failure, the runner stops at the first failed migration. The transaction for that migration is rolled back, leaving previously applied migrations intact.

### preview()

Alias for `run(db, target=target, dry_run=True)` -- preview what would be applied without side effects.

### MigrationResult

Per-migration result dataclass:

```python
@dataclass
class MigrationResult:
    name: str  # Migration file stem
    status: str  # "applied", "skipped", "failed", "dry_run", "fake"
    duration_ms: float  # Execution time (0.0 for dry_run/fake)
    sql_statements: int  # Number of SQL statements in the migration
    error: str | None  # Error message if status is "failed"
    warnings: list[str]  # Destructive operation warnings
```

### MigrationRunReport

Aggregate report from a full migration run:

```python
@dataclass
class MigrationRunReport:
    results: list[MigrationResult]   # Per-migration results
    total_duration_ms: float         # Wall clock time for the entire run
    applied_count: int               # Migrations successfully applied
    skipped_count: int               # Migrations skipped
    failed_count: int                # Migrations that failed

    @property
    def success(self) -> bool:       # True if failed_count == 0
```

### Destructive Operation Detection

Before applying each migration, the runner scans SQL statements for potentially destructive patterns:

| Pattern       | Warning                                             |
| ------------- | --------------------------------------------------- |
| `DROP TABLE`  | Table removal -- data loss                          |
| `DROP COLUMN` | Column removal -- data loss                         |
| `TRUNCATE`    | All rows deleted                                    |
| `DELETE FROM` | Row deletion                                        |
| `ALTER TABLE` | May drop constraints, change types, remove NOT NULL |

Warnings are attached to the `MigrationResult.warnings` list. In `dry_run` mode, this lets you review destructive operations before committing.

### Progress Callback

The `on_progress` callback is called before and after each migration:

```python
def on_progress(migration_name: str, status: str, index: int, total: int):
    print(f"[{index}/{total}] {migration_name}: {status}")
```

| Parameter        | Type  | Description                                                              |
| ---------------- | ----- | ------------------------------------------------------------------------ |
| `migration_name` | `str` | Stem name of the migration file                                          |
| `status`         | `str` | `"starting"`, `"applied"`, `"failed"`, `"dry_run"`, `"fake"`             |
| `index`          | `int` | Current migration index (0-based for "starting", 1-based for completion) |
| `total`          | `int` | Total number of pending migrations                                       |

### Example Usage

```python
from hyperdjango.migrations import (
    MigrationEngine,
    AsyncMigrationRunner,
    MigrationRunReport,
)

engine = MigrationEngine("migrations")

# Build the migration system
system = engine._system  # or construct HyperUltimateMigrationSystem directly

runner = AsyncMigrationRunner(system)

# Preview pending migrations
preview = await runner.run(db, dry_run=True)
for result in preview.results:
    print(f"  {result.name}: {result.sql_statements} statements")
    for warning in result.warnings:
        print(f"    WARNING: {warning}")


# Apply with progress reporting
def progress(name, status, idx, total):
    print(f"[{idx}/{total}] {name}: {status}")


report = await runner.run(db, on_progress=progress)

if report.success:
    print(f"Applied {report.applied_count} migrations in {report.total_duration_ms}ms")
else:
    failed = [r for r in report.results if r.status == "failed"]
    for f in failed:
        print(f"FAILED: {f.name} — {f.error}")
```

## Multi-Database Migrations

When using multiple databases via `ConnectionManager`, run migrations against each database independently:

```python
from hyperdjango.multi_db import ConnectionManager
from hyperdjango.migrations import MigrationEngine

connections = ConnectionManager()
await connections.configure(
    {
        "default": "postgres://localhost/myapp",
        "analytics": "postgres://localhost/warehouse",
    }
)

# Separate migration directories per database
engine_default = MigrationEngine("migrations/default")
engine_analytics = MigrationEngine("migrations/analytics")

# Run migrations on each
await engine_default.migrate(connections["default"])
await engine_analytics.migrate(connections["analytics"])
```

### Per-Model Database Binding

Models with `Meta.database` automatically use the correct connection:

```python
class AnalyticsEvent(Model):
    class Meta:
        table = "events"
        database = "analytics"  # Always queries the analytics DB


class User(Model):
    class Meta:
        table = "users"
        # No database — uses "default"
```

### Router-Based Read/Write Splitting

Route reads to replicas, writes to primary:

```python
from hyperdjango.multi_db import DatabaseRouter


class PrimaryReplicaRouter(DatabaseRouter):
    def db_for_read(self, model):
        return "replica"

    def db_for_write(self, model):
        return "default"

    def allow_migrate(self, db_name, model):
        # Only migrate on the primary
        return db_name == "default"


connections.router = PrimaryReplicaRouter()

# Reads go to replica, writes go to primary
users = await User.objects.all()  # → replica
await user.save()  # → default

# Explicit override
users = await User.objects.using("default").all()  # → primary
```

### Migration Squashing with Multi-DB

Squash each database's migrations independently:

```python
# Squash default DB migrations
result = await engine_default.squash(connections["default"])

# Squash analytics DB migrations
result = await engine_analytics.squash(connections["analytics"])
```

See [Multi-Database Routing](multi-db.md) for the full `ConnectionManager` and `DatabaseRouter` API.
